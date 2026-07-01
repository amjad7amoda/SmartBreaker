from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

from .models import User


@shared_task
def send_otp_email_task(user_id, otp_plain, otp_valid_minutes):
    user = User.objects.get(id=user_id)
    send_mail(
        subject='Your Smart Breaker account has been approved',
        message=(
            f'Your account request has been approved.\n\n'
            f'Use the following one-time password to log in: {otp_plain}\n'
            f'This code expires in {otp_valid_minutes} minutes and can only be used once.\n\n'
            f'After logging in you will be asked to set a permanent password.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )


@shared_task
def send_denial_email_task(email):
    send_mail(
        subject='Your Smart Breaker account request was denied',
        message='Your account request has been reviewed and was not approved.',
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
    )


@shared_task
def send_password_reset_email_task(user_id, code_plain, code_valid_minutes):
    user = User.objects.get(id=user_id)
    send_mail(
        subject='Reset your Smart Breaker password',
        message=(
            f'A password reset was requested for your account.\n\n'
            f'Use the following code to reset your password: {code_plain}\n'
            f'This code expires in {code_valid_minutes} minutes and can only be used once.\n\n'
            f"If you didn't request this, you can safely ignore this email."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )
