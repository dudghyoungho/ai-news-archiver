from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('create/', views.htmx_link_create, name='htmx_link_create'),
    
    path('signup/', views.SignUpView.as_view(), name='signup'),

    path('recommendation/<int:pk>/convert/', views.convert_recommendation, name='convert_recommendation'),
    path('stats/', views.stats_page, name='stats_page'),
    path('stats/content/', views.stats_content, name='stats_content'),
    path("recommend/interest/", views.htmx_recommend_interest, name="htmx_recommend_interest"),
    path("recommend/explore/", views.htmx_recommend_explore, name="htmx_recommend_explore"),

    path('api/links/create/', views.LinkCreateView.as_view(), name='api_link_create'),
    path('api/links/list/', views.LinkListView.as_view(), name='api_link_list'),
    path('api/links/<int:link_id>/', views.LinkDetailView.as_view(), name='api_link_detail'),
    path('api/links/<int:link_id>/retry/', views.LinkRetryView.as_view(), name='api_link_retry'),

    path("api/whoami/", views.api_whoami, name="api_whoami"),
]