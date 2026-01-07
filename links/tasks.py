# links/tasks.py

import html
import difflib # [필수] 텍스트 유사도 비교용
import numpy as np
from datetime import timedelta
from dateutil import parser as date_parser
from celery import shared_task
from django.contrib.auth.models import User
from django.db import transaction, IntegrityError # IntegrityError 추가
from django.utils import timezone
import logging
from collections import Counter 
from pgvector.django import CosineDistance

from .models import Link, UserProfile
from .crawler import get_naver_news_info, search_naver_news
from .ai import generate_summary_and_tags, get_embedding, get_embeddings_batch, update_user_interest_profile, get_recommendation_keywords

logger = logging.getLogger(__name__)

# 재시도할 만한 실패 사유
RETRYABLE_REASON_PREFIXES = (
    "FETCH_TIMEOUT", "FETCH_REQUEST_EXCEPTION", "CONNECTION_FAILED", "NETWORK_ERROR",
)
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}

@shared_task(bind=True, max_retries=3)
def crawl_and_save_link(self, link_id: int):
    # (기존 crawl_and_save_link 코드와 동일합니다. 위에서 잘 작성하셨으므로 생략하지 않고 그대로 둡니다.)
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

    # 재시도 로직
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

    # DB 반영
    with transaction.atomic():
        link = Link.objects.select_for_update().get(id=link_id)
        crawler_status = data.get("status")

        if crawler_status == "FAILED":
            link.status = "FAILED"
            link.failed_reason = data.get("failed_reason", "CRAWLER_FAILED")
            link.save()
            return f"Link {link_id} FAILED"

        link.status = "COMPLETED" if crawler_status == "SUCCESS" else "PARTIAL"
        link.title = data.get("title", "") or link.title
        link.content = data.get("content", "") or link.content
        link.publisher = data.get("publisher", "") or link.publisher
        link.image_url = data.get("image_url")
        link.published_at = data.get("published_at")
        
        if data.get("normalized_url"):
            link.url = data.get("normalized_url")

        # AI 요약 및 임베딩
        if link.content:
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
            # 프로필 업데이트 트리거
            try:
                update_user_interest_profile(user_id)
            except Exception:
                pass
            return f"Link {link_id} SUCCESS"

        except IntegrityError:
            # 중복 처리 로직 (간소화)
            link.status = "FAILED"
            link.failed_reason = "DUPLICATE"
            link.save()
            return "Duplicate link"

@shared_task
def retry_failed_links():
    # (기존 코드 유지)
    pass

@shared_task
def recommend_articles_daily():
    for user in User.objects.all():
        recommend_articles_for_user.delay(user.id)
    return "Started tasks"



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

        # ================================================================
        # [수정] 2. Two-Track 데이터 수집 (장기/단기 분리)
        # ================================================================
        
        # A. 단기 기억 (Short-term): 최근 24시간 내 읽은 기사 (최대 5개)
        one_day_ago = timezone.now() - timedelta(days=1)
        short_term_links = Link.objects.filter(
            user=user, 
            status='COMPLETED',
            created_at__gte=one_day_ago
        ).order_by('-created_at')[:5]
        
        short_term_text = "\n".join([f"- {l.title}" for l in short_term_links])

        # B. 장기 기억 (Long-term): 태그 통계 + 벡터 기반 대표 기사
        one_month_ago = timezone.now() - timedelta(days=30)
        
        # B-1. 태그 통계
        long_term_qs = Link.objects.filter(
            user=user,
            status='COMPLETED',
            created_at__gte=one_month_ago
        )
        all_tags = []
        for tags in long_term_qs.values_list('tags', flat=True):
            if tags: all_tags.extend(tags)
        
        top_tags = [tag for tag, count in Counter(all_tags).most_common(5)]

        # B-2. 벡터 기반 대표 기사 (Core Interest)
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

        # 읽은 기록이 아예 없으면 중단
        if not short_term_text and not long_term_text:
            return "Not enough history"

        # 3. 키워드 추천 (ai.py 호출 - 인자 2개 전달)
        # 주의: ai.py의 get_recommendation_keywords 함수도 인자 2개를 받도록 수정되어야 합니다!
        keywords = get_recommendation_keywords(short_term_text, long_term_text)
        
        if not keywords: return "Failed keywords"
        logger.info(f"[Recommend] Keywords: {keywords}")

        # 4. 네이버 뉴스 검색 (후보군 확보)
        raw_candidates = []
        seen_urls = set()
        existing_urls = set(Link.objects.filter(user=user).values_list('url', flat=True))

        for kw in keywords:
            items = search_naver_news(kw, display=100)
            if not items:
                logger.warning(f"[Recommend] Naver Search returned 0 items for keyword: {kw}")
            
            for item in items:
                url = item.get('link', '')
                if 'naver.com' not in url: continue # 네이버 뉴스만
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

        # ================================================================
        # [최적화 1] 텍스트 중복 제거
        # ================================================================
        unique_candidates = []
        TITLE_SIMILARITY_THRESHOLD = 0.6 
        
        # A. 후보군끼리 중복 제거
        for cand in raw_candidates:
            is_duplicate = False
            for unique in unique_candidates:
                seq = difflib.SequenceMatcher(None, cand['clean_title'], unique['clean_title'])
                if seq.ratio() > TITLE_SIMILARITY_THRESHOLD:
                    is_duplicate = True
                    break
            if not is_duplicate:
                unique_candidates.append(cand)

        # B. DB 기사와 중복 제거
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

        # ================================================================
        # [최적화 2] 배치 임베딩
        # ================================================================
        vectors = get_embeddings_batch(texts_to_embed) if texts_to_embed else []

        # 5. 점수 계산
        scored_candidates = []
        for i, cand in enumerate(final_candidates_to_embed):
            vec = vectors[i]
            if vec is None: continue
            
            cand_vec = np.array(vec, dtype=np.float32)
            norm_u = np.linalg.norm(user_vector)
            norm_c = np.linalg.norm(cand_vec)
            similarity = np.dot(user_vector, cand_vec) / (norm_u * norm_c) if (norm_u > 0 and norm_c > 0) else 0

            # 5-2. [수정] 시간(Hour) 단위 초정밀 최신성 점수
            try:
                pub_date = date_parser.parse(cand['item']['pubDate'])
                # Timezone 처리 (Naive vs Aware)
                if pub_date.tzinfo is None:
                    now = timezone.now().replace(tzinfo=None)
                else:
                    now = timezone.now()
                
                # 시간 차이 계산
                time_diff = now - pub_date
                hours_diff = max(0, time_diff.total_seconds()) / 3600

                if hours_diff < 1: recency_score = 1.0      # 1시간 이내 (속보)
                elif hours_diff < 6: recency_score = 0.9    # 반나절 이내
                elif hours_diff < 12: recency_score = 0.8   # 당일 뉴스
                elif hours_diff < 24: recency_score = 0.6   # 하루 전
                else: 
                    days = hours_diff / 24
                    recency_score = max(0, 0.5 - (days * 0.15))
            except Exception:
                recency_score = 0.5

            keyword_score = 1.0 if cand['keyword'] in cand['clean_title'] else 0.0
            
            # 가중치 (유사도7 : 최신성2 : 키워드1)
            final_score = (similarity * 0.7) + (recency_score * 0.2) + (keyword_score * 0.1)
            scored_candidates.append((final_score, cand))

        # 6. 랭킹 & 쿼터제 (다양성 보장)
        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        
        final_top_5 = []
        keyword_counts = {}
        
        for score, cand in scored_candidates:
            if len(final_top_5) >= 5: break
            
            kw = cand['keyword']
            current_count = keyword_counts.get(kw, 0)
            
            # 한 키워드당 최대 2개까지만 (다양성 강제)
            if current_count >= 2: continue
            
            final_top_5.append((score, cand))
            keyword_counts[kw] = current_count + 1

        # 부족하면 채우기
        if len(final_top_5) < 5:
            picked_urls = set(c['url'] for s, c in final_top_5)
            for score, cand in scored_candidates:
                if len(final_top_5) >= 5: break
                if cand['url'] not in picked_urls:
                    final_top_5.append((score, cand))

        # 7. 저장
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