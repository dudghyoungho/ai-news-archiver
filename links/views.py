# links/views.py
import json
from django.shortcuts import get_object_or_404, render, redirect
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.decorators import login_required # 추가
from django.views.decorators.http import require_POST     # 추가

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

from .models import Link
from .tasks import crawl_and_save_link
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
    # [수정] 사용자가 화면에서 봐야 할 기사들만 필터링
    links = Link.objects.filter(
        Q(user=user) & 
        (Q(status='COMPLETED') | Q(status='RECOMMENDED') | Q(status='PENDING') | Q(status='PROCESSING'))
    ).order_by('-created_at')
    
    # 처리 중인 건 확인
    has_pending = links.filter(status__in=['PENDING', 'PROCESSING']).exists()
    
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



def stats_page(request):
    """통계 페이지 껍데기"""
    return render(request, 'links/stats.html')

def stats_content(request):
    """HTMX로 로딩되는 실제 통계 데이터"""
    user = request.user
    
    # 1. 읽은 기사 가져오기
    completed_links = Link.objects.filter(user=user, status='COMPLETED')

    # 데이터가 없으면 빈 화면 표시
    if not completed_links.exists():
        return render(request, 'links/partials/stats_empty.html')

    # =========================================================
    # [Logic 1] AI 지식 브리핑 (GPT) - 데이터가 있을 때만
    # =========================================================
    ai_insight = None
    if hasattr(user, 'profile') and user.profile.interest_vector is not None:
        # 내 취향과 가장 가까운 기사 5개 찾기
        closest_links = Link.objects.filter(
            user=user,
            status='COMPLETED',
            embedding__isnull=False
        ).annotate(
            distance=CosineDistance('embedding', user.profile.interest_vector)
        ).order_by('distance')[:5]
        
        representative_texts = [link.title for link in closest_links]
        
        # GPT 분석 호출 (시간이 좀 걸릴 수 있음)
        if representative_texts:
            ai_insight = analyze_user_interest(representative_texts)

    # =========================================================
    # [Logic 2] 페르소나 분석 (utils.py 활용)
    # =========================================================
    persona = determine_persona(completed_links)

    # =========================================================
    # [Logic 3] 차트 데이터 계산 (이 부분이 누락되어 에러가 났을 것임)
    # =========================================================
    
    # 3-1. 태그 데이터 준비 (Bar Chart)
    all_tags = []
    for link in completed_links:
        if link.tags:
            all_tags.extend(link.tags)
            
    tag_counts = Counter(all_tags).most_common(10)
    tag_labels = [tag for tag, count in tag_counts]
    tag_data = [count for tag, count in tag_counts]

    # 3-2. 카테고리 데이터 준비 (Radar Chart)
    # utils.py의 CATEGORY_KEYWORDS를 재활용하여 일관성 유지
    cat_scores = {k: 0 for k in CATEGORY_KEYWORDS.keys()}
    
    for tag in all_tags:
        for cat, keywords in CATEGORY_KEYWORDS.items():
            if any(k in tag for k in keywords):
                cat_scores[cat] += 1
                break
    
    cat_labels = list(cat_scores.keys())
    cat_data = list(cat_scores.values())

    # 3-3. 일별 추이 데이터 준비 (Line Chart)
    daily_stats = (
        completed_links
        .annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(count=Count('id'))
        .order_by('date')
    )
    # 날짜 포맷팅 (MM-DD)
    trend_labels = [item['date'].strftime('%m-%d') for item in daily_stats][-14:] # 최근 2주만
    trend_data = [item['count'] for item in daily_stats][-14:]

    # =========================================================
    # [Final] 컨텍스트 조립
    # =========================================================
    context = {
        'persona': persona,
        'ai_insight': ai_insight,
        'total_count': completed_links.count(),
        
        # JSON 직렬화 (Template에서 safe 필터 사용)
        'tag_labels': json.dumps(tag_labels),
        'tag_data': json.dumps(tag_data),
        'cat_labels': json.dumps(cat_labels),
        'cat_data': json.dumps(cat_data),
        'trend_labels': json.dumps(trend_labels),
        'trend_data': json.dumps(trend_data),
    }
    
    return render(request, 'links/partials/stats_content.html', context)