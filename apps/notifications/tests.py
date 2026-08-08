from django.db import transaction
from django.test import TestCase, override_settings

from apps.accounts.models import User

from .models import Notification
from .services import notify, notify_many


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class NotificationTaskTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(email='user@example.com', role='home_user')
        self.other = User.objects.create_user(email='other@example.com', role='technician')

    def test_notify_creates_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            notify(self.user, 'Breaker tripped')

        notification = Notification.objects.get(recipient=self.user)
        self.assertEqual(notification.message, 'Breaker tripped')
        self.assertFalse(notification.is_read)

    def test_notify_accepts_a_user_id(self):
        with self.captureOnCommitCallbacks(execute=True):
            notify(self.user.id, 'Breaker restored')

        self.assertTrue(Notification.objects.filter(recipient=self.user, message='Breaker restored').exists())

    def test_notify_is_not_dispatched_when_the_transaction_rolls_back(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    notify(self.user, 'Never sent')
                    raise RuntimeError('boom')
            except RuntimeError:
                pass

        self.assertFalse(Notification.objects.filter(message='Never sent').exists())

    def test_notify_many_creates_one_per_recipient(self):
        with self.captureOnCommitCallbacks(execute=True):
            notify_many([self.user, self.other], 'Site offline')

        self.assertEqual(Notification.objects.filter(message='Site offline').count(), 2)

    def test_notify_many_with_no_recipients_is_a_noop(self):
        with self.captureOnCommitCallbacks(execute=True):
            notify_many([], 'Nobody')

        self.assertEqual(Notification.objects.count(), 0)
