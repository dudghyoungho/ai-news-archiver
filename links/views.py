# links/views.py

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

from .models import Link
from .tasks import crawl_and_save_link
from .serializers import LinkSerializer

from collections import Counter

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
    공통 로직 분리: 링크 리스트와 pending 여부를 반환
    """
    links = Link.objects.filter(user=user).order_by('-created_at')
    
    # [핵심] 진행 중인(PENDING, PROCESSING) 건이 하나라도 있는지 확인
    # exists()는 쿼리가 매우 가벼움 (데이터 전체 로딩 X)
    has_pending = links.filter(status__in=['PENDING', 'PROCESSING']).exists()
    
    return {
        'links': links,
        'has_pending': has_pending  # 이 변수가 HTML을 제어함
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
    
    # 1. 내 링크 가져오기
    links = Link.objects.filter(user=request.user).order_by('-created_at')

    # 2. 태그 통계 계산 (Python 레벨에서 간단 처리)
    all_tags = []
    for link in links:
        if link.tags: # 태그가 있으면
            all_tags.extend(link.tags)
    
    # 가장 많이 등장한 태그 Top 5 추출
    # 예: [('경제', 5), ('AI', 3), ...]
    tag_counts = Counter(all_tags).most_common(5)
    
    # Chart.js에 넣기 좋게 라벨과 데이터로 분리
    chart_labels = [tag for tag, count in tag_counts]
    chart_data = [count for tag, count in tag_counts]

    context = get_link_context(request.user)

    context.update({
        'chart_labels': chart_labels,
        'chart_data': chart_data,
    })
    
    # 3. HTMX 요청 분기 처리 (중요!)
    # HTMX가 "리스트만 업데이트해줘"라고 요청하면(hx-target="#link-list"), 
    # 전체 페이지가 아니라 '리스트 부분'만 렌더링해서 보내줍니다.
    if request.headers.get('HX-Request'):
        return render(request, 'links/partials/link_list.html', context)
    
    return render(request, 'links/index.html', context)



