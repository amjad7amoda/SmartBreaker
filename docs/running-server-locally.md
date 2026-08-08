# Running the SmartBreaker Server Locally

This guide starts the Django API on a local Linux machine. It also shows how to
add Redis, Celery workers, and Celery Beat when testing polling or the KBS
engine.

For the complete Raspberry Pi → KBS → breaker flow, see
`docs/running-kbs-engine.md`.

## Choose a local mode

| Mode | Processes | Use it for |
|---|---|---|
| API only | PostgreSQL + Django | Models, migrations, admin, and basic REST APIs |
| Full backend | PostgreSQL + Redis + Django + worker + Beat | Email tasks, Tuya polling, KBS cycles, and KBS action execution |

The current merged `docker-compose.yml` has an indentation error and does not
pass `docker compose config`. The commands below therefore run Django directly
and start PostgreSQL/Redis independently.

## 1. Prerequisites

Install:

- Python 3.12 or newer;
- Python's `venv` module;
- PostgreSQL 16 or 17;
- Redis 7 or 8 when running Celery;
- Git and curl.

Check the main tools:

```bash
python3 --version
psql --version
redis-cli --version
```

## 2. Enter the project

```bash
cd /home/alayham/Documents/smart/SmartBreaker
```

All remaining commands assume this is the current directory.

## 3. Create the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Activate the environment again in every new terminal:

```bash
cd /home/alayham/Documents/smart/SmartBreaker
source .venv/bin/activate
```

## 4. Start PostgreSQL and Redis

If PostgreSQL and Redis are already installed as operating-system services,
start them with the service manager for your system and continue to the next
section.

Alternatively, start only these dependencies with Docker. The first run is:

```bash
docker volume create smartbreaker-local-pgdata

docker run -d \
  --name smartbreaker-local-db \
  -e POSTGRES_DB=smartbreaker \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=smartbreaker \
  -p 5432:5432 \
  -v smartbreaker-local-pgdata:/var/lib/postgresql/data \
  postgres:17-alpine

docker run -d \
  --name smartbreaker-local-redis \
  -p 6379:6379 \
  redis:8-alpine
```

On later runs, reuse the same containers:

```bash
docker start smartbreaker-local-db smartbreaker-local-redis
```

If ports 5432 or 6379 are already occupied, use the existing services or map
different host ports and update `.env` accordingly.

Verify both dependencies:

```bash
pg_isready -h 127.0.0.1 -p 5432 -U postgres
redis-cli -h 127.0.0.1 -p 6379 ping
```

Redis should reply with `PONG`.

## 5. Create `.env`

Create or update `.env` in the repository root:

```dotenv
SECRET_KEY=local-development-only
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

DB_NAME=smartbreaker
DB_USER=postgres
DB_PASSWORD=smartbreaker
DB_HOST=127.0.0.1
DB_PORT=5432

CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/0
CELERY_TASK_ALWAYS_EAGER=False

# Shared Tuya/poller cache. Use a Redis database separate from the broker.
REDIS_URL=redis://127.0.0.1:6379/1

# Required before saving Tuya credentials.
TUYA_FERNET_KEY=replace-with-a-fernet-key

# Required when testing OTP, approval, denial, or password-reset email tasks.
DEFAULT_FROM_EMAIL=
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
```

Generate a local Fernet key once:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep the same key after storing Tuya credentials. Changing it makes existing
encrypted Tuya secrets unreadable.

The current `config/settings/base.py` contains a hard-coded development
`SECRET_KEY` and `DEBUG=True` after reading `.env`. This does not prevent local
startup, but it must be corrected before production deployment.

## 6. Prepare the database

Run Django checks and migrations:

```bash
python manage.py check
python manage.py migrate
```

Create the initial administrator when needed:

```bash
python manage.py createsuperuser
```

Admin accounts cannot be created through the registration API.

## 7. Start the API server

For access only from this computer:

```bash
python manage.py runserver 127.0.0.1:8000
```

The API is now available at:

```text
http://127.0.0.1:8000/api/
```

The Django admin is available at:

```text
http://127.0.0.1:8000/admin/
```

To let a Raspberry Pi or another device on the LAN reach Django, bind to every
interface:

```bash
python manage.py runserver 0.0.0.0:8000
```

Then use the computer's LAN address from the Pi, for example
`http://192.168.1.20:8000`. Permit port 8000 through the local firewall only on
the trusted network.

## 8. Verify the API

In another terminal:

```bash
curl -i http://127.0.0.1:8000/api/accounts/requests/
```

A `401 Unauthorized` response is expected without a JWT and proves that Django
is reachable and authentication is active.

You can also open the admin login page:

```bash
curl -I http://127.0.0.1:8000/admin/login/
```

## 9. Start the full Celery/KBS backend

The Django server alone does not run periodic KBS cycles or execute queued KBS
actions. Keep Django running and open two more terminals.

Terminal 2—Celery worker:

```bash
cd /home/alayham/Documents/smart/SmartBreaker
source .venv/bin/activate
celery -A config worker -l info
```

Terminal 3—Celery Beat:

```bash
cd /home/alayham/Documents/smart/SmartBreaker
source .venv/bin/activate
celery -A config beat -l info
```

The worker handles email, breaker polling, KBS cycles, and real KBS actions.
Beat dispatches the KBS scan every minute and reads dynamic breaker polling
schedules from `django-celery-beat`.

Verify that the worker responds:

```bash
celery -A config inspect ping
```

At least one worker should return `pong`.

## 10. Run automated tests

The project includes self-contained test settings using an in-memory SQLite
database, eager Celery tasks, and in-memory email:

```bash
DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test
```

Run the standalone edge tests separately:

```bash
python3 -m unittest \
  edge.test_tier1_kbs \
  edge.test_simulator_bridge \
  edge.test_audit
```

## 11. Stop the local environment

Stop Django, the Celery worker, and Beat with `Ctrl+C` in their terminals.

If Docker started the dependencies, stop them without deleting their data:

```bash
docker stop smartbreaker-local-db smartbreaker-local-redis
```

To remove the containers later while preserving the PostgreSQL volume:

```bash
docker rm smartbreaker-local-db smartbreaker-local-redis
```

Do not remove `smartbreaker-local-pgdata` unless the local database should be
permanently deleted.

## Troubleshooting

### `docker compose` reports a YAML parsing error

Use the manual instructions in this guide until `docker-compose.yml` is fixed.
Validate the file after editing it with:

```bash
docker compose config --quiet
```

### `connection refused` on port 5432

PostgreSQL is not running, the host/port in `.env` is wrong, or another service
owns the port. Check:

```bash
pg_isready -h 127.0.0.1 -p 5432 -U postgres
```

### `Error 111 connecting to Redis`

Start Redis and verify `redis-cli ping`. Django can serve basic requests without
Redis, but Celery, shared polling cache, and the full KBS loop require it.

### `No module named django` or `No module named django_celery_beat`

Activate `.venv` and reinstall dependencies:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Port 8000 is already in use

Start Django on another port:

```bash
python manage.py runserver 127.0.0.1:8001
```

Update Postman or frontend base URLs to use the same port.

### Email tasks fail locally

Provide valid SMTP values in `.env`, or avoid running email-producing flows.
The current shared settings use the SMTP backend rather than a development
console backend.

### Tuya credentials cannot be saved

Check `TUYA_FERNET_KEY`, internet access, Tuya region, client ID, and client
secret. Credential creation performs a live Tuya verification.
