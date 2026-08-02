"""Fact gathering: turn the latest telemetry + breaker state into one typed
snapshot (``SystemFacts``) that the rule layer can reason about.

``decide()`` in ``rules.py`` is a pure function of ``SystemFacts``, so all
database access and unit conversion happens here and only here.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.breakers.models import Breaker, BreakerReading
from apps.telemetry.models import Reading

from .derived import (
    hours_until,
    in_window,
    is_daytime,
    is_sudden_draw,
    is_sudden_drop,
    joule_deficit_J,
    mean,
    ramped_threshold,
)
from .weather import get_weather_context

TRIP_MEMORY_HOURS = 12  # how long a KBS trip is remembered after the user re-enables the breaker, so one night never trips the same breaker twice (h)


@dataclass
class BreakerFacts:
    """Everything the rules need to know about one breaker this cycle."""

    id: int                          # Breaker primary key (unitless)
    device_id: str                   # hardware identifier (unitless)
    priority_type: str               # importance category: 'mandatory'|'normal'|'comfort'|'ac_grid'
    priority_degree: int             # importance inside the category; higher = more important (unitless)
    load_type: str                   # electrical profile: 'motor'|'normal'
    peak_load_W: float | None        # learned peak draw (W)
    mean_load_W: float | None        # learned steady-state draw (W)
    cycle_start: time | None         # comfort schedule window start (local clock time)
    cycle_end: time | None           # comfort schedule window end (local clock time)
    switch: bool                     # current relay position: True = ON (flag)
    online: bool                     # breaker reachable on the network (flag)
    fault: str                       # device fault flags; empty = healthy (text)
    locked_out: bool                 # tripped by the KBS, awaiting user re-enable (flag)
    recently_tripped: bool           # was tripped by the KBS within TRIP_MEMORY_HOURS and re-enabled by the user; do not trip again (flag)
    event_required: bool             # a currently running scheduled event needs this breaker ON; treat like mandatory while it lasts (flag)
    cur_power_W: float | None        # instantaneous draw, converted from the device's mW (W)
    minutes_since_on: float | None   # minutes since the last OFF->ON; None if OFF or unknown (min)

    @property
    def category_rank(self):
        """Numeric importance of the category: 3=mandatory, 2=normal, 1=comfort, 0=ac_grid (unitless)."""
        return Breaker.CATEGORY_RANK.get(self.priority_type, 0)

    @property
    def healthy(self):
        """True when the breaker can safely be commanded: online and no fault (flag)."""
        return self.online and not self.fault

    def expected_draw_W(self, motor_peak_minutes):
        """Power this breaker pulls (or will pull) while ON (W).

        Motor loads draw ``peak_load_W`` during their first ``motor_peak_minutes``
        after switch-on (inrush phase), then settle to ``mean_load_W``. For a
        breaker that is OFF and being considered for switch-on, the inrush
        phase is still ahead, so the peak applies.
        """
        in_peak_phase = (
            self.load_type == 'motor'
            and (self.minutes_since_on is None or self.minutes_since_on < motor_peak_minutes)
        )  # True while the motor inrush phase applies (flag)
        if in_peak_phase and self.peak_load_W is not None:
            return self.peak_load_W
        if self.mean_load_W is not None:
            return self.mean_load_W
        if self.cur_power_W is not None:
            return self.cur_power_W
        return 0.0

    def in_schedule_window(self, local_t):
        """True when the local clock time is inside this breaker's daily schedule window (flag)."""
        return in_window(local_t, self.cycle_start, self.cycle_end)

    def in_usage_window(self, local_t):
        """True when the user normally uses this breaker at this local time (flag).

        A breaker without a configured window counts as always-in-use. Loads
        outside their usage window are preferred shedding candidates — the
        user is not using them right now anyway.
        """
        if self.cycle_start is None or self.cycle_end is None:
            return True
        return in_window(local_t, self.cycle_start, self.cycle_end)


@dataclass
class SystemFacts:
    """One immutable snapshot of the whole site, ready for the rule layer."""

    organization_id: int             # Organization primary key (unitless)
    now: datetime                    # cycle wall-clock time (UTC timestamp)
    local_time: time                 # cycle time on the site's local clock (local clock time)

    is_daytime: bool                 # True between day_start/sunrise and day_end/sunset (flag)
    season: str                      # meteorological season: 'winter'|'spring'|'summer'|'autumn'
    weather_condition: str | None    # condition from the weather API; None = API not available
    power_saving: bool               # user-selected power-saving mode (flag)
    event_upcoming: bool             # a scheduled event is active or starts within event_prep_hours (flag)
    stability_threshold_percent: float  # battery threshold active THIS cycle: normal or event-raised (% of capacity)

    battery_capacity_percent: float | None  # battery state of charge (% of capacity)
    battery_remaining_Wh: float      # usable energy left in the battery right now (Wh)
    battery_stable: bool             # battery_capacity_percent >= stability_threshold_percent (flag)
    battery_voltage_V: float | None  # battery bank voltage (V)
    battery_low: bool                # battery voltage within battery_low_margin_V of the configured floor AND not charging -> countdown protection needed (flag)
    battery_draw_W: float            # power currently drained from the battery (W)
    battery_buffer_Wh: float         # energy the site may still spend after the low trigger, before breakers flip OFF (Wh)

    grid_breaker_on: bool            # the site's AC-grid breaker is currently closed (flag)
    grid_energized: bool             # the inverter senses real grid voltage -> the state grid is delivering (flag)
    grid_failed: bool                # grid breaker ON but no grid voltage: the state grid is out (flag)

    heatsink_temp_C: float | None    # inverter heatsink temperature (°C)
    heat_high: bool                  # heatsink temperature at/above the protection limit (flag)
    joule_deficit_J: float           # cumulative (load - PV) energy over the deficit window (J)
    deficit_high: bool               # joule deficit at/above the protection limit (flag)
    overload: bool                   # current load at/above what the inverter can sustain -> shedding is the only real fix (flag)

    pv_power_W: float                # current PV production (W)
    pv_baseline_W: float | None      # recent PV baseline, latest sample excluded (W)
    sudden_pv_drop: bool             # PV fell suddenly below its baseline (flag)

    load_power_W: float              # current total AC load on the inverter (W)
    load_baseline_W: float | None    # recent load baseline, latest sample excluded (W)
    sudden_draw: bool                # load jumped suddenly above its baseline (flag)
    sudden_draw_culprit_id: int | None  # Breaker pk with the largest recent power jump; None = unknown (unitless)

    mean_load_on_W: float            # summed steady draw of all currently-ON loads, ac_grid excluded (W)
    headroom_W: float                # AC power the inverter can still supply on top of the current load (W)
    max_inverter_power_W: float      # maximum continuous AC output the inverter tolerates (W)

    hours_to_morning: float          # hours until day_start/sunrise — the night reserve horizon (h)
    mandatory_need_Wh: float         # energy the mandatory loads need from now until morning (Wh)

    motor_peak_minutes: int          # inrush duration of motor loads (min)

    breakers: list                   # every breaker of the site as BreakerFacts (list[BreakerFacts])


def gather_facts(organization, kbs, now=None):
    """Build the ``SystemFacts`` snapshot for one site, or None when no
    inverter reading exists yet (nothing to decide on).

    organization: the site (Organization)
    kbs:          the site's engine configuration (KBSSettings)
    now:          cycle time override for tests; defaults to the current time (UTC timestamp)
    """
    now = now or timezone.now()
    local_now = timezone.localtime(now)  # site-local wall clock; TODO(timezone): per-organization timezone
    window_minutes = max(kbs.deficit_window_minutes, kbs.baseline_minutes)  # widest look-back any derivation needs (min)
    readings = list(
        Reading.objects.filter(
            organization=organization,
            timestamp__gte=now - timedelta(minutes=window_minutes),
            timestamp__lte=now,
        ).order_by('timestamp')
    )  # inverter snapshots inside the look-back window, oldest first
    if not readings:
        return None
    latest = readings[-1]  # most recent inverter snapshot

    weather = get_weather_context(
        float(organization.latitude), float(organization.longitude), local_now
    )
    day_start = weather.sunrise or kbs.day_start  # start of daytime: API sunrise, else configured fallback (local clock time)
    day_end = weather.sunset or kbs.day_end       # end of daytime: API sunset, else configured fallback (local clock time)

    pv_now_W = _pv_power_W(latest)                              # current PV production (W)
    load_now_W = latest.ac_output_active_power_W or 0.0         # current total AC load (W)

    baseline_cutoff = now - timedelta(minutes=kbs.baseline_minutes)  # start of the baseline window (UTC timestamp)
    baseline_rows = [r for r in readings if r.timestamp >= baseline_cutoff and r is not latest]  # baseline samples, latest excluded
    pv_baseline_W = mean([_pv_power_W(r) for r in baseline_rows])                 # recent PV average (W)
    load_baseline_W = mean([r.ac_output_active_power_W for r in baseline_rows])   # recent load average (W)

    deficit_cutoff = now - timedelta(minutes=kbs.deficit_window_minutes)  # start of the deficit window (UTC timestamp)
    deficit_J = joule_deficit_J([
        (r.timestamp, r.ac_output_active_power_W, _pv_power_W(r))
        for r in readings if r.timestamp >= deficit_cutoff
    ])  # cumulative energy drawn beyond PV production (J)

    active_event = organization.scheduled_events.filter(
        start_at__lte=now, end_at__gte=now,
    ).first()  # event running right now, if any
    next_event = organization.scheduled_events.filter(
        start_at__gt=now,
        start_at__lte=now + timedelta(hours=kbs.event_prep_hours),
    ).order_by('start_at').first()  # nearest event inside the preparation ramp, if any
    event_upcoming = active_event is not None or next_event is not None  # inside an event or its ramp (flag)
    if active_event is not None:
        hours_until_event = 0.0  # event already running -> full event threshold (h)
    elif next_event is not None:
        hours_until_event = (next_event.start_at - now).total_seconds() / 3600.0  # time left on the ramp (h)
    else:
        hours_until_event = None  # no event in sight
    threshold_percent = ramped_threshold(
        kbs.stability_threshold_percent,
        kbs.event_stability_threshold_percent,
        hours_until_event,
        kbs.event_prep_hours,
    )  # stability threshold active this cycle: ramps from normal to event level over the prep window (% of capacity)

    required_ids = (
        set(active_event.required_breakers.values_list('id', flat=True))
        if active_event is not None else set()
    )  # breakers the running event needs ON (Breaker pks)
    breaker_facts = [
        _breaker_facts(b, now, required_ids) for b in
        organization.breakers.select_related('status').all()
    ]  # snapshot of every breaker at the site

    battery_percent = latest.battery_capacity_percent  # state of charge (% of capacity)
    hours_to_morning = hours_until(local_now, day_start)  # night reserve horizon (h)
    mandatory_need_Wh = sum(
        b.expected_draw_W(kbs.motor_peak_minutes) * hours_to_morning
        for b in breaker_facts
        if b.priority_type == 'mandatory' or b.event_required
    )  # energy the mandatory (and event-required) loads need until morning (Wh)

    if latest.battery_voltage_V is not None and latest.battery_discharge_current_A is not None:
        battery_draw_W = latest.battery_voltage_V * latest.battery_discharge_current_A  # measured discharge power: V x A (W)
    else:
        battery_draw_W = max(load_now_W - pv_now_W, 0.0)  # fallback estimate: load not covered by PV (W)
    battery_low = (
        latest.battery_voltage_V is not None
        and latest.battery_voltage_V <= kbs.battery_low_voltage_V + kbs.battery_low_margin_V
        and (latest.battery_charge_current_A or 0.0) <= 0.5
    )  # bank voltage close to the floor AND not charging (charge current <= 0.5 A); a charging battery recovers on its own (flag)

    grid_facts = next((b for b in breaker_facts if b.priority_type == 'ac_grid'), None)  # the site's AC-grid breaker snapshot (BreakerFacts | None)
    grid_breaker_on = bool(grid_facts and grid_facts.switch)                             # grid breaker closed (flag)
    grid_energized = (latest.grid_voltage_V or 0.0) >= kbs.grid_present_min_V            # inverter senses real grid voltage (flag)

    sudden_draw = is_sudden_draw(load_now_W, load_baseline_W, kbs.sudden_draw_W)

    return SystemFacts(
        organization_id=organization.id,
        now=now,
        local_time=local_now.time(),
        is_daytime=is_daytime(
            local_now.time(), day_start, day_end,
            pv_power_W=pv_now_W, pv_day_min_W=kbs.pv_day_min_W,
        ),
        season=weather.season,
        weather_condition=weather.condition,
        power_saving=kbs.power_saving,
        event_upcoming=event_upcoming,
        stability_threshold_percent=threshold_percent,
        battery_capacity_percent=battery_percent,
        battery_remaining_Wh=(battery_percent or 0.0) / 100.0 * kbs.battery_capacity_Wh,
        battery_stable=battery_percent is not None and battery_percent >= threshold_percent,
        battery_voltage_V=latest.battery_voltage_V,
        battery_low=battery_low,
        battery_draw_W=battery_draw_W,
        battery_buffer_Wh=kbs.battery_shutdown_buffer_percent / 100.0 * kbs.battery_capacity_Wh,
        grid_breaker_on=grid_breaker_on,
        grid_energized=grid_energized,
        grid_failed=grid_breaker_on and not grid_energized,
        heatsink_temp_C=latest.heatsink_temp_C,
        heat_high=(
            latest.heatsink_temp_C is not None
            and latest.heatsink_temp_C >= kbs.heatsink_temp_limit_C
        ),
        joule_deficit_J=deficit_J,
        deficit_high=deficit_J >= kbs.joule_deficit_limit_J,
        overload=load_now_W >= kbs.max_inverter_power_W,
        pv_power_W=pv_now_W,
        pv_baseline_W=pv_baseline_W,
        sudden_pv_drop=is_sudden_drop(pv_now_W, pv_baseline_W, kbs.sudden_drop_fraction),
        load_power_W=load_now_W,
        load_baseline_W=load_baseline_W,
        sudden_draw=sudden_draw,
        sudden_draw_culprit_id=(
            _sudden_draw_culprit_id(organization, now, kbs.baseline_minutes)
            if sudden_draw else None
        ),
        mean_load_on_W=sum(
            b.expected_draw_W(kbs.motor_peak_minutes)
            for b in breaker_facts
            if b.switch and b.priority_type != 'ac_grid'
        ),
        headroom_W=max(kbs.max_inverter_power_W - load_now_W, 0.0),
        max_inverter_power_W=kbs.max_inverter_power_W,
        hours_to_morning=hours_to_morning,
        mandatory_need_Wh=mandatory_need_Wh,
        motor_peak_minutes=kbs.motor_peak_minutes,
        breakers=breaker_facts,
    )


def facts_to_json(facts):
    """SystemFacts as a JSON-serializable dict, for the decision audit snapshot."""
    return _jsonable(asdict(facts))


def _jsonable(value):
    """Recursively convert datetimes/times to ISO strings so the dict is JSON-safe."""
    if isinstance(value, (datetime, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _pv_power_W(reading):
    """PV production of one inverter snapshot (W).

    Prefers the inverter's own charging-power register; falls back to
    PV voltage x current when that register is missing.
    """
    if reading.pv_charging_power_W is not None:
        return reading.pv_charging_power_W
    if reading.pv_input_voltage_V is not None and reading.pv_input_current_A is not None:
        return reading.pv_input_voltage_V * reading.pv_input_current_A
    return 0.0


def _breaker_facts(breaker, now, event_required_ids):
    """Build the BreakerFacts snapshot for one breaker at time ``now`` (UTC timestamp).

    event_required_ids: pks of breakers a currently running event needs ON (set)
    """
    status = getattr(breaker, 'status', None)  # latest live state; None if the device never reported
    minutes_since_on = None  # minutes since the last OFF->ON transition (min)
    if status is not None and status.switch and status.last_switched_on_at is not None:
        minutes_since_on = (now - status.last_switched_on_at).total_seconds() / 60.0
    recently_tripped = (
        not breaker.locked_out
        and breaker.locked_at is not None
        and now - breaker.locked_at < timedelta(hours=TRIP_MEMORY_HOURS)
    )  # tripped recently but the user re-enabled it -> the KBS must not trip it again (flag)
    return BreakerFacts(
        id=breaker.id,
        device_id=breaker.device_id,
        priority_type=breaker.priority_type,
        priority_degree=breaker.priority_degree,
        load_type=breaker.load_type,
        peak_load_W=breaker.peak_load_W,
        mean_load_W=breaker.mean_load_W,
        cycle_start=breaker.cycle_start,
        cycle_end=breaker.cycle_end,
        switch=status.switch if status else False,
        online=status.online if status else False,
        fault=status.fault if status else '',
        locked_out=breaker.locked_out,
        recently_tripped=recently_tripped,
        event_required=breaker.id in event_required_ids,
        cur_power_W=(
            status.cur_power_mW / 1000.0
            if status and status.cur_power_mW is not None else None
        ),  # device reports mW -> convert to W
        minutes_since_on=minutes_since_on,
    )


def _sudden_draw_culprit_id(organization, now, baseline_minutes):
    """Breaker pk whose power rose the most over the baseline window; None if unclear (unitless).

    Compares each breaker's newest sample against the average of its older
    samples inside the window — the biggest positive jump is the culprit of
    the sudden draw.
    """
    window_start = now - timedelta(minutes=baseline_minutes)  # start of the comparison window (UTC timestamp)
    rows = (
        BreakerReading.objects
        .filter(breaker__organization=organization, timestamp__gte=window_start, timestamp__lte=now)
        .order_by('timestamp')
        .values_list('breaker_id', 'cur_power_mW')
    )
    by_breaker = {}  # breaker_id -> chronological list of power samples (mW)
    for breaker_id, power_mW in rows:
        by_breaker.setdefault(breaker_id, []).append(power_mW)

    culprit_id = None   # best candidate so far (Breaker pk)
    biggest_jump_W = 0.0  # its power jump (W)
    for breaker_id, powers in by_breaker.items():
        if len(powers) < 2:
            continue
        earlier_mW = mean(powers[:-1])       # average draw before the newest sample (mW)
        latest_mW = powers[-1]               # newest draw (mW)
        if earlier_mW is None or latest_mW is None:
            continue
        jump_W = (latest_mW - earlier_mW) / 1000.0  # rise of this breaker's draw (W)
        if jump_W > biggest_jump_W:
            biggest_jump_W = jump_W
            culprit_id = breaker_id
    return culprit_id
