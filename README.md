# Authentication System

This document describes the registration-approval + OTP authentication flow built for
`apps/accounts`, and how to run/test it locally.

## Flow overview

1. **Request an account** — a prospective user submits `email`, `phone`, and `role`
   (`home_user` or `technician`). No password is collected at this stage.
2. **Admin review** — an admin approves or denies the request, either from the Django
   admin panel or via the REST API. Admin accounts can only be created via
   `manage.py createsuperuser` — there is no API or admin-panel way to create one.
3. **Approval** — creates the `User` (inactive, no usable password), generates a
   one-time password (OTP), and emails it to the user.
4. **OTP login** — the user logs in once with the OTP. This activates the account and
   issues JWT tokens, but flags `must_set_password: true`.
5. **Set password** — the user must set a real password before doing anything else.
   After this, `must_set_password` is cleared.
6. **Normal login** — from then on, the user logs in with email + password.

Two recovery paths exist for when things go wrong mid-flow:

- **Resend OTP** — if the user loses their session, lets the OTP expire, or otherwise
  gets stuck before finishing steps 4–5, they can request a fresh OTP (invalidates the
  old one).
- **Forgot password** — once fully set up, a normal "forgot password" flow (emailed
  reset code + new password) is available.

## Data model (`apps/accounts/models.py`)

**`User`**
- `is_active` — `False` until OTP login succeeds.
- `must_set_password` — `True` until the user sets a real password after OTP login.
- `otp_hash` / `otp_expires_at` / `otp_last_sent_at` — activation OTP (hashed, expiring,
  rate-limited resend).
- `reset_code_hash` / `reset_code_expires_at` / `reset_code_last_sent_at` — password
  reset code (separate from the OTP fields since it's a different flow, only usable by
  already-activated accounts).

**`RegistrationRequest`**
- `email`, `phone`, `role` (`home_user` / `technician` only — admin is not requestable).
- `status`: `pending` / `approved` / `denied`.
- `reviewed_by`, `reviewed_at`, `created_user` — set when an admin acts on the request.

## Service layer (`apps/accounts/services.py`)

Central place for all state-changing logic, shared by both the Django admin actions and
the REST endpoints:

- `approve_request` / `deny_request` — review a `RegistrationRequest`.
- `resend_otp` — re-issues an OTP for an account stuck mid-setup.
- `request_password_reset` / `confirm_password_reset` — forgot-password flow for
  already-activated accounts.

All of these are wrapped in `@transaction.atomic`, and email sending is dispatched via
`transaction.on_commit(...)` so an email is only ever sent after the database change it
describes has actually committed.

## Asynchronous email (Celery)

Email sending runs as Celery tasks (`apps/accounts/tasks.py`) backed by Redis:

- `send_otp_email_task`
- `send_denial_email_task`
- `send_password_reset_email_task`

**Settings** (`config/settings/base.py`): `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`,
`CELERY_TASK_ALWAYS_EAGER` (env-driven). **Broker**: Redis via `docker-compose.yml`.

To run locally:
```bash
docker compose up -d redis
celery -A config worker -l info
```

## Email delivery

Both dev and prod use real SMTP (`EMAIL_BACKEND`, `EMAIL_HOST*` in
`config/settings/base.py`), reading credentials from `.env`. A Gmail App Password is
required in `.env` (`EMAIL_HOST_PASSWORD`) for outgoing mail to actually work — the
console backend was replaced because it only logs emails instead of sending them.

## API endpoints (`apps/accounts/urls.py`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/accounts/requests/` | Public | Submit a registration request |
| POST | `/api/accounts/requests/<id>/approve/` | Admin | Approve a request |
| POST | `/api/accounts/requests/<id>/deny/` | Admin | Deny a request |
| POST | `/api/accounts/otp-login/` | Public | Log in with OTP, activates account |
| POST | `/api/accounts/resend-otp/` | Public | Re-issue OTP for a stuck account |
| POST | `/api/accounts/set-password/` | Authenticated | Set first real password |
| POST | `/api/accounts/forgot-password/` | Public | Request a password reset code |
| POST | `/api/accounts/reset-password/` | Public | Confirm code + set new password |
| POST | `/api/accounts/login/` | Public | Normal email + password login |
| POST | `/api/accounts/login/refresh/` | Public | Refresh JWT access token |

`forgot-password`, `resend-otp`, and the request-creation endpoint all return generic,
identical responses regardless of whether the target email exists or is eligible, to
avoid leaking account existence (user enumeration).

## Admin panel (`apps/accounts/admin.py`)

- `User` admin has **no "Add" permission** — accounts can only be created via the
  request-approval flow or `createsuperuser`.
- `RegistrationRequest` admin has `Approve selected` / `Deny selected` bulk actions that
  call the same service functions as the REST endpoints.

## Database note

While building this, the local Postgres schema was found to have drifted from any
tracked migration (a stale `is_email_verified` column and one leftover test row from
earlier manual testing). With explicit confirmation, the `accounts` app's tables were
reset and migrations re-applied cleanly.

## Testing

`apps/accounts/tests.py` — 13 tests covering the full request → approve → OTP login →
set-password → login flow, the deny path, resend-otp (recovery, cooldown, silent no-op),
forgot-password (full reset, mismatched confirmation, cooldown, silent no-op), and the
admin's "no add permission" restriction.

```bash
python manage.py test apps.accounts.tests
```

Tests use `CELERY_TASK_ALWAYS_EAGER=True` and the `locmem` email backend so the full
flow runs synchronously and in-memory without a live broker or real SMTP.
