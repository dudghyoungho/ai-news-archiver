from django.urls import path
from . import views  # views 모듈 전체 import (에러 방지)

urlpatterns = [
    # ==============================
    # 1. 화면 (HTML/HTMX) - 짧고 간결하게
    # ==============================
    
    # 메인 페이지: localhost:8000/
    path('', views.index, name='index'),
    
    # HTMX 요청용: localhost:8000/create/
    path('create/', views.htmx_link_create, name='htmx_link_create'),


    # ==============================
    # 2. API (JSON) - /api/links/ 프리픽스 명시
    # ==============================
    
    # DRF 생성: localhost:8000/api/links/create/
    path('api/links/create/', views.LinkCreateView.as_view(), name='api_link_create'),
    
    # DRF 목록: localhost:8000/api/links/list/
    path('api/links/list/', views.LinkListView.as_view(), name='api_link_list'),
    
    # DRF 상세: localhost:8000/api/links/1/
    path('api/links/<int:link_id>/', views.LinkDetailView.as_view(), name='api_link_detail'),
    
    # DRF 재시도: localhost:8000/api/links/1/retry/
    path('api/links/<int:link_id>/retry/', views.LinkRetryView.as_view(), name='api_link_retry'),

    # [추가] 추천 기사 변환 및 리다이렉트 URL
    path('recommendation/<int:pk>/convert/', views.convert_recommendation, name='convert_recommendation'),

    # 통계 페이지: localhost:8000/stats
    path('stats/', views.stats_page, name='stats_page'),       # 껍데기 페이지
    path('stats/content/', views.stats_content, name='stats_content'), # 데이터 로딩용 (HTMX)

]