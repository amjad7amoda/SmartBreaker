import json
import time
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.telemetry.models import Reading
from apps.telemetry.serializers import ReadingSerializer
from apps.kbs.models import Tier1SafetyState
from apps.notifications.services import notify

from .models import (
    BreakerAction,
    BreakerReading,
    BreakerStatus,
    TuyaCredential,
)
from .tuya import TuyaClient, TuyaError

SPEC_CACHE_TTL = 60 * 60 * 24

SWITCH_READ_CODE = 'switch_1'
SWITCH_WRITE_CODE = 'switch'

CHILD_LOCK_CODE = 'child_lock'

COUNTDOWN_CODE = 'countdown_1'
MAX_COUNTDOWN_MINUTES = 1440  # Tuya caps countdown_1 at 86400 seconds.
MAX_COUNTDOWN_SECONDS = MAX_COUNTDOWN_MINUTES * 60

CONFIRM_RETRY_DELAY = 0.6

CURRENT_UNIT_DIVISORS = {'ma': 1000.0, 'a': 1.0}


def specifications(client, device_id):
    """Return {code: (scale, unit)}. Empty when Tuya cannot tell us."""
    cache_key = f'tuya:spec:{device_id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        spec = client.get_device_specification(device_id)
    except TuyaError:
        return {}

    scales = {}
    for item in spec.get('status', []):
        values = item.get('values')
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except ValueError:
                continue
        if isinstance(values, dict):
            scales[item['code']] = (int(values.get('scale', 0)), values.get('unit', ''))

    cache.set(cache_key, scales, SPEC_CACHE_TTL)
    return scales

def scaled(properties, specs, code):
    if code not in properties or properties[code] is None:
        return None, ''
    scale, unit = specs.get(code, (None, ''))
    if scale is None:
        return properties[code], unit
    return properties[code] / (10 ** scale), unit

def client_for(breaker):
    credential = TuyaCredential.objects.filter(organization=breaker.organization).first()
    if credential is None:
        raise LookupError(
            f'No Tuya credentials configured for "{breaker.organization.name}".'
        )
    return TuyaClient(credential)

def latest_telemetry(organization_id):
    reading = (
        Reading.objects.filter(organization_id=organization_id)
        .order_by('-timestamp')
        .first()
    )
    return ReadingSerializer(reading).data if reading else None

def record_action(breaker, action, result, source, actor=None, reason=''):
    return BreakerAction.objects.create(
        breaker=breaker,
        action=action,
        source=source,
        reason=reason,
        actor=actor,
        confirmed=result.get('confirmed'),
        telemetry=latest_telemetry(breaker.organization_id),
        breaker_status=result.get('status'),
    )


def tier1_interlock_reason(breaker, turn_on):
    if not turn_on or breaker.priority_type == 'ac_grid':
        return ''
    
    safety = Tier1SafetyState.objects.filter(
        organization_id=breaker.organization_id,
        active=True,
    ).only('situation').first()
    if safety is None:
        return ''
    situation = safety.situation or 'active danger'
    return (
        f'Tier-1 safety interlock is active ({situation}); '
        'non-grid loads cannot be switched on until the danger clears.'
    )


def write_and_confirm(breaker, write_code, value, status_key):
    client = client_for(breaker)
    client.send_commands(breaker.device_id, [{'code': write_code, 'value': value}])

    status = read_status(breaker)
    if status[status_key] is not value:
        time.sleep(CONFIRM_RETRY_DELAY)
        status = read_status(breaker)
    return status

def set_switch(breaker, turn_on, source='manual', actor=None, reason=''):
    turn_on = bool(turn_on)
    interlock_reason = tier1_interlock_reason(breaker, turn_on)
    if interlock_reason:
        result = {
            'device_id': breaker.device_id,
            'requested': 'on',
            'confirmed': False,
            'blocked': True,
            'reason': interlock_reason,
            'status': None,
        }
        audit_reason = '; '.join(
            item for item in (reason, interlock_reason) if item
        )
        record_action(
            breaker, 'switch_on', result, source, actor, audit_reason,
        )
        return result

    status = write_and_confirm(breaker, SWITCH_WRITE_CODE, turn_on, 'is_on')

    result = {
        'device_id': breaker.device_id,
        'requested': 'on' if turn_on else 'off',
        'confirmed': status['is_on'] is turn_on,
        'status': status,
    }

    if not result['confirmed'] and status['child_lock']:
        result['reason'] = 'The breaker is child-locked; disable the lock before switching it.'

    record_action(
        breaker, 'switch_on' if turn_on else 'switch_off', result, source, actor, reason
    )
    return result

def set_child_lock(breaker, enabled, source='manual', actor=None, reason=''):

    enabled = bool(enabled)

    current = read_status(breaker)
    if current['child_lock'] is enabled:
        return {
            'device_id': breaker.device_id,
            'requested': enabled,
            'confirmed': True,
            'changed': False,
            'reason': (
                'The breaker is already child-locked.' if enabled
                else 'The breaker is already unlocked.'
            ),
            'status': current,
        }

    status = write_and_confirm(breaker, CHILD_LOCK_CODE, enabled, 'child_lock')

    if breaker.child_lock != status['child_lock']:
        breaker.child_lock = status['child_lock']
        breaker.save(update_fields=['child_lock'])

    result = {
        'device_id': breaker.device_id,
        'requested': enabled,
        'confirmed': status['child_lock'] is enabled,
        'changed': True,
        'status': status,
    }
    if enabled and result['confirmed']:
        result['warning'] = (
            'The breaker is locked open and cannot be switched, by this API or '
            'at the panel, until the child lock is disabled.'
        )

    record_action(
        breaker, 'child_lock_on' if enabled else 'child_lock_off', result, source, actor, reason
    )

    notify(
        breaker.organization.owner_id,
        f'Breaker {breaker.label} has been locked.' if enabled
        else f'Breaker {breaker.label} has been unlocked.',
    )
    return result

def countdown_confirmed(status, seconds):
    remaining = status['countdown_s'] or 0
    return remaining == 0 if seconds == 0 else remaining > 0


def set_countdown_seconds(
    breaker,
    seconds,
    source='manual',
    actor=None,
    reason='',
    desired_state=None,
):
    """Set Tuya's second-based relay timer.

    desired_state is used by the KBS executor to make the toggle idempotent.
    The public Backend V1 API leaves it unset and retains toggle semantics.
    """
    seconds = int(seconds)
    if not 0 <= seconds <= MAX_COUNTDOWN_SECONDS:
        raise ValueError(
            f'countdown must be between 0 and {MAX_COUNTDOWN_SECONDS} seconds',
        )

    current = read_status(breaker)
    if (
        seconds
        and desired_state is not None
        and current.get('is_on') is bool(desired_state)
    ):
        return {
            'device_id': breaker.device_id,
            'requested_seconds': seconds,
            'requested_minutes': seconds / 60,
            'confirmed': True,
            'changed': False,
            'remaining_s': current.get('countdown_s') or 0,
            'action': None,
            'switches_at': None,
            'status': current,
        }

    will_turn_on = seconds > 0 and current.get('is_on') is not True
    interlock_reason = tier1_interlock_reason(breaker, will_turn_on)
    if interlock_reason:
        result = {
            'device_id': breaker.device_id,
            'requested_seconds': seconds,
            'requested_minutes': seconds / 60,
            'confirmed': False,
            'blocked': True,
            'changed': False,
            'remaining_s': current.get('countdown_s') or 0,
            'action': 'on',
            'switches_at': None,
            'reason': interlock_reason,
            'status': current,
        }
        audit_reason = '; '.join(
            item for item in (reason, interlock_reason) if item
        )
        record_action(
            breaker, 'countdown_set', result, source, actor, audit_reason,
        )
        return result

    client = client_for(breaker)
    client.send_commands(breaker.device_id, [{'code': COUNTDOWN_CODE, 'value': seconds}])

    status = read_status(breaker)
    if not countdown_confirmed(status, seconds):
        time.sleep(CONFIRM_RETRY_DELAY)
        status = read_status(breaker)

    remaining = status['countdown_s'] or 0
    result = {
        'device_id': breaker.device_id,
        'requested_seconds': seconds,
        'requested_minutes': seconds // 60 if seconds % 60 == 0 else seconds / 60,
        'confirmed': countdown_confirmed(status, seconds),
        'changed': True,
        'remaining_s': remaining,
        # Tuya's countdown toggles the relay rather than opening it. This status was
        # read after the write but before expiry, so is_on is still the state the
        # countdown will act against.
        'action': ('off' if status['is_on'] else 'on') if remaining else None,
        'switches_at': (
            (timezone.now() + timedelta(seconds=remaining)).isoformat() if remaining else None
        ),
        'status': status,
    }
    if remaining and status['child_lock']:
        result['warning'] = (
            'The breaker is child-locked, so the countdown will not be able to move '
            'the relay until the lock is released.'
        )

    record_action(breaker, 'countdown_set' if seconds else 'countdown_cancel', result, source, actor, reason)
    return result


def set_countdown(breaker, minutes, source='manual', actor=None, reason=''):
    return set_countdown_seconds(
        breaker,
        int(minutes) * 60,
        source=source,
        actor=actor,
        reason=reason,
    )


def milli(value):
    """Tuya is read in base units; the status tables store milli-units."""
    return None if value is None else value * 1000.0


@transaction.atomic
def persist_status(breaker, status):
    observed_at = timezone.now().replace(microsecond=0)
    current, _ = BreakerStatus.objects.select_for_update().get_or_create(breaker=breaker)

    switch = current.switch if status['is_on'] is None else bool(status['is_on'])
    if switch and not current.switch:
        current.last_switched_on_at = observed_at
    current.switch = switch
    current.online = bool(status['online'])
    current.child_lock = bool(status['child_lock'])
    current.countdown_1_s = max(int(status['countdown_s'] or 0), 0)
    current.fault = str(status['fault'] or '')[:100]
    current.units_resolved = bool(status['units_resolved'])
    current.cur_current_mA = milli(status['current_A'])
    current.cur_power_mW = milli(status['power_W'])
    current.cur_voltage_mV = milli(status['voltage_V'])
    current.save()

    BreakerReading.objects.get_or_create(
        breaker=breaker,
        timestamp=observed_at,
        defaults=current.as_sample(),
    )
    return current


def read_status(breaker, include_raw=False):
    client = client_for(breaker)
    result = client.get_device_properties(breaker.device_id)
    raw_properties = result.get('properties', [])
    properties = {p['code']: p['value'] for p in raw_properties}
    specs = specifications(client, breaker.device_id)

    voltage, _ = scaled(properties, specs, 'cur_voltage')
    power, _ = scaled(properties, specs, 'cur_power')
    current, current_unit = scaled(properties, specs, 'cur_current')
    if current is not None:
        current /= CURRENT_UNIT_DIVISORS.get(current_unit.lower(), 1.0)

    child_lock = properties.get(CHILD_LOCK_CODE)
    if child_lock is not None and breaker.child_lock != child_lock:
        breaker.child_lock = child_lock
        breaker.save(update_fields=['child_lock'])

    status = {
        'device_id': breaker.device_id,
        'name': breaker.name,
        'organization': breaker.organization_id,
        'priorty_type': breaker.priority_type,
        'priority': breaker.priority_degree,
        'type': breaker.load_type,
        'online': properties.get('online_state') == 'online',
        'is_on': properties.get(SWITCH_READ_CODE),
        'child_lock': child_lock,
        'countdown_s': properties.get(COUNTDOWN_CODE),
        'fault': properties.get('fault'),
        'voltage_V': voltage,
        'current_A': current,
        'power_W': power,
        'units_resolved': bool(specs),
    }
    persist_status(breaker, status)
    if include_raw:
        status['raw'] = raw_properties
    return status
