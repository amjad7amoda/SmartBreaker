"""Execute persisted real-site KBS intents through Backend V1 services."""

import logging

from django.db import transaction

from apps.breakers import services as breaker_services

from .models import BreakerAction, KBSSettings


logger = logging.getLogger(__name__)


def _finish(
    action_id,
    status,
    *,
    resulting_state=None,
    failure_reason='',
):
    with transaction.atomic():
        action = BreakerAction.objects.select_for_update().get(pk=action_id)
        action.status = status
        action.resulting_state = resulting_state
        action.failure_reason = str(failure_reason or '')[:500]
        action.save()
        return action.status


def _claim_real_action(action_id):
    with transaction.atomic():
        action = (
            BreakerAction.objects.select_for_update()
            .select_related('decision__organization')
            .filter(pk=action_id)
            .first()
        )
        if action is None or action.status != 'pending':
            return None
        data_source = KBSSettings.objects.filter(
            organization_id=action.decision.organization_id,
        ).values_list('data_source', flat=True).first()
        if data_source != 'real':
            return None
        action.status = 'scheduled'
        action.save()
        return action.id


def execute_action(action_id):
    """Claim and execute one action, returning its durable status."""
    if _claim_real_action(action_id) is None:
        return (
            BreakerAction.objects.filter(pk=action_id)
            .values_list('status', flat=True)
            .first()
            or 'missing'
        )

    action = (
        BreakerAction.objects.select_related(
            'breaker', 'decision__organization',
        ).get(pk=action_id)
    )
    if action.breaker is None:
        return _finish(
            action_id,
            'failed',
            failure_reason='The breaker no longer exists.',
        )

    target_state = action.action == 'on'
    try:
        if action.countdown_s:
            scheduled_seconds = min(
                action.countdown_s,
                breaker_services.MAX_COUNTDOWN_SECONDS,
            )
            result = breaker_services.set_countdown_seconds(
                action.breaker,
                scheduled_seconds,
                source='kbs',
                reason=action.reason,
                desired_state=target_state,
            )
            if result.get('blocked'):
                return _finish(
                    action_id,
                    'blocked',
                    resulting_state=(result.get('status') or {}).get('is_on'),
                    failure_reason=result.get('reason'),
                )
            if result.get('changed') is False:
                return _finish(
                    action_id,
                    'noop',
                    resulting_state=target_state,
                )
            if result.get('confirmed'):
                return _finish(action_id, 'scheduled')
            return _finish(
                action_id,
                'failed',
                failure_reason='Tuya did not confirm the countdown.',
            )

        result = breaker_services.set_switch(
            action.breaker,
            target_state,
            source='kbs',
            reason=action.reason,
        )
        resulting_state = (result.get('status') or {}).get('is_on')
        if result.get('blocked'):
            return _finish(
                action_id,
                'blocked',
                resulting_state=resulting_state,
                failure_reason=result.get('reason'),
            )
        if result.get('confirmed'):
            return _finish(
                action_id,
                'applied',
                resulting_state=target_state,
            )
        return _finish(
            action_id,
            'failed',
            resulting_state=resulting_state,
            failure_reason=result.get('reason') or 'Tuya did not confirm the switch.',
        )
    except Exception as exc:
        logger.exception('KBS action %s failed during backend execution', action_id)
        return _finish(action_id, 'failed', failure_reason=str(exc))


def confirm_action(action_id):
    """Confirm a previously scheduled countdown against live device state."""
    action = (
        BreakerAction.objects.select_related('breaker')
        .filter(pk=action_id, status='scheduled', countdown_s__gt=0)
        .first()
    )
    if action is None:
        return (
            BreakerAction.objects.filter(pk=action_id)
            .values_list('status', flat=True)
            .first()
            or 'missing'
        )
    if action.breaker is None:
        return _finish(
            action_id,
            'failed',
            failure_reason='The breaker no longer exists.',
        )

    try:
        device_status = breaker_services.read_status(action.breaker)
    except Exception as exc:
        logger.exception('Unable to confirm KBS action %s', action_id)
        return _finish(action_id, 'failed', failure_reason=str(exc))

    target_state = action.action == 'on'
    resulting_state = device_status.get('is_on')
    if resulting_state is target_state:
        return _finish(
            action_id,
            'applied',
            resulting_state=resulting_state,
        )
    return _finish(
        action_id,
        'failed',
        resulting_state=resulting_state,
        failure_reason='Countdown elapsed but the device did not reach the requested state.',
    )
