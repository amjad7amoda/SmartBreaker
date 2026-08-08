from .base import *

DEBUG = True
ALLOWED_HOSTS = ['*']

# Development only: let the browser-based simulator (file:// or any localhost
# port) POST readings to the ingestion endpoints.
CORS_ALLOW_ALL_ORIGINS = True