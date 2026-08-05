import json

from django.core.cache import cache
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.organizations.models import Organization

TASK_NAME = 'apps.breakers.tasks.refresh_organization_breakers'

POLL_INTERVAL_SECONDS = 30
STATUS_TTL = POLL_INTERVAL_SECONDS * 3
IDLE_TIMEOUT = 5 * 60
SCHEDULE_RECHECK = 60
USER_ORGS_TTL = 5 * 60


def status_key(device_id):
    return f'tuya:status:{device_id}'


def activity_key(organization_id):
    return f'org:active:{organization_id}'


def schedule_name(organization_id):
    return f'poll-breakers-org-{organization_id}'


def cache_status(device_id, status):
    cache.set(status_key(device_id), status, STATUS_TTL)


def cached_status(device_id):
    return cache.get(status_key(device_id))

def organization_is_active(organization_id):
    return cache.get(activity_key(organization_id)) is not None


def ensure_schedule(organization_id):
    interval, _ = IntervalSchedule.objects.get_or_create(
        every=POLL_INTERVAL_SECONDS, period=IntervalSchedule.SECONDS
    )
    PeriodicTask.objects.update_or_create(
        name=schedule_name(organization_id),
        defaults={
            'task': TASK_NAME,
            'interval': interval,
            'args': json.dumps([organization_id]),
            'enabled': True,
        },
    )


def disable_schedule(organization_id):
    PeriodicTask.objects.filter(name=schedule_name(organization_id)).update(enabled=False)


def touch_organization(organization_id):
    """Mark the organization as in use, creating or re-enabling its poller if needed."""
    cache.set(activity_key(organization_id), True, IDLE_TIMEOUT)

    recheck = f'org:scheduled:{organization_id}'
    if cache.get(recheck):
        return
    ensure_schedule(organization_id)
    cache.set(recheck, True, SCHEDULE_RECHECK)


def organization_ids_for(user):
    """Only organizations the user owns. Staff can read every breaker, but an admin
    signing in is not a reason to start polling every organization in the system."""
    key = f'user:orgs:{user.pk}'
    ids = cache.get(key)
    if ids is None:
        ids = list(Organization.objects.filter(owner=user).values_list('id', flat=True))
        cache.set(key, ids, USER_ORGS_TTL)
    return ids
