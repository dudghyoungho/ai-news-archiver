# links/tasks.py

from celery import shared_task
from django.db import transaction, IntegrityError
from django.utils import timezone
from .models import Link
from .crawler import get_naver_news_info
import logging
from .ai import generate_summary_and_tags, get_embedding, update_user_interest_profile

logger = logging.getLogger(__name__)


# 재시도할 만한 실패 사유(네트워크/일시적 장애 계열)만 선별
RETRYABLE_REASON_PREFIXES = (
    "FETCH_TIMEOUT",
    "FETCH_REQUEST_EXCEPTION",
    "CONNECTION_FAILED",
    "NETWORK_ERROR",
)

RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}


@shared_task(bind=True, max_retries=3)
def crawl_and_save_link(self, link_id: int):
    """
    Link 객체의 ID를 받아 크롤링을 수행하고 DB를 업데이트하는 비동기 작업

    반영한 4가지 필수 수정사항:
    1) SOFT_SUCCESS status 매핑 명확화 (FAILED로 찍혔다가 덮는 구조 제거)
    2) transaction.atomic + select_for_update(+) 상태 가드로 중복 실행 방지
    3) IntegrityError(UniqueConstraint) 발생 시 중복 병합/처리
    4) 네트워크성 오류에 한해 self.retry() 적용
    """
    # 1) 중복 실행 방지: row lock + 상태 가드
    with transaction.atomic():
        try:
            link = Link.objects.select_for_update().get(id=link_id)
        except Link.DoesNotExist:
            logger.error(f"[crawl_and_save_link] Link {link_id} does not exist.")
            return "Link not found"

        # 이미 처리 완료/부분완료/실패라면 재처리하지 않음 (idempotency 강화)
        if link.status in ("COMPLETED", "PARTIAL", "FAILED"):
            logger.info(f"[crawl_and_save_link] Link {link_id} already finalized: {link.status}")
            return f"Link {link_id} already finalized: {link.status}"

        # 이미 다른 워커가 처리 중이면 스킵 (row lock을 쓰므로 보통 여기 도달이 드뭄)
        if link.status == "PROCESSING":
            logger.info(f"[crawl_and_save_link] Link {link_id} is already processing.")
            return f"Link {link_id} already processing"

        # 대기중 -> 처리중
        link.status = "PROCESSING"
        link.save(update_fields=["status"])

        # row lock은 여기서 풀어도 되지만(커밋وبية),
        # PROCESSING 표기 후 네트워크 작업은 락 밖에서 수행하는 게 좋음
        url_to_crawl = link.url
        user_id = link.user_id

    # 2) 크롤러 실행 (락 밖)
    data = get_naver_news_info(url_to_crawl)

    # 4) 네트워크성 오류만 재시도 적용
    try:
        http_status = data.get("http_status")
        reason = (data.get("failed_reason") or "").strip()

        is_retryable = (
            (http_status in RETRYABLE_HTTP_STATUS) or
            any(reason.startswith(pfx) for pfx in RETRYABLE_REASON_PREFIXES)
        )

        if data.get("status") == "FAILED" and is_retryable:
            # 재시도 카운트 증가 및 이유 기록(운영 가시성)
            with transaction.atomic():
                link = Link.objects.select_for_update().get(id=link_id)
                link.retry_count = (link.retry_count or 0) + 1
                link.failed_reason = f"RETRYING: {reason or 'RETRYABLE_FAILURE'}"
                link.save(update_fields=["retry_count", "failed_reason", "updated_at"])
            raise self.retry(exc=Exception(reason or "Retryable failure"), countdown=60)

    except self.MaxRetriesExceededError:
        # 최대 재시도 초과 시 최종 실패로 처리
        with transaction.atomic():
            try:
                link = Link.objects.select_for_update().get(id=link_id)
                link.status = "FAILED"
                link.failed_reason = f"MAX_RETRIES_EXCEEDED: {data.get('failed_reason', '')}"
                link.save(update_fields=["status", "failed_reason", "updated_at"])
            except Link.DoesNotExist:
                pass
        return "Max retries exceeded"
    except Exception:
        # retry가 발생하면 Celery가 예외를 처리하므로 여기서 반환하지 않음
        raise

    # 3) 크롤링 결과를 DB 반영 + IntegrityError 처리
    with transaction.atomic():
        link = Link.objects.select_for_update().get(id=link_id)

        crawler_status = data.get("status")
        failed_reason = (data.get("failed_reason") or "").strip()

        # --- 실패 처리 ---
        if crawler_status == "FAILED":
            link.status = "FAILED"
            link.failed_reason = failed_reason or "CRAWLER_FAILED"
            # retry_count는 위 retry 로직에서 이미 올렸을 수 있으니 여기선 건드리지 않음
            link.save(update_fields=["status", "failed_reason", "updated_at"])
            return f"Link {link_id} processed: FAILED"

        # --- SUCCESS / SOFT_SUCCESS 처리 (1번: status 매핑 명확화) ---
        # 모델에 PARTIAL을 추가했다고 가정
        if crawler_status == "SUCCESS":
            link.status = "COMPLETED"
            link.failed_reason = "" # 성공했으니 에러 메시지 초기화
        elif crawler_status == "SOFT_SUCCESS":
            link.status = "PARTIAL"
            link.failed_reason = f"SOFT_SUCCESS: {failed_reason}"
        else:
            # FAILED인 경우 (위에서 처리했지만 안전장치)
            pass

        # 공통 필드 업데이트
        link.title = data.get("title", "") or link.title
        link.content = data.get("content", "") or link.content

        if data.get("status") in ["SUCCESS", "SOFT_SUCCESS"] and link.content:
            try:
                # 제목과 본문을 줘서 요약 받아오기
                ai_result = generate_summary_and_tags(link.title, link.content)
                if ai_result:
                    link.summary = ai_result.get("summary", "")
                    link.tags = ai_result.get("tags", [])
                    logger.info(f"AI Summary generated for Link {link_id}")
            except Exception as e:
                logger.error(f"AI Generation Failed for Link {link_id}: {e}")
            try:
                # 제목과 본문을 합쳐서 벡터화 (검색/추천 품질 향상)
                text_for_embedding = f"{link.title}\n{link.content}"
                
                vector = get_embedding(text_for_embedding)
                
                if vector:
                    link.embedding = vector  # VectorField에 리스트 저장
                    logger.info(f"Embedding generated for Link {link_id}")
            except Exception as e:
                logger.error(f"Embedding Generation Failed for Link {link_id}: {e}")



        link.publisher = data.get("publisher", "") or link.publisher
        link.image_url = data.get("image_url")
        link.published_at = data.get("published_at")  # None 가능 (크롤러 정책)
        link.naver_oid = data.get("naver_oid")
        link.naver_aid = data.get("naver_aid")

        # 정규화 URL 덮어쓰기 (원본 보존이 필요하면 original_url 필드 권장)
        normalized_url = data.get("normalized_url")
        if normalized_url:
            link.url = normalized_url

        # IntegrityError 대비: 먼저 저장 시도
        try:
            with transaction.atomic():
                link.save()        
            try: # 저장이 확실히 성공한 직후에 사용자 프로필 업데이트 실행
                update_user_interest_profile(user_id)
            except Exception as e:
                logger.error(f"Failed to update user profile for user {user_id}: {e}")

            return f"Link {link_id} processed: {link.status}"

        except IntegrityError as e:
            # (user, naver_oid, naver_aid) UniqueConstraint 충돌 가능
            logger.warning(f"[crawl_and_save_link] IntegrityError for Link {link_id}: {e}")

            oid = link.naver_oid
            aid = link.naver_aid

            if not oid or not aid:
                # oid/aid 없이 충돌은 드물지만, 안전하게 실패 처리
                link.status = "FAILED"
                link.failed_reason = "INTEGRITY_ERROR_WITHOUT_OID_AID"
                link.save(update_fields=["status", "failed_reason", "updated_at"])
                return f"Link {link_id} failed due to IntegrityError (no oid/aid)"

            # 중복 대상(이미 존재하는 동일 기사) 찾기
            existing = (
                Link.objects.select_for_update()
                .filter(user_id=user_id, naver_oid=oid, naver_aid=aid)
                .exclude(id=link_id)
                .order_by("-updated_at")
                .first()
            )

            if not existing:
                # 찾지 못하면 그냥 실패 처리
                link.status = "FAILED"
                link.failed_reason = "INTEGRITY_ERROR_DUPLICATE_NOT_FOUND"
                link.save(update_fields=["status", "failed_reason", "updated_at"])
                return f"Link {link_id} failed: duplicate not found"

            # 병합 정책(권장 최소):
            # - 기존 레코드가 비어있으면 새 데이터로 채움
            # - 기존 레코드가 이미 COMPLETED면 그대로 두고, 현재 레코드는 FAILED(중복)로 표시
            fields_to_fill = ["title", "content", "publisher", "image_url", "published_at"]
            updated = False
            for f in fields_to_fill:
                if not getattr(existing, f) and getattr(link, f):
                    setattr(existing, f, getattr(link, f))
                    updated = True

            # 상태 우선순위: COMPLETED > PARTIAL > FAILED/PENDING/PROCESSING
            # existing이 PARTIAL/FAILED면 새 결과로 업그레이드 가능
            if existing.status != "COMPLETED":
                existing.status = link.status
                updated = True

            if updated:
                # 기존 레코드 저장
                existing.save()

            # 현재 레코드는 중복으로 실패 처리(혹은 삭제 정책도 가능)
            link.status = "FAILED"
            link.failed_reason = f"DUPLICATE_OF:{existing.id}"
            link.save(update_fields=["status", "failed_reason", "updated_at"])

            return f"Link {link_id} marked duplicate of {existing.id}"
        

@shared_task
def retry_failed_links():
    """
    주기적으로 실행되어 FAILED 상태인 링크들을 다시 시도하는 작업
    """
    # 예: 최근 24시간 내 실패한 링크 중 재시도 횟수가 3회 미만인 것
    failed_links = Link.objects.filter(
        status='FAILED', 
        retry_count__lt=3
    )
    
    count = 0
    for link in failed_links:
        print(f"[Celery Beat] Retrying link {link.id}: {link.url}")
        # 상태 초기화 후 재큐잉
        link.status = 'PENDING'
        link.save()
        crawl_and_save_link.delay(link.id)
        count += 1
        
    return f"Retried {count} failed links."