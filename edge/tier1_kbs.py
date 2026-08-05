"""Dependency-free Tier-1 safety KBS with deterministic decision tracing."""

from dataclasses import dataclass, field


TRACE_VERSION = 1


def _step(trace, code, kind, outcome, summary, **evidence):
    trace.append({
        'code': code,
        'kind': kind,
        'outcome': outcome,
        'summary': summary,
        'evidence': evidence,
    })


@dataclass
class Tier1Config:
    heatsink_temp_limit_C: float = 70.0
    max_inverter_power_W: float = 5000.0
    overload_fraction: float = 1.05
    battery_low_voltage_V: float = 24.0
    battery_low_margin_V: float = 0.5
    battery_critical_margin_V: float = 0.1
    battery_capacity_Wh: float = 5000.0
    battery_shutdown_buffer_percent: float = 2.0
    grid_present_min_V: float = 100.0
    charge_current_idle_A: float = 0.5
    countdown_min_s: int = 60
    countdown_max_s: int = 3600


@dataclass
class BreakerState:
    device_id: str
    priority_type: str
    priority_degree: int = 1
    switch: bool = False
    online: bool = True
    cur_power_W: float = 0.0

    CATEGORY_RANK = {'comfort': 1, 'normal': 2, 'mandatory': 3}

    @property
    def category_rank(self):
        return self.CATEGORY_RANK.get(self.priority_type, 0)

    @property
    def sheddable(self):
        return self.switch and self.online and self.priority_type in ('comfort', 'normal')


@dataclass
class InverterState:
    ac_output_active_power_W: float = 0.0
    heatsink_temp_C: float = 25.0
    battery_voltage_V: float = 26.0
    battery_capacity_percent: float = 100.0
    battery_charge_current_A: float = 0.0
    battery_discharge_current_A: float = 0.0
    grid_voltage_V: float = 0.0
    pv_charging_power_W: float = 0.0


@dataclass
class Command:
    device_id: str
    action: str
    countdown_s: int = 0
    reason: str = ''
    action_id: str = ''
    status: str = 'pending'


@dataclass
class Tier1Result:
    situation: str
    commands: list = field(default_factory=list)
    notify: str = ''
    trace_version: int = TRACE_VERSION
    trace: list = field(default_factory=list)
    event_id: str = ''
    event_type: str = 'decision'
    upload_state: str = 'not_saved'


def shed_order(breakers, trace=None, code_prefix='breaker.shed'):
    """Return eligible running loads least-important first and record why."""
    candidates = []
    for breaker in breakers:
        included = breaker.sheddable
        if trace is not None:
            _step(
                trace, f'{code_prefix}.eligibility', 'breaker_selection',
                'included' if included else 'excluded',
                f'{breaker.device_id} is {"eligible" if included else "not eligible"} for shedding.',
                device_id=breaker.device_id, switch=breaker.switch, online=breaker.online,
                priority_type=breaker.priority_type, priority_degree=breaker.priority_degree,
            )
        if included:
            candidates.append(breaker)
    ordered = sorted(candidates, key=lambda item: (item.category_rank, item.priority_degree))
    if trace is not None:
        _step(
            trace, f'{code_prefix}.ranking', 'breaker_ranking', 'selected',
            'Eligible breakers ranked least important first.',
            candidates=[item.device_id for item in candidates],
            ranked=[item.device_id for item in ordered],
            ranking=['category_rank_ascending', 'priority_degree_ascending'],
        )
    return ordered


def grid_breaker(breakers):
    return next((breaker for breaker in breakers if breaker.priority_type == 'ac_grid'), None)


def graceful_countdown_s(buffer_Wh, draw_W, cfg):
    if draw_W <= 0:
        return cfg.countdown_max_s
    return int(min(max(buffer_Wh / draw_W * 3600.0, cfg.countdown_min_s), cfg.countdown_max_s))


def _shed_all(breakers, reason, trace, prefix):
    ordered = shed_order(breakers, trace, prefix)
    for breaker in ordered:
        _step(
            trace, f'{prefix}.include', 'breaker_selection', 'included',
            f'{breaker.device_id} selected for immediate shedding.',
            device_id=breaker.device_id, draw=max(breaker.cur_power_W, 0.0), unit='W',
        )
    return [
        Command(device_id=breaker.device_id, action='off', reason=reason)
        for breaker in ordered
    ]


def _shed_until_within(breakers, target_W, current_load_W, reason, trace, prefix):
    commands = []
    remaining_W = current_load_W
    for breaker in shed_order(breakers, trace, prefix):
        within = remaining_W <= target_W
        _step(
            trace, f'{prefix}.budget', 'budget', 'passed' if within else 'failed',
            'Checked estimated remaining load against the target.',
            actual=remaining_W, operator='<=', threshold=target_W, unit='W',
        )
        if within:
            break
        commands.append(Command(
            device_id=breaker.device_id, action='off', reason=reason,
        ))
        remaining_W -= max(breaker.cur_power_W, 0.0)
        _step(
            trace, f'{prefix}.include', 'breaker_selection', 'included',
            f'{breaker.device_id} selected for shedding.',
            device_id=breaker.device_id, draw=max(breaker.cur_power_W, 0.0),
            unit='W', budget=target_W, remaining_capacity=remaining_W,
        )
    return commands


def _guard(trace, code, passed, summary, **evidence):
    _step(trace, code, 'guard', 'passed' if passed else 'failed', summary, **evidence)
    return passed


def evaluate(inverter, breakers, cfg=None):
    """Evaluate hardware-danger rules. The first matching situation wins."""
    cfg = cfg or Tier1Config()
    trace = []
    load_W = inverter.ac_output_active_power_W
    charging = inverter.battery_charge_current_A > cfg.charge_current_idle_A
    v_bat = inverter.battery_voltage_V

    overheat = inverter.heatsink_temp_C >= cfg.heatsink_temp_limit_C
    _guard(
        trace, 'tier1.guard.inverter_overheat', overheat,
        'Checked inverter heatsink temperature against its safety limit.',
        actual=inverter.heatsink_temp_C, operator='>=',
        threshold=cfg.heatsink_temp_limit_C, unit='C',
    )
    if overheat:
        _step(trace, 'tier1.branch.inverter_overheat', 'branch', 'selected',
              'Selected immediate inverter overheat protection.')
        return Tier1Result(
            situation='inverter_overheat',
            commands=_shed_all(breakers, 'tier1: inverter overheating', trace,
                               'tier1.overheat.breaker'),
            notify=(
                f'Tier-1: heatsink at {inverter.heatsink_temp_C} °C reached the limit '
                f'({cfg.heatsink_temp_limit_C} °C). Non-mandatory loads shed locally.'
            ),
            trace=trace,
        )

    overload_limit_W = cfg.max_inverter_power_W * cfg.overload_fraction
    overloaded = load_W >= overload_limit_W
    _guard(
        trace, 'tier1.guard.inverter_overload', overloaded,
        'Checked live load against the configured overload trigger.',
        actual=load_W, operator='>=', threshold=overload_limit_W, unit='W',
        rating=cfg.max_inverter_power_W, overload_fraction=cfg.overload_fraction,
    )
    if overloaded:
        _step(trace, 'tier1.branch.inverter_overload', 'branch', 'selected',
              'Selected priority shedding until the inverter rating is met.')
        return Tier1Result(
            situation='inverter_overload',
            commands=_shed_until_within(
                breakers, cfg.max_inverter_power_W, load_W,
                'tier1: inverter overload', trace, 'tier1.overload.breaker',
            ),
            notify=(
                f'Tier-1: load {load_W:.0f} W exceeded the inverter limit '
                f'({cfg.max_inverter_power_W:.0f} W). Loads shed by priority.'
            ),
            trace=trace,
        )

    critical_threshold = cfg.battery_low_voltage_V + cfg.battery_critical_margin_V
    battery_critical = not charging and v_bat <= critical_threshold
    _guard(
        trace, 'tier1.guard.battery_critical', battery_critical,
        'Checked whether a non-charging battery reached the critical voltage margin.',
        actual=v_bat, operator='<=', threshold=critical_threshold, unit='V', charging=charging,
    )
    if battery_critical:
        _step(trace, 'tier1.branch.battery_critical', 'branch', 'selected',
              'Selected immediate battery-floor protection.')
        return Tier1Result(
            situation='battery_critical',
            commands=_shed_all(
                breakers, 'tier1: battery at its protection floor', trace,
                'tier1.battery_critical.breaker',
            ),
            notify=(
                f'Tier-1: battery at {v_bat:.2f} V reached its floor '
                f'({cfg.battery_low_voltage_V} V). Non-mandatory loads shed immediately.'
            ),
            trace=trace,
        )

    low_threshold = cfg.battery_low_voltage_V + cfg.battery_low_margin_V
    battery_low = not charging and v_bat <= low_threshold
    _guard(
        trace, 'tier1.guard.battery_low', battery_low,
        'Checked whether a non-charging battery entered the graceful shutdown margin.',
        actual=v_bat, operator='<=', threshold=low_threshold, unit='V', charging=charging,
    )
    if battery_low:
        buffer_Wh = cfg.battery_shutdown_buffer_percent / 100.0 * cfg.battery_capacity_Wh
        measured_draw_W = v_bat * inverter.battery_discharge_current_A
        draw_W = measured_draw_W or max(load_W - inverter.pv_charging_power_W, 0.0)
        countdown_s = graceful_countdown_s(buffer_Wh, draw_W, cfg)
        sheds = shed_order(breakers, trace, 'tier1.battery_low.breaker')
        _step(
            trace, 'tier1.battery_low.countdown', 'calculation', 'selected',
            'Calculated the graceful battery-protection countdown.',
            budget=buffer_Wh, draw=draw_W, remaining_capacity=countdown_s,
            budget_unit='Wh', draw_unit='W', result_unit='s',
        )
        if not sheds:
            _step(trace, 'tier1.battery_low.noop', 'noop', 'noop',
                  'Battery is still low but no additional eligible load can be shed.')
        for breaker in sheds:
            _step(
                trace, 'tier1.battery_low.breaker.include', 'breaker_selection', 'included',
                f'{breaker.device_id} selected for countdown shutdown.',
                device_id=breaker.device_id, countdown=countdown_s, unit='s',
            )
        _step(trace, 'tier1.branch.battery_low', 'branch', 'selected',
              'Selected graceful battery shutdown.')
        return Tier1Result(
            situation='battery_low',
            commands=[
                Command(
                    device_id=breaker.device_id, action='off', countdown_s=countdown_s,
                    reason='tier1: battery safety countdown',
                )
                for breaker in sheds
            ],
            notify=(
                (
                    f'Tier-1: battery at {v_bat:.2f} V is close to its floor. '
                    f'{len(sheds)} breaker(s) will switch off in '
                    f'~{countdown_s // 60} min: '
                    f'{", ".join(breaker.device_id for breaker in sheds)}.'
                )
                if sheds else
                (
                    f'Tier-1: battery at {v_bat:.2f} V remains close to its '
                    'floor; the safety hold stays active and no additional '
                    'eligible loads remain to shed.'
                )
            ),
            trace=trace,
        )

    grid = grid_breaker(breakers)
    grid_outage = grid is not None and grid.switch and inverter.grid_voltage_V < cfg.grid_present_min_V
    _guard(
        trace, 'tier1.guard.grid_outage', grid_outage,
        'Checked for a closed AC-grid breaker with no energized grid input.',
        grid_breaker_present=grid is not None, grid_breaker_on=bool(grid and grid.switch),
        actual=inverter.grid_voltage_V, operator='<', threshold=cfg.grid_present_min_V, unit='V',
    )
    if grid_outage:
        thin_threshold = cfg.battery_low_voltage_V + 2 * cfg.battery_low_margin_V
        battery_thin = not charging and v_bat <= thin_threshold
        _guard(
            trace, 'tier1.guard.grid_outage.battery_thin', battery_thin,
            'Checked whether the battery is too thin to carry the outage.',
            actual=v_bat, operator='<=', threshold=thin_threshold, unit='V', charging=charging,
        )
        if battery_thin:
            sheds = _shed_until_within(
                breakers, max(inverter.pv_charging_power_W, 0.0), load_W,
                'tier1: grid outage while the battery is low', trace,
                'tier1.grid_outage.breaker',
            )
            if sheds:
                _step(trace, 'tier1.branch.grid_outage', 'branch', 'selected',
                      'Selected grid-outage shedding while preserving the grid breaker.')
                return Tier1Result(
                    situation='grid_outage',
                    commands=sheds,
                    notify=(
                        'Tier-1: the AC-grid breaker is closed but the grid delivers no power '
                        f'({inverter.grid_voltage_V:.0f} V) and the battery is low. '
                        'Loads shed by priority; the grid breaker stays ON to resume automatically.'
                    ),
                    trace=trace,
                )
            _step(trace, 'tier1.grid_outage.noop', 'noop', 'noop',
                  'Grid is unavailable but no additional eligible load can be shed.')
            _step(trace, 'tier1.branch.grid_outage', 'branch', 'selected',
                  'Kept the grid-outage safety hold active with no new command.')
            return Tier1Result(
                situation='grid_outage',
                commands=[],
                notify=(
                    'Tier-1: the grid remains unavailable and the battery is '
                    'low. The safety hold stays active; no additional eligible '
                    'loads remain to shed.'
                ),
                trace=trace,
            )

    _step(trace, 'tier1.branch.no_critical_situation', 'branch', 'selected',
          'No Tier-1 situation requires action; Tier-2 remains in charge.')
    return Tier1Result(situation='', trace=trace)
