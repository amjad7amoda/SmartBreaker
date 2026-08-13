import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import Breaker, BreakerReading, TuyaCredential
from .services import read_status
from .tuya import TuyaError

logger = logging.getLogger(__name__)


@shared_task
def poll_all_breakers():
    organization_ids = list(
        Breaker.objects
        .filter(organization_id__in=TuyaCredential.objects.values('organization_id'))
        .values_list('organization_id', flat=True)
        .distinct()
    )
    for organization_id in organization_ids:
        refresh_organization_breakers.delay(organization_id)
    return {'organizations': len(organization_ids)}


@shared_task
def refresh_organization_breakers(organization_id):
    refreshed = failed = 0
    breakers = Breaker.objects.filter(
        organization_id=organization_id
    ).select_related('organization')

    for breaker in breakers:
        try:
            read_status(breaker)
        except (TuyaError, LookupError) as exc:
            failed += 1
            logger.warning('Poll failed for %s: %s', breaker.device_id, exc)
        else:
            refreshed += 1

    return {'organization': organization_id, 'refreshed': refreshed, 'failed': failed}


@shared_task
def purge_breaker_readings():
    cutoff = timezone.now() - timedelta(
        minutes=settings.BREAKER_READING_RETENTION_MINUTES,
    )
    deleted, _ = BreakerReading.objects.filter(timestamp__lt=cutoff).delete()
    return {'deleted': deleted}
