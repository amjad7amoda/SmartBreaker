from django.urls import path

from .views import BreakerStatusIngestView

urlpatterns = [
    path('status/', BreakerStatusIngestView.as_view(), name='breaker-status-ingest'),
]
