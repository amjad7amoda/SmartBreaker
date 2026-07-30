"""The decision tree of the main KBS, as a pure function of ``SystemFacts``.

``decide()`` mirrors the project flowchart: every cycle it walks exactly one
branch and returns the target breaker switches plus any alerts. It touches no
database and no clock — everything it needs is inside the facts snapshot —
so every branch is unit-testable with fabricated facts.

Branch codes (returned in ``RuleResult.branch``):
    protect_inverter                  heat/joule-deficit emergency shedding
    protect_battery                   battery near its voltage floor -> countdown shutdown
    day.surplus.comfort_on            PV covers the loads -> scheduled comfort ON
    day.battery_stable.comfort_on     battery above threshold -> scheduled comfort ON
    day.deficit.power_saving          PV short, saving mode -> keep best subset
    day.deficit.buy_grid              PV short, no saving -> AC-grid breaker ON
    day.deficit.grid_out.shed         grid tried but delivers nothing -> keep it ON, shed by priority
    day.sudden_drop.grid_out.shed     same fallback on the sudden-drop path
    night.sudden_draw.grid_out.shed   same fallback at night
    day.sudden_drop.battery_ok        sudden PV drop, battery rides it through
    day.sudden_drop.power_saving      sudden PV drop, saving mode -> best subset
    day.sudden_drop.buy_grid          sudden PV drop -> AC-grid breaker ON
    night.calm.battery                quiet night -> run from battery
    night.sudden_draw.battery_ok      reserve still covers mandatory until morning
    night.sudden_draw.trip            saving mode -> trip the culprit breaker
    night.sudden_draw.buy_grid        reserve short -> AC-grid breaker ON
"""

from dataclasses import dataclass, field

from .derived import graceful_countdown_s
from .grouping import first_group_within_headroom, select_best_subset


@dataclass
class ActionIntent:
    """One switch command the engine wants executed."""

    breaker_id: int      # Breaker primary key (unitless)
    device_id: str       # hardware identifier, for readable logs (unitless)
    action: str          # target relay state: 'on' | 'off'
    reason: str          # why the KBS wants this switch (text)
    lockout: bool = False  # True = also lock the breaker until the user re-enables it (flag)
    countdown_s: int = 0   # 0 = switch immediately; >0 = arm the device countdown so the switch happens after this delay (s)


@dataclass
class AlertIntent:
    """One notification the engine wants raised."""

    kind: str      # Alert.KIND_CHOICES code (text)
    severity: str  # 'info' | 'warning' | 'critical'
    message: str   # human-readable description (text)


@dataclass
class RuleResult:
    """Outcome of one decision cycle."""

    branch: str                                  # decision-tree path code (text)
    actions: list = field(default_factory=list)  # switch commands to execute (list[ActionIntent])
    alerts: list = field(default_factory=list)   # notifications to raise (list[AlertIntent])


def decide(facts):
    """Walk the decision tree once and return the resulting ``RuleResult``.

    facts: the SystemFacts snapshot gathered for this cycle
    """
    # Inverter protection outranks everything: high heatsink temperature or a
    # high cumulative joule deficit means the hardware itself is at risk.
    if facts.heat_high or facts.deficit_high:
        return _protect_inverter(facts)
    # Battery protection comes next: the bank must never reach its voltage
    # floor, so a graceful countdown shutdown is scheduled while it still can.
    if facts.battery_low:
        return _protect_battery(facts)
    if facts.is_daytime:
        if facts.sudden_pv_drop:
            result = _daytime_sudden_drop(facts)
        else:
            result = _daytime_normal(facts)
    else:
        result = _night(facts)
    # Whatever the branch decided, a running scheduled event gets its required
    # breakers switched ON (within head-room) — they are treated as mandatory.
    _ensure_event_required_on(facts, result)
    return result


# --------------------------------------------------------------------------
# branches
# --------------------------------------------------------------------------

def _protect_inverter(facts):
    """Emergency shedding: switch off every non-mandatory load immediately.

    Mandatory breakers are never touched; the AC-grid breaker goes ON as the
    last resort so the mandatory loads stay fed from the grid while the
    inverter recovers.
    """
    result = RuleResult(branch='protect_inverter')
    for breaker in _shed_order(facts):
        result.actions.append(ActionIntent(
            breaker_id=breaker.id, device_id=breaker.device_id, action='off',
            reason='emergency shed: inverter protection',
        ))
    _set_grid(facts, result, on=True, reason='feed remaining loads from grid while the inverter recovers')
    result.alerts.append(AlertIntent(
        kind='inverter_protection', severity='critical',
        message=(
            f'Inverter protection engaged: heatsink {facts.heatsink_temp_C} degC, '
            f'joule deficit {facts.joule_deficit_J:.0f} J. Non-mandatory loads shed.'
        ),
    ))
    return result


def _protect_battery(facts):
    """Battery near its voltage floor: schedule a graceful countdown shutdown.

    Instead of cutting loads instantly, every sheddable running load gets its
    device countdown armed so it flips OFF after the site has spent at most
    ``battery_buffer_Wh`` more energy (buffer / current draw). The user is
    notified which breakers will switch off and when. Without power saving,
    the AC-grid breaker also goes ON so the grid takes over the load at once.
    """
    result = RuleResult(branch='protect_battery')
    countdown_s = graceful_countdown_s(facts.battery_buffer_Wh, facts.battery_draw_W)  # delay before the scheduled switch-off (s)
    sheds = _shed_order(facts)  # running sheddable loads, least important first (list[BreakerFacts])
    for breaker in sheds:
        result.actions.append(ActionIntent(
            breaker_id=breaker.id, device_id=breaker.device_id, action='off',
            reason='battery safety: scheduled shutdown to protect the battery',
            countdown_s=countdown_s,
        ))
    if not facts.power_saving:
        _set_grid(facts, result, on=True, reason='battery near its voltage floor: grid takes over')
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
    result.alerts.append(AlertIntent(kind='battery_low', severity='critical', message=message))
    return result


def _daytime_normal(facts):
    """Daytime without a sudden PV drop: enable comfort on schedule if the
    system can afford it, otherwise fall back to saving mode or the grid."""
    surplus = facts.pv_power_W > facts.mean_load_on_W  # panels currently out-produce the running loads (flag)
    if surplus or facts.battery_stable:
        branch = 'day.surplus.comfort_on' if surplus else 'day.battery_stable.comfort_on'
        result = RuleResult(branch=branch)
        _turn_on_due_comfort(facts, result)
        _set_grid(facts, result, on=False, reason='PV/battery cover the loads')
        return result
    if facts.power_saving:
        result = RuleResult(branch='day.deficit.power_saving')
        _keep_best_subset(facts, result, budget_W=facts.pv_power_W)
        _set_grid(facts, result, on=False, reason='power saving: no grid purchase')
        return result
    result = RuleResult(branch='')
    _buy_grid_or_shed(facts, result, prefix='day.deficit',
                      reason='PV short and battery below threshold')
    return result


def _daytime_sudden_drop(facts):
    """Sudden PV drop during the day: diagnose it (season/weather), then decide
    based on battery stability and power-saving mode."""
    result = RuleResult(branch='')
    if facts.season == 'summer':
        result.alerts.append(AlertIntent(
            kind='panel_fault', severity='warning',
            message=(
                f'Sudden PV drop in summer ({facts.pv_baseline_W:.0f} W -> '
                f'{facts.pv_power_W:.0f} W): possible panel fault or shading on the panel.'
            ),
        ))
    else:
        condition = facts.weather_condition or 'cloud/storm'  # best explanation available (text)
        result.alerts.append(AlertIntent(
            kind='weather_drop', severity='info',
            message=(
                f'Sudden PV drop in {facts.season} ({facts.pv_baseline_W:.0f} W -> '
                f'{facts.pv_power_W:.0f} W): most likely weather ({condition}).'
            ),
        ))

    if facts.battery_stable:
        result.branch = 'day.sudden_drop.battery_ok'
        _set_grid(facts, result, on=False, reason='battery rides through the PV drop')
        return result
    if facts.power_saving:
        result.branch = 'day.sudden_drop.power_saving'
        _keep_best_subset(facts, result, budget_W=facts.pv_power_W)
        _set_grid(facts, result, on=False, reason='power saving: no grid purchase')
        return result
    _buy_grid_or_shed(facts, result, prefix='day.sudden_drop',
                      reason='PV dropped and battery below threshold')
    return result


def _night(facts):
    """Night: run from battery; on a sudden draw make sure the mandatory loads
    (servers, ...) still reach the morning."""
    if not facts.sudden_draw:
        result = RuleResult(branch='night.calm.battery')
        if facts.battery_remaining_Wh >= facts.mandatory_need_Wh:
            # Enough energy and no unusual draw -> grid power is not needed.
            _set_grid(facts, result, on=False, reason='night: battery covers the reserve, grid not needed')
        # else: leave the grid breaker as it is — if it is ON and delivering,
        # it keeps relieving the battery until the reserve is safe again.
        return result

    if facts.battery_remaining_Wh >= facts.mandatory_need_Wh:
        result = RuleResult(branch='night.sudden_draw.battery_ok')
        _set_grid(facts, result, on=False, reason='reserve still covers mandatory loads until morning')
        return result

    culprit = _culprit(facts)  # breaker behind the sudden draw, if identifiable (BreakerFacts | None)
    can_trip = (
        facts.power_saving
        and culprit is not None
        and culprit.priority_type in ('normal', 'comfort')
        and not culprit.recently_tripped  # user re-enabled it tonight -> respect that, buy grid instead
    )  # flag
    if can_trip:
        result = RuleResult(branch='night.sudden_draw.trip')
        result.actions.append(ActionIntent(
            breaker_id=culprit.id, device_id=culprit.device_id, action='off',
            reason='night sudden draw endangers the morning reserve', lockout=True,
        ))
        result.alerts.append(AlertIntent(
            kind='night_trip', severity='warning',
            message=(
                f'Breaker {culprit.device_id} tripped: its sudden draw endangers the '
                f'mandatory night reserve. Re-enable it manually to override.'
            ),
        ))
        _set_grid(facts, result, on=False, reason='power saving: culprit tripped instead of buying grid')
        return result

    result = RuleResult(branch='')
    _buy_grid_or_shed(facts, result, prefix='night.sudden_draw',
                      reason='night reserve short for mandatory loads until morning')
    return result


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _shed_order(facts):
    """Currently-ON sheddable loads, least important first (list[BreakerFacts]).

    Loads outside their user-configured usage window come first (the user is
    not using them right now anyway), then comfort before normal, and inside
    a category the lowest priority degree first. Mandatory, the AC-grid
    breaker, and event-required loads are never listed.
    """
    return sorted(
        [
            b for b in facts.breakers
            if b.switch and b.priority_type in ('comfort', 'normal') and not b.event_required
        ],
        key=lambda b: (b.in_usage_window(facts.local_time), b.category_rank, b.priority_degree),
    )


def _turn_on_due_comfort(facts, result):
    """Switch ON the comfort breakers whose schedule window contains now,
    limited to what the inverter head-room tolerates this cycle.

    Motor loads enter through their peak draw, so only the first group fits
    now; the remaining ones follow on the next cycles once earlier loads
    settle — this produces the staggered start.
    """
    due = []  # comfort breakers that should be ON now and can be commanded (list[BreakerFacts])
    for b in facts.breakers:
        if b.priority_type != 'comfort' or b.switch or b.locked_out:
            continue
        if not b.in_schedule_window(facts.local_time):
            continue
        if not b.healthy:
            result.alerts.append(AlertIntent(
                kind='breaker_fault', severity='warning',
                message=(
                    f'Comfort breaker {b.device_id} is due ON but '
                    f'{"faulted: " + b.fault if b.fault else "offline"}.'
                ),
            ))
            continue
        due.append(b)
    for b in first_group_within_headroom(due, facts.headroom_W, facts.motor_peak_minutes):
        result.actions.append(ActionIntent(
            breaker_id=b.id, device_id=b.device_id, action='on',
            reason='comfort schedule window and the system affords it',
        ))


def _keep_best_subset(facts, result, budget_W):
    """Power-saving: keep the most important possible set of running loads
    within ``budget_W`` and shed the rest (instead of buying grid power).

    budget_W: power the system can sustainably supply right now, e.g. current
              PV production (W). Mandatory loads are served first off-budget —
              they are never shed — and only the remainder is auctioned among
              the normal/comfort loads.
    """
    mandatory_draw_W = sum(
        b.expected_draw_W(facts.motor_peak_minutes)
        for b in facts.breakers
        if b.switch and (b.priority_type == 'mandatory' or b.event_required)
    )  # power the mandatory (and event-required) loads consume right now (W)
    sheddable = _shed_order(facts)  # running normal/comfort loads (list[BreakerFacts])
    keep = select_best_subset(
        sheddable,
        max(budget_W - mandatory_draw_W, 0.0),
        facts.motor_peak_minutes,
    )  # loads that stay ON (list[BreakerFacts])
    keep_ids = {b.id for b in keep}  # ids of the kept loads (set)
    for b in sheddable:
        if b.id not in keep_ids:
            result.actions.append(ActionIntent(
                breaker_id=b.id, device_id=b.device_id, action='off',
                reason='power saving: outside the affordable subset',
            ))


def _ensure_event_required_on(facts, result):
    """Switch ON the breakers a currently running event needs, within head-room.

    Event-required breakers are treated like mandatory loads for the whole
    event window: they are excluded from every shedding list, and here they
    are brought ON if anything switched them off before the event started.
    """
    candidates = []  # event-required breakers that are OFF and can be commanded (list[BreakerFacts])
    already_commanded = {a.breaker_id for a in result.actions}  # breakers this cycle already targets (set of pks)
    for b in facts.breakers:
        if not b.event_required or b.switch or b.locked_out or b.id in already_commanded:
            continue
        if not b.healthy:
            result.alerts.append(AlertIntent(
                kind='breaker_fault', severity='warning',
                message=(
                    f'Breaker {b.device_id} is required by a scheduled event but '
                    f'{"faulted: " + b.fault if b.fault else "offline"}.'
                ),
            ))
            continue
        candidates.append(b)
    for b in first_group_within_headroom(candidates, facts.headroom_W, facts.motor_peak_minutes):
        result.actions.append(ActionIntent(
            breaker_id=b.id, device_id=b.device_id, action='on',
            reason='required by the running scheduled event',
        ))


def _buy_grid_or_shed(facts, result, prefix, reason):
    """Buy grid electricity — with the real-world fallback when the grid is out.

    Cycle-based sensing: the first cycle switches the AC-grid breaker ON; on
    the next cycle the inverter's grid voltage shows whether the state grid is
    actually delivering. If it is not (``grid_failed``), the breaker stays ON
    (supply resumes by itself the moment the grid returns) and — even without
    power-saving mode — comfort/normal loads are shed by priority, because
    waiting for a dead grid would just drain the battery.

    prefix: branch-code prefix of the calling path, e.g. 'day.deficit' (text)
    reason: why grid power is wanted (text)
    """
    if facts.grid_failed:
        result.branch = f'{prefix}.grid_out.shed'
        _keep_best_subset(facts, result, budget_W=facts.pv_power_W)
        result.alerts.append(AlertIntent(
            kind='grid_outage', severity='critical',
            message=(
                'AC-grid breaker is ON but the grid delivers no power. '
                'Shedding comfort/normal loads by priority until the grid returns.'
            ),
        ))
        return
    result.branch = f'{prefix}.buy_grid'
    _set_grid(facts, result, on=True, reason=reason)


def _culprit(facts):
    """The BreakerFacts of the sudden-draw culprit, or None when unknown."""
    if facts.sudden_draw_culprit_id is None:
        return None
    return next((b for b in facts.breakers if b.id == facts.sudden_draw_culprit_id), None)


def _set_grid(facts, result, on, reason):
    """Command the AC-grid breaker to the wanted state, if it exists and differs.

    on:     True = buy grid electricity, False = stop buying (flag)
    reason: why the grid state was chosen (text)
    """
    grid = next((b for b in facts.breakers if b.priority_type == 'ac_grid'), None)  # the site's AC-grid breaker (BreakerFacts | None)
    if grid is None or grid.switch == on:
        return
    if on and not grid.healthy:
        result.alerts.append(AlertIntent(
            kind='breaker_fault', severity='critical',
            message=(
                f'AC-grid breaker {grid.device_id} needed ON but '
                f'{"faulted: " + grid.fault if grid.fault else "offline"}.'
            ),
        ))
    result.actions.append(ActionIntent(
        breaker_id=grid.id, device_id=grid.device_id,
        action='on' if on else 'off', reason=reason,
    ))
