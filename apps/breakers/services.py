import json
import time

from django.core.cache import cache

from .models import TuyaCredential
from .tuya import TuyaClient, TuyaError

# Device specifications describe the hardware, not its state, so they are
# effectively immutable and worth caching aggressively.
SPEC_CACHE_TTL = 60 * 60 * 24

# Reading and writing the relay use different codes on this device family: the
# shadow reports `switch_1` while the instruction set accepts `switch`. Verify
# against `tuya_check`, which prints the writable codes, before changing these.
SWITCH_READ_CODE = 'switch_1'
SWITCH_WRITE_CODE = 'switch'

# A full lockout, not just a button guard: the device opens the relay and
# ignores every command, at the panel and over the API, until it is released.
# Enabling it therefore de-energises the load.
CHILD_LOCK_CODE = 'child_lock'

# Tuya's device shadow lags the physical relay by a moment, so an immediate
# read-back often still shows the old state. One short retry converts most of
# those into a confirmed result without making the request feel stuck.
CONFIRM_RETRY_DELAY = 0.6

# Tuya reports integers plus a scale: the real value is raw / 10**scale. The
# unit is reported separately and is not always the one we want to expose
# (current is commonly milliamps), so it is converted explicitly below.
CURRENT_UNIT_DIVISORS = {'ma': 1000.0, 'a': 1.0}


def _specifications(client, device_id):
    """Return {code: (scale, unit)}. Empty when Tuya cannot tell us."""
    cache_key = f'tuya:spec:{device_id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        spec = client.get_device_specification(device_id)
    except TuyaError:
        # A missing spec must not fail the whole read; the caller is told that
        # units are unresolved instead of being handed mis-scaled numbers.
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


def _scaled(properties, specs, code):
    if code not in properties or properties[code] is None:
        return None, ''
    scale, unit = specs.get(code, (None, ''))
    if scale is None:
        return properties[code], unit
    return properties[code] / (10 ** scale), unit


def _client_for(breaker):
    credential = TuyaCredential.objects.filter(organization=breaker.organization).first()
    if credential is None:
        raise LookupError(
            f'No Tuya credentials configured for "{breaker.organization.name}".'
        )
    return TuyaClient(credential)


def _write_and_confirm(breaker, write_code, value, status_key):
    client = _client_for(breaker)
    client.send_commands(breaker.device_id, [{'code': write_code, 'value': value}])

    status = read_status(breaker)
    if status[status_key] is not value:
        time.sleep(CONFIRM_RETRY_DELAY)
        status = read_status(breaker)
    return status


def set_switch(breaker, turn_on):
    turn_on = bool(turn_on)
    status = _write_and_confirm(breaker, SWITCH_WRITE_CODE, turn_on, 'is_on')

    result = {
        'device_id': breaker.device_id,
        'requested': 'on' if turn_on else 'off',
        'confirmed': status['is_on'] is turn_on,
        'status': status,
    }
    # The lock silently swallows switch commands, which otherwise looks like an
    # unexplained failure. The state was just read, so this costs nothing.
    if not result['confirmed'] and status['child_lock']:
        result['reason'] = 'The breaker is child-locked; disable the lock before switching it.'
    return result


def set_child_lock(breaker, enabled):
    """Engage or release the device lockout.

    On this hardware the lock is not merely a button guard: it opens the relay
    and refuses every command, local or remote, until it is released. Enabling
    it therefore cuts power to the load, which the response states plainly
    rather than leaving the caller to discover it.
    """
    enabled = bool(enabled)
    status = _write_and_confirm(breaker, CHILD_LOCK_CODE, enabled, 'child_lock')

    result = {
        'device_id': breaker.device_id,
        'requested': enabled,
        'confirmed': status['child_lock'] is enabled,
        'status': status,
    }
    if enabled and result['confirmed']:
        result['warning'] = (
            'The breaker is locked open and cannot be switched, by this API or '
            'at the panel, until the child lock is disabled.'
        )
    return result


def read_status(breaker, include_raw=False):
    client = _client_for(breaker)
    result = client.get_device_properties(breaker.device_id)
    raw_properties = result.get('properties', [])
    properties = {p['code']: p['value'] for p in raw_properties}
    specs = _specifications(client, breaker.device_id)

    voltage, _ = _scaled(properties, specs, 'cur_voltage')
    power, _ = _scaled(properties, specs, 'cur_power')
    current, current_unit = _scaled(properties, specs, 'cur_current')
    if current is not None:
        current /= CURRENT_UNIT_DIVISORS.get(current_unit.lower(), 1.0)

    # The device is authoritative for the lock; our column is a mirror, so a
    # read is also the reconciliation point after a change made in the Tuya app.
    child_lock = properties.get(CHILD_LOCK_CODE)
    if child_lock is not None and breaker.child_lock != child_lock:
        breaker.child_lock = child_lock
        breaker.save(update_fields=['child_lock'])

    status = {
        'device_id': breaker.device_id,
        'organization': breaker.organization_id,
        'online': properties.get('online_state') == 'online',
        'is_on': properties.get(SWITCH_READ_CODE),
        'child_lock': child_lock,
        'fault': properties.get('fault'),
        'voltage_V': voltage,
        'current_A': current,
        'power_W': power,
        'units_resolved': bool(specs),
    }
    if include_raw:
        status['raw'] = raw_properties
    return status
