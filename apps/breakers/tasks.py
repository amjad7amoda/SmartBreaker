import logging

from celery import shared_task

from . import scheduling
from .models import Breaker
from .services import read_status
from .tuya import TuyaError

logger = logging.getLogger(__name__)


@shared_task
def refresh_organization_breakers(organization_id):

    if not scheduling.organization_is_active(organization_id):
        scheduling.disable_schedule(organization_id)
        return {'organization': organization_id, 'stopped': 'idle'}

    refreshed = failed = 0
    breakers = Breaker.objects.filter(
        organization_id=organization_id
    ).select_related('organization')

    for breaker in breakers:
        try:
            # read_status caches on the way out, so there is nothing to store here.
            read_status(breaker)
        except (TuyaError, LookupError) as exc:
            failed += 1
            logger.warning('Poll failed for %s: %s', breaker.device_id, exc)
        else:
            refreshed += 1

    return {'organization': organization_id, 'refreshed': refreshed, 'failed': failed}
