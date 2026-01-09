"""
Django settings for config project.
"""

import os
from pathlib import Path
from datetime import timedelta

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# ==========================================
# 1. 보안 및 환경 변수 설정 (배포 대비)
# ==========================================

# .env 파일에서 읽어오거나, 없으면 기본값 사용
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-default-dev-key')

# 배포 시에는 반드시 .env에서 DEBUG=False로 설정해야 함
DEBUG = os.environ.get('DEBUG', 'True') == 'True'

# 콤마(,)로 구분된 호스트 허용 목록
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1,0.0.0.0').split(',')


# ==========================================
# 2. 애플리케이션 정의
# ==========================================

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # 3rd Party Apps
    'corsheaders',
    'rest_framework',
    'rest_framework_simplejwt', # JWT 인증 추가
    
    # Local Apps
    'links',           
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware', # 가장 상단 권장
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'], # 로그인 템플릿 경로 추가
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# ==========================================
# 3. 데이터베이스 설정
# ==========================================

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'postgres'),
        'USER': os.environ.get('POSTGRES_USER', 'postgres'),
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', 'postgres'),
        'HOST': os.environ.get('POSTGRES_HOST', 'db'), # docker-compose 서비스명
        'PORT': 5432,
    }
}


# ==========================================
# 4. 비밀번호 및 언어/시간 설정
# ==========================================

AUTH_PASSWORD_VALIDATORS = [
    { 'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator', },
    { 'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', },
    { 'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator', },
    { 'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator', },
]

LANGUAGE_CODE = 'ko-kr' # 한국어 설정
TIME_ZONE = 'Asia/Seoul'
USE_I18N = True
USE_TZ = True


# ==========================================
# 5. 정적 파일 및 미디어 설정 (배포 필수)
# ==========================================

STATIC_URL = 'static/'
# collectstatic 실행 시 파일이 모이는 곳 (Nginx가 참조)
STATIC_ROOT = BASE_DIR / 'static' 

MEDIA_URL = '/media/'
# 유저가 업로드한 파일(썸네일 등)이 저장되는 곳
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ==========================================
# 6. 로그인/로그아웃 리다이렉트
# ==========================================

LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'


# ==========================================
# 7. DRF & JWT 인증 설정 (하이브리드 구조)
# ==========================================

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        # 1순위: JWT (크롬 익스텐션/API 용)
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        # 2순위: 세션 (웹 브라우저/Admin 용)
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
}

# JWT 세부 설정
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=1),   # 1시간 동안 유효
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),   # 재발급 토큰은 7일
    'ROTATE_REFRESH_TOKENS': False,
    'BLACKLIST_AFTER_ROTATION': False,
    'AUTH_HEADER_TYPES': ('Bearer',),
}


# ==========================================
# 8. CORS & CSRF 설정
# ==========================================

# 개발 편의를 위해 모든 도메인 허용 (배포 시 보안 강화 필요)
CORS_ALLOW_ALL_ORIGINS = True 

CSRF_TRUSTED_ORIGINS = [
    'http://localhost:8000',
    'http://127.0.0.1:8000',
    # 크롬 익스텐션 ID (필수)
    'chrome-extension://kcimckmkkbnlleakaibncopaalgndomn', 
]


# ==========================================
# 9. Celery & Redis 설정
# ==========================================

CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://redis:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_BROKER_URL', 'redis://redis:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Asia/Seoul'


# ==========================================
# 10. 외부 API 키 (.env 연동)
# ==========================================

NAVER_CLIENT_ID = os.environ.get('NAVER_CLIENT_ID')
NAVER_CLIENT_SECRET = os.environ.get('NAVER_CLIENT_SECRET')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')