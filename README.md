# Smart Breaker

Backend and knowledge-based control service for the Smart Breaker platform:
Django + Django REST Framework, JWT auth, Celery/Redis for async work, Postgres
for storage, and coordinated Tier-1/Tier-2 breaker control.

A site is one organization: a solar installation with an inverter, a battery and a
set of smart breakers. An edge device (Raspberry Pi) pushes telemetry and runs the
Tier-1 safety engine locally; this backend stores the history, runs the Tier-2
supervisory engine, drives real breakers through Tuya, and keeps an auditable
record of every decision either tier made.

## Tech stack

- **Django 6** / **Django REST Framework** — API layer
- **PostgreSQL** — primary database (`DISTINCT ON` is used, so it is not optional)
- **djangorestframework-simplejwt** — JWT authentication for humans; edge devices
  use a separate `Authorization: Device <id>.<secret>` scheme
- **Celery + Redis** — email, polling, KBS cycles and device-action execution
- **Tuya Cloud** — real breaker control, credentials encrypted at rest with Fernet
- **Channels / Daphne** — reserved for realtime features (installed, not yet wired up)
- **django-cors-headers** — CORS
- **Tier-2 KBS + Tier-1 interlock** — auditable decisions with real-site Tuya execution

## Project layout

```
config/                  Django project (settings, root urls, celery app, wsgi/asgi)
apps/accounts/           Users, registration approval, OTP login, password reset
apps/organizations/      Organizations (multi-tenancy), admin approval
apps/breakers/           Breaker registry, Tuya credentials/client, polling, command audit
apps/telemetry/          Site-level readings ingested from the edge agent
apps/notifications/      Per-user in-app notifications
apps/kbs/                Tier-2 engine, Django adapter, audit API and action executor
apps/query_params.py     Shared `?since=`/`?until=` time-window filtering
edge/                    Tier-1 safety engine, audit spool and backend bridge
simulator/               Browser simulator and real-world scenario data
Dockerfile               App image (web, worker, beat all run from it)
docker-compose.yml       Full local stack: db, redis, web, worker, beat
requirements.txt         Python dependencies
manage.py
```

Settings are split under `config/settings/`: `base.py` (shared), `development.py`
and `test.py`. Everything — the containers included — runs `development.py`; the
database lives only in the `db` container, so there is no local Postgres to keep
in sync.

## Local setup with Docker (recommended)

Everything — Postgres, Redis, the Django dev server, the Celery worker and beat —
runs as containers. You only need Docker Desktop and a filled-in `.env`.

```bash
docker compose up --build      # -d to detach
```

That starts `db` and `redis` first (`web`, `worker` and `beat` all wait on their
healthchecks). There is no separate migrate service — `web` runs
`manage.py migrate --noinput` itself before starting the dev server. The API is on
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

   TUYA_FERNET_KEY=           # Fernet key encrypting stored Tuya secrets
   ALLOWED_HOSTS=             # comma-separated; defaults to * while DEBUG
   CACHE_URL=                 # or REDIS_URL; falls back to locmem if unset
   BREAKER_POLL_SECONDS=30            # how often beat polls Tuya
   BREAKER_READING_RETENTION_MINUTES=60
   BREAKER_PURGE_SECONDS=300          # how often old readings are purged
   ```
   Both dev and prod send real email via SMTP (no console backend) — without valid
   `EMAIL_HOST_*` credentials, outgoing mail (OTPs, approval/denial notices, password
   resets) will fail to send.

   `TUYA_FERNET_KEY` must be set before any Tuya credential is stored; rotating it
   makes existing encrypted secrets unreadable.
3. **Start Postgres and Redis** (database, cache, Celery broker) — the compose
   services are named `db` and `redis`:
   ```bash
   docker compose up -d db redis
   ```
   Remember that `db` is published on host port **5433**, so `DB_PORT=5433` here.
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
8. **Run Celery beat** (dispatches breaker polling and reading retention):
   ```bash
   celery -A config beat -l info
   ```
   Beat currently schedules only `poll-breakers` and `purge-breaker-readings`
   (`CELERY_BEAT_SCHEDULE` in `config/settings/base.py`). Tier-2 cycles are *not*
   on a timer: the periodic `kbs-dispatch` entry is commented out, and a cycle is
   instead queued per site by telemetry ingestion — see
   [Decision flow](#decision-flow) below.

For a real site, configure its Tuya credentials, set `KBSSettings.data_source`
to `real`, and set its mode to `active`. A committed KBS intent is then queued
through `apps.kbs.executor`, which uses the Backend V1 Tuya services and records
both the KBS outcome and the device-action audit. Simulator intents remain local
and can only be acknowledged through the simulator API.

## Decision flow

Two tiers decide, and they do not share code. Tier-1 is the dependency-free safety
engine that runs on the edge device (`edge/tier1_kbs.py`); Tier-2 is the
supervisory engine that runs in Django (`apps/kbs/engine/`).

1. **Ingest** — the edge agent POSTs site readings to `/api/telemetry/readings/`
   (unauthenticated; the Pi has no credentials to present) and breaker snapshots
   to `/api/breakers/status/`.
2. **Dispatch** — on commit, `apps.telemetry.services.dispatch_kbs_cycles` queues
   one `run_kbs_cycle_for_org` task per organization that appeared in the batch.
   Separately, `poll-breakers` reads real Tuya devices on the beat schedule.
3. **Tier-2 decides** — `apps.kbs.services.run_cycle` builds facts
   (`engine/facts.py`, `engine/derived.py`), applies the rules (`engine/rules.py`,
   with fuzzy scoring in `engine/fuzzy.py` when `tier2_policy` selects it), and
   persists a `KBSDecision` with a step-by-step `trace` plus its `BreakerAction`
   intents.
4. **Tier-1 preempts** — the edge posts its own events to
   `/api/kbs/edge/decision-events/`. A Tier-1 safety episode marks any pending
   Tier-2 action `superseded`: safety always outranks supervision.
5. **Execute** — for a `real` site, `apps.kbs.executor` drives the intent through
   the Tuya service and records the outcome. For a `simulator` site the intent
   stays local and is acknowledged via `/api/kbs/sim/ack/`.

Every decision, on either tier, is queryable afterwards through
`/api/kbs/decision-logs/`.

## Running tests

```bash
DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test
python -m unittest edge.test_tier1_kbs edge.test_simulator_bridge edge.test_audit
```

The Django suite is 240 tests; the `edge` suite is 26 and has no Django dependency,
which is why it runs under plain `unittest`.

Tests use `CELERY_TASK_ALWAYS_EAGER=True` and Django's `locmem` email backend, so the
full request → email → confirmation flows run synchronously and in-memory, without a
live broker or real SMTP.

**Known failure:** `config/settings/test.py` runs on in-memory SQLite, but
`/api/telemetry/readings/latest/` uses PostgreSQL's `DISTINCT ON`. So
`apps.telemetry.tests.ReadingReadTests.test_latest_returns_one_row_per_site` and
`test_latest_can_be_narrowed_to_one_site` error out with
`NotSupportedError: DISTINCT ON fields is not supported by this database backend`.
Everything else passes. Point the test settings at Postgres to run those two.

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

### `apps.breakers` — devices, Tuya and command audit

A `Breaker` belongs to an organization and is keyed by its Tuya `device_id`.
Scoping is by role: technicians and admins see every breaker, a `home_user` sees
only breakers in organizations they own. Writes (create/update/delete, Tuya
credentials) are technician-or-admin; control actions and reads are open to any
authenticated user in scope.

Two different ways to read state, and the difference matters:

- `/<device_id>/status/` calls Tuya on **every** request — never stale, but it
  costs a round trip and counts against the Tuya rate limit.
- `/statuses/` and `/statuses/<device_id>/` serve the last row the poller or the
  edge agent stored. Safe to poll from a dashboard. A breaker that has never
  reported has no row and is simply absent from the list.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET/POST | `/api/breakers/` | Read: any; create: technician/admin | List (`?organization=`) or register a breaker |
| GET/PATCH | `/api/breakers/<device_id>/` | Read: any; update: technician/admin | Retrieve or update a breaker |
| DELETE | `/api/breakers/<device_id>/delete/` | Technician/admin | Delete a breaker |
| GET | `/api/breakers/<device_id>/status/` | Authenticated | Live status, read straight from Tuya (`?raw=1` for the raw payload) |
| GET | `/api/breakers/statuses/` | Authenticated | Last stored snapshot of every breaker in scope (`?device_id=`, `?organization=`, `?is_on=`, `?online=`) |
| GET | `/api/breakers/statuses/<device_id>/` | Authenticated | Last stored snapshot of one breaker |
| POST | `/api/breakers/status/` | Public | Edge/simulator snapshot ingest (batch) |
| POST | `/api/breakers/<device_id>/switch/` | Authenticated | Turn the relay on/off |
| POST | `/api/breakers/<device_id>/child-lock/` | Authenticated | Engage the device lockout (also opens the relay) |
| POST | `/api/breakers/<device_id>/countdown/` | Authenticated | Flip the relay after a delay, in minutes |
| GET | `/api/breakers/readings/` | Authenticated | Sample history, paged, newest first (`?device_id=`, `?organization=`, `?since=`, `?until=`) |
| GET | `/api/breakers/<device_id>/readings/` | Authenticated | Same, scoped to one device — unknown device is a 404, not an empty page |
| GET | `/api/breakers/actions/` | Authenticated | Device-command audit (`?device_id=`, `?organization=`, `?action=`, `?source=`) |
| GET | `/api/breakers/actions/<id>/` | Authenticated | One audited command |
| GET/POST | `/api/breakers/tuya-credentials/` | Technician/admin | List or add per-organization Tuya credentials |
| GET/PATCH/DELETE | `/api/breakers/tuya-credentials/<id>/` | Technician/admin | Manage one credential |

Tuya secrets are encrypted at rest with `TUYA_FERNET_KEY` (`apps/breakers/crypto.py`).
Management commands `tuya_check`, `tuya_set_secret` and `tuya_verify_sign` help
diagnose credential and request-signing problems.

Readings are deliberately short-lived: `purge-breaker-readings` deletes anything
older than `BREAKER_READING_RETENTION_MINUTES` (default 60).

### `apps.telemetry` — site-level readings

One `Reading` is a whole-site sample (PV, inverter, battery, grid), as opposed to
`apps.breakers`' per-device samples. `POST /readings/` is deliberately
unauthenticated — the Pi has no credentials to present — while reading back
requires a login and is scoped to the caller's organizations. Every accepted batch
queues a Tier-2 cycle for each organization it touched.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/telemetry/readings/` | Public | Ingest one reading or a batch |
| GET | `/api/telemetry/readings/` | Authenticated | Paged history (`?organization=`, `?since=`, `?until=`) |
| GET | `/api/telemetry/readings/latest/` | Authenticated | Newest reading per site — one row per organization |

### `apps.notifications` — in-app notifications

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/notifications/` | Authenticated | Own notifications, newest first |
| POST | `/api/notifications/<id>/mark-read/` | Authenticated | Mark one as read |

### `apps.kbs` — Tier-2 engine, edge bridge and audit

Three separate audiences, three separate auth schemes:

**Edge devices** authenticate with `Authorization: Device <device-id>.<secret>`
(`apps/kbs/authentication.py`), not JWT. Provision one with
`python manage.py provision_edge_device`. Event and action ingest are idempotent
by `event_id` / `action_id`: a replay is reported as `duplicate`, and a replay
whose immutable fields disagree with the stored record is `rejected` rather than
silently overwriting the audit.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/kbs/edge/tier1-config/` | Edge device | Fetch the site's authoritative Tier-1 thresholds |
| POST | `/api/kbs/edge/decision-events/` | Edge device | Push Tier-1 decision/clear/error events (≤100 per call) |
| POST | `/api/kbs/edge/action-results/` | Edge device | Report execution results for issued actions (≤200 per call) |

**Operators** read the audit with a normal JWT. Admins and technicians see every
organization; an owner sees only their own.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/kbs/decision-logs/` | Authenticated | Paged decision log (`?tier=`, `?event_type=`, `?branch=`, `?organization=`, `?after=`, `?before=`, `?has_actions=`) |
| GET | `/api/kbs/decision-logs/<event_id>/` | Authenticated | One decision with full facts, trace, actions and alerts |

**The simulator** endpoints are unauthenticated and are for local development
only. The mutating ones (`reset`, `breaker-override`) additionally refuse any
organization whose `data_source` is not `simulator`, so they cannot touch a real
site.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/kbs/sim/state/` | Public | Settings, latest telemetry, breakers, Tier-1 hold, pending actions, alerts |
| POST | `/api/kbs/sim/run-cycle/` | Public | Run one Tier-2 cycle now and return its branch, trace and actions |
| POST | `/api/kbs/sim/ack/` | Public | Acknowledge simulator actions as applied/failed |
| POST | `/api/kbs/sim/reset/` | Public, simulator sites only | Clear run history, keep configuration (`confirm: true` required) |
| POST | `/api/kbs/sim/breaker-override/` | Public, simulator sites only | Force a breaker on/off and record the reading |
| GET | `/api/kbs/sim/climate/` | Public | Validated source climatology (`?city=`, `?month=`) |
| PATCH | `/api/kbs/settings/` | Public | Update a site's `KBSSettings` (mode, policy, cycle time, battery limits, …) |

`GET /sim/climate/` returns **503** rather than substituting invented values when
the source CSV is missing or fails validation.

## Simulator

`simulator/` is a standalone browser page (HTML/CSS/JS, no build step) that models
a whole solar site — PV, inverter, battery and breakers — against the *real*
Python engines: Tier-1 over a loopback bridge, Tier-2 over the `/api/kbs/sim/`
endpoints. The browser applies what the engines return; it never decides which
rule fired.

```bash
python manage.py seed_simulator
python -m http.server 8791 --directory simulator
```

Then open <http://127.0.0.1:8791>. Serving the folder is preferred over opening
`index.html` from `file://`, because the Tier-1 bridge CORS policy trusts that
local origin.

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
