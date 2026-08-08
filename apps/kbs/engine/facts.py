"""Pure, immutable inputs for the Tier-2 decision engine."""

from dataclasses import asdict, dataclass
from datetime import datetime, time

from .derived import in_window


CATEGORY_RANK = {'comfort': 1, 'normal': 2, 'mandatory': 3}
SHEDDABLE_TYPES = ('comfort', 'normal')
GRID_TYPE = 'ac_grid'


@dataclass(frozen=True)
class BreakerFacts:
    id: int
    device_id: str
    priority_type: str
    priority_degree: int
    load_type: str
    peak_load_W: float | None
    mean_load_W: float | None
    cycle_start: time | None
    cycle_end: time | None
    switch: bool
    online: bool
    fault: str
    locked_out: bool
    recently_tripped: bool
    event_required: bool
    cur_power_W: float | None
    minutes_since_on: float | None

    @property
    def category_rank(self):
        return CATEGORY_RANK.get(self.priority_type, 0)

    @property
    def healthy(self):
        return self.online and not self.fault

    def expected_draw_W(self, motor_peak_minutes):
        in_peak_phase = (
            self.load_type == 'motor'
            and (self.minutes_since_on is None or self.minutes_since_on < motor_peak_minutes)
        )
        if in_peak_phase and self.peak_load_W is not None:
            return self.peak_load_W
        if self.mean_load_W is not None:
            return self.mean_load_W
        if self.cur_power_W is not None:
            return self.cur_power_W
        return 0.0

    def in_schedule_window(self, local_t):
        return in_window(local_t, self.cycle_start, self.cycle_end)

    def in_usage_window(self, local_t):
        if self.cycle_start is None or self.cycle_end is None:
            return True
        return in_window(local_t, self.cycle_start, self.cycle_end)


@dataclass(frozen=True)
class SystemFacts:
    organization_id: int
    now: datetime
    local_time: time
    is_daytime: bool
    season: str
    weather_condition: str | None
    power_saving: bool
    event_upcoming: bool
    stability_threshold_percent: float
    battery_capacity_percent: float | None
    battery_remaining_Wh: float
    battery_stable: bool
    battery_voltage_V: float | None
    battery_low: bool
    battery_draw_W: float
    battery_buffer_Wh: float
    grid_breaker_on: bool
    grid_energized: bool
    grid_failed: bool
    heatsink_temp_C: float | None
    heat_high: bool
    joule_deficit_J: float
    deficit_high: bool
    overload: bool
    pv_power_W: float
    pv_baseline_W: float | None
    sudden_pv_drop: bool
    load_power_W: float
    load_baseline_W: float | None
    sudden_draw: bool
    sudden_draw_culprit_id: int | None
    mean_load_on_W: float
    headroom_W: float
    max_inverter_power_W: float
    hours_to_morning: float
    mandatory_need_Wh: float
    motor_peak_minutes: int
    breakers: tuple[BreakerFacts, ...]
    # Raw thresholds are retained alongside derived booleans so audit traces
    # show the comparison that was actually made.
    battery_low_threshold_V: float | None = None
    heatsink_temp_limit_C: float | None = None
    joule_deficit_limit_J: float | None = None
    grid_present_min_V: float | None = None
    sudden_drop_fraction: float | None = None
    sudden_draw_W: float | None = None
    pv_day_min_W: float | None = None


def facts_to_dict(facts):
    return _jsonable(asdict(facts))


def _jsonable(value):
    if isinstance(value, (datetime, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
