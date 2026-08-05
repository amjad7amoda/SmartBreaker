from celery import shared_task
from .models import Notification


@shared_task
def send_notification_task(recipient_id, message):
    Notification.objects.create(recipient_id=recipient_id, message=message)


@shared_task
def send_bulk_notification_task(recipient_ids, message):
    Notification.objects.bulk_create([
        Notification(recipient_id=recipient_id, message=message)
        for recipient_id in recipient_ids
    ])
