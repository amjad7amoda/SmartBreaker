"""Cycle orchestration: gather facts, run the rules, persist the outcome.

The persisted ``BreakerAction`` rows are the engine's output — the new state
the breakers must be switched to. The edge (Raspberry Pi) picks them up,
executes the switches, and marks them ``executed``.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

ALERT_COOLDOWN_MINUTES = 5   # do not repeat an alert of the same kind within this many real minutes (min)
ACTION_DEDUPE_MINUTES = 10   # do not re-issue an identical command while one is still pending and fresher than this; older pending commands are stale (the executor never applied them) and must not block new decisions (min)

from apps.breakers.models import Breaker

from ..models import Alert, BreakerAction, KBSDecision, KBSSettings
from .facts import system_fact
from .gathering import facts_to_json, gather_facts
from .rules import decide

logger = logging.getLogger(__name__)


def run_cycle(organization, now=None):
    """Run one full KBS cycle for a site; returns the stored ``KBSDecision``
    or None when the engine did not act (observing mode / no telemetry).

    organization: the site to decide for (Organization)
    now:          cycle time override for tests; defaults to current time (UTC timestamp)
    """
    kbs, _ = KBSSettings.objects.get_or_create(organization=organization)  # site engine config; created with defaults (observing) on first contact
    if kbs.mode != 'active':
        # Observing phase: ingestion keeps collecting readings for the load /
        # night-usage learning, but the engine takes no actions yet.
        logger.info('KBS observing, no actions: org=%s', organization.id)
        return None

    if now is None and kbs.data_source == 'simulator':
        # Simulator mode: the data's own timeline is the truth — anchor the
        # cycle to the newest reading so simulated time (faster clock, other
        # time of day) drives day/night, look-back windows and events.
        latest = organization.readings.order_by('-timestamp').values_list('timestamp', flat=True).first()  # newest reading time (UTC timestamp)
        if latest is None:
            logger.warning('KBS skipped, no readings for data clock: org=%s', organization.id)
            return None
        now = latest

    facts = gather_facts(organization, kbs, now=now)
    if facts is None:
        logger.warning('KBS skipped, no inverter readings in window: org=%s', organization.id)
        return None

    result = decide(facts)
    return _persist(organization, facts, result)


@transaction.atomic
def _persist(organization, facts, result):
    """Store the decision, its switch commands, alerts, and lockouts (KBSDecision)."""
    cycle_now = system_fact(facts)['now']  # the cycle time the decision was based on (UTC timestamp)
    decision = KBSDecision.objects.create(
        organization=organization,
        branch=result.branch,
        facts=facts_to_json(facts),
    )
    for intent in result.actions:
        if BreakerAction.objects.filter(
            breaker_id=intent.breaker_id, action=intent.action, executed=False,
            created_at__gte=timezone.now() - timedelta(minutes=ACTION_DEDUPE_MINUTES),
        ).exists():
            continue  # the same command is already pending and still fresh — do not stack duplicates
        BreakerAction.objects.create(
            decision=decision,
            breaker_id=intent.breaker_id,
            action=intent.action,
            countdown_s=intent.countdown_s,
            reason=intent.reason,
        )
        if intent.lockout:
            Breaker.objects.filter(id=intent.breaker_id).update(
                locked_out=True,
                lockout_reason=intent.reason,
                locked_at=cycle_now,
            )
    for alert in result.alerts:
        if Alert.objects.filter(
            organization=organization, kind=alert.kind,
            created_at__gte=timezone.now() - timedelta(minutes=ALERT_COOLDOWN_MINUTES),
        ).exists():
            continue  # same-kind alert already raised moments ago — avoid spamming the user every cycle
        Alert.objects.create(
            organization=organization,
            kind=alert.kind,
            severity=alert.severity,
            message=alert.message,
        )
    logger.info(
        'KBS decision: org=%s branch=%s actions=%d alerts=%d',
        organization.id, result.branch, len(result.actions), len(result.alerts),
    )
    return decision
