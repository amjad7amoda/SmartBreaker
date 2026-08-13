from django.urls import path

from . import views

urlpatterns = [
    path('readings/', views.ReadingIngestView.as_view(), name='reading-ingest'),
    path('readings/latest/', views.ReadingLatestView.as_view(), name='reading-latest'),
]
