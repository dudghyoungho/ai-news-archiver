# links/tasks.py

import html
import difflib # 텍스트 유사도 비교용
import logging
import numpy as np
from datetime import timedelta
from collections import Counter # [필수] 태그 통계용

from celery import shared_task
from django.contrib.auth.models import User
from django.db import transaction, IntegrityError
from django.utils import timezone
from dateutil import parser as date_parser

# [필수] 벡터 거리 계산 및 모델 임포트
from pgvector.django import CosineDistance
from .models import Link, UserProfile

# [필수] 크롤러 및 AI 모듈 임포트
from .crawler import get_naver_news_info, search_naver_news
from .ai import (
    generate_summary_and_tags, 
    get_embedding, 
    get_embeddings_batch, 
    update_user_interest_profile, 
    get_recommendation_keywords
)

logger = logging.getLogger(__name__)

# 재시도할 만한 실패 사유
RETRYABLE_REASON_PREFIXES = (
    "FETCH_TIMEOUT", "FETCH_REQUEST_EXCEPTION", "CONNECTION_FAILED", "NETWORK_ERROR",
)
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}


# =========================================================
# 1. 크롤링 및 저장 태스크
# =========================================================
@shared_task(bind=True, max_retries=3)
def crawl_and_save_link(self, link_id: int):
    # 1) 중복 실행 방지 및 상태 가드
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

    # 2) 크롤러 실행 (crawler.py의 로직을 따름)
    data = get_naver_news_info(url_to_crawl)

    # 3) 네트워크성 오류 재시도 처리
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

    # 4) DB 반영 및 AI 후처리
    with transaction.atomic():
        link = Link.objects.select_for_update().get(id=link_id)
        crawler_status = data.get("status")

        # --- 실패 (포토뉴스 포함) ---
        if crawler_status == "FAILED":
            link.status = "FAILED"
            link.failed_reason = data.get("failed_reason", "CRAWLER_FAILED")
            link.save()
            return f"Link {link_id} FAILED: {link.failed_reason}"

        # --- 성공 데이터 반영 ---
        link.title = data.get("title", "") or link.title
        link.content = data.get("content", "") or link.content
        link.publisher = data.get("publisher", "") or link.publisher
        link.image_url = data.get("image_url")
        link.published_at = data.get("published_at")
        link.naver_oid = data.get("naver_oid")
        link.naver_aid = data.get("naver_aid")
        
        if data.get("normalized_url"):
            link.url = data.get("normalized_url")

        # 상태 결정: SUCCESS면 바로 COMPLETED (crawler.py에서 길이 체크 함)
        if crawler_status == "SUCCESS":
            link.status = "COMPLETED"
        else:
            link.status = "PARTIAL"

        # --- AI 요약 및 임베딩 (COMPLETED일 때만) ---
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
            # 프로필 업데이트 (내가 읽은 글이 추가됐으니 취향 업데이트)
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


# =========================================================
# 2. 추천 시스템 (Two-Track + Hourly Recency + Dedup)
# =========================================================
@shared_task
def recommend_articles_for_user(user_id):
    try:
        user = User.objects.get(id=user_id)
        
        # 1. 사용자 프로필 확인
        try:
            profile = user.profile
            if profile.interest_vector is None:
                logger.info(f"User {user_id} has no interest vector. Skipping.")
                return "No interest vector"
            user_vector = np.array(profile.interest_vector, dtype=np.float32)
        except UserProfile.DoesNotExist:
            return "No user profile"

        # 2. Two-Track 데이터 수집 (장기/단기 분리)
        
        # A. 단기 기억 (Short-term)
        one_day_ago = timezone.now() - timedelta(days=1)
        short_term_links = Link.objects.filter(
            user=user, 
            status='COMPLETED',
            created_at__gte=one_day_ago
        ).order_by('-created_at')[:5]
        
        short_term_text = "\n".join([f"- {l.title}" for l in short_term_links])

        # B. 장기 기억 (Long-term)
        one_month_ago = timezone.now() - timedelta(days=30)
        
        # B-1. 태그 통계
        long_term_qs = Link.objects.filter(
            user=user, status='COMPLETED', created_at__gte=one_month_ago
        )
        all_tags = []
        for tags in long_term_qs.values_list('tags', flat=True):
            if tags: all_tags.extend(tags)
        
        top_tags = [tag for tag, count in Counter(all_tags).most_common(5)]

        # B-2. 벡터 기반 대표 기사
        core_interest_articles = []
        if user.profile.interest_vector is not None:
            core_links = Link.objects.filter(
                user=user,
                status='COMPLETED',
                created_at__gte=one_month_ago,
                embedding__isnull=False
            ).annotate(
                distance=CosineDistance('embedding', user.profile.interest_vector)
            ).order_by('distance')[:3]
            core_interest_articles = [l.title for l in core_links]

        long_term_text = (
            f"Top Tags: {', '.join(top_tags)}\n"
            f"Representative Articles: {', '.join(core_interest_articles)}"
        )

        if not short_term_text and not long_term_text:
            return "Not enough history"

        # 3. 키워드 추천
        keywords = get_recommendation_keywords(short_term_text, long_term_text)
        if not keywords: return "Failed keywords"
        logger.info(f"[Recommend] Keywords: {keywords}")

        # 4. 네이버 뉴스 검색 (후보군 확보)
        raw_candidates = []
        seen_urls = set()
        existing_urls = set(Link.objects.filter(user=user).values_list('url', flat=True))

        for kw in keywords:
            # display=100으로 늘려 아웃링크 필터링 대비
            items = search_naver_news(kw, display=100)
            
            for item in items:
                url = item.get('originallink') or item.get('link')
                # 네이버 뉴스만 허용
                if 'naver.com' not in url: continue
                
                if url in seen_urls or url in existing_urls: continue
                
                seen_urls.add(url)
                clean_title = html.unescape(item.get('title', '')).replace('<b>', '').replace('</b>', '')
                
                raw_candidates.append({
                    'item': item,
                    'clean_title': clean_title,
                    'url': url,
                    'keyword': kw
                })

        if not raw_candidates: return "No candidates"

        # 5. 중복 제거 (Dedup)
        unique_candidates = []
        TITLE_SIMILARITY_THRESHOLD = 0.6 
        
        # A. 후보군끼리
        for cand in raw_candidates:
            is_duplicate = False
            for unique in unique_candidates:
                seq = difflib.SequenceMatcher(None, cand['clean_title'], unique['clean_title'])
                if seq.ratio() > TITLE_SIMILARITY_THRESHOLD:
                    is_duplicate = True
                    break
            if not is_duplicate:
                unique_candidates.append(cand)

        # B. DB 기사와
        recent_titles = list(Link.objects.filter(
            user=user,
            created_at__gte=timezone.now() - timedelta(days=3)
        ).values_list('title', flat=True))

        final_candidates_to_embed = []
        texts_to_embed = []

        for cand in unique_candidates:
            is_already_read = False
            for db_title in recent_titles:
                seq = difflib.SequenceMatcher(None, cand['clean_title'], db_title)
                if seq.ratio() > TITLE_SIMILARITY_THRESHOLD:
                    is_already_read = True
                    break
            
            if not is_already_read:
                final_candidates_to_embed.append(cand)
                texts_to_embed.append(f"{cand['clean_title']}\n{cand['item']['description']}")

        # 6. 배치 임베딩 및 점수 계산
        vectors = get_embeddings_batch(texts_to_embed) if texts_to_embed else []
        scored_candidates = []
        
        for i, cand in enumerate(final_candidates_to_embed):
            vec = vectors[i]
            if vec is None: continue
            
            cand_vec = np.array(vec, dtype=np.float32)
            norm_u = np.linalg.norm(user_vector)
            norm_c = np.linalg.norm(cand_vec)
            similarity = np.dot(user_vector, cand_vec) / (norm_u * norm_c) if (norm_u > 0 and norm_c > 0) else 0

            # [시간 단위 최신성]
            try:
                pub_date = date_parser.parse(cand['item']['pubDate'])
                if pub_date.tzinfo is None:
                    now = timezone.now().replace(tzinfo=None)
                else:
                    now = timezone.now()
                
                hours_diff = max(0, (now - pub_date).total_seconds()) / 3600
                
                if hours_diff < 1: recency_score = 1.0
                elif hours_diff < 6: recency_score = 0.9
                elif hours_diff < 12: recency_score = 0.8
                elif hours_diff < 24: recency_score = 0.6
                else: 
                    days = hours_diff / 24
                    recency_score = max(0, 0.5 - (days * 0.15))
            except Exception:
                recency_score = 0.5

            keyword_score = 1.0 if cand['keyword'] in cand['clean_title'] else 0.0
            
            final_score = (similarity * 0.7) + (recency_score * 0.2) + (keyword_score * 0.1)
            scored_candidates.append((final_score, cand))

        # 7. 랭킹 & 쿼터제
        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        
        final_top_5 = []
        keyword_counts = {}
        
        for score, cand in scored_candidates:
            if len(final_top_5) >= 5: break
            kw = cand['keyword']
            current_count = keyword_counts.get(kw, 0)
            
            if current_count >= 2: continue # 쿼터 제한
            
            final_top_5.append((score, cand))
            keyword_counts[kw] = current_count + 1

        # 부족하면 채우기
        if len(final_top_5) < 5:
            picked_urls = set(c['url'] for s, c in final_top_5)
            for score, cand in scored_candidates:
                if len(final_top_5) >= 5: break
                if cand['url'] not in picked_urls:
                    final_top_5.append((score, cand))

        # 8. 저장
        saved_count = 0
        with transaction.atomic():
            for score, cand in final_top_5:
                if Link.objects.filter(user=user, url=cand['url']).exists(): continue

                Link.objects.create(
                    user=user,
                    url=cand['url'],
                    title=cand['clean_title'],
                    image_url=None, 
                    publisher="AI Recommend", 
                    status='RECOMMENDED',
                    failed_reason=f"Score: {score:.4f}, Keyword: {cand['keyword']}"
                )
                saved_count += 1
        
        logger.info(f"[Recommend] Saved {saved_count} articles.")
        return f"Saved {saved_count}"

    except Exception as e:
        logger.error(f"[Recommend] Error: {e}")
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