"""Dependency-free Mamdani inference and controller-band hysteresis.

The fuzzy layer is deliberately deterministic and side-effect free.  Django
owns persistence; this module owns only numeric validation, membership/rule
evaluation, centroid defuzzification, and the state-transition calculation.
"""

from dataclasses import dataclass
from datetime import datetime
import math


PROFILE_VERSION = 'mamdani-v1'

POWER_TERMS = ('deficit', 'balanced', 'surplus')
RESERVE_TERMS = ('short', 'adequate', 'ample')
TREND_TERMS = ('falling', 'steady', 'rising')
RISK_TERMS = ('low', 'watch', 'high')


# The table order is part of the public, auditable profile.  Each row is
# (power balance, battery reserve, net-power trend, risk consequent).
_CONSEQUENTS = {
    ('deficit', 'short'): ('high', 'high', 'high'),
    ('deficit', 'adequate'): ('high', 'high', 'watch'),
    ('deficit', 'ample'): ('high', 'watch', 'watch'),
    ('balanced', 'short'): ('high', 'high', 'watch'),
    ('balanced', 'adequate'): ('high', 'watch', 'low'),
    ('balanced', 'ample'): ('watch', 'low', 'low'),
    ('surplus', 'short'): ('high', 'watch', 'watch'),
    ('surplus', 'adequate'): ('watch', 'low', 'low'),
    ('surplus', 'ample'): ('low', 'low', 'low'),
}
RULE_TABLE = tuple(
    (power, reserve, trend, _CONSEQUENTS[(power, reserve)][trend_index])
    for power in POWER_TERMS
    for reserve in RESERVE_TERMS
    for trend_index, trend in enumerate(TREND_TERMS)
)


def _finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _clamp(value, minimum, maximum):
    return min(max(float(value), minimum), maximum)


def left_shoulder(value, full_until, zero_at):
    """Left-shoulder membership with inclusive endpoints."""
    if value <= full_until:
        return 1.0
    if value >= zero_at:
        return 0.0
    return (zero_at - value) / (zero_at - full_until)


def triangle(value, left, peak, right):
    """Triangular membership with a unit peak and zero-valued feet."""
    if value == peak:
        return 1.0
    if value <= left or value >= right:
        return 0.0
    if value < peak:
        return (value - left) / (peak - left)
    return (right - value) / (right - peak)


def right_shoulder(value, zero_at, full_from):
    """Right-shoulder membership with inclusive endpoints."""
    if value <= zero_at:
        return 0.0
    if value >= full_from:
        return 1.0
    return (value - zero_at) / (full_from - zero_at)


def fuzzify_inputs(power_balance_ratio, battery_reserve_margin, net_power_trend):
    """Return the three normalized input membership maps.

    All three inputs are fractions of inverter or battery capacity.  Clamping
    bounds extreme but valid telemetry while retaining the original values in
    the evaluation payload.
    """
    balance = _clamp(power_balance_ratio, -1.0, 1.0)
    reserve = _clamp(battery_reserve_margin, -1.0, 1.0)
    trend = _clamp(net_power_trend, -1.0, 1.0)
    return {
        'power_balance': {
            'deficit': left_shoulder(balance, -0.25, 0.0),
            'balanced': triangle(balance, -0.25, 0.0, 0.25),
            'surplus': right_shoulder(balance, 0.0, 0.25),
        },
        'battery_reserve': {
            'short': left_shoulder(reserve, -0.10, 0.10),
            'adequate': triangle(reserve, -0.10, 0.10, 0.30),
            'ample': right_shoulder(reserve, 0.10, 0.30),
        },
        'net_power_trend': {
            'falling': left_shoulder(trend, -0.15, 0.0),
            'steady': triangle(trend, -0.15, 0.0, 0.15),
            'rising': right_shoulder(trend, 0.0, 0.15),
        },
    }


def output_membership(term, score):
    if term == 'low':
        return left_shoulder(score, 25.0, 45.0)
    if term == 'watch':
        return triangle(score, 25.0, 50.0, 75.0)
    if term == 'high':
        return right_shoulder(score, 55.0, 75.0)
    raise ValueError(f'unknown risk term: {term}')


def infer_risk(power_balance_ratio, battery_reserve_margin, net_power_trend):
    """Run min conjunction, max aggregation, and centroid defuzzification."""
    memberships = fuzzify_inputs(
        power_balance_ratio, battery_reserve_margin, net_power_trend,
    )
    fired_rules = []
    aggregated_strengths = {term: 0.0 for term in RISK_TERMS}
    for index, (power, reserve, trend, consequent) in enumerate(RULE_TABLE, start=1):
        strength = min(
            memberships['power_balance'][power],
            memberships['battery_reserve'][reserve],
            memberships['net_power_trend'][trend],
        )
        if strength <= 0.0:
            continue
        aggregated_strengths[consequent] = max(
            aggregated_strengths[consequent], strength,
        )
        fired_rules.append({
            'rule_id': index,
            'if': {
                'power_balance': power,
                'battery_reserve': reserve,
                'net_power_trend': trend,
            },
            'then': consequent,
            'strength': round(strength, 6),
        })

    numerator = 0.0
    denominator = 0.0
    # 0.25-point sampling includes both output endpoints and is deterministic
    # across supported Python versions.
    for step in range(401):
        score = step / 4.0
        aggregated = max(
            min(strength, output_membership(term, score))
            for term, strength in aggregated_strengths.items()
        )
        numerator += score * aggregated
        denominator += aggregated
    risk_score = numerator / denominator if denominator else None
    return {
        'memberships': {
            name: {term: round(value, 6) for term, value in terms.items()}
            for name, terms in memberships.items()
        },
        'fired_rules': fired_rules,
        'aggregated_strengths': {
            term: round(value, 6) for term, value in aggregated_strengths.items()
        },
        'risk_score': round(risk_score, 3) if risk_score is not None else None,
    }


def score_band(score):
    if score >= 65.0:
        return 'high'
    if score <= 35.0:
        return 'low'
    return 'watch'


def evaluate_fuzzy(facts):
    """Build normalized inputs from a SystemFacts-compatible value."""
    required = (
        ('pv_power_W', getattr(facts, 'pv_power_W', None)),
        ('load_power_W', getattr(facts, 'load_power_W', None)),
        ('max_inverter_power_W', getattr(facts, 'max_inverter_power_W', None)),
        ('battery_capacity_percent', getattr(facts, 'battery_capacity_percent', None)),
        ('battery_capacity_Wh', getattr(facts, 'battery_capacity_Wh', None)),
        ('stability_threshold_percent', getattr(facts, 'stability_threshold_percent', None)),
        ('night_reserve_percent', getattr(facts, 'night_reserve_percent', None)),
        ('mandatory_need_Wh', getattr(facts, 'mandatory_need_Wh', None)),
        ('pv_baseline_W', getattr(facts, 'pv_baseline_W', None)),
        ('load_baseline_W', getattr(facts, 'load_baseline_W', None)),
    )
    invalid = []
    if not getattr(facts, 'pv_power_valid', True):
        invalid.append('missing_pv_power')
    if not getattr(facts, 'load_power_valid', True):
        invalid.append('missing_load_power')
    for name, value in required:
        if not _finite_number(value):
            invalid.append(f'invalid_{name}')
    rating = getattr(facts, 'max_inverter_power_W', None)
    capacity = getattr(facts, 'battery_capacity_Wh', None)
    if _finite_number(rating) and rating <= 0:
        invalid.append('non_positive_max_inverter_power_W')
    if _finite_number(capacity) and capacity <= 0:
        invalid.append('non_positive_battery_capacity_Wh')
    for name in (
        'pv_power_W', 'load_power_W', 'pv_baseline_W', 'load_baseline_W',
        'mandatory_need_Wh',
    ):
        value = getattr(facts, name, None)
        if _finite_number(value) and value < 0:
            invalid.append(f'negative_{name}')
    for name in (
        'battery_capacity_percent', 'stability_threshold_percent',
        'night_reserve_percent',
    ):
        value = getattr(facts, name, None)
        if _finite_number(value) and not 0 <= float(value) <= 100:
            invalid.append(f'out_of_range_{name}')
    hours_to_morning = getattr(facts, 'hours_to_morning', None)
    if (
        not getattr(facts, 'is_daytime', False)
        and (
            not _finite_number(hours_to_morning)
            or float(hours_to_morning) <= 0
        )
    ):
        invalid.append('invalid_hours_to_morning')
    if invalid:
        return {
            'profile_version': PROFILE_VERSION,
            'valid': False,
            'fallback_reason': ','.join(dict.fromkeys(invalid)),
            'inputs': {},
            'memberships': {},
            'fired_rules': [],
            'aggregated_strengths': {},
            'risk_score': None,
            'inferred_band': None,
        }

    pv_W = float(facts.pv_power_W)
    load_W = float(facts.load_power_W)
    rating_W = float(facts.max_inverter_power_W)
    capacity_Wh = float(facts.battery_capacity_Wh)
    soc_percent = float(facts.battery_capacity_percent)
    event_target_percent = (
        float(facts.stability_threshold_percent)
        if getattr(facts, 'event_upcoming', False) else 0.0
    )
    mandatory_target_percent = max(
        float(facts.mandatory_need_Wh) / capacity_Wh * 100.0, 0.0,
    )
    reserve_target_percent = max(
        float(facts.night_reserve_percent),
        event_target_percent,
        mandatory_target_percent,
    )
    reserve_margin_Wh = (soc_percent - reserve_target_percent) / 100.0 * capacity_Wh
    power_balance_ratio = (pv_W - load_W) / rating_W
    current_net_W = pv_W - load_W
    baseline_net_W = float(facts.pv_baseline_W) - float(facts.load_baseline_W)
    net_power_trend = (current_net_W - baseline_net_W) / rating_W
    battery_reserve_margin = reserve_margin_Wh / capacity_Wh

    inferred = infer_risk(
        power_balance_ratio, battery_reserve_margin, net_power_trend,
    )
    risk_score = inferred['risk_score']
    hours_to_morning = getattr(facts, 'hours_to_morning', 0.0)
    if getattr(facts, 'is_daytime', False):
        safe_budget_W = max(pv_W, 0.0)
    else:
        usable_reserve_Wh = max(reserve_margin_Wh, 0.0)
        safe_budget_W = max(pv_W, 0.0) + usable_reserve_Wh / max(
            float(hours_to_morning), 1.0 / 12.0,
        )
    safe_budget_W = min(max(safe_budget_W, 0.0), rating_W)
    return {
        'profile_version': PROFILE_VERSION,
        'valid': risk_score is not None,
        'fallback_reason': None if risk_score is not None else 'empty_output_aggregation',
        'inputs': {
            'power_balance_ratio': round(power_balance_ratio, 6),
            'battery_reserve_margin': round(battery_reserve_margin, 6),
            'net_power_trend': round(net_power_trend, 6),
            'current_net_power_W': round(current_net_W, 3),
            'baseline_net_power_W': round(baseline_net_W, 3),
            'battery_soc_percent': round(soc_percent, 3),
            'battery_capacity_Wh': round(capacity_Wh, 3),
            'event_target_percent': round(event_target_percent, 3),
            'mandatory_target_percent': round(mandatory_target_percent, 3),
            'night_reserve_percent': round(float(facts.night_reserve_percent), 3),
            'reserve_target_percent': round(reserve_target_percent, 3),
            'reserve_margin_Wh': round(reserve_margin_Wh, 3),
            'safe_budget_W': round(safe_budget_W, 3),
        },
        **inferred,
        'inferred_band': score_band(risk_score) if risk_score is not None else None,
    }


@dataclass(frozen=True)
class ControllerSnapshot:
    current_band: str = 'watch'
    candidate_band: str = ''
    consecutive_cycles: int = 0
    last_risk_score: float | None = None
    last_evaluated_at: datetime | None = None
    profile_version: str = PROFILE_VERSION


def _candidate(snapshot, band):
    count = (
        snapshot.consecutive_cycles + 1
        if snapshot.candidate_band == band else 1
    )
    return band, count


def advance_controller(snapshot, evaluation, evaluated_at, cycle_seconds):
    """Apply band hysteresis and return (new snapshot, transition evidence)."""
    stale_after_s = max(float(cycle_seconds), 1.0) * 2.0
    profile_changed = snapshot.profile_version != PROFILE_VERSION
    stale = (
        snapshot.last_evaluated_at is not None
        and (evaluated_at - snapshot.last_evaluated_at).total_seconds() > stale_after_s
    )
    working = snapshot
    if stale or profile_changed:
        working = ControllerSnapshot()

    previous_band = working.current_band
    transition = 'held'
    if not evaluation.get('valid'):
        return working, {
            'previous_band': snapshot.current_band,
            'current_band': working.current_band,
            'candidate_band': working.candidate_band or None,
            'consecutive_cycles': working.consecutive_cycles,
            'transition': 'stale_reset' if stale or profile_changed else 'invalid_hold',
            'stale_reset': stale,
            'profile_reset': profile_changed,
            'advanced': False,
        }

    score = float(evaluation['risk_score'])
    band = working.current_band
    candidate_band = ''
    consecutive_cycles = 0

    if band == 'high':
        if score <= 55.0:
            candidate_band, consecutive_cycles = _candidate(working, 'watch')
            if consecutive_cycles >= 2:
                band = 'low' if score <= 35.0 else 'watch'
                candidate_band, consecutive_cycles = '', 0
                transition = 'confirmed_high_exit'
            else:
                transition = 'confirming_high_exit'
    elif band == 'low':
        if score >= 75.0:
            band = 'high'
            transition = 'immediate_high_entry'
        elif score >= 45.0:
            band = 'watch'
            transition = 'low_exit'
    else:
        band = 'watch'
        if score >= 75.0:
            band = 'high'
            transition = 'immediate_high_entry'
        elif score >= 65.0:
            candidate_band, consecutive_cycles = _candidate(working, 'high')
            if consecutive_cycles >= 2:
                band = 'high'
                candidate_band, consecutive_cycles = '', 0
                transition = 'confirmed_high_entry'
            else:
                transition = 'confirming_high_entry'
        elif score <= 25.0:
            band = 'low'
            transition = 'immediate_low_entry'
        elif score <= 35.0:
            candidate_band, consecutive_cycles = _candidate(working, 'low')
            if consecutive_cycles >= 2:
                band = 'low'
                candidate_band, consecutive_cycles = '', 0
                transition = 'confirmed_low_entry'
            else:
                transition = 'confirming_low_entry'

    next_snapshot = ControllerSnapshot(
        current_band=band,
        candidate_band=candidate_band,
        consecutive_cycles=consecutive_cycles,
        last_risk_score=score,
        last_evaluated_at=evaluated_at,
        profile_version=PROFILE_VERSION,
    )
    return next_snapshot, {
        'previous_band': snapshot.current_band,
        'current_band': next_snapshot.current_band,
        'candidate_band': next_snapshot.candidate_band or None,
        'consecutive_cycles': next_snapshot.consecutive_cycles,
        'transition': transition,
        'stale_reset': stale,
        'profile_reset': profile_changed,
        'advanced': True,
    }
