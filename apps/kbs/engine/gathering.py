"""Fact gathering: turn the latest telemetry + breaker state into the facts
the Experta engine reasons over.

All database access, clock reads and unit conversions happen here and only
here. ``gather_facts()`` returns a list of ready-to-declare facts — one
``SystemFact`` followed by one ``BreakerFact`` per breaker — so the rule layer
stays a pure function of working memory.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.breakers.models import Breaker, BreakerReading
from apps.telemetry.models import Reading

from .derived import (
    expected_draw_W,
    hours_until,
    in_window,
    is_daytime,
    is_sudden_draw,
    is_sudden_drop,
    joule_deficit_J,
    mean,
    ramped_threshold,
)
from .facts import (
    GRID_TYPE,
    SHEDDABLE_TYPES,
    BreakerFact,
    SystemFact,
    breaker_facts,
    system_fact,
)
from .weather import get_weather_context

TRIP_MEMORY_HOURS = 12  # how long a KBS trip is remembered after the user re-enables the breaker, so one night never trips the same breaker twice (h)


def gather_facts(organization, kbs, now=None):
    """Snapshot one site as facts; None when there is no telemetry to reason on.

    organization: the site to snapshot (Organization)
    kbs:          the site's engine configuration (KBSSettings)
    now:          cycle time override for tests/simulation (UTC timestamp)

    returns: [SystemFact, BreakerFact, ...] ready for ``decide()``
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
    breakers = [
        _breaker_fact(b, now, local_now.time(), required_ids, kbs.motor_peak_minutes)
        for b in organization.breakers.select_related('status').all()
    ]  # snapshot of every breaker at the site (list[BreakerFact])

    battery_percent = latest.battery_capacity_percent  # state of charge (% of capacity)
    hours_to_morning = hours_until(local_now, day_start)  # night reserve horizon (h)
    mandatory_need_Wh = sum(
        b['expected_draw_W'] * hours_to_morning
        for b in breakers
        if b['priority_type'] == 'mandatory' or b['event_required']
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

    grid = next((b for b in breakers if b['priority_type'] == GRID_TYPE), None)  # the site's AC-grid breaker snapshot (BreakerFact | None)
    grid_breaker_on = bool(grid and grid['switch'])                              # grid breaker closed (flag)
    grid_energized = (latest.grid_voltage_V or 0.0) >= kbs.grid_present_min_V    # inverter senses real grid voltage (flag)

    sudden_draw = is_sudden_draw(load_now_W, load_baseline_W, kbs.sudden_draw_W)

    system = SystemFact(
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
            b['expected_draw_W'] for b in breakers
            if b['switch'] and b['priority_type'] != GRID_TYPE
        ),
        headroom_W=max(kbs.max_inverter_power_W - load_now_W, 0.0),
        max_inverter_power_W=kbs.max_inverter_power_W,
        hours_to_morning=hours_to_morning,
        mandatory_need_Wh=mandatory_need_Wh,
        motor_peak_minutes=kbs.motor_peak_minutes,
    )
    return [system, *breakers]


def facts_to_json(facts):
    """The gathered snapshot as a JSON-serializable dict, for the decision audit.

    facts: the fact list returned by ``gather_facts`` (list[Fact])
    """
    system = system_fact(facts)
    return _jsonable({
        'system': system.as_dict() if system is not None else {},
        'breakers': [b.as_dict() for b in breaker_facts(facts)],
    })


def _jsonable(value):
    """Recursively convert datetimes/times to ISO strings so the dict is JSON-safe."""
    if isinstance(value, (datetime, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
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


def _breaker_fact(breaker, now, local_t, event_required_ids, motor_peak_minutes):
    """Build the BreakerFact for one breaker at time ``now`` (UTC timestamp).

    breaker:            the breaker row, with its ``status`` pre-selected (Breaker)
    local_t:            cycle time on the site's local clock (local clock time)
    event_required_ids: pks of breakers a currently running event needs ON (set)
    motor_peak_minutes: how long a motor load draws its peak after switch-on (min)
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
    cur_power_W = (
        status.cur_power_mW / 1000.0
        if status and status.cur_power_mW is not None else None
    )  # device reports mW -> convert to W
    fault = status.fault if status else ''      # device fault flags; empty = healthy (text)
    online = status.online if status else False  # breaker reachable (flag)
    event_required = breaker.id in event_required_ids  # a running event needs this breaker ON (flag)
    return BreakerFact(
        id=breaker.id,
        device_id=breaker.device_id,
        priority_type=breaker.priority_type,
        priority_degree=breaker.priority_degree,
        category_rank=Breaker.CATEGORY_RANK.get(breaker.priority_type, 0),
        load_type=breaker.load_type,
        peak_load_W=breaker.peak_load_W,
        mean_load_W=breaker.mean_load_W,
        cur_power_W=cur_power_W,
        expected_draw_W=expected_draw_W(
            breaker.load_type, minutes_since_on, motor_peak_minutes,
            breaker.peak_load_W, breaker.mean_load_W, cur_power_W,
        ),
        cycle_start=breaker.cycle_start,
        cycle_end=breaker.cycle_end,
        in_schedule_window=in_window(local_t, breaker.cycle_start, breaker.cycle_end),
        # A breaker without a configured window counts as always-in-use: loads
        # outside their usage window are preferred shedding candidates.
        in_usage_window=(
            True if breaker.cycle_start is None or breaker.cycle_end is None
            else in_window(local_t, breaker.cycle_start, breaker.cycle_end)
        ),
        switch=status.switch if status else False,
        online=online,
        fault=fault,
        healthy=online and not fault,
        minutes_since_on=minutes_since_on,
        locked_out=breaker.locked_out,
        recently_tripped=recently_tripped,
        event_required=event_required,
        sheddable=breaker.priority_type in SHEDDABLE_TYPES and not event_required,
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
