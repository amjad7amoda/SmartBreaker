from celery import shared_task
from django.utils import timezone

from apps.organizations.models import Organization

from .models import KBSDecision, KBSSettings
from .services import run_cycle


@shared_task
def run_kbs_cycles():
    now = timezone.now()  # dispatch wall-clock time (UTC timestamp)
    due = 0               # number of sites queued this dispatch (count)
    for kbs in KBSSettings.objects.filter(mode='active').select_related('organization'):
        last = (
            KBSDecision.objects
            .filter(organization=kbs.organization, tier='tier2')
            .order_by('-received_at')
            .first()
        )  # most recent cycle of this site, if any
        elapsed_s = (now - last.received_at).total_seconds() if last else None  # seconds since the last cycle (s)
        if last is None or elapsed_s >= kbs.cycle_seconds:
            run_kbs_cycle_for_org.delay(kbs.organization_id)
            due += 1
    return due


@shared_task
def run_kbs_cycle_for_org(organization_id):
    """Run one KBS cycle for one site.

    organization_id: Organization primary key (unitless)
    """
    organization = Organization.objects.get(id=organization_id)
    decision = run_cycle(organization)
    return decision.branch if decision else None
