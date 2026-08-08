# Smart Breaker

Backend and knowledge-based control service for the Smart Breaker platform:
Django + Django REST Framework, JWT auth, Celery/Redis for async work, Postgres
for storage, and coordinated Tier-1/Tier-2 breaker control.

## Tech stack

- **Django 6** / **Django REST Framework** — API layer
- **PostgreSQL** — primary database
- **djangorestframework-simplejwt** — JWT authentication
- **Celery + Redis** — email, polling, KBS cycles and device-action execution
- **Channels / Daphne** — reserved for realtime features (installed, not yet wired up)
- **django-cors-headers** — CORS
- **Tier-2 KBS + Tier-1 interlock** — auditable decisions with real-site Tuya execution

## Project layout

```
config/                Django project (settings, root urls, celery app, wsgi/asgi)
apps/accounts/          Users, registration approval, OTP login, password reset
apps/organizations/      Organizations (multi-tenancy), admin approval
apps/kbs/                Tier-2 engine, Django adapter, audit API and action executor
edge/                    Tier-1 safety engine, audit spool and backend bridge
simulator/               Browser simulator and real-world scenario data
docker-compose.yml       Local Redis and Postgres
Dockerfile               App image (web, worker, beat all run from it)
docker-compose.yml       Full local stack: postgres, redis, web, worker, beat
requirements.txt         Python dependencies
manage.py
```

Settings are split under `config/settings/`: `base.py` (shared), `development.py`,
`production.py`. `manage.py` and `celery.py` default to `config.settings.development`.

## Local setup with Docker (recommended)

Everything — Postgres, Redis, the Django dev server, the Celery worker and beat —
runs as containers. You only need Docker Desktop and a filled-in `.env`.

```bash
docker compose up --build      # -d to detach
```

That starts, in order: `db` and `redis` (waited on via healthchecks), a one-shot
`migrate` service, then `web`, `worker` and `beat`. The API is on
http://localhost:8000.

The `DB_HOST`, `DB_PORT` and `CELERY_*` values in `.env` are overridden in
`docker-compose.yml` so the containers reach `db` and `redis` instead of
`localhost` — everything else (`SECRET_KEY`, `EMAIL_*`, `TUYA_FERNET_KEY`) is read
from `.env` as usual. `DB_NAME`/`DB_USER`/`DB_PASSWORD` also seed the Postgres
container, so they must be set before the first `up`.

Postgres is published on host port **5433**, not 5432, to avoid clashing with a
natively installed Postgres. Inside the compose network it's still `db:5432`.

Common commands:

```bash
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py test apps.accounts.tests apps.organizations.tests
docker compose logs -f worker
docker compose down            # add -v to also wipe the database volume
```

The project directory is bind-mounted into the containers, so edits reload the dev
server without a rebuild. Rebuild only when `requirements.txt` changes:
`docker compose up -d --build`.

## Local setup without Docker

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
2. **Configure environment** — copy `.env` and fill in real values:
   ```
   SECRET_KEY=
   DEBUG=True
   DB_NAME=, DB_USER=, DB_PASSWORD=, DB_HOST=, DB_PORT=

   DEFAULT_FROM_EMAIL=
   EMAIL_HOST=smtp.gmail.com
   EMAIL_PORT=587
   EMAIL_USE_TLS=True
   EMAIL_HOST_USER=
   EMAIL_HOST_PASSWORD=        # Gmail App Password, not your account password

   CELERY_BROKER_URL=redis://localhost:6379/0
   CELERY_RESULT_BACKEND=redis://localhost:6379/0
   CELERY_TASK_ALWAYS_EAGER=False
   ```
   Both dev and prod send real email via SMTP (no console backend) — without valid
   `EMAIL_HOST_*` credentials, outgoing mail (OTPs, approval/denial notices, password
   resets) will fail to send.
3. **Start Postgres and Redis** (database, cache, Celery broker):
   ```bash
   docker compose up -d postgres redis
   ```
4. **Run migrations**
   ```bash
   python manage.py migrate
   ```
5. **Create the first admin account** — admins can *only* be created this way, never
   through the API or Django admin "Add" button:
   ```bash
   python manage.py createsuperuser
   ```
6. **Run the server**
   ```bash
   python manage.py runserver
   ```
7. **Run the Celery worker** (needed for email, polling, KBS cycles and real-site
   breaker actions):
   ```bash
   celery -A config worker -l info
   ```
8. **Run Celery beat** (dispatches per-site polling and Tier-2 cycles):
   ```bash
   celery -A config beat -l info
   ```

For a real site, configure its Tuya credentials, set `KBSSettings.data_source`
to `real`, and set its mode to `active`. A committed KBS intent is then queued
through `apps.kbs.executor`, which uses the Backend V1 Tuya services and records
both the KBS outcome and the device-action audit. Simulator intents remain local
and can only be acknowledged through the simulator API.

## Running tests

```bash
DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test
python3 -m unittest edge.test_tier1_kbs edge.test_simulator_bridge edge.test_audit
```

Tests use `CELERY_TASK_ALWAYS_EAGER=True` and Django's `locmem` email backend, so the
full request → email → confirmation flows run synchronously and in-memory, without a
live broker or real SMTP.

## Apps

### `apps.accounts` — authentication & user lifecycle

New users don't self-register with a password. Instead:

1. **Request an account** — submit `email`, `phone`, `role` (`home_user` or
   `technician`). No password collected yet.
2. **Admin review** — approve or deny, from the Django admin panel or the REST API.
   Admin accounts can only be created via `createsuperuser`.
3. **Approval** — creates the `User` (inactive, no usable password), generates an OTP,
   emails it.
4. **OTP login** — logging in with the OTP activates the account and issues JWTs, but
   flags `must_set_password: true`.
5. **Set password** — required before anything else works. After this,
   `must_set_password` clears and normal email+password login works.

Recovery paths: **resend OTP** (for a lost session / expired code before finishing
setup) and **forgot password** (emailed reset code, for already-activated accounts).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/accounts/requests/` | Public | Submit a registration request |
| GET | `/api/accounts/requests/` | Admin | List requests, optional `?status=` filter |
| GET | `/api/accounts/requests/<id>/` | Admin | Retrieve a single request |
| POST | `/api/accounts/requests/<id>/approve/` | Admin | Approve a request |
| POST | `/api/accounts/requests/<id>/deny/` | Admin | Deny a request |
| POST | `/api/accounts/otp-login/` | Public | Log in with OTP, activates account |
| POST | `/api/accounts/resend-otp/` | Public | Re-issue OTP for a stuck account |
| POST | `/api/accounts/set-password/` | Authenticated | Set first real password |
| POST | `/api/accounts/forgot-password/` | Public | Request a password reset code |
| POST | `/api/accounts/reset-password/` | Public | Confirm code + set new password |
| POST | `/api/accounts/login/` | Public | Normal email + password login |
| POST | `/api/accounts/login/refresh/` | Public | Refresh JWT access token |

`forgot-password`, `resend-otp`, and request-creation all return generic, identical
responses regardless of whether the target email exists or is eligible, to avoid
leaking account existence.

### `apps.organizations` — multi-tenancy

A fully-set-up user (finished OTP login + set their password) can request an
organization by submitting `name`, `phone`, `latitude`, `longitude`. A user can own
multiple organizations. New organizations start `pending`; an admin approves (→
`active`, owner emailed) or denies (→ the organization is deleted, owner emailed).

Owners can update or delete their own organizations; admins can delete (but not update)
any organization, in addition to the approve/deny review actions.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/organizations/` | Authenticated + password set | Request a new organization (`pending`) |
| GET | `/api/organizations/` | Authenticated | Admin: all orgs, `?status=`/`?owner=` filters. Others: only their own |
| GET | `/api/organizations/<id>/` | Owner or admin | Retrieve a single organization |
| PATCH/PUT | `/api/organizations/<id>/` | Owner only | Update name/phone/location |
| DELETE | `/api/organizations/<id>/` | Owner or admin | Delete the organization |
| POST | `/api/organizations/<id>/approve/` | Admin | Approve → active, email sent |
| POST | `/api/organizations/<id>/deny/` | Admin | Deny → deleted, email sent |

## Asynchronous email (Celery)

Email is dispatched via Celery tasks in each app's `tasks.py` (`apps/accounts/tasks.py`,
`apps/organizations/tasks.py`), backed by Redis. Service-layer functions
(`apps/accounts/services.py`, `apps/organizations/services.py`) wrap their DB writes in
`@transaction.atomic` and dispatch the corresponding email task via
`transaction.on_commit(...)`, so an email is only ever queued after the database change
it describes has actually committed.

Celery settings (`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`,
`CELERY_TASK_ALWAYS_EAGER`) live in `config/settings/base.py` and are read from `.env`.

## Admin panel conventions

- `User` admin has no "Add" permission — accounts only come from the request-approval
  flow or `createsuperuser`.
- `RegistrationRequest` and `Organization` admins both expose `Approve selected` /
  `Deny selected` bulk actions that call the same service functions used by the REST
  endpoints, so behavior (email, atomicity) is identical either way.
