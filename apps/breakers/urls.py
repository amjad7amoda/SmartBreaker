from django.urls import path

from . import views

urlpatterns = [
    path('tuya-credentials/', views.TuyaCredentialListCreateView.as_view(), name='tuya-credential-list-create'),
    path('tuya-credentials/<int:pk>/', views.TuyaCredentialDetailView.as_view(), name='tuya-credential-detail'),
    path('actions/', views.BreakerActionListView.as_view(), name='breaker-action-list'),
    path('actions/<int:pk>/', views.BreakerActionDetailView.as_view(), name='breaker-action-detail'),
    path('', views.BreakerListCreateView.as_view(), name='breaker-list-create'),
    path('<str:device_id>/status/', views.BreakerStatusView.as_view(), name='breaker-status'),
    path('<str:device_id>/switch/', views.BreakerSwitchView.as_view(), name='breaker-switch'),
    path('<str:device_id>/child-lock/', views.BreakerChildLockView.as_view(), name='breaker-child-lock'),
    path('<str:device_id>/countdown/', views.BreakerCountdownView.as_view(), name='breaker-countdown'),
    path('<str:device_id>/', views.BreakerDetailView.as_view(), name='breaker-detail'),
    path('<str:device_id>/delete/', views.BreakerDeleteView.as_view(), name='breaker-delete'),]
