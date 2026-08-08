from django.db import transaction

from .tasks import send_bulk_notification_task, send_notification_task


def notify(user, message):
    recipient_id = getattr(user, 'id', user)

    transaction.on_commit(
        lambda: send_notification_task.delay(recipient_id, message)
    )


def notify_many(users, message):
    recipient_ids = [getattr(user, 'id', user) for user in users]
    
    if not recipient_ids:
        return

    transaction.on_commit(
        lambda: send_bulk_notification_task.delay(recipient_ids, message)
    )
