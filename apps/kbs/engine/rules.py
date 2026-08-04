"""Pure Tier-2 decision tree with a deterministic trace for every cycle."""

from dataclasses import dataclass, field

from .derived import graceful_countdown_s
from .grouping import first_group_within_headroom, select_best_subset


TRACE_VERSION = 1


def _step(trace, code, kind, outcome, summary, **evidence):
    trace.append({
        'code': code, 'kind': kind, 'outcome': outcome,
        'summary': summary, 'evidence': evidence,
    })


def _guard(trace, code, passed, summary, **evidence):
    _step(trace, code, 'guard', 'passed' if passed else 'failed', summary, **evidence)
    return passed


@dataclass
class ActionIntent:
    breaker_id: int
    device_id: str
    action: str
    reason: str
    lockout: bool = False
    countdown_s: int = 0


@dataclass
class AlertIntent:
    kind: str
    severity: str
    message: str


@dataclass
class RuleResult:
    branch: str
    actions: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    trace_version: int = TRACE_VERSION
    trace: list = field(default_factory=list)


def _finish(result, trace):
    _step(
        trace, f'tier2.branch.{result.branch or "none"}', 'branch', 'selected',
        f'Selected Tier-2 branch {result.branch or "none"}.', branch=result.branch,
    )
    for action in result.actions:
        _step(
            trace, 'tier2.output.action', 'output', 'emitted',
            f'{action.device_id} -> {action.action}.', device_id=action.device_id,
            action=action.action, countdown_s=action.countdown_s, reason=action.reason,
        )
    for alert in result.alerts:
        _step(trace, 'tier2.output.alert', 'alert', 'emitted', alert.message,
              alert_kind=alert.kind, severity=alert.severity)
    result.trace_version = TRACE_VERSION
    result.trace = trace
    return result


def decide(facts):
    """Walk one decision path without side effects."""
    trace = []
    stressed = facts.heat_high or facts.deficit_high
    _guard(
        trace, 'tier2.guard.inverter_stress', stressed,
        'Checked derived heatsink and cumulative-deficit protection signals.',
        heat_high=facts.heat_high, deficit_high=facts.deficit_high,
        heat_actual=facts.heatsink_temp_C, heat_threshold=facts.heatsink_temp_limit_C,
        heat_unit='C', deficit_actual=facts.joule_deficit_J,
        deficit_threshold=facts.joule_deficit_limit_J, deficit_unit='J',
    )
    inverter_alert = None
    if stressed:
        _guard(
            trace, 'tier2.guard.live_overload', facts.overload,
            'Checked whether live load is high enough for shedding to help.',
            actual=facts.load_power_W, operator='>=',
            threshold=facts.max_inverter_power_W, unit='W',
        )
        if facts.overload:
            return _finish(_protect_inverter_overload(facts, trace), trace)
        if facts.heat_high:
            inverter_alert = _inverter_heat_alert(facts)

    _guard(
        trace, 'tier2.guard.battery_low', facts.battery_low,
        'Checked battery voltage against the non-charging protection threshold.',
        actual=facts.battery_voltage_V, operator='<=',
        threshold=facts.battery_low_threshold_V, unit='V',
    )
    if facts.battery_low:
        result = _protect_battery(facts, trace)
    elif facts.is_daytime:
        _guard(trace, 'tier2.guard.daytime', True, 'Selected the daytime decision tree.',
               actual=facts.local_time.isoformat())
        _guard(
            trace, 'tier2.guard.sudden_pv_drop', facts.sudden_pv_drop,
            'Checked current PV against its recent drop threshold.',
            actual=facts.pv_power_W, baseline=facts.pv_baseline_W,
            threshold=facts.sudden_drop_fraction, unit='fraction',
        )
        result = _daytime_sudden_drop(facts, trace) if facts.sudden_pv_drop else _daytime_normal(facts, trace)
    else:
        _guard(trace, 'tier2.guard.daytime', False, 'Selected the nighttime decision tree.',
               actual=facts.local_time.isoformat())
        result = _night(facts, trace)

    if inverter_alert is not None:
        result.alerts.insert(0, inverter_alert)
    _ensure_event_required_on(facts, result, trace)
    return _finish(result, trace)


def _protect_inverter_overload(facts, trace):
    result = RuleResult(branch='protect_inverter.overload')
    remaining_W = facts.load_power_W
    for breaker in _shed_order(facts, trace):
        within = remaining_W <= facts.max_inverter_power_W
        _guard(
            trace, 'tier2.overload.remaining_load', within,
            'Checked estimated load after priority shedding.', actual=remaining_W,
            operator='<=', threshold=facts.max_inverter_power_W, unit='W',
        )
        if within:
            break
        result.actions.append(ActionIntent(
            breaker_id=breaker.id, device_id=breaker.device_id, action='off',
            reason='emergency shed: inverter overload',
        ))
        remaining_W -= max(breaker.cur_power_W or 0.0, 0.0)
        _step(trace, 'tier2.overload.breaker', 'breaker_selection', 'included',
              f'{breaker.device_id} selected for overload shedding.',
              device_id=breaker.device_id, draw=max(breaker.cur_power_W or 0.0, 0.0),
              budget=facts.max_inverter_power_W, remaining_capacity=remaining_W, unit='W')
    result.alerts.append(AlertIntent(
        kind='inverter_protection', severity='critical',
        message=(
            f'Inverter overload: load {facts.load_power_W:.0f} W at/above the '
            f'{facts.max_inverter_power_W:.0f} W rating (heatsink {facts.heatsink_temp_C} degC, '
            f'joule deficit {facts.joule_deficit_J:.0f} J). Loads shed by priority until it fits.'
        ),
    ))
    return result


def _inverter_heat_alert(facts):
    return AlertIntent(
        kind='inverter_protection', severity='critical',
        message=(
            f'Inverter heatsink at {facts.heatsink_temp_C} degC exceeds the limit but '
            f'current load ({facts.load_power_W:.0f} W) is within the '
            f'{facts.max_inverter_power_W:.0f} W rating -- likely a cooling or hardware '
            f'fault rather than an overload. Inspect the inverter.'
        ),
    )


def _protect_battery(facts, trace):
    result = RuleResult(branch='protect_battery')
    countdown_s = graceful_countdown_s(facts.battery_buffer_Wh, facts.battery_draw_W)
    _step(
        trace, 'tier2.battery.countdown', 'calculation', 'selected',
        'Calculated the battery-protection countdown.', budget=facts.battery_buffer_Wh,
        draw=facts.battery_draw_W, remaining_capacity=countdown_s,
        budget_unit='Wh', draw_unit='W', result_unit='s',
    )
    sheds = _shed_order(facts, trace)
    for breaker in sheds:
        result.actions.append(ActionIntent(
            breaker_id=breaker.id, device_id=breaker.device_id, action='off',
            reason='battery safety: scheduled shutdown to protect the battery',
            countdown_s=countdown_s,
        ))
    if not facts.power_saving:
        _set_grid(facts, result, True, 'battery near its voltage floor: grid takes over', trace)
    if sheds:
        message = (
            f'Battery at {facts.battery_voltage_V} V is close to its protection floor. '
            f'These breakers will switch off in ~{countdown_s // 60} min for battery safety: '
            f'{", ".join(b.device_id for b in sheds)}.'
        )
    else:
        message = (
            f'Battery at {facts.battery_voltage_V} V is close to its protection floor and only '
            f'mandatory loads are still running — nothing left to shed.'
        )
    result.alerts.append(AlertIntent('battery_low', 'critical', message))
    return result


def _daytime_normal(facts, trace):
    surplus = facts.pv_power_W > facts.mean_load_on_W
    _guard(trace, 'tier2.guard.day_surplus', surplus,
           'Checked whether PV exceeds expected running load.', actual=facts.pv_power_W,
           operator='>', threshold=facts.mean_load_on_W, unit='W')
    _guard(trace, 'tier2.guard.battery_stable', facts.battery_stable,
           'Checked charge state against the active stability threshold.',
           actual=facts.battery_capacity_percent, operator='>=',
           threshold=facts.stability_threshold_percent, unit='percent')
    if surplus or facts.battery_stable:
        branch = 'day.surplus.comfort_on' if surplus else 'day.battery_stable.comfort_on'
        result = RuleResult(branch=branch)
        _turn_on_due_comfort(facts, result, trace)
        _set_grid(facts, result, False, 'PV/battery cover the loads', trace)
        return result
    _guard(trace, 'tier2.guard.power_saving', facts.power_saving,
           'Checked whether the site forbids grid purchase.', actual=facts.power_saving)
    if facts.power_saving:
        result = RuleResult(branch='day.deficit.power_saving')
        _keep_best_subset(facts, result, facts.pv_power_W, trace)
        _set_grid(facts, result, False, 'power saving: no grid purchase', trace)
        return result
    result = RuleResult(branch='')
    _buy_grid_or_shed(facts, result, 'day.deficit',
                      'PV short and battery below threshold', trace)
    return result


def _daytime_sudden_drop(facts, trace):
    result = RuleResult(branch='')
    if facts.season == 'summer':
        result.alerts.append(AlertIntent(
            'panel_fault', 'warning',
            f'Sudden PV drop in summer ({facts.pv_baseline_W:.0f} W -> {facts.pv_power_W:.0f} W): possible panel fault or shading on the panel.',
        ))
    else:
        condition = facts.weather_condition or 'cloud/storm'
        result.alerts.append(AlertIntent(
            'weather_drop', 'info',
            f'Sudden PV drop in {facts.season} ({facts.pv_baseline_W:.0f} W -> {facts.pv_power_W:.0f} W): most likely weather ({condition}).',
        ))
    _guard(trace, 'tier2.guard.sudden_drop.battery_stable', facts.battery_stable,
           'Checked whether the battery can ride through the PV drop.',
           actual=facts.battery_capacity_percent, operator='>=',
           threshold=facts.stability_threshold_percent, unit='percent')
    if facts.battery_stable:
        result.branch = 'day.sudden_drop.battery_ok'
        _set_grid(facts, result, False, 'battery rides through the PV drop', trace)
        return result
    _guard(trace, 'tier2.guard.sudden_drop.power_saving', facts.power_saving,
           'Checked whether grid purchase is disabled.', actual=facts.power_saving)
    if facts.power_saving:
        result.branch = 'day.sudden_drop.power_saving'
        _keep_best_subset(facts, result, facts.pv_power_W, trace)
        _set_grid(facts, result, False, 'power saving: no grid purchase', trace)
        return result
    _buy_grid_or_shed(facts, result, 'day.sudden_drop',
                      'PV dropped and battery below threshold', trace)
    return result


def _night(facts, trace):
    _guard(trace, 'tier2.guard.sudden_draw', facts.sudden_draw,
           'Checked load against the configured sudden-draw threshold.',
           actual=facts.load_power_W, baseline=facts.load_baseline_W,
           threshold=facts.sudden_draw_W, unit='W')
    if not facts.sudden_draw:
        result = RuleResult(branch='night.calm.battery')
        enough = facts.battery_remaining_Wh >= facts.mandatory_need_Wh
        _guard(trace, 'tier2.guard.night_reserve', enough,
               'Checked battery energy against mandatory need until morning.',
               actual=facts.battery_remaining_Wh, operator='>=',
               threshold=facts.mandatory_need_Wh, unit='Wh')
        if enough:
            _set_grid(facts, result, False,
                      'night: battery covers the reserve, grid not needed', trace)
        else:
            _step(trace, 'tier2.grid.preserve', 'noop', 'noop',
                  'Reserve is short; preserved the current grid breaker state.')
        return result
    enough = facts.battery_remaining_Wh >= facts.mandatory_need_Wh
    _guard(trace, 'tier2.guard.night_reserve', enough,
           'Checked remaining battery energy against mandatory need until morning.',
           actual=facts.battery_remaining_Wh, operator='>=',
           threshold=facts.mandatory_need_Wh, unit='Wh')
    if enough:
        result = RuleResult(branch='night.sudden_draw.battery_ok')
        _set_grid(facts, result, False,
                  'reserve still covers mandatory loads until morning', trace)
        return result
    culprit = _culprit(facts)
    can_trip = (
        facts.power_saving and culprit is not None
        and culprit.priority_type in ('normal', 'comfort') and not culprit.recently_tripped
    )
    _guard(trace, 'tier2.guard.trip_culprit', can_trip,
           'Checked whether the sudden-draw culprit may be tripped.',
           power_saving=facts.power_saving, culprit=culprit.device_id if culprit else None,
           recently_tripped=culprit.recently_tripped if culprit else None)
    if can_trip:
        result = RuleResult(branch='night.sudden_draw.trip')
        result.actions.append(ActionIntent(
            culprit.id, culprit.device_id, 'off',
            'night sudden draw endangers the morning reserve', lockout=True,
        ))
        result.alerts.append(AlertIntent(
            'night_trip', 'warning',
            f'Breaker {culprit.device_id} tripped: its sudden draw endangers the mandatory night reserve. Re-enable it manually to override.',
        ))
        _set_grid(facts, result, False,
                  'power saving: culprit tripped instead of buying grid', trace)
        return result
    result = RuleResult(branch='')
    _buy_grid_or_shed(facts, result, 'night.sudden_draw',
                      'night reserve short for mandatory loads until morning', trace)
    return result


def _shed_order(facts, trace):
    candidates = []
    for breaker in facts.breakers:
        included = (
            breaker.switch and breaker.priority_type in ('comfort', 'normal')
            and not breaker.event_required
        )
        _step(trace, 'tier2.shed.eligibility', 'breaker_selection',
              'included' if included else 'excluded',
              f'{breaker.device_id} is {"eligible" if included else "not eligible"} for shedding.',
              device_id=breaker.device_id, switch=breaker.switch,
              priority_type=breaker.priority_type, priority_degree=breaker.priority_degree,
              event_required=breaker.event_required,
              protected=breaker.priority_type == 'mandatory' or breaker.event_required)
        if included:
            candidates.append(breaker)
    ordered = sorted(
        candidates,
        key=lambda b: (b.in_usage_window(facts.local_time), b.category_rank, b.priority_degree),
    )
    _step(trace, 'tier2.shed.ranking', 'breaker_ranking', 'selected',
          'Eligible breakers ranked for shedding.',
          candidates=[b.device_id for b in candidates], ranked=[b.device_id for b in ordered],
          ranking=['outside_usage_window_first', 'category_rank_ascending', 'priority_degree_ascending'])
    return ordered


def _turn_on_due_comfort(facts, result, trace):
    due = []
    for breaker in facts.breakers:
        if breaker.priority_type != 'comfort' or breaker.switch or breaker.locked_out:
            _step(trace, 'tier2.comfort.eligibility', 'breaker_selection', 'excluded',
                  f'{breaker.device_id} is not an OFF, unlocked comfort candidate.',
                  device_id=breaker.device_id, priority_type=breaker.priority_type,
                  switch=breaker.switch, locked_out=breaker.locked_out)
            continue
        if not breaker.in_schedule_window(facts.local_time):
            _step(trace, 'tier2.comfort.schedule', 'breaker_selection', 'excluded',
                  f'{breaker.device_id} is outside its comfort schedule.',
                  device_id=breaker.device_id, actual=facts.local_time.isoformat(),
                  operator='in_window', threshold=[str(breaker.cycle_start), str(breaker.cycle_end)])
            continue
        if not breaker.healthy:
            _step(trace, 'tier2.comfort.health', 'breaker_selection', 'excluded',
                  f'{breaker.device_id} cannot be switched on because it is unhealthy.',
                  device_id=breaker.device_id, online=breaker.online, fault=breaker.fault)
            result.alerts.append(AlertIntent(
                'breaker_fault', 'warning',
                f'Comfort breaker {breaker.device_id} is due ON but '
                f'{"faulted: " + breaker.fault if breaker.fault else "offline"}.',
            ))
            continue
        due.append(breaker)
    selected = first_group_within_headroom(due, facts.headroom_W, facts.motor_peak_minutes)
    selected_ids = {breaker.id for breaker in selected}
    remaining_W = facts.headroom_W
    for breaker in sorted(due, key=lambda b: (-b.category_rank, -b.priority_degree)):
        draw_W = breaker.expected_draw_W(facts.motor_peak_minutes)
        included = breaker.id in selected_ids
        _step(trace, 'tier2.comfort.headroom', 'breaker_selection',
              'included' if included else 'excluded',
              f'{breaker.device_id} {"fits" if included else "does not fit"} available inverter headroom.',
              device_id=breaker.device_id, draw=draw_W, unit='W',
              budget=facts.headroom_W, remaining_capacity=remaining_W)
        if included:
            remaining_W -= draw_W
    for breaker in selected:
        result.actions.append(ActionIntent(
            breaker.id, breaker.device_id, 'on',
            'comfort schedule window and the system affords it',
        ))


def _keep_best_subset(facts, result, budget_W, trace):
    mandatory_draw_W = sum(
        breaker.expected_draw_W(facts.motor_peak_minutes)
        for breaker in facts.breakers
        if breaker.switch and (breaker.priority_type == 'mandatory' or breaker.event_required)
    )
    affordable_W = max(budget_W - mandatory_draw_W, 0.0)
    _step(trace, 'tier2.subset.budget', 'budget', 'selected',
          'Reserved supply for mandatory and event-required loads.', budget=budget_W,
          mandatory_draw=mandatory_draw_W, unit='W', remaining_capacity=affordable_W)
    sheddable = _shed_order(facts, trace)
    keep = select_best_subset(sheddable, affordable_W, facts.motor_peak_minutes)
    keep_ids = {breaker.id for breaker in keep}
    for breaker in sheddable:
        included = breaker.id in keep_ids
        _step(trace, 'tier2.subset.selection', 'breaker_selection',
              'included' if included else 'excluded',
              f'{breaker.device_id} is {"inside" if included else "outside"} the affordable subset.',
              device_id=breaker.device_id,
              draw=breaker.expected_draw_W(facts.motor_peak_minutes), budget=affordable_W, unit='W')
        if not included:
            result.actions.append(ActionIntent(
                breaker.id, breaker.device_id, 'off',
                'power saving: outside the affordable subset',
            ))


def _ensure_event_required_on(facts, result, trace):
    candidates = []
    already_commanded = {action.breaker_id for action in result.actions}
    for breaker in facts.breakers:
        if not breaker.event_required:
            continue
        if breaker.switch or breaker.locked_out or breaker.id in already_commanded:
            _step(trace, 'tier2.event_required.eligibility', 'breaker_selection', 'excluded',
                  f'{breaker.device_id} needs no new event override command.',
                  device_id=breaker.device_id, switch=breaker.switch,
                  locked_out=breaker.locked_out,
                  already_commanded=breaker.id in already_commanded, protected=True)
            continue
        if not breaker.healthy:
            _step(trace, 'tier2.event_required.health', 'breaker_selection', 'excluded',
                  f'{breaker.device_id} is event-required but unhealthy.',
                  device_id=breaker.device_id, online=breaker.online,
                  fault=breaker.fault, protected=True)
            result.alerts.append(AlertIntent(
                'breaker_fault', 'warning',
                f'Breaker {breaker.device_id} is required by a scheduled event but '
                f'{"faulted: " + breaker.fault if breaker.fault else "offline"}.',
            ))
            continue
        candidates.append(breaker)
    selected = first_group_within_headroom(candidates, facts.headroom_W, facts.motor_peak_minutes)
    selected_ids = {breaker.id for breaker in selected}
    for breaker in candidates:
        included = breaker.id in selected_ids
        _step(trace, 'tier2.event_required.headroom', 'breaker_selection',
              'included' if included else 'excluded',
              f'{breaker.device_id} {"fits" if included else "does not fit"} event headroom.',
              device_id=breaker.device_id,
              draw=breaker.expected_draw_W(facts.motor_peak_minutes),
              budget=facts.headroom_W, unit='W', protected=True)
    for breaker in selected:
        result.actions.append(ActionIntent(
            breaker.id, breaker.device_id, 'on',
            'required by the running scheduled event',
        ))


def _buy_grid_or_shed(facts, result, prefix, reason, trace):
    _guard(trace, 'tier2.guard.grid_failed', facts.grid_failed,
           'Checked whether the closed grid path is delivering voltage.',
           grid_breaker_on=facts.grid_breaker_on, grid_energized=facts.grid_energized,
           operator='>=', threshold=facts.grid_present_min_V, unit='V')
    if facts.grid_failed:
        result.branch = f'{prefix}.grid_out.shed'
        _keep_best_subset(facts, result, facts.pv_power_W, trace)
        result.alerts.append(AlertIntent(
            'grid_outage', 'critical',
            'AC-grid breaker is ON but the grid delivers no power. Shedding comfort/normal loads by priority until the grid returns.',
        ))
        return
    result.branch = f'{prefix}.buy_grid'
    _set_grid(facts, result, True, reason, trace)


def _culprit(facts):
    if facts.sudden_draw_culprit_id is None:
        return None
    return next((b for b in facts.breakers if b.id == facts.sudden_draw_culprit_id), None)


def _set_grid(facts, result, on, reason, trace):
    grid = next((b for b in facts.breakers if b.priority_type == 'ac_grid'), None)
    if grid is None:
        _step(trace, 'tier2.grid.noop', 'noop', 'noop',
              'No AC-grid breaker is configured.', requested_state=on)
        return
    if grid.switch == on:
        _step(trace, 'tier2.grid.noop', 'noop', 'noop',
              'AC-grid breaker is already in the requested state.',
              device_id=grid.device_id, requested_state=on, actual_state=grid.switch)
        return
    if on and not grid.healthy:
        _step(trace, 'tier2.grid.health', 'breaker_selection', 'excluded',
              'AC-grid command cannot be emitted because the breaker is unhealthy.',
              device_id=grid.device_id, online=grid.online, fault=grid.fault)
        result.alerts.append(AlertIntent(
            'breaker_fault', 'critical',
            f'AC-grid breaker {grid.device_id} needed ON but '
            f'{"faulted: " + grid.fault if grid.fault else "offline"}.',
        ))
        return
    _step(trace, 'tier2.grid.command', 'breaker_selection', 'included',
          'AC-grid breaker selected for a state change.', device_id=grid.device_id,
          actual_state=grid.switch, requested_state=on)
    result.actions.append(ActionIntent(
        grid.id, grid.device_id, 'on' if on else 'off', reason,
    ))
