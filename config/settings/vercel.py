from .production import *
import os

ALLOWED_HOSTS = [
    '.vercel.app',
    os.getenv('ALLOWED_HOSTS', ''),
]

CELERY_TASK_ALWAYS_EAGER = True

STATIC_ROOT = BASE_DIR / 'staticfiles'
STATIC_URL = 'static/'

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = os.getenv('CORS_ALLOWED_ORIGINS', '').split(',')
