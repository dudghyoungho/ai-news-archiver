import json
from django.shortcuts import get_object_or_404, render, redirect
from django.db import transaction
from datetime import timedelta
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.authentication import BasicAuthentication
from django.db.models import Q


from collections import Counter
from django.shortcuts import render
from django.db.models import Count
from django.db.models.functions import TruncDate
from pgvector.django import CosineDistance # 임포트 확인
from .ai import analyze_user_interest # 임포트 추가

from .models import Link, UserProfile
from .tasks import crawl_and_save_link, recommend_articles_for_user, recommend_exploratory_articles
from .serializers import LinkSerializer
from .utils import determine_persona, CATEGORY_KEYWORDS




@method_decorator(csrf_exempt, name='dispatch')
class LinkCreateView(APIView):
    """
    사용자가 네이버 뉴스 URL을 저장하면:
    1) Link 레코드를 PENDING으로 생성
    2) Celery Task를 큐에 넣어 비동기 크롤링 시작
    """

    authentication_classes = [BasicAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        url = (request.data.get("url") or "").strip()
        if not url:
            return Response({"detail": "url is required"}, status=status.HTTP_400_BAD_REQUEST)

        # 최소한의 도메인 가드(완벽 검증은 crawler가 oid/aid 파싱하며 수행)
        # 필요하면 더 엄격하게: n.news.naver.com / news.naver.com 만 허용
        if "naver.com" not in url:
            return Response({"detail": "Only Naver URLs are allowed"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            # 같은 사용자가 같은 URL을 연속 저장하는 중복(완전한 중복 방지는 oid/aid 파싱 후 가능)
            # 간단 가드: 같은 url이 PENDING/PROCESSING이면 기존 것을 재사용
            existing = (
                Link.objects.select_for_update()
                .filter(user=request.user, url=url, status__in=["PENDING", "PROCESSING"])
                .order_by("-created_at")
                .first()
            )
            if existing:
                # 이미 작업이 돌아가고 있으면 그 링크를 그대로 반환
                return Response(
                    {
                        "id": existing.id,
                        "status": existing.status,
                        "message": "Already queued/processing",
                    },
                    status=status.HTTP_200_OK,
                )

            link = Link.objects.create(
                user=request.user,
                url=url,
                status="PENDING",
                failed_reason="",
                retry_count=0,
            )

        # 비동기 작업 큐잉
        crawl_and_save_link.delay(link.id)

        return Response(
            {"id": link.id, "status": link.status, "message": "Queued"},
            status=status.HTTP_201_CREATED,
        )


class LinkListView(APIView):
    """
    로그인한 사용자의 링크 목록 조회
    쿼리 파라미터:
      - status=PENDING|PROCESSING|COMPLETED|FAILED|PARTIAL
      - q=검색어(제목/언론사/요약/본문 일부 검색 - 간단 contains)
      - ordering=created_at|published_at (기본: -created_at)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Link.objects.filter(user=request.user)

        st = (request.query_params.get("status") or "").strip().upper()
        if st:
            qs = qs.filter(status=st)

        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                title__icontains=q
            ) | qs.filter(
                publisher__icontains=q
            ) | qs.filter(
                summary__icontains=q
            ) | qs.filter(
                content__icontains=q
            )

        ordering = (request.query_params.get("ordering") or "-created_at").strip()
        allowed = {"created_at", "-created_at", "published_at", "-published_at", "updated_at", "-updated_at"}
        if ordering not in allowed:
            ordering = "-created_at"
        qs = qs.order_by(ordering)

        serializer = LinkSerializer(qs[:200], many=True)
        return Response(serializer.data)


class LinkDetailView(APIView):
    """
    단건 조회
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, link_id: int):
        link = get_object_or_404(Link, id=link_id, user=request.user)
        # 수동 매핑 대신 시리얼라이저 사용
        serializer = LinkSerializer(link) 
        return Response(serializer.data, status=status.HTTP_200_OK)


class LinkRetryView(APIView):
    """
    FAILED / PARTIAL 링크를 사용자가 재시도할 수 있게 하는 엔드포인트.
    - FAILED: 크롤링 재시도
    - PARTIAL: 본문/메타 보강 시도(정책상 허용하면)
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, link_id: int):
        with transaction.atomic():
            link = get_object_or_404(Link.objects.select_for_update(), id=link_id, user=request.user)

            # 처리 중이면 재시도 금지
            if link.status == "PROCESSING":
                return Response({"detail": "Already processing"}, status=status.HTTP_409_CONFLICT)

            # 재시도 가능한 상태만 허용
            if link.status not in ("FAILED", "PARTIAL", "PENDING"):
                return Response({"detail": f"Retry not allowed for status={link.status}"},
                                status=status.HTTP_400_BAD_REQUEST)

            # 상태 리셋
            link.status = "PENDING"
            link.failed_reason = ""
            link.save(update_fields=["status", "failed_reason", "updated_at"])

        crawl_and_save_link.delay(link.id)
        return Response({"id": link.id, "status": link.status, "message": "Re-queued"}, status=status.HTTP_200_OK)
    

def get_link_context(user):
    """
    공통 로직: 사용자가 봐야 할 의미 있는 링크(완료됨 + 추천됨)와
    현재 서버에서 처리 중인 상태를 통합 관리합니다.
    """

    # 10분 이상 PROCESSING이면 비정상으로 보고 FAILED 처리
    stale_cutoff = timezone.now() - timedelta(minutes=10)
    Link.objects.filter(
        user=user,
        status='PROCESSING',
        updated_at__lt=stale_cutoff
    ).update(status='FAILED', failed_reason='STALE_PROCESSING_TIMEOUT')

    # PENDING도 너무 오래되면 FAILED 처리(선택)
    Link.objects.filter(
        user=user,
        status='PENDING',
        updated_at__lt=stale_cutoff
    ).update(status='FAILED', failed_reason='STALE_PENDING_TIMEOUT')

    # [수정] 사용자가 화면에서 봐야 할 기사들만 필터링
    links = Link.objects.filter(
        user=user,
        status__in=['COMPLETED', 'RECOMMENDED', 'FAILED', 'PARTIAL']
    ).order_by('-created_at')
    
    # 처리 중인 건 확인
    has_pending = Link.objects.filter(
        user=user,
        status__in=['PENDING', 'PROCESSING']
    ).exists()
    
    return {
        'links': links,
        'has_pending': has_pending
    }

@login_required
@require_POST
def htmx_link_create(request):
    """
    index.html의 폼에서 호출되는 뷰입니다.
    데이터 저장 후 'JSON'이 아닌 '갱신된 리스트 HTML'을 반환합니다.
    """
    url = request.POST.get('url', '').strip()

    # 1. 유효성 검사 (간단 버전)
    if not url or "naver.com" not in url:
        # 에러 시 사용자에게 알림을 주고 싶다면 여기서 에러 메시지가 포함된 HTML을 줄 수도 있음
        # 일단은 무시하고 현재 리스트 반환 (또는 Htmx Response Header로 트리거 가능)
        pass 
    else:
        # 2. 저장 로직 (LinkCreateView의 로직을 재사용)
        with transaction.atomic():
            # 중복 방지 로직
            existing = (
                Link.objects.select_for_update()
                .filter(user=request.user, url=url, status__in=["PENDING", "PROCESSING"])
                .order_by("-created_at")
                .first()
            )
            
            if not existing:
                link = Link.objects.create(
                    user=request.user,
                    url=url,
                    status="PENDING",
                    failed_reason="",
                    retry_count=0,
                )
                # 3. 비동기 크롤링 시작
                crawl_and_save_link.delay(link.id)

    # 4. 저장 완료 후 '최신 리스트'를 다시 조회
    context = get_link_context(request.user)
    # 5. 리스트 HTML 조각만 렌더링해서 반환 (HTMX가 이걸 받아서 갈아끼움)
    return render(request, 'links/partials/link_list.html', context)


@login_required
@require_POST
def htmx_recommend_interest(request):
    """
    관심사 기반 추천 즉시 실행 (Celery 큐잉)
    """
    recommend_articles_for_user.delay(request.user.id)

    # 바로 UI를 갱신하고 싶으면, '대기중' 안내를 같이 보여주면 좋음
    context = get_link_context(request.user)
    return render(request, "links/partials/link_list.html", context)

@login_required
@require_POST
def htmx_recommend_explore(request):
    """
    탐험 추천 즉시 실행 (Celery 큐잉)
    """
    recommend_exploratory_articles.delay(request.user.id)

    context = get_link_context(request.user)
    return render(request, "links/partials/link_list.html", context)

@login_required
def index(request):
    # 1. 공통 컨텍스트 가져오기 (이미 필터링된 links가 들어있음)
    context = get_link_context(request.user)
    links = context['links']

    # 2. 태그 통계 계산 (이미 가져온 links 활용)
    all_tags = []
    for link in links:
        if link.status == 'COMPLETED' and link.tags: # 통계는 읽은 기사로만
            all_tags.extend(link.tags)
    
    tag_counts = Counter(all_tags).most_common(5)
    chart_labels = [tag for tag, count in tag_counts]
    chart_data = [count for tag, count in tag_counts]

    # 컨텍스트 업데이트
    context.update({
        'chart_labels': chart_labels,
        'chart_data': chart_data,
    })
    
    # 3. HTMX 요청 분기 처리
    if request.headers.get('HX-Request'):
        return render(request, 'links/partials/link_list.html', context)
    
    return render(request, 'links/index.html', context)


def convert_recommendation(request, pk):
    """
    추천 기사를 클릭했을 때 실행되는 중간 경유 뷰.
    1. 상태를 RECOMMENDED -> PENDING으로 변경
    2. 크롤링/요약 태스크 트리거 (비동기)
    3. 실제 기사 URL로 리다이렉트
    """
    # 내 소유의 링크인지 확인하며 가져오기
    link = get_object_or_404(Link, pk=pk, user=request.user)
    
    # 이미 완료된 게 아니라면, 크롤링 시작
    if link.status == 'RECOMMENDED':
        link.status = 'PENDING'
        link.save(update_fields=['status'])
        
        # 비동기 작업 시작 (Celery)
        crawl_and_save_link.delay(link.id)
    
    # 사용자에게는 원래 가려던 뉴스 페이지를 보여줌
    return redirect(link.url)

@login_required
def stats_page(request):
    """
    스냅샷 기반 '껍데기' 페이지.
    - 여기서는 절대 GPT/통계 계산을 하지 않음
    - 저장된 snapshot이 있으면 그것을 렌더
    - 없으면 empty 화면
    """
    user = request.user

    # 프로필 보장
    profile, _ = UserProfile.objects.get_or_create(user=user)

    snapshot = profile.stats_snapshot or {}
    has_snapshot = bool(snapshot) and snapshot.get("total_count", 0) > 0

    if not has_snapshot:
        # 아직 snapshot이 없다면 빈 화면(안내) 렌더
        return render(request, "links/stats.html", {
            "has_snapshot": False,
            "snapshot_updated_at": profile.stats_snapshot_updated_at,
        })

    # snapshot을 stats_content.html이 기대하는 변수명으로 매핑해서 전달
    context = {
        "has_snapshot": True,
        "snapshot_updated_at": profile.stats_snapshot_updated_at,

        "persona": snapshot.get("persona"),
        "ai_insight": snapshot.get("ai_insight"),
        "total_count": snapshot.get("total_count", 0),

        # 아래 값들은 템플릿에서 JS로 읽기 쉽도록 JSON 문자열로 유지
        "tag_labels": json.dumps(snapshot.get("tag_labels", []), ensure_ascii=False),
        "tag_data": json.dumps(snapshot.get("tag_data", [])),
        "cat_labels": json.dumps(snapshot.get("cat_labels", []), ensure_ascii=False),
        "cat_data": json.dumps(snapshot.get("cat_data", [])),
        "trend_labels": json.dumps(snapshot.get("trend_labels", []), ensure_ascii=False),
        "trend_data": json.dumps(snapshot.get("trend_data", [])),
    }

    # stats.html 안에서 partial include로 렌더하는 구조라면
    # stats.html이 context를 그대로 받아야 함
    return render(request, "links/stats.html", context)


@login_required
def stats_content(request):
    """
    '새로고침 버튼'으로만 호출되는 HTMX partial.
    - 통계 계산 + AI 브리핑 생성(원하면)
    - 결과를 UserProfile.stats_snapshot에 저장
    - partial(stats_content.html) 반환
    """
    user = request.user
    profile, _ = UserProfile.objects.get_or_create(user=user)

    # 1) 읽은 기사
    completed_links = Link.objects.filter(user=user, status="COMPLETED")

    if not completed_links.exists():
        # snapshot도 비워두고 updated_at도 갱신하지 않음(원하면 초기화 가능)
        return render(request, "links/partials/stats_empty.html")

    # =========================================================
    # [Logic 1] AI 지식 브리핑 (선택)
    # - 핵심: stats_content에서만 호출됨
    # =========================================================
    ai_insight = None
    if profile.interest_vector is not None:
        closest_links = (
            Link.objects.filter(user=user, status="COMPLETED", embedding__isnull=False)
            .annotate(distance=CosineDistance("embedding", profile.interest_vector))
            .order_by("distance")[:5]
        )
        representative_texts = [l.title for l in closest_links if l.title]
        if representative_texts:
            try:
                ai_insight = analyze_user_interest(representative_texts)
            except Exception as e:
                logger.warning(f"[stats_content] analyze_user_interest error user={user.id}: {e}")
                ai_insight = None

    # =========================================================
    # [Logic 2] 페르소나
    # =========================================================
    persona = determine_persona(completed_links)

    # =========================================================
    # [Logic 3] 차트 데이터
    # =========================================================
    # 3-1. 태그 (Bar)
    all_tags = []
    for link in completed_links:
        if link.tags:
            all_tags.extend(link.tags)

    tag_counts = Counter(all_tags).most_common(10)
    tag_labels = [tag for tag, count in tag_counts]
    tag_data = [count for tag, count in tag_counts]

    # 3-2. 카테고리 (Radar)
    cat_scores = {k: 0 for k in CATEGORY_KEYWORDS.keys()}
    for tag in all_tags:
        for cat, keywords in CATEGORY_KEYWORDS.items():
            if any(k in tag for k in keywords):
                cat_scores[cat] += 1
                break
    cat_labels = list(cat_scores.keys())
    cat_data = list(cat_scores.values())

    # 3-3. 추이 (Line) - 최근 14일
    daily_stats = (
        completed_links
        .annotate(date=TruncDate("created_at"))
        .values("date")
        .annotate(count=Count("id"))
        .order_by("date")
    )
    trend_labels = [item["date"].strftime("%m-%d") for item in daily_stats][-14:]
    trend_data = [item["count"] for item in daily_stats][-14:]

    # =========================================================
    # [Snapshot 저장] (핵심)
    # =========================================================
    snapshot = {
        "persona": persona,
        "ai_insight": ai_insight,
        "total_count": completed_links.count(),

        "tag_labels": tag_labels,
        "tag_data": tag_data,
        "cat_labels": cat_labels,
        "cat_data": cat_data,
        "trend_labels": trend_labels,
        "trend_data": trend_data,
    }

    profile.stats_snapshot = snapshot
    profile.stats_snapshot_updated_at = timezone.now()
    profile.save(update_fields=["stats_snapshot", "stats_snapshot_updated_at"])

    # =========================================================
    # [템플릿 컨텍스트]
    # =========================================================
    context = {
        "persona": persona,
        "ai_insight": ai_insight,
        "total_count": snapshot["total_count"],

        # 템플릿에서 JS 안전하게 쓰도록 JSON 문자열로
        "tag_labels": json.dumps(tag_labels, ensure_ascii=False),
        "tag_data": json.dumps(tag_data),
        "cat_labels": json.dumps(cat_labels, ensure_ascii=False),
        "cat_data": json.dumps(cat_data),
        "trend_labels": json.dumps(trend_labels, ensure_ascii=False),
        "trend_data": json.dumps(trend_data),

        "snapshot_updated_at": profile.stats_snapshot_updated_at,
    }

    return render(request, "links/partials/stats_content.html", context)