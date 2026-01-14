import html
import difflib
import logging
import numpy as np
import re

from datetime import timedelta
from collections import Counter
from celery import shared_task
from django.contrib.auth.models import User
from django.db import transaction, IntegrityError
from django.utils import timezone
from dateutil import parser as date_parser
from pgvector.django import CosineDistance

from .models import Link, UserProfile
from .crawler import get_naver_news_info, search_naver_news, parse_naver_ids_and_normalize_url
from .ai import (
    generate_summary_and_tags, 
    get_embedding, 
    get_embeddings_batch, 
    update_user_interest_profile, 
    get_recommendation_keywords,
    get_exploration_keywords,
)

logger = logging.getLogger(__name__)

RETRYABLE_REASON_PREFIXES = (
    "FETCH_TIMEOUT", "FETCH_REQUEST_EXCEPTION", "CONNECTION_FAILED", "NETWORK_ERROR",
)
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}

def is_valid_naver_article(url):
    """
    URL이 네이버 뉴스 본문 페이지인지 (oid, aid 추출 가능한지) 확인합니다.
    """
    patterns = [
        r"article/(\d+)/(\d+)",       # n.news.naver.com/article/001/000123
        r"read\.nhn\?.*oid=(\d+)",    # news.naver.com/main/read.nhn?oid=001&aid=123
    ]
    return any(re.search(p, url) for p in patterns)


@shared_task(bind=True, max_retries=3)
def crawl_and_save_link(self, link_id: int):
    with transaction.atomic():
        try:
            link = Link.objects.select_for_update().get(id=link_id)
        except Link.DoesNotExist:
            return "Link not found"
        
        if link.status in ("COMPLETED", "PARTIAL", "FAILED"):
            return f"Link {link_id} already finalized"
        if link.status == "PROCESSING":
            return f"Link {link_id} already processing"

        link.status = "PROCESSING"
        link.save(update_fields=["status"])
        url_to_crawl = link.url
        user_id = link.user_id

    data = get_naver_news_info(url_to_crawl)

    try:
        http_status = data.get("http_status")
        reason = (data.get("failed_reason") or "").strip()
        is_retryable = (http_status in RETRYABLE_HTTP_STATUS) or any(reason.startswith(pfx) for pfx in RETRYABLE_REASON_PREFIXES)

        if data.get("status") == "FAILED" and is_retryable:
            with transaction.atomic():
                link = Link.objects.select_for_update().get(id=link_id)
                link.retry_count = (link.retry_count or 0) + 1
                link.failed_reason = f"RETRYING: {reason}"
                link.save()
            raise self.retry(exc=Exception(reason), countdown=60)
    except self.MaxRetriesExceededError:
        with transaction.atomic():
            link = Link.objects.select_for_update().get(id=link_id)
            link.status = "FAILED"
            link.failed_reason = "MAX_RETRIES_EXCEEDED"
            link.save()
        return "Max retries exceeded"
    except Exception:
        raise

    with transaction.atomic():
        link = Link.objects.select_for_update().get(id=link_id)
        crawler_status = data.get("status")

        if crawler_status == "FAILED":
            link.status = "FAILED"
            link.failed_reason = data.get("failed_reason", "CRAWLER_FAILED")
            link.save()
            return f"Link {link_id} FAILED: {link.failed_reason}"

        link.title = data.get("title", "") or link.title
        link.content = data.get("content", "") or link.content
        link.publisher = data.get("publisher", "") or link.publisher
        link.image_url = data.get("image_url")
        link.published_at = data.get("published_at")
        link.naver_oid = data.get("naver_oid")
        link.naver_aid = data.get("naver_aid")
        
        if data.get("normalized_url"):
            link.url = data.get("normalized_url")

        if crawler_status == "SUCCESS":
            link.status = "COMPLETED"
        else:
            link.status = "PARTIAL"

        if link.status == "COMPLETED" and link.content:
            try:
                ai_result = generate_summary_and_tags(link.title, link.content)
                if ai_result:
                    link.summary = ai_result.get("summary", "")
                    link.tags = ai_result.get("tags", [])
            except Exception as e:
                logger.error(f"AI Summary Error: {e}")
            
            try:
                text_for_embedding = f"{link.title}\n{link.content}"
                vector = get_embedding(text_for_embedding)
                if vector:
                    link.embedding = vector
            except Exception as e:
                logger.error(f"Embedding Error: {e}")

        try:
            link.save()
            if link.status == "COMPLETED":
                try:
                    update_user_interest_profile(user_id)
                except Exception:
                    pass
            return f"Link {link_id} processed: {link.status}"

        except IntegrityError:
            link.status = "FAILED"
            link.failed_reason = "DUPLICATE_ENTRY"
            link.save()
            return "Duplicate link"


@shared_task
def recommend_articles_for_user(user_id: int):
    """
    사용자의 '관심사 기반' 추천 (Exploit)
    - user.profile.interest_vector 기준으로 네이버 뉴스 후보를 scoring 후 RECOMMENDED 저장
    - 외부 언론사 링크는 normalize_naver_candidate에서 컷
    """
    try:
        user = User.objects.get(id=user_id)

        try:
            profile = user.profile
            if profile.interest_vector is None:
                logger.info(f"[Exploit] user={user_id} has no interest_vector")
                return "No interest vector"
            user_vector = np.array(profile.interest_vector, dtype=np.float32)
        except UserProfile.DoesNotExist:
            return "No user profile"

        now = timezone.now()
        one_day_ago = now - timedelta(days=1)
        one_month_ago = now - timedelta(days=30)

        short_links = Link.objects.filter(
            user=user,
            status="COMPLETED",
            created_at__gte=one_day_ago
        ).order_by("-created_at")[:5]

        short_term_text = "\n".join([f"- {l.title}" for l in short_links])

        long_qs = Link.objects.filter(
            user=user,
            status="COMPLETED",
            created_at__gte=one_month_ago
        )

        all_tags = []
        for tags in long_qs.values_list("tags", flat=True):
            if tags:
                all_tags.extend(tags)
        top_tags = [t for t, _ in Counter(all_tags).most_common(5)]

        core_titles = []
        core_links = Link.objects.filter(
            user=user,
            status="COMPLETED",
            created_at__gte=one_month_ago,
            embedding__isnull=False
        ).annotate(
            distance=CosineDistance("embedding", profile.interest_vector)
        ).order_by("distance")[:3]
        core_titles = [l.title for l in core_links]

        long_term_text = (
            f"Top Tags: {', '.join(top_tags)}\n"
            f"Representative Articles: {', '.join(core_titles)}"
        )

        if not short_term_text and not long_term_text:
            logger.info(f"[Exploit] user={user_id} not enough history")
            return "Not enough history"

        keywords = get_recommendation_keywords(short_term_text, long_term_text)
        if not keywords:
            return "No keywords"
        logger.info(f"[Exploit] user={user_id} keywords={keywords}")

        from .recommend_utils import normalize_naver_candidate

        existing_urls = set(Link.objects.filter(user=user).values_list("url", flat=True))
        seen_urls = set()

        raw_candidates = []
        for kw in keywords:
            items = search_naver_news(kw, display=100)

            for item in items:
                raw_url = item.get("link")
                ident = normalize_naver_candidate(raw_url)
                if not ident:
                    continue

                url = ident["normalized_url"]
                if url in seen_urls or url in existing_urls:
                    continue

                seen_urls.add(url)

                clean_title = html.unescape(item.get("title", "")).replace("<b>", "").replace("</b>", "")
                clean_desc = html.unescape(item.get("description", "")).replace("<b>", "").replace("</b>", "")

                raw_candidates.append({
                    "url": url,
                    "oid": ident["oid"],
                    "aid": ident["aid"],
                    "title": clean_title,
                    "desc": clean_desc,
                    "keyword": kw,
                    "pubDate": item.get("pubDate", "")
                })

        logger.info(f"[Exploit] user={user_id} raw_candidates={len(raw_candidates)}")
        if not raw_candidates:
            return "No candidates"

        TITLE_SIM_THRESHOLD = 0.6
        unique_candidates = []
        for cand in raw_candidates:
            dup = False
            for u in unique_candidates:
                if difflib.SequenceMatcher(None, cand["title"], u["title"]).ratio() > TITLE_SIM_THRESHOLD:
                    dup = True
                    break
            if not dup:
                unique_candidates.append(cand)

        logger.info(f"[Exploit] user={user_id} unique_candidates={len(unique_candidates)}")
        if not unique_candidates:
            return "No unique candidates"

        texts_to_embed = [f"{c['title']}\n{c['desc']}" for c in unique_candidates]
        vectors = get_embeddings_batch(texts_to_embed)

        scored = []
        norm_u = np.linalg.norm(user_vector)

        for cand, vec in zip(unique_candidates, vectors):
            if vec is None:
                continue

            cand_vec = np.array(vec, dtype=np.float32)
            norm_c = np.linalg.norm(cand_vec)
            similarity = (np.dot(user_vector, cand_vec) / (norm_u * norm_c)) if (norm_u > 0 and norm_c > 0) else 0.0

            recency_score = 0.5
            try:
                pub_date = date_parser.parse(cand["pubDate"]) if cand["pubDate"] else None
                if pub_date:
                    if pub_date.tzinfo is None:
                        pub_date = timezone.make_aware(pub_date, timezone.get_current_timezone())
                    hours = max(0, (now - pub_date).total_seconds()) / 3600
                    if hours < 1: recency_score = 1.0
                    elif hours < 6: recency_score = 0.9
                    elif hours < 12: recency_score = 0.8
                    elif hours < 24: recency_score = 0.6
                    else:
                        days = hours / 24
                        recency_score = max(0.0, 0.5 - days * 0.15)
            except Exception:
                recency_score = 0.5

            keyword_score = 1.0 if (cand["keyword"] and cand["keyword"] in cand["title"]) else 0.0
            final_score = (similarity * 0.7) + (recency_score * 0.2) + (keyword_score * 0.1)

            scored.append((final_score, similarity, recency_score, keyword_score, cand))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return "No scored candidates"

        final_top = []
        kw_counts = {}
        for s, sim, r, k, cand in scored:
            if len(final_top) >= 5:
                break
            kw = cand["keyword"]
            if kw_counts.get(kw, 0) >= 2:
                continue
            final_known = cand["url"]
            final_top.append((s, sim, r, k, cand))
            kw_counts[kw] = kw_counts.get(kw, 0) + 1

        if len(final_top) < 5:
            picked = set(x[4]["url"] for x in final_top)
            for s, sim, r, k, cand in scored:
                if len(final_top) >= 5:
                    break
                if cand["url"] in picked:
                    continue
                final_top.append((s, sim, r, k, cand))
                picked.add(cand["url"])

        saved = 0
        with transaction.atomic():
            for s, sim, r, k, cand in final_top:
                if Link.objects.filter(user=user, url=cand["url"]).exists():
                    continue

                Link.objects.create(
                    user=user,
                    url=cand["url"],
                    naver_oid=cand["oid"],
                    naver_aid=cand["aid"],
                    title=cand["title"],
                    publisher="AI Recommend",
                    status="RECOMMENDED",
                    recommendation_type="PERSONAL",
                    failed_reason=f"[Exploit] score={s:.4f} sim={sim:.4f} recency={r:.2f} kw={cand['keyword']}"
                )
                saved += 1

        logger.info(f"[Exploit] user={user_id} saved={saved}")
        return f"Saved {saved}"

    except Exception as e:
        logger.error(f"[Exploit] Error user={user_id}: {e}", exc_info=True)
        return f"Error: {e}"


@shared_task
def retry_failed_links():
    """주기적 재시도 태스크"""
    failed_links = Link.objects.filter(status='FAILED', retry_count__lt=3)
    count = 0
    for link in failed_links:
        link.status = 'PENDING'
        link.save()
        crawl_and_save_link.delay(link.id)
        count += 1
    return f"Retried {count} failed links."


@shared_task
def recommend_articles_daily():
    """모든 유저 대상 추천 실행"""
    for user in User.objects.all():
        recommend_articles_for_user.delay(user.id)
    return "Started tasks"

import logging

logger = logging.getLogger(__name__)

@shared_task
def recommend_exploratory_articles(user_id: int):
    """
    사용자의 '지식 공백' 기반 추천 (Explore)
    - strong/weak 카테고리에서 "브릿지 키워드 + 와일드카드"로 탐험 추천
    - 너무 취향에 붙는 기사(similarity 너무 높음)는 제외 (새로움 확보)
    """
    from .utils import analyze_knowledge_gap, is_within_six_months, is_too_similar
    from .recommend_utils import normalize_naver_candidate

    try:
        user = User.objects.get(id=user_id)
        profile = getattr(user, "profile", None)
        if not profile or profile.interest_vector is None:
            return "User vector not found"

        user_vector = np.array(profile.interest_vector, dtype=np.float32)
        norm_u = np.linalg.norm(user_vector)
        now = timezone.now()

        strong, weak = analyze_knowledge_gap(user)
        keywords = get_exploration_keywords(strong, weak)
        if not keywords:
            return "No exploration keywords"

        logger.info(f"[Explore] user={user_id} strong={strong} weak={weak} keywords={keywords}")

        existing_urls = set(Link.objects.filter(user=user).values_list("url", flat=True))
        existing_titles = list(Link.objects.filter(user=user).values_list("title", flat=True))

        candidates = []
        seen_urls = set()

        for kw in keywords:
            items = search_naver_news(kw, display=80)

            for item in items:
                raw_url = item.get("link")
                ident = normalize_naver_candidate(raw_url)
                if not ident:
                    continue

                url = ident["normalized_url"]
                if url in existing_urls or url in seen_urls:
                    continue

                pub_raw = item.get("pubDate") or ""
                if not is_within_six_months(pub_raw):
                    continue

                clean_title = html.unescape(item.get("title", "")).replace("<b>", "").replace("</b>", "")
                if is_too_similar(clean_title, existing_titles):
                    continue

                seen_urls.add(url)
                existing_titles.append(clean_title)

                clean_desc = html.unescape(item.get("description", "")).replace("<b>", "").replace("</b>", "")

                candidates.append({
                    "url": url,
                    "oid": ident["oid"],
                    "aid": ident["aid"],
                    "title": clean_title,
                    "desc": clean_desc,
                    "keyword": kw,
                    "pubDate": item.get("pubDate", "")
                })

        logger.info(f"[Explore] user={user_id} candidates={len(candidates)}")
        if not candidates:
            return "No candidates"

        texts = [f"{c['title']}\n{c['desc']}" for c in candidates]
        vectors = get_embeddings_batch(texts)

        scored = []
        for cand, vec in zip(candidates, vectors):
            if vec is None:
                continue

            cand_vec = np.array(vec, dtype=np.float32)
            norm_c = np.linalg.norm(cand_vec)

            sim = (np.dot(user_vector, cand_vec) / (norm_u * norm_c)) if (norm_u > 0 and norm_c > 0) else 0.0

            if sim > 0.85:
                continue
            if sim < 0.15:
                continue

            recency_score = 0.5
            try:
                pub_date = date_parser.parse(cand["pubDate"]) if cand["pubDate"] else None
                if pub_date:
                    if pub_date.tzinfo is None:
                        pub_date = timezone.make_aware(pub_date, timezone.get_current_timezone())
                    hours = max(0, (now - pub_date).total_seconds()) / 3600
                    if hours < 6:
                        recency_score = 1.0
                    elif hours < 24:
                        recency_score = 0.8
                    else:
                        days = hours / 24
                        recency_score = max(0.3, 0.7 - days * 0.1)
            except Exception:
                recency_score = 0.5

            novelty = 1.0 - sim
            final_score = (novelty * 0.45) + (recency_score * 0.35) + (sim * 0.20)

            scored.append((final_score, sim, recency_score, cand))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return "No scored exploration candidates"

        final_picks = scored[:3]

        saved = 0
        with transaction.atomic():
            for s, sim, r, cand in final_picks:
                if Link.objects.filter(user=user, url=cand["url"]).exists():
                    continue

                Link.objects.create(
                    user=user,
                    url=cand["url"],
                    naver_oid=cand["oid"],
                    naver_aid=cand["aid"],
                    title=cand["title"],
                    publisher="AI Explore",
                    status="RECOMMENDED",
                    recommendation_type="EXPLORE",
                    failed_reason=f"[Explore] score={s:.4f} sim={sim:.4f} recency={r:.2f} kw={cand['keyword']}"
                )
                saved += 1

        logger.info(f"[Explore] user={user_id} saved={saved}")
        return f"Saved {saved}"

    except Exception as e:
        logger.error(f"[Explore] Error user={user_id}: {e}", exc_info=True)
        return f"Error: {e}"