import urllib.request
import json
import logging
from django.conf import settings
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

from django.utils import timezone

NAVER_CLIENT_ID = getattr(settings, 'NAVER_CLIENT_ID', 'YOUR_CLIENT_ID_HERE')
NAVER_CLIENT_SECRET = getattr(settings, 'NAVER_CLIENT_SECRET', 'YOUR_CLIENT_SECRET_HERE')

logger = logging.getLogger(__name__)


# -----------------------------
# Session (재사용 + Retry)
# -----------------------------
_SESSION: Optional[requests.Session] = None

def get_session() -> requests.Session:
    """
    네트워크 불안정/레이트리밋(429)/일시적 5xx 대응을 위한 세션.
    - 모듈 전역으로 재사용하여 커넥션 풀을 활용
    """
    global _SESSION
    if _SESSION is not None:
        return _SESSION

    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,  # 1s, 2s, 4s...
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,  # raise_for_status는 직접 호출
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    })

    _SESSION = session
    return session


# -----------------------------
# URL Parsing / Normalization
# -----------------------------
@dataclass
class NaverNewsIdentity:
    oid: str
    aid: str
    normalized_url: str


def parse_naver_ids_and_normalize_url(url: str) -> Optional[NaverNewsIdentity]:
    """
    네이버 뉴스 URL에서 oid/aid를 안전하게 추출하고,
    가능하면 표준 형태(n.news.naver.com/mnews/article/{oid}/{aid})로 정규화.

    지원 예시:
    - https://n.news.naver.com/mnews/article/001/0014400000
    - https://n.news.naver.com/article/001/0014400000
    - https://news.naver.com/main/read.naver?oid=001&aid=0014400000
    """
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        query = parse_qs(parsed.query)

        # 1) query 기반: read.naver?oid=...&aid=...
        if "oid" in query and "aid" in query:
            oid = query["oid"][0]
            aid = query["aid"][0]
            if oid.isdigit() and aid.isdigit():
                normalized = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
                return NaverNewsIdentity(oid=oid, aid=aid, normalized_url=normalized)

        # 2) path 기반: /mnews/article/{oid}/{aid} 또는 /article/{oid}/{aid}
        m = re.search(r"/(?:mnews/)?article/(?P<oid>\d{3,})/(?P<aid>\d{5,})", path)
        if m:
            oid = m.group("oid")
            aid = m.group("aid")
            normalized = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
            return NaverNewsIdentity(oid=oid, aid=aid, normalized_url=normalized)

        # 3) 혹시라도 /read?oid=...&aid=... 같은 변형이 있을 수 있어 대비 (느슨)
        #    (원칙적으로 위에서 대부분 잡힘)
        m2 = re.search(r"oid=(\d+).*aid=(\d+)", parsed.query)
        if m2:
            oid, aid = m2.group(1), m2.group(2)
            if oid.isdigit() and aid.isdigit():
                normalized = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
                return NaverNewsIdentity(oid=oid, aid=aid, normalized_url=normalized)

        return None

    except Exception:
        return None


# -----------------------------
# Published At Parsing
# -----------------------------
def parse_iso_datetime(value: str) -> Optional[datetime]:
    """
    ISO-8601 유사 문자열을 파싱.
    - 'Z' 처리
    - timezone-aware 변환 (가능하면)
    """
    if not value:
        return None
    try:
        v = value.strip()
        # Python fromisoformat은 'Z'를 바로 처리 못하는 경우가 많아서 보정
        if v.endswith("Z"):
            v = v.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if timezone.is_naive(dt):
            # naive면 UTC로 간주하지 말고, 우선 KST로 둠(네이버 뉴스는 보통 KST)
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt
    except Exception:
        return None


def parse_korean_datetime(value: str) -> Optional[datetime]:
    """
    예: '2024.01.14. 오전 11:05'
        '2024.01.14. 오후 3:07'
    """
    if not value:
        return None
    text = value.strip()

    m = re.search(
        r"(?P<y>\d{4})\.(?P<m>\d{2})\.(?P<d>\d{2})\.\s*(?P<ap>오전|오후)\s*(?P<h>\d{1,2}):(?P<min>\d{2})",
        text
    )
    if not m:
        return None

    y = int(m.group("y"))
    mo = int(m.group("m"))
    d = int(m.group("d"))
    ap = m.group("ap")
    h = int(m.group("h"))
    mi = int(m.group("min"))

    # 오전/오후 보정
    if ap == "오후" and h != 12:
        h += 12
    if ap == "오전" and h == 12:
        h = 0

    try:
        dt = datetime(y, mo, d, h, mi, 0)
        return timezone.make_aware(dt, timezone.get_current_timezone())
    except Exception:
        return None


def extract_published_at(soup: BeautifulSoup) -> Optional[datetime]:
    """
    published_at 추출 우선순위:
    1) meta article:published_time (ISO)
    2) data-date-time attribute
    3) 화면 텍스트(오전/오후 포맷)
    """
    # 1) meta
    for prop in ["article:published_time", "og:article:published_time"]:
        meta = soup.select_one(f'meta[property="{prop}"]')
        if meta and meta.get("content"):
            dt = parse_iso_datetime(meta["content"])
            if dt:
                return dt

    # 2) data-date-time
    date_tag = soup.select_one(".media_end_head_info_datestamp_time")
    if date_tag:
        # 네이버는 data-date-time으로 'YYYY-MM-DD HH:MM:SS' 형태가 자주 나옴
        raw = date_tag.get("data-date-time")
        if raw:
            # ISO 아니지만 fromisoformat에 가끔 들어맞음 (공백 포함도 가능)
            dt = parse_iso_datetime(raw.replace(" ", "T"))
            if dt:
                return dt
            # 실패 시 직접 파싱
            try:
                dt2 = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                return timezone.make_aware(dt2, timezone.get_current_timezone())
            except Exception:
                pass

        # 화면 텍스트
        txt = date_tag.get_text(strip=True)
        dt3 = parse_korean_datetime(txt)
        if dt3:
            return dt3

    # 3) 다른 후보(예비)
    txt_candidates = [
        ".media_end_head_info_datestamp span",
        ".media_end_head_info_datestamp_time",
    ]
    for sel in txt_candidates:
        t = soup.select_one(sel)
        if t:
            dt = parse_korean_datetime(t.get_text(" ", strip=True))
            if dt:
                return dt

    return None


# -----------------------------
# Access Restricted Detection
# -----------------------------
def detect_access_restriction(soup: BeautifulSoup) -> Optional[str]:
    """
    제한 페이지(로그인/연령/권한)를 완벽히 잡는 건 어렵지만,
    오탐을 줄이기 위해 '본문이 비어있고' 특정 키워드가 있을 때만 제한으로 판단.
    """
    text = soup.get_text(" ", strip=True)

    # 키워드 후보 (필요 시 더 추가)
    keywords = [
        "로그인이 필요",
        "연령 확인",
        "본인확인",
        "권한이 없습니다",
        "접근이 제한",
    ]

    hit = any(k in text for k in keywords)
    if not hit:
        return None

    # 본문 후보 자체가 안 잡히면 제한으로 볼 가능성이 높음
    if not (soup.select_one("#dic_area") or soup.select_one("#articeBody")):
        return "ACCESS_RESTRICTED"

    return None


# -----------------------------
# Content Extraction / Cleaning
# -----------------------------
def clean_article_text(container) -> str:
    """
    기사 본문 컨테이너에서 불필요한 태그 제거 및 텍스트 정제.
    - a 태그는 제거(decompose)하면 텍스트도 날아가므로 unwrap으로 텍스트 보존
    """
    # 제거 대상
    for tag in container.select("script, style, .img_desc, .end_photo_org, .byline, .reporter_area"):
        tag.decompose()

    # a 태그는 텍스트 유지
    for a in container.select("a"):
        a.unwrap()

    # 광고/공유/관련기사 등 자주 섞이는 요소가 있으면 selector 기반 제거 추가 가능
    text = container.get_text(separator="\n", strip=True)

    # 너무 많은 공백/빈 줄 정리
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def extract_title(soup: BeautifulSoup) -> Optional[str]:
    og_title = soup.select_one('meta[property="og:title"]')
    if og_title and og_title.get("content"):
        t = og_title["content"].strip()
        return t or None

    title_tag = soup.select_one("#title_area span") or soup.select_one("h2.media_end_head_headline")
    if title_tag:
        t = title_tag.get_text(strip=True)
        return t or None

    return None


def extract_content(soup: BeautifulSoup) -> str:
    selectors = ["#dic_area", "#articeBody", ".news_end_font"]
    for sel in selectors:
        tag = soup.select_one(sel)
        if tag:
            return clean_article_text(tag)
    return ""


def extract_publisher(soup: BeautifulSoup) -> str:
    # meta가 있으면 우선
    meta = soup.select_one('meta[property="og:article:author"]')
    if meta and meta.get("content"):
        return meta["content"].strip()

    logo = soup.select_one(".media_end_head_top_logo img")
    if logo and logo.get("title"):
        return logo["title"].strip()

    return "Unknown"


def extract_image_url(soup: BeautifulSoup) -> Optional[str]:
    og_image = soup.select_one('meta[property="og:image"]')
    if og_image and og_image.get("content"):
        return og_image["content"].strip()

    # fallback: 본문 내 첫 이미지(너무 제한적인 selector는 피함)
    for sel in ["#dic_area img", "article img", "img"]:
        img = soup.select_one(sel)
        if img and img.get("src"):
            return img.get("src")
    return None


# -----------------------------
# Main Function
# -----------------------------
def get_naver_news_info(url: str) -> Dict[str, Any]:
    """
    네이버 뉴스 URL → 기사 정보 추출
    반환 status:
    - SUCCESS: 본문까지 충분히 확보
    - SOFT_SUCCESS: 본문이 짧거나 없지만 제목/메타는 확보(아카이빙/접근 기록용으로 가치 있음)
    - FAILED: URL 형식 불가/접근 불가/제목조차 파싱 불가 등
    """
    session = get_session()

    result: Dict[str, Any] = {
        "status": "FAILED",
        "title": "",
        "content": "",
        "naver_oid": None,
        "naver_aid": None,
        "publisher": "",
        "published_at": None,  # 기사 발행 시각(가능하면)
        "crawled_at": timezone.now(),  # 크롤링 시각(항상)
        "image_url": None,
        "normalized_url": None,
        "failed_reason": "",
        "http_status": None,
    }

    # 1) oid/aid 파싱 + normalize
    ident = parse_naver_ids_and_normalize_url(url)
    if not ident:
        result["failed_reason"] = "INVALID_NAVER_URL_FORMAT"
        return result

    result["naver_oid"] = ident.oid
    result["naver_aid"] = ident.aid
    result["normalized_url"] = ident.normalized_url

    # 2) Fetch (정규화 URL로 가져오는 것을 권장)
    fetch_url = ident.normalized_url

    try:
        resp = session.get(fetch_url, timeout=(3, 10))
        result["http_status"] = resp.status_code

        # 403/404 같은 경우는 명확히 분리
        if resp.status_code in (403, 404):
            result["failed_reason"] = f"ACCESS_DENIED_OR_NOT_FOUND({resp.status_code})"
            return result

        # 그 외 에러는 raise_for_status로 처리
        resp.raise_for_status()

    except requests.exceptions.Timeout:
        result["failed_reason"] = "FETCH_TIMEOUT"
        return result
    except requests.exceptions.RequestException as e:
        result["failed_reason"] = f"FETCH_REQUEST_EXCEPTION({str(e)})"
        return result

    soup = BeautifulSoup(resp.content, "html.parser")

    # 3) 제한 페이지 감지
    restriction = detect_access_restriction(soup)
    if restriction:
        # 제한이더라도 제목/메타는 나올 수 있으니 아래에서 최대한 추출 시도
        result["failed_reason"] = restriction

    # 4) Extract fields
    title = extract_title(soup)
    if not title:
        # 제목도 못 뽑으면 아카이빙 가치가 거의 없으므로 실패
        result["failed_reason"] = result["failed_reason"] or "PARSE_NO_TITLE"
        return result

    result["title"] = title
    result["publisher"] = extract_publisher(soup)
    result["image_url"] = extract_image_url(soup)
    result["published_at"] = extract_published_at(soup)  # 못 구하면 None

    content = extract_content(soup)
    result["content"] = content

    # 5) Success policy (프로젝트 목적 반영)
    # - 본문이 충분하면 SUCCESS
    # - 본문이 짧거나 없어도 "접근 기록 + 요약"은 가능하므로 SOFT_SUCCESS
    # - 다만 접근 제한이면서 본문도 없으면 SOFT_SUCCESS에 reason 남김
    min_len = 200  # 요약/검색 품질을 위해 권장(서비스에 맞게 조정)
    if len(content) >= min_len and not result["failed_reason"]:
        result["status"] = "SUCCESS"
        return result

    # 본문이 짧거나 제한/파싱 이슈가 있을 때
    if len(content) < min_len:
        # 기존 reason이 없다면 짧음 이유를 부여
        if not result["failed_reason"]:
            result["failed_reason"] = "SOFT_CONTENT_TOO_SHORT"
        result["status"] = "SOFT_SUCCESS"
        return result

    # 본문은 충분한데 다른 reason이 있을 경우도 soft로 둠
    result["status"] = "SOFT_SUCCESS"
    if not result["failed_reason"]:
        result["failed_reason"] = "SOFT_UNKNOWN"
    return result



def search_naver_news(keyword, display=20):
    """
    네이버 뉴스 검색 API를 사용하여 관련 기사 목록을 가져옵니다.
    """
    encText = urllib.parse.quote(keyword)
    url = f"https://openapi.naver.com/v1/search/news?query={encText}&display={display}&sort=sim"

    try:
        request = urllib.request.Request(url)
        request.add_header("X-Naver-Client-Id", NAVER_CLIENT_ID)
        request.add_header("X-Naver-Client-Secret", NAVER_CLIENT_SECRET)
        
        response = urllib.request.urlopen(request)
        res_code = response.getcode()

        if res_code == 200:
            response_body = response.read()
            data = json.loads(response_body.decode('utf-8'))
            
            # 우리가 필요한 형태로 데이터 정제
            results = []
            for item in data.get('items', []):
                results.append({
                    "title": item['title'].replace('<b>', '').replace('</b>', ''), # 강조 태그 제거
                    "originallink": item['originallink'], # 원본 URL
                    "link": item['link'], # 네이버 뉴스 URL (있으면 우선 사용)
                    "description": item['description'].replace('<b>', '').replace('</b>', ''),
                    "pubDate": item['pubDate']
                })
            return results
        else:
            logging.error(f"[search_naver_news] Error Code: {res_code}")
            return []

    except Exception as e:
        logging.error(f"[search_naver_news] Exception: {e}")
        return []