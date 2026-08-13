"""Fan-out from telemetry ingestion to the Tier-2 KBS."""

import logging

from django.db import transaction


logger = logging.getLogger(__name__)


def _queue_cycles(organization_ids):
    from apps.kbs.tasks import run_kbs_cycle_for_org

    for organization_id in organization_ids:
        try:
            run_kbs_cycle_for_org.delay(organization_id)
        except Exception:
            logger.exception(
                'Unable to queue KBS cycle for org %s', organization_id,
            )


def dispatch_kbs_cycles(organization_ids):
    ids = sorted(set(organization_ids))  # one cycle per site, stable order
    if not ids:
        return
    transaction.on_commit(lambda: _queue_cycles(ids))
