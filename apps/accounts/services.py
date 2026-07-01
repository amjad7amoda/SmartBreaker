import secrets

from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.utils import timezone

from .models import User
from .tasks import (
    send_denial_email_task,
    send_otp_email_task,
    send_password_reset_email_task,
)

OTP_LENGTH = 6
OTP_VALID_MINUTES = 1440
OTP_RESEND_COOLDOWN_SECONDS = 60

RESET_CODE_LENGTH = 6
RESET_CODE_VALID_MINUTES = 30
RESET_CODE_RESEND_COOLDOWN_SECONDS = 60


def generate_otp():
    return ''.join(secrets.choice('0123456789') for _ in range(OTP_LENGTH))


def _issue_otp(user):
    otp_plain = generate_otp()
    user.otp_hash = make_password(otp_plain)
    user.otp_expires_at = timezone.now() + timezone.timedelta(minutes=OTP_VALID_MINUTES)
    user.otp_last_sent_at = timezone.now()
    user.save()

    transaction.on_commit(
        lambda: send_otp_email_task.delay(user.id, otp_plain, OTP_VALID_MINUTES)
    )


@transaction.atomic
def approve_request(registration_request, reviewed_by):
    if registration_request.status != 'pending':
        raise ValueError('Only pending requests can be approved.')

    user = User.objects.create_user(
        email=registration_request.email,
        role=registration_request.role,
        phone=registration_request.phone,
    )
    _issue_otp(user)

    registration_request.status = 'approved'
    registration_request.reviewed_by = reviewed_by
    registration_request.reviewed_at = timezone.now()
    registration_request.created_user = user
    registration_request.save()

    return user


@transaction.atomic
def deny_request(registration_request, reviewed_by):
    if registration_request.status != 'pending':
        raise ValueError('Only pending requests can be denied.')

    registration_request.status = 'denied'
    registration_request.reviewed_by = reviewed_by
    registration_request.reviewed_at = timezone.now()
    registration_request.save()

    transaction.on_commit(
        lambda: send_denial_email_task.delay(registration_request.email)
    )


@transaction.atomic
def resend_otp(user):
    """Re-issues an OTP for a user who never finished the OTP-login/set-password flow
    (e.g. lost their session or let the OTP expire before completing setup)."""
    if not user.must_set_password:
        raise ValueError('Account setup is already complete.')

    if user.otp_last_sent_at:
        elapsed = (timezone.now() - user.otp_last_sent_at).total_seconds()
        if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
            raise ValueError('Please wait before requesting another OTP.')

    _issue_otp(user)


@transaction.atomic
def request_password_reset(user):
    """Sends a password-reset code. Only for accounts that already completed setup
    (activated + real password already set) — mid-setup accounts should use resend_otp instead."""
    if user.must_set_password:
        raise ValueError('Account setup is not complete yet.')

    if user.reset_code_last_sent_at:
        elapsed = (timezone.now() - user.reset_code_last_sent_at).total_seconds()
        if elapsed < RESET_CODE_RESEND_COOLDOWN_SECONDS:
            raise ValueError('Please wait before requesting another reset code.')

    code_plain = ''.join(secrets.choice('0123456789') for _ in range(RESET_CODE_LENGTH))
    user.reset_code_hash = make_password(code_plain)
    user.reset_code_expires_at = timezone.now() + timezone.timedelta(minutes=RESET_CODE_VALID_MINUTES)
    user.reset_code_last_sent_at = timezone.now()
    user.save()

    transaction.on_commit(
        lambda: send_password_reset_email_task.delay(user.id, code_plain, RESET_CODE_VALID_MINUTES)
    )


@transaction.atomic
def confirm_password_reset(user, new_password):
    user.set_password(new_password)
    user.clear_reset_code()
    user.save()
