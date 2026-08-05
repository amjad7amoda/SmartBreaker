from django.urls import path
from .views import MarkNotificationReadView, NotificationListView

urlpatterns = [
    path('', NotificationListView.as_view(), name='notification-list'),
    path('<int:pk>/mark-read/', MarkNotificationReadView.as_view(), name='mark-notification-read'),
]