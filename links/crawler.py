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


_SESSION: Optional[requests.Session] = None

def get_session() -> requests.Session:
    """
    Session for confronting : Unstability of Network/Rate Limit/Temporary 5xx Error.
    -> Use connection pool, reusing module by global.
    """
    global _SESSION
    if _SESSION is not None:
        return _SESSION

    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
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

@dataclass
class NaverNewsIdentity:
    oid: str
    aid: str
    normalized_url: str


def parse_naver_ids_and_normalize_url(url: str) -> Optional[NaverNewsIdentity]:
    """
    Extract oid/aid from Naver News URL safely, normalize the addrress by formal type (n.news.naver.com/mnews/article/{oid}/{aid}) if possible.
    Examples of supporting type:
    - https://n.news.naver.com/mnews/article/001/0014400000
    - https://n.news.naver.com/article/001/0014400000
    - https://news.naver.com/main/read.naver?oid=001&aid=0014400000
    """
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        query = parse_qs(parsed.query)

        if "oid" in query and "aid" in query:
            oid = query["oid"][0]
            aid = query["aid"][0]
            if oid.isdigit() and aid.isdigit():
                normalized = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
                return NaverNewsIdentity(oid=oid, aid=aid, normalized_url=normalized)

        m = re.search(r"/(?:mnews/)?article/(?P<oid>\d{3,})/(?P<aid>\d{5,})", path)
        if m:
            oid = m.group("oid")
            aid = m.group("aid")
            normalized = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
            return NaverNewsIdentity(oid=oid, aid=aid, normalized_url=normalized)

        m2 = re.search(r"oid=(\d+).*aid=(\d+)", parsed.query)
        if m2:
            oid, aid = m2.group(1), m2.group(2)
            if oid.isdigit() and aid.isdigit():
                normalized = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
                return NaverNewsIdentity(oid=oid, aid=aid, normalized_url=normalized)

        return None

    except Exception:
        return None

def parse_iso_datetime(value: str) -> Optional[datetime]:
    """
    Parse similar string. Use "Z" Processing method, return timezone-aware.
    """
    if not value:
        return None
    try:
        v = value.strip()
        if v.endswith("Z"):
            v = v.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt
    except Exception:
        return None


def parse_korean_datetime(value: str) -> Optional[datetime]:
    """
    Ex : '2024.01.14. AM 11:05'
        '2024.01.14. PM 3:07'
    """
    if not value:
        return None
    text = value.strip()

    m = re.search(
        r"(?P<y>\d{4})\.(?P<m>\d{2})\.(?P<d>\d{2})\.\s*(?P<ap>AM|PM)\s*(?P<h>\d{1,2}):(?P<min>\d{2})",
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
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0

    try:
        dt = datetime(y, mo, d, h, mi, 0)
        return timezone.make_aware(dt, timezone.get_current_timezone())
    except Exception:
        return None


def extract_published_at(soup: BeautifulSoup) -> Optional[datetime]:
    """
    priority of extracting published_at :
    1) meta article:published_time (ISO)
    2) data-date-time attribute
    3) text on moniter (format of AM/PM or 오전/오후 (in Korean))
    """
    for prop in ["article:published_time", "og:article:published_time"]:
        meta = soup.select_one(f'meta[property="{prop}"]')
        if meta and meta.get("content"):
            dt = parse_iso_datetime(meta["content"])
            if dt:
                return dt

    date_tag = soup.select_one(".media_end_head_info_datestamp_time")
    if date_tag:
        raw = date_tag.get("data-date-time")
        if raw:
            dt = parse_iso_datetime(raw.replace(" ", "T"))
            if dt:
                return dt
            try:
                dt2 = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                return timezone.make_aware(dt2, timezone.get_current_timezone())
            except Exception:
                pass

        txt = date_tag.get_text(strip=True)
        dt3 = parse_korean_datetime(txt)
        if dt3:
            return dt3

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


# Access Restricted Detection
def detect_access_restriction(soup: BeautifulSoup) -> Optional[str]:
    """
    제한 페이지(로그인/연령/권한)를 완벽히 잡는 건 어렵지만,
    오탐을 줄이기 위해 '본문이 비어있고' 특정 키워드가 있을 때만 제한으로 판단.
    """
    text = soup.get_text(" ", strip=True)
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

    if not (soup.select_one("#dic_area") or soup.select_one("#articeBody")):
        return "ACCESS_RESTRICTED"

    return None


# Content Extraction / Cleaning
def clean_article_text(container) -> str:
    """
    Refine texts, decompose unnessasary tags from the article body container.
    - save 'a' tag by unwraping, since if it's decomposed, the text flys off together.
    """
    for tag in container.select("script, style, .img_desc, .end_photo_org, .byline, .reporter_area"):
        tag.decompose()

    for a in container.select("a"):
        a.unwrap()

    text = container.get_text(separator="\n", strip=True)

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

    for sel in ["#dic_area img", "article img", "img"]:
        img = soup.select_one(sel)
        if img and img.get("src"):
            return img.get("src")
    return None


# Main Function
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

    ident = parse_naver_ids_and_normalize_url(url)
    if not ident:
        result["failed_reason"] = "INVALID_NAVER_URL_FORMAT"
        return result

    result["naver_oid"] = ident.oid
    result["naver_aid"] = ident.aid
    result["normalized_url"] = ident.normalized_url

    fetch_url = ident.normalized_url

    try:
        resp = session.get(fetch_url, timeout=(3, 10))
        result["http_status"] = resp.status_code

        if resp.status_code in (403, 404):
            result["failed_reason"] = f"ACCESS_DENIED_OR_NOT_FOUND({resp.status_code})"
            return result

        resp.raise_for_status()

    except requests.exceptions.Timeout:
        result["failed_reason"] = "FETCH_TIMEOUT"
        return result
    except requests.exceptions.RequestException as e:
        result["failed_reason"] = f"FETCH_REQUEST_EXCEPTION({str(e)})"
        return result

    soup = BeautifulSoup(resp.content, "html.parser")

    restriction = detect_access_restriction(soup)
    if restriction:
        result["failed_reason"] = restriction

    title = extract_title(soup)
    if not title:
        result["failed_reason"] = result["failed_reason"] or "PARSE_NO_TITLE"
        return result

    result["title"] = title
    result["publisher"] = extract_publisher(soup)
    result["image_url"] = extract_image_url(soup)
    result["published_at"] = extract_published_at(soup) 

    content = extract_content(soup)
    result["content"] = content

    if len(content) >= min_len and not result["failed_reason"]:
        result["status"] = "SUCCESS"
        return result

    if len(content) < min_len:
        if not result["failed_reason"]:
            result["failed_reason"] = "SOFT_CONTENT_TOO_SHORT"
        result["status"] = "SOFT_SUCCESS"
        return result

    result["status"] = "SOFT_SUCCESS"
    if not result["failed_reason"]:
        result["failed_reason"] = "SOFT_UNKNOWN"
    return result



import urllib.parse
import urllib.request
import json
import logging
import html as ihtml

logger = logging.getLogger(__name__)

def search_naver_news(keyword, display=20, sort="sim"):
    keyword = (keyword or "").strip()
    if not keyword:
        logger.warning("[search_naver_news] empty keyword")
        return []

    cid = (NAVER_CLIENT_ID or "").strip()
    csec = (NAVER_CLIENT_SECRET or "").strip()

    logger.warning(
        "[search_naver_news] keyword=%r cid_len=%s csec_len=%s",
        keyword,
        len(cid),
        len(csec),
    )

    if not cid or not csec:
        logger.error("[search_naver_news] NAVER API keys missing (cid/csec empty)")
        return []

    enc = urllib.parse.quote(keyword)
    url = f"https://openapi.naver.com/v1/search/news?query={enc}&display={display}&sort={sort}"

    try:
        req = urllib.request.Request(url)
        req.add_header("X-Naver-Client-Id", cid)
        req.add_header("X-Naver-Client-Secret", csec)

        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.getcode() != 200:
                logger.error(f"[search_naver_news] Error Code: {resp.getcode()}")
                return []
            data = json.loads(resp.read().decode("utf-8"))

        results = []
        for item in data.get("items") or []:
            title = ihtml.unescape((item.get("title") or "")).replace("<b>", "").replace("</b>", "").strip()
            desc  = ihtml.unescape((item.get("description") or "")).replace("<b>", "").replace("</b>", "").strip()
            results.append({
                "title": title,
                "originallink": item.get("originallink") or "",
                "link": item.get("link") or "",
                "description": desc,
                "pubDate": item.get("pubDate") or "",
            })

        return results

    except Exception as e:
        logger.error(f"[search_naver_news] Exception: {e}", exc_info=True)
        return []