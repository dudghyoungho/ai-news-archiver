import os
from celery import Celery
# [추가] 주기적 실행 스케줄을 위한 crontab 임포트
from celery.schedules import crontab

# Django의 settings 모듈을 Celery의 기본 설정으로 지정
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')

# 문자열로 등록한 설정이 'CELERY'로 시작함을 알림
app.config_from_object('django.conf:settings', namespace='CELERY')

# INSTALLED_APPS에 등록된 모든 tasks.py를 자동으로 찾음
app.autodiscover_tasks()

# ========================================================
# [수정] Celery Beat 스케줄 정의
# ========================================================
app.conf.beat_schedule = {
    
    # 1. 뉴스 추천 시스템 가동 (매 시간 정각에 실행)
    # 기존 'daily'라는 이름의 함수를 쓰지만, 실제로는 1시간마다 돕니다.
    'recommend-articles-every-hour': {
        'task': 'links.tasks.recommend_articles_daily', # tasks.py에 정의된 함수 이름
        'schedule': crontab(minute=0), # 0분마다 (예: 1:00, 2:00, 3:00 ...)
        'args': (), # 전달할 인자가 있다면 여기에 (현재는 없음)
    },

    # 2. 실패한 링크 크롤링 재시도 (매 30분마다 실행)
    # 네트워크 오류 등으로 실패한 건들을 주기적으로 다시 시도합니다.
    'retry-failed-links-every-30-min': {
        'task': 'links.tasks.retry_failed_links',
        'schedule': crontab(minute='*/30'), # 30분 주기 (예: 1:00, 1:30, 2:00 ...)
    },
}