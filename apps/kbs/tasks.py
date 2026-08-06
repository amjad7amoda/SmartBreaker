from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.organizations.models import Organization

from .models import KBSDecision, KBSSettings
from .executor import confirm_action, execute_action
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


@shared_task
def confirm_kbs_action(action_id):
    """Confirm that a device reached the state requested by a KBS countdown."""
    return confirm_action(action_id)


@shared_task
def execute_kbs_action(action_id):
    """Execute one real-site KBS intent through Backend V1's Tuya service."""
    outcome = execute_action(action_id)
    if outcome == 'scheduled' and not settings.CELERY_TASK_ALWAYS_EAGER:
        from .models import BreakerAction

        action = BreakerAction.objects.filter(pk=action_id).only(
            'countdown_s',
        ).first()
        if action is not None:
            confirm_kbs_action.apply_async(
                args=[action_id],
                countdown=max(action.countdown_s, 1) + 5,
            )
    return outcome
