import os
from celery import Celery

# Django의 settings 모듈을 Celery의 기본 설정으로 지정
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')

# 문자열로 등록한 설정이 'CELERY'로 시작함을 알림
app.config_from_object('django.conf:settings', namespace='CELERY')

# INSTALLED_APPS에 등록된 모든 tasks.py를 자동으로 찾음
app.autodiscover_tasks()