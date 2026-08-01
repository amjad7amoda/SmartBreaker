from datetime import datetime, time
from experta import Fact, Field
from schema import Or as AnyOf

# Value schemas, kept short so the templates below stay readable.
NUMBER = AnyOf(int, float)                 # a measured or derived quantity
OPTIONAL_NUMBER = AnyOf(int, float, None)  # quantity the telemetry may not report
OPTIONAL_TEXT = AnyOf(str, None)           # text that may be unavailable
OPTIONAL_ID = AnyOf(int, None)             # primary key that may be unknown
OPTIONAL_TIME = AnyOf(time, None)          # clock time that may be unconfigured

SHEDDABLE_TYPES = ('comfort', 'normal')  # categories the KBS is allowed to switch off
GRID_TYPE = 'ac_grid'                    # the special breaker that buys state-grid electricity


class SystemFact(Fact):
    organization_id = Field(int, mandatory=True)             # Organization primary key (unitless)
    now = Field(datetime, mandatory=True)                    # cycle wall-clock time (UTC timestamp)
    local_time = Field(time, mandatory=True)                 # cycle time on the site's local clock (local clock time)

    is_daytime = Field(bool, mandatory=True)                 # True between day_start/sunrise and day_end/sunset (flag)
    season = Field(str, mandatory=True)                      # meteorological season: 'winter'|'spring'|'summer'|'autumn'
    weather_condition = Field(OPTIONAL_TEXT, default=None)   # condition from the weather API; None = API not available
    power_saving = Field(bool, mandatory=True)               # user-selected power-saving mode (flag)
    event_upcoming = Field(bool, mandatory=True)             # a scheduled event is active or starts within event_prep_hours (flag)
    stability_threshold_percent = Field(NUMBER, mandatory=True)  # battery threshold active THIS cycle: normal or event-raised (% of capacity)

    battery_capacity_percent = Field(OPTIONAL_NUMBER, default=None)  # battery state of charge (% of capacity)
    battery_remaining_Wh = Field(NUMBER, mandatory=True)     # usable energy left in the battery right now (Wh)
    battery_stable = Field(bool, mandatory=True)             # battery_capacity_percent >= stability_threshold_percent (flag)
    battery_voltage_V = Field(OPTIONAL_NUMBER, default=None)  # battery bank voltage (V)
    battery_low = Field(bool, mandatory=True)                # bank voltage within battery_low_margin_V of the floor AND not charging -> countdown protection needed (flag)
    battery_draw_W = Field(NUMBER, mandatory=True)           # power currently drained from the battery (W)
    battery_buffer_Wh = Field(NUMBER, mandatory=True)        # energy the site may still spend after the low trigger, before breakers flip OFF (Wh)

    grid_breaker_on = Field(bool, mandatory=True)            # the site's AC-grid breaker is currently closed (flag)
    grid_energized = Field(bool, mandatory=True)             # the inverter senses real grid voltage -> the state grid is delivering (flag)
    grid_failed = Field(bool, mandatory=True)                # grid breaker ON but no grid voltage: the state grid is out (flag)

    heatsink_temp_C = Field(OPTIONAL_NUMBER, default=None)   # inverter heatsink temperature (degC)
    heat_high = Field(bool, mandatory=True)                  # heatsink temperature at/above the protection limit (flag)
    joule_deficit_J = Field(NUMBER, mandatory=True)          # cumulative (load - PV) energy over the deficit window (J)
    deficit_high = Field(bool, mandatory=True)               # joule deficit at/above the protection limit (flag)

    pv_power_W = Field(NUMBER, mandatory=True)               # current PV production (W)
    pv_baseline_W = Field(OPTIONAL_NUMBER, default=None)     # recent PV baseline, latest sample excluded (W)
    sudden_pv_drop = Field(bool, mandatory=True)             # PV fell suddenly below its baseline (flag)

    load_power_W = Field(NUMBER, mandatory=True)             # current total AC load on the inverter (W)
    load_baseline_W = Field(OPTIONAL_NUMBER, default=None)   # recent load baseline, latest sample excluded (W)
    sudden_draw = Field(bool, mandatory=True)                # load jumped suddenly above its baseline (flag)
    sudden_draw_culprit_id = Field(OPTIONAL_ID, default=None)  # Breaker pk with the largest recent power jump; None = unknown (unitless)

    mean_load_on_W = Field(NUMBER, mandatory=True)           # summed steady draw of all currently-ON loads, ac_grid excluded (W)
    headroom_W = Field(NUMBER, mandatory=True)               # AC power the inverter can still supply on top of the current load (W)
    max_inverter_power_W = Field(NUMBER, mandatory=True)     # maximum continuous AC output the inverter tolerates (W)

    hours_to_morning = Field(NUMBER, mandatory=True)         # hours until day_start/sunrise — the night reserve horizon (h)
    mandatory_need_Wh = Field(NUMBER, mandatory=True)        # energy the mandatory loads need from now until morning (Wh)

    motor_peak_minutes = Field(int, mandatory=True)          # inrush duration assumed when computing BreakerFact.expected_draw_W (min)


class BreakerFact(Fact):
    id = Field(int, mandatory=True)                          # Breaker primary key (unitless)
    device_id = Field(str, mandatory=True)                   # hardware identifier (unitless)
    priority_type = Field(str, mandatory=True)               # importance category: 'mandatory'|'normal'|'comfort'|'ac_grid'
    priority_degree = Field(int, mandatory=True)             # importance inside the category; higher = more important (unitless)
    category_rank = Field(int, mandatory=True)               # numeric importance of the category: 3=mandatory, 2=normal, 1=comfort, 0=ac_grid (unitless)
    load_type = Field(str, mandatory=True)                   # electrical profile: 'motor'|'normal'

    peak_load_W = Field(OPTIONAL_NUMBER, default=None)       # learned peak draw (W)
    mean_load_W = Field(OPTIONAL_NUMBER, default=None)       # learned steady-state draw (W)
    cur_power_W = Field(OPTIONAL_NUMBER, default=None)       # instantaneous draw, converted from the device's mW (W)
    expected_draw_W = Field(NUMBER, mandatory=True)          # power this breaker pulls (or will pull) while ON, motor inrush already accounted for (W)

    cycle_start = Field(OPTIONAL_TIME, default=None)         # daily usage/schedule window start (local clock time)
    cycle_end = Field(OPTIONAL_TIME, default=None)           # daily usage/schedule window end (local clock time)
    in_schedule_window = Field(bool, mandatory=True)         # the local clock is inside this breaker's configured window (flag)
    in_usage_window = Field(bool, mandatory=True)            # the user normally uses this breaker right now; a breaker without a window counts as always in use (flag)

    switch = Field(bool, mandatory=True)                     # current relay position: True = ON (flag)
    online = Field(bool, mandatory=True)                     # breaker reachable on the network (flag)
    fault = Field(str, mandatory=True)                       # device fault flags; empty = healthy (text)
    healthy = Field(bool, mandatory=True)                    # online and no fault -> safe to command (flag)
    minutes_since_on = Field(OPTIONAL_NUMBER, default=None)  # minutes since the last OFF->ON; None if OFF or unknown (min)

    locked_out = Field(bool, mandatory=True)                 # tripped by the KBS, awaiting user re-enable (flag)
    recently_tripped = Field(bool, mandatory=True)           # was tripped by the KBS within TRIP_MEMORY_HOURS and re-enabled by the user; do not trip again (flag)
    event_required = Field(bool, mandatory=True)             # a currently running scheduled event needs this breaker ON; treat like mandatory while it lasts (flag)
    sheddable = Field(bool, mandatory=True)                  # comfort/normal and not event-required -> the KBS may switch it off (flag)


class DecisionFact(Fact):
    """The decision-tree branch this cycle committed to.

    Its presence is what makes the tree a tree: every branch rule carries a
    ``NOT(DecisionFact())`` condition, so the first branch that fires removes
    all competing branches from the agenda. Exactly one per cycle.
    """

    branch = Field(str, mandatory=True)  # decision-tree path code, e.g. 'day.deficit.buy_grid' (text)


class CommandFact(Fact):
    """One switch command the engine wants executed."""

    breaker_id = Field(int, mandatory=True)   # Breaker primary key (unitless)
    device_id = Field(str, mandatory=True)    # hardware identifier, for readable logs (unitless)
    action = Field(str, mandatory=True)       # target relay state: 'on' | 'off'
    reason = Field(str, mandatory=True)       # why the KBS wants this switch (text)
    lockout = Field(bool, default=False)      # True = also lock the breaker until the user re-enables it (flag)
    countdown_s = Field(int, default=0)       # 0 = switch immediately; >0 = arm the device countdown so the switch happens after this delay (s)


class AlertFact(Fact):
    """One notification the engine wants raised."""

    kind = Field(str, mandatory=True)      # Alert.KIND_CHOICES code (text)
    severity = Field(str, mandatory=True)  # 'info' | 'warning' | 'critical'
    message = Field(str, mandatory=True)   # human-readable description (text)


# --------------------------------------------------------------------------
# working-memory accessors
# --------------------------------------------------------------------------

def system_fact(facts):
    """The single SystemFact inside a fact collection; None when absent."""
    return next((f for f in facts if isinstance(f, SystemFact)), None)


def breaker_facts(facts):
    """Every BreakerFact inside a fact collection, in declaration order (list)."""
    return [f for f in facts if isinstance(f, BreakerFact)]


def grid_fact(facts):
    """The site's AC-grid breaker; None when the site has none."""
    return next(
        (f for f in breaker_facts(facts) if f['priority_type'] == GRID_TYPE),
        None,
    )
