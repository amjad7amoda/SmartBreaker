"""Pure numeric helpers that turn raw telemetry windows into derived facts.

No Django imports here — everything operates on plain values so it is easy to
unit-test and to reuse on the Raspberry Pi emergency KBS later.
"""

from datetime import datetime, time, timedelta


def mean(values):
    """Arithmetic mean of a list of numbers; None if the list is empty (same unit as input)."""
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def joule_deficit_J(samples):
    """Cumulative energy the loads drew beyond what the panels produced (J).

    ``samples`` is a chronologically ordered list of tuples
    ``(timestamp, load_W, pv_W)``:
      - timestamp: sample time (datetime)
      - load_W:    total AC load at that moment (W)
      - pv_W:      PV production at that moment (W)

    Integrates ``max(load_W - pv_W, 0)`` over time with the trapezoid rule.
    Only the deficit counts: surplus moments do not cancel earlier deficits,
    because the deficit is what stresses the battery/inverter.
    """
    total_J = 0.0  # accumulated deficit energy (J)
    for (t0, load0, pv0), (t1, load1, pv1) in zip(samples, samples[1:]):
        d0 = max((load0 or 0.0) - (pv0 or 0.0), 0.0)  # deficit power at segment start (W)
        d1 = max((load1 or 0.0) - (pv1 or 0.0), 0.0)  # deficit power at segment end (W)
        dt_s = (t1 - t0).total_seconds()              # segment duration (s)
        if dt_s > 0:
            total_J += (d0 + d1) / 2.0 * dt_s         # trapezoid: W * s = J
    return total_J


def is_sudden_drop(current_W, baseline_W, drop_fraction, min_baseline_W=100.0):
    """True when a power signal fell 'suddenly' below its recent baseline (flag).

    current_W:      latest value of the signal (W)
    baseline_W:     recent average of the signal, excluding the latest value (W)
    drop_fraction:  relative drop that counts as sudden, e.g. 0.4 = 40% (fraction, 0-1)
    min_baseline_W: baselines below this are noise, never 'sudden' (W)
    """
    if baseline_W is None or baseline_W < min_baseline_W:
        return False
    return (current_W or 0.0) <= baseline_W * (1.0 - drop_fraction)


def is_sudden_draw(current_W, baseline_W, jump_W):
    """True when total load jumped 'suddenly' above its recent baseline (flag).

    current_W:  latest total load (W)
    baseline_W: recent average load, excluding the latest value (W)
    jump_W:     absolute jump that counts as sudden (W)
    """
    if baseline_W is None:
        return False
    return (current_W or 0.0) - baseline_W >= jump_W


def season_at(month, latitude_deg):
    """Meteorological season at the site ('winter'|'spring'|'summer'|'autumn').

    month:        calendar month, 1-12
    latitude_deg: site latitude; negative = southern hemisphere (degrees)
    """
    northern = {12: 'winter', 1: 'winter', 2: 'winter',
                3: 'spring', 4: 'spring', 5: 'spring',
                6: 'summer', 7: 'summer', 8: 'summer',
                9: 'autumn', 10: 'autumn', 11: 'autumn'}
    season = northern[month]
    if latitude_deg < 0:  # southern hemisphere: seasons are flipped
        season = {'winter': 'summer', 'summer': 'winter',
                  'spring': 'autumn', 'autumn': 'spring'}[season]
    return season


def is_daytime(local_t, day_start, day_end, pv_power_W=0.0, pv_day_min_W=None):
    """True when it is daytime: the clock window says so OR the panels produce (flag).

    Combining both signals means a storm that zeroes PV during the clock-day
    still counts as day (no false switch to the night logic), and panels
    producing before the configured day_start still count as day.

    local_t:      current local clock time (time)
    day_start:    start of daytime, e.g. sunrise (local clock time)
    day_end:      end of daytime, e.g. sunset (local clock time)
    pv_power_W:   current PV production (W)
    pv_day_min_W: production at/above which the panels prove daylight; None disables the PV signal (W)
    """
    if in_window(local_t, day_start, day_end):
        return True
    return pv_day_min_W is not None and pv_power_W >= pv_day_min_W


def ramped_threshold(normal_percent, event_percent, hours_until_event, prep_hours):
    """Battery stability threshold while approaching a scheduled event (% of capacity).

    Ramps linearly from the normal threshold up to the event threshold over
    the preparation window, so the system hoards energy gradually instead of
    jumping the target at once:
      event still prep_hours away -> normal_percent
      event starting/active       -> event_percent

    normal_percent:    everyday stability threshold (% of capacity)
    event_percent:     threshold wanted by the time the event starts (% of capacity)
    hours_until_event: time left until the event starts; <=0 = active; None = no event (h)
    prep_hours:        length of the preparation ramp before the event (h)
    """
    if hours_until_event is None:
        return normal_percent
    if prep_hours <= 0 or hours_until_event <= 0:
        return event_percent
    progress = min(max(1.0 - hours_until_event / prep_hours, 0.0), 1.0)  # 0 = ramp start, 1 = event start (fraction)
    return normal_percent + (event_percent - normal_percent) * progress


def graceful_countdown_s(buffer_Wh, draw_W, min_s=60, max_s=3600):
    """Seconds a breaker may stay ON before its countdown flips it OFF, such
    that the battery only spends the allowed buffer energy meanwhile (s).

    Example: buffer 2% of a 5000 Wh bank = 100 Wh at a 1200 W draw ->
    100 / 1200 * 3600 = 300 s of remaining usage before shutdown.

    buffer_Wh: battery energy the site tolerates spending before shutdown (Wh)
    draw_W:    current battery discharge power (W); <=0 = not draining
    min_s:     shortest countdown the devices get, so the user has warning time (s)
    max_s:     longest useful countdown; also used when the battery is not draining (s)
    """
    if draw_W <= 0:
        return max_s
    return int(min(max(buffer_Wh / draw_W * 3600.0, min_s), max_s))


def hours_until(local_now, target):
    """Hours from a local clock time until the next occurrence of ``target`` (h).

    local_now: current local time (datetime)
    target:    target local clock time, e.g. day_start/sunrise (time)
    """
    candidate = local_now.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )  # today's occurrence of the target clock time (datetime)
    if candidate <= local_now:
        candidate += timedelta(days=1)  # already passed today -> next occurrence is tomorrow
    return (candidate - local_now).total_seconds() / 3600.0


def in_window(local_t, start, end):
    """True when a local clock time falls inside a daily window, handling midnight wrap (flag).

    local_t: time to test (local clock time)
    start:   window start (local clock time)
    end:     window end (local clock time); end < start means the window wraps midnight
    """
    if start is None or end is None:
        return False
    if start <= end:
        return start <= local_t < end
    return local_t >= start or local_t < end  # wraps past midnight
