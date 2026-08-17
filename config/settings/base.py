from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv
import os
load_dotenv()

SECRET_KEY = os.getenv('SECRET_KEY')

# Build paths inside the project like this: BASE_DIR / 'subdir'.
# Three parents: this file is config/settings/base.py, so the repository root is
# three levels up. Two would land in config/ and put staticfiles/ inside the
# settings package.
BASE_DIR = Path(__file__).resolve().parent.parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-2(m0qmell5qtw4vm2vsuw=!7__99*ns^@_*hna@qah38ubcwiw'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

# The Pi posts readings to the server's LAN address, not to localhost, so an
# empty list would 400 every ingest request while DEBUG is on.
ALLOWED_HOSTS = [h for h in os.getenv('ALLOWED_HOSTS', '*' if DEBUG else '').split(',') if h]


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_celery_beat',

    # Local apps
    'apps.accounts',
    'apps.organizations',
    'apps.breakers',
    'apps.telemetry',
    'apps.notifications',
    'apps.kbs',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
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
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default':{
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME'),
        'USER': os.getenv('DB_USER'),
        'PASSWORD': os.getenv('DB_PASSWORD'),
        'HOST': os.getenv('DB_HOST'),
        'PORT': os.getenv('DB_PORT'),
    }
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Damascus'
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
# Unused while DEBUG serves static files directly, but collectstatic refuses to
# run without a destination, so keep one defined.
STATIC_ROOT = BASE_DIR / 'staticfiles'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
}

# Auth Model / JWT Settings
AUTH_USER_MODEL = 'accounts.User'
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
}

# Email Settings
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST') or 'smtp.gmail.com'
EMAIL_PORT = int(os.getenv('EMAIL_PORT') or '587')
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')
DEFAULT_FROM_EMAIL = (
    os.getenv('DEFAULT_FROM_EMAIL') or EMAIL_HOST_USER or 'no-reply@smartbreaker.local'
)
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True') == 'True'

# Tuya Platform
TUYA_FERNET_KEY = os.getenv('TUYA_FERNET_KEY')

# Breaker polling and reading retention
BREAKER_POLL_SECONDS = int(os.getenv('BREAKER_POLL_SECONDS') or '30')
BREAKER_READING_RETENTION_MINUTES = int(
    os.getenv('BREAKER_READING_RETENTION_MINUTES') or '60'
)
BREAKER_PURGE_SECONDS = int(os.getenv('BREAKER_PURGE_SECONDS') or '300')

# Celery Beat and Worker CONSTANTS
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TASK_ALWAYS_EAGER = os.getenv('CELERY_TASK_ALWAYS_EAGER', 'False') == 'True'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'

# Cache Settings: if CACHE_URL is set, use Redis; otherwise, use local memory cache.
CACHE_URL = os.getenv('CACHE_URL') or os.getenv('REDIS_URL')
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': CACHE_URL,
    } if CACHE_URL else {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'smartbreaker',
    }
}

CELERY_BEAT_SCHEDULE = {
    # Always on: the KBS reads persisted breaker state every cycle, so polling cannot
    # depend on someone being signed in.
    'poll-breakers': {
        'task': 'apps.breakers.tasks.poll_all_breakers',
        'schedule': float(BREAKER_POLL_SECONDS),
    },
    'purge-breaker-readings': {
        'task': 'apps.breakers.tasks.purge_breaker_readings',
        'schedule': float(BREAKER_PURGE_SECONDS),
    },
    # 'kbs-dispatch': {
    #     'task': 'apps.kbs.tasks.run_kbs_cycles',
    #     'schedule': 60.0,
    # },
}

ALLOWED_HOSTS = [
    "happier-professor-aids.ngrok-free.dev",
    "localhost",
    "127.0.0.1"
]

CSRF_TRUSTED_ORIGINS = [
    "https://happier-professor-aids.ngrok-free.dev",
]