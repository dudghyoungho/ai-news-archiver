import json
import logging

from django.shortcuts import get_object_or_404, render, redirect
from django.db import transaction
from datetime import timedelta
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.urls import reverse_lazy
from django.views.generic import CreateView
from django.contrib.auth.forms import UserCreationForm

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.authentication import SessionAuthentication, BasicAuthentication
from django.db.models import Q

from django.http import JsonResponse

from collections import Counter
from django.shortcuts import render
from django.db.models import Count
from django.db.models.functions import TruncDate
from pgvector.django import CosineDistance
from .ai import analyze_user_interest

from .models import Link, UserProfile
from .tasks import crawl_and_save_link, recommend_articles_for_user, recommend_exploratory_articles
from .serializers import LinkSerializer
from .utils import determine_persona, CATEGORY_KEYWORDS

logger = logging.getLogger(__name__)



class LinkCreateView(APIView):
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        url = (request.data.get("url") or "").strip()
        if not url:
            return Response({"detail": "url is required"}, status=status.HTTP_400_BAD_REQUEST)

        if "naver.com" not in url:
            return Response({"detail": "Only Naver URLs are allowed"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            existing = (
                Link.objects.select_for_update()
                .filter(user=request.user, url=url, status__in=["PENDING", "PROCESSING"])
                .order_by("-created_at")
                .first()
            )
            if existing:
                return Response(
                    {"id": existing.id, "status": existing.status, "message": "Already queued/processing"},
                    status=status.HTTP_200_OK,
                )

            link = Link.objects.create(
                user=request.user,
                url=url,
                status="PENDING",
                failed_reason="",
                retry_count=0,
            )

        crawl_and_save_link.delay(link.id)
        return Response({"id": link.id, "status": link.status, "message": "Queued"}, status=status.HTTP_201_CREATED)


class LinkListView(APIView):
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
    permission_classes = [IsAuthenticated]

    def get(self, request, link_id: int):
        link = get_object_or_404(Link, id=link_id, user=request.user)
        serializer = LinkSerializer(link) 
        return Response(serializer.data, status=status.HTTP_200_OK)


class LinkRetryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, link_id: int):
        with transaction.atomic():
            link = get_object_or_404(Link.objects.select_for_update(), id=link_id, user=request.user)

            if link.status == "PROCESSING":
                return Response({"detail": "Already processing"}, status=status.HTTP_409_CONFLICT)

            if link.status not in ("FAILED", "PARTIAL", "PENDING"):
                return Response({"detail": f"Retry not allowed for status={link.status}"},
                                status=status.HTTP_400_BAD_REQUEST)

            link.status = "PENDING"
            link.failed_reason = ""
            link.save(update_fields=["status", "failed_reason", "updated_at"])

        crawl_and_save_link.delay(link.id)
        return Response({"id": link.id, "status": link.status, "message": "Re-queued"}, status=status.HTTP_200_OK)
    

class SignUpView(CreateView):
    """
    회원가입 뷰: Django 내장 폼을 사용하여 유저 생성
    성공 시 로그인 페이지로 이동
    """
    form_class = UserCreationForm
    success_url = reverse_lazy('login')
    template_name = 'registration/signup.html'



def get_link_context(user):
    stale_cutoff = timezone.now() - timedelta(minutes=10)
    Link.objects.filter(
        user=user,
        status='PROCESSING',
        updated_at__lt=stale_cutoff
    ).update(status='FAILED', failed_reason='STALE_PROCESSING_TIMEOUT')

    Link.objects.filter(
        user=user,
        status='PENDING',
        updated_at__lt=stale_cutoff
    ).update(status='FAILED', failed_reason='STALE_PENDING_TIMEOUT')

    links = Link.objects.filter(
        user=user,
        status__in=['COMPLETED', 'RECOMMENDED', 'FAILED', 'PARTIAL']
    ).order_by('-created_at')
    
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

    if not url or "naver.com" not in url:
        pass 
    else:
        with transaction.atomic():
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
                crawl_and_save_link.delay(link.id)

    context = get_link_context(request.user)
    return render(request, 'links/partials/link_list.html', context)


@login_required
@require_POST
def htmx_recommend_interest(request):
    """
    관심사 기반 추천 즉시 실행 (Celery 큐잉)
    """
    res = recommend_articles_for_user.delay(request.user.id)
    logger.info(f"[HTMX] interest recommend queued user={request.user.id} task_id={res.id}")
    context = get_link_context(request.user)
    return render(request, "links/partials/link_list.html", context)

@login_required
@require_POST
def htmx_recommend_explore(request):
    """
    탐험 추천 즉시 실행 (Celery 큐잉)
    """
    res = recommend_exploratory_articles.delay(request.user.id)
    logger.info(f"[HTMX] explore recommend queued user={request.user.id} task_id={res.id}")

    context = get_link_context(request.user)
    return render(request, "links/partials/link_list.html", context)

@login_required
def index(request):
    context = get_link_context(request.user)
    links = context['links']
    all_tags = []
    for link in links:
        if link.status == 'COMPLETED' and link.tags:
            all_tags.extend(link.tags)
    
    tag_counts = Counter(all_tags).most_common(5)
    chart_labels = [tag for tag, count in tag_counts]
    chart_data = [count for tag, count in tag_counts]

    context.update({
        'chart_labels': chart_labels,
        'chart_data': chart_data,
    })
    
    if request.headers.get('HX-Request'):
        return render(request, 'links/partials/link_list.html', context)
    
    return render(request, 'links/index.html', context)


def convert_recommendation(request, pk):
    link = get_object_or_404(Link, pk=pk, user=request.user)
    
    if link.status == 'RECOMMENDED':
        link.status = 'PENDING'
        link.save(update_fields=['status'])
        
        crawl_and_save_link.delay(link.id)
    return redirect(link.url)

@login_required
def stats_page(request):
    user = request.user
    profile, _ = UserProfile.objects.get_or_create(user=user)

    snapshot = profile.stats_snapshot or {}
    has_snapshot = bool(snapshot) and snapshot.get("total_count", 0) > 0

    if not has_snapshot:
        return render(request, "links/stats.html", {
            "has_snapshot": False,
            "snapshot_updated_at": profile.stats_snapshot_updated_at,
        })

    context = {
        "has_snapshot": True,
        "snapshot_updated_at": profile.stats_snapshot_updated_at,

        "persona": snapshot.get("persona"),
        "ai_insight": snapshot.get("ai_insight"),
        "total_count": snapshot.get("total_count", 0),

        "tag_labels": json.dumps(snapshot.get("tag_labels", []), ensure_ascii=False),
        "tag_data": json.dumps(snapshot.get("tag_data", [])),
        "cat_labels": json.dumps(snapshot.get("cat_labels", []), ensure_ascii=False),
        "cat_data": json.dumps(snapshot.get("cat_data", [])),
        "trend_labels": json.dumps(snapshot.get("trend_labels", []), ensure_ascii=False),
        "trend_data": json.dumps(snapshot.get("trend_data", [])),
    }

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

    completed_links = Link.objects.filter(user=user, status="COMPLETED")

    if not completed_links.exists():
        return render(request, "links/partials/stats_empty.html")

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

    persona = determine_persona(completed_links)

    all_tags = []
    for link in completed_links:
        if link.tags:
            all_tags.extend(link.tags)

    tag_counts = Counter(all_tags).most_common(10)
    tag_labels = [tag for tag, count in tag_counts]
    tag_data = [count for tag, count in tag_counts]

    cat_scores = {k: 0 for k in CATEGORY_KEYWORDS.keys()}
    for tag in all_tags:
        for cat, keywords in CATEGORY_KEYWORDS.items():
            if any(k in tag for k in keywords):
                cat_scores[cat] += 1
                break
    cat_labels = list(cat_scores.keys())
    cat_data = list(cat_scores.values())

    daily_stats = (
        completed_links
        .annotate(date=TruncDate("created_at"))
        .values("date")
        .annotate(count=Count("id"))
        .order_by("date")
    )
    trend_labels = [item["date"].strftime("%m-%d") for item in daily_stats][-14:]
    trend_data = [item["count"] for item in daily_stats][-14:]

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

    context = {
        "persona": persona,
        "ai_insight": ai_insight,
        "total_count": snapshot["total_count"],

        "tag_labels": json.dumps(tag_labels, ensure_ascii=False),
        "tag_data": json.dumps(tag_data),
        "cat_labels": json.dumps(cat_labels, ensure_ascii=False),
        "cat_data": json.dumps(cat_data),
        "trend_labels": json.dumps(trend_labels, ensure_ascii=False),
        "trend_data": json.dumps(trend_data),

        "snapshot_updated_at": profile.stats_snapshot_updated_at,
    }

    return render(request, "links/partials/stats_content.html", context)


@login_required
def api_whoami(request):
    return JsonResponse({
        "id": request.user.id,
        "username": request.user.username,
        "is_superuser": request.user.is_superuser,
    })