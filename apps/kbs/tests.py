"""Unit tests for the KBS decision tree.

``decide()`` is a pure function of ``SystemFacts``, so every flowchart branch
is exercised here with fabricated facts — no database, no clock, no network.
"""

from datetime import datetime, time, timezone as dt_timezone

from django.test import SimpleTestCase

from .engine.derived import graceful_countdown_s, is_daytime, ramped_threshold
from .engine.facts import BreakerFacts, SystemFacts
from .engine.rules import decide

UTC = dt_timezone.utc


def make_breaker(**overrides):
    """A BreakerFacts with sensible defaults, overridable per test."""
    values = dict(
        id=1, device_id='b1',
        priority_type='normal',  # importance category
        priority_degree=1,       # importance inside the category (unitless)
        load_type='normal',      # electrical profile
        peak_load_W=None,        # learned peak draw (W)
        mean_load_W=100.0,       # learned steady draw (W)
        cycle_start=None,        # schedule window start (local clock time)
        cycle_end=None,          # schedule window end (local clock time)
        switch=True,             # relay ON (flag)
        online=True,             # reachable (flag)
        fault='',                # healthy (text)
        locked_out=False,        # not tripped (flag)
        recently_tripped=False,  # no trip memory (flag)
        event_required=False,    # no running event needs it (flag)
        cur_power_W=100.0,       # instantaneous draw (W)
        minutes_since_on=60.0,   # long past any motor peak (min)
    )
    values.update(overrides)
    return BreakerFacts(**values)


def make_facts(breakers, **overrides):
    """A SystemFacts snapshot describing a healthy summer noon, overridable per test."""
    values = dict(
        organization_id=1,
        now=datetime(2026, 7, 30, 12, 0, tzinfo=UTC),  # cycle time (UTC timestamp)
        local_time=time(12, 0),          # site-local clock (local clock time)
        is_daytime=True,                 # noon (flag)
        season='summer',
        weather_condition=None,
        power_saving=False,
        event_upcoming=False,
        stability_threshold_percent=50.0,  # active battery threshold (% of capacity)
        battery_capacity_percent=80.0,     # state of charge (% of capacity)
        battery_remaining_Wh=4000.0,       # energy left (Wh)
        battery_stable=True,
        battery_voltage_V=26.5,            # healthy bank voltage (V)
        battery_low=False,                 # not near the floor (flag)
        battery_draw_W=0.0,                # nothing drained from the battery (W)
        battery_buffer_Wh=100.0,           # 2% of a 5000 Wh bank (Wh)
        grid_breaker_on=False,             # AC-grid breaker open (flag)
        grid_energized=False,              # no grid voltage sensed (flag)
        grid_failed=False,                 # no outage condition (flag)
        heatsink_temp_C=40.0,            # cool inverter (°C)
        heat_high=False,
        joule_deficit_J=0.0,             # no deficit (J)
        deficit_high=False,
        overload=False,                  # current load within the inverter rating (flag)
        pv_power_W=3000.0,               # strong production (W)
        pv_baseline_W=3000.0,            # steady baseline (W)
        sudden_pv_drop=False,
        load_power_W=1000.0,             # current total load (W)
        load_baseline_W=1000.0,          # steady baseline (W)
        sudden_draw=False,
        sudden_draw_culprit_id=None,
        mean_load_on_W=1000.0,           # summed steady draw of ON loads (W)
        headroom_W=4000.0,               # inverter spare capacity (W)
        max_inverter_power_W=5000.0,     # inverter rating (W)
        hours_to_morning=0.0,            # irrelevant at noon (h)
        mandatory_need_Wh=0.0,           # irrelevant at noon (Wh)
        motor_peak_minutes=20,           # motor inrush duration (min)
        breakers=breakers,
    )
    values.update(overrides)
    return SystemFacts(**values)


def actions_by_device(result):
    """Map device_id -> ActionIntent for compact assertions."""
    return {a.device_id: a for a in result.actions}


class ProtectInverterTests(SimpleTestCase):

    def test_overload_sheds_comfort_then_normal_never_mandatory(self):
        breakers = [
            make_breaker(id=1, device_id='server', priority_type='mandatory'),
            make_breaker(id=2, device_id='fridge', priority_type='normal', priority_degree=5),
            make_breaker(id=3, device_id='tv', priority_type='comfort', priority_degree=2),
            make_breaker(id=4, device_id='lights', priority_type='comfort', priority_degree=7),
            make_breaker(id=5, device_id='grid', priority_type='ac_grid', switch=False),
        ]
        result = decide(make_facts(
            breakers, heat_high=True, heatsink_temp_C=85.0,
            overload=True, load_power_W=5250.0,
        ))

        self.assertEqual(result.branch, 'protect_inverter.overload')
        off_devices = [a.device_id for a in result.actions if a.action == 'off']
        # comfort first (lowest degree first), then normal; mandatory untouched
        self.assertEqual(off_devices, ['tv', 'lights', 'fridge'])
        # the AC-grid breaker is the inverter's own input, not a separate feed
        # to the loads -- switching it on would add current, not remove it.
        self.assertNotIn('grid', actions_by_device(result))
        self.assertEqual(result.alerts[0].kind, 'inverter_protection')
        self.assertEqual(result.alerts[0].severity, 'critical')

    def test_heat_high_without_overload_alerts_but_does_not_shed(self):
        breakers = [
            make_breaker(id=1, device_id='tv', priority_type='comfort'),
        ]
        result = decide(make_facts(breakers, heat_high=True, heatsink_temp_C=85.0))

        # current load is within the rating: shedding would not cool the unit
        off_devices = [a.device_id for a in result.actions if a.action == 'off']
        self.assertNotIn('tv', off_devices)
        self.assertEqual(result.alerts[0].kind, 'inverter_protection')
        self.assertEqual(result.alerts[0].severity, 'critical')
        self.assertIn('cooling', result.alerts[0].message.lower())

    def test_deficit_alone_without_overload_falls_through_to_day_night(self):
        result = decide(make_facts([], deficit_high=True, joule_deficit_J=4_000_000.0))
        # a trailing energy deficit with no live overload isn't an emergency:
        # the cycle proceeds to the normal day/night branch (noon, PV surplus)
        self.assertEqual(result.branch, 'day.surplus.comfort_on')
        self.assertFalse(any(a.kind == 'inverter_protection' for a in result.alerts))

    def test_out_of_usage_window_loads_shed_first(self):
        breakers = [
            # same category and degree; 'inuse' is inside its usage window, 'idle' is not
            make_breaker(id=1, device_id='inuse', priority_type='comfort', priority_degree=3,
                         cycle_start=time(8, 0), cycle_end=time(20, 0)),
            make_breaker(id=2, device_id='idle', priority_type='comfort', priority_degree=3,
                         cycle_start=time(20, 0), cycle_end=time(23, 0)),
        ]
        result = decide(make_facts(breakers, heat_high=True, overload=True, load_power_W=5250.0))
        off_devices = [a.device_id for a in result.actions if a.action == 'off']
        self.assertEqual(off_devices, ['idle', 'inuse'])


class BatteryProtectionTests(SimpleTestCase):

    def low_battery_facts(self, breakers, **overrides):
        values = dict(
            battery_low=True, battery_voltage_V=24.4,  # within margin of the 24.0 V floor (V)
            battery_draw_W=1200.0,                     # current discharge power (W)
            battery_buffer_Wh=100.0,                   # 2% of a 5000 Wh bank (Wh)
        )
        values.update(overrides)
        return make_facts(breakers, **values)

    def test_countdown_shutdown_sized_by_buffer_over_draw(self):
        breakers = [
            make_breaker(id=1, device_id='server', priority_type='mandatory'),
            make_breaker(id=2, device_id='tv', priority_type='comfort'),
            make_breaker(id=3, device_id='fridge', priority_type='normal'),
            make_breaker(id=4, device_id='grid', priority_type='ac_grid', switch=False),
        ]
        result = decide(self.low_battery_facts(breakers))

        self.assertEqual(result.branch, 'protect_battery')
        by_device = actions_by_device(result)
        # 100 Wh buffer / 1200 W draw = 300 s countdown on every sheddable load
        self.assertEqual(by_device['tv'].action, 'off')
        self.assertEqual(by_device['tv'].countdown_s, 300)
        self.assertEqual(by_device['fridge'].countdown_s, 300)
        self.assertNotIn('server', by_device)              # mandatory never touched
        self.assertEqual(by_device['grid'].action, 'on')   # grid takes over (no power saving)
        self.assertEqual(result.alerts[0].kind, 'battery_low')
        self.assertIn('tv', result.alerts[0].message)      # user is told which breakers switch off

    def test_power_saving_does_not_buy_grid(self):
        breakers = [
            make_breaker(id=2, device_id='tv', priority_type='comfort'),
            make_breaker(id=4, device_id='grid', priority_type='ac_grid', switch=False),
        ]
        result = decide(self.low_battery_facts(breakers, power_saving=True))
        self.assertNotIn('grid', actions_by_device(result))
        self.assertEqual(actions_by_device(result)['tv'].countdown_s, 300)


class ScheduledEventTests(SimpleTestCase):

    def test_event_required_breaker_is_never_shed(self):
        breakers = [
            make_breaker(id=1, device_id='projector', priority_type='comfort', event_required=True),
            make_breaker(id=2, device_id='tv', priority_type='comfort'),
        ]
        result = decide(make_facts(breakers, heat_high=True, overload=True, load_power_W=5250.0))
        off_devices = [a.device_id for a in result.actions if a.action == 'off']
        self.assertEqual(off_devices, ['tv'])  # the event load survives even emergency shedding

    def test_event_required_breaker_is_switched_on(self):
        breakers = [
            make_breaker(id=1, device_id='projector', priority_type='comfort',
                         switch=False, event_required=True, mean_load_W=200.0),
        ]
        result = decide(make_facts(breakers, pv_power_W=4000.0, mean_load_on_W=500.0))
        on = actions_by_device(result)['projector']
        self.assertEqual(on.action, 'on')
        self.assertIn('event', on.reason)

    def test_event_load_counts_into_power_saving_budget(self):
        breakers = [
            make_breaker(id=1, device_id='projector', priority_type='comfort',
                         event_required=True, mean_load_W=600.0),
            make_breaker(id=2, device_id='tv', priority_type='comfort', mean_load_W=500.0),
        ]
        # budget = PV 1000 W - event load 600 W = 400 W -> tv (500 W) must shed
        result = decide(make_facts(
            breakers, pv_power_W=1000.0, mean_load_on_W=1100.0,
            battery_stable=False, battery_capacity_percent=30.0, power_saving=True,
        ))
        by_device = actions_by_device(result)
        self.assertEqual(by_device['tv'].action, 'off')
        self.assertNotIn('projector', by_device)


class GridOutageTests(SimpleTestCase):

    def test_day_deficit_with_dead_grid_sheds_even_without_saving(self):
        breakers = [
            make_breaker(id=1, device_id='server', priority_type='mandatory', mean_load_W=300.0),
            make_breaker(id=2, device_id='fridge', priority_type='normal', priority_degree=5, mean_load_W=400.0),
            make_breaker(id=3, device_id='tv', priority_type='comfort', priority_degree=2, mean_load_W=500.0),
            make_breaker(id=4, device_id='grid', priority_type='ac_grid', switch=True),
        ]
        # grid breaker already ON but the state grid delivers nothing
        result = decide(make_facts(
            breakers, pv_power_W=200.0, mean_load_on_W=1200.0,
            battery_stable=False, battery_capacity_percent=30.0, power_saving=False,
            grid_breaker_on=True, grid_energized=False, grid_failed=True,
        ))
        self.assertEqual(result.branch, 'day.deficit.grid_out.shed')
        by_device = actions_by_device(result)
        # budget = PV 200 W - mandatory 300 W = 0 -> everything sheddable goes off, comfort first
        off_devices = [a.device_id for a in result.actions if a.action == 'off']
        self.assertEqual(off_devices, ['tv', 'fridge'])
        self.assertNotIn('grid', by_device)        # grid breaker deliberately stays ON
        self.assertNotIn('server', by_device)      # mandatory untouched
        self.assertEqual(result.alerts[0].kind, 'grid_outage')

    def test_day_deficit_with_live_grid_still_buys(self):
        breakers = [make_breaker(id=4, device_id='grid', priority_type='ac_grid', switch=False)]
        result = decide(make_facts(
            breakers, pv_power_W=200.0, mean_load_on_W=1200.0,
            battery_stable=False, battery_capacity_percent=30.0, power_saving=False,
        ))
        self.assertEqual(result.branch, 'day.deficit.buy_grid')
        self.assertEqual(actions_by_device(result)['grid'].action, 'on')

    def test_night_reserve_short_with_dead_grid_sheds(self):
        breakers = [
            make_breaker(id=1, device_id='heater', priority_type='comfort', mean_load_W=2000.0),
            make_breaker(id=4, device_id='grid', priority_type='ac_grid', switch=True),
        ]
        result = decide(make_facts(
            breakers, is_daytime=False, local_time=time(23, 0),
            pv_power_W=0.0, pv_baseline_W=0.0,
            sudden_draw=True, hours_to_morning=7.0,
            mandatory_need_Wh=2100.0, battery_remaining_Wh=1000.0,
            power_saving=False,
            grid_breaker_on=True, grid_energized=False, grid_failed=True,
        ))
        self.assertEqual(result.branch, 'night.sudden_draw.grid_out.shed')
        self.assertEqual(actions_by_device(result)['heater'].action, 'off')
        self.assertNotIn('grid', actions_by_device(result))

    def test_calm_night_keeps_needed_grid_on(self):
        # grid ON and delivering, reserve still short -> the engine must NOT switch it off
        breakers = [make_breaker(id=4, device_id='grid', priority_type='ac_grid', switch=True)]
        result = decide(make_facts(
            breakers, is_daytime=False, local_time=time(23, 0),
            pv_power_W=0.0, pv_baseline_W=0.0,
            hours_to_morning=7.0, mandatory_need_Wh=2100.0, battery_remaining_Wh=1000.0,
            grid_breaker_on=True, grid_energized=True,
        ))
        self.assertEqual(result.branch, 'night.calm.battery')
        self.assertNotIn('grid', actions_by_device(result))

    def test_calm_night_with_enough_reserve_switches_grid_off(self):
        breakers = [make_breaker(id=4, device_id='grid', priority_type='ac_grid', switch=True)]
        result = decide(make_facts(
            breakers, is_daytime=False, local_time=time(23, 0),
            pv_power_W=0.0, pv_baseline_W=0.0,
            hours_to_morning=7.0, mandatory_need_Wh=2100.0, battery_remaining_Wh=4000.0,
            grid_breaker_on=True, grid_energized=True,
        ))
        self.assertEqual(actions_by_device(result)['grid'].action, 'off')


class DerivedTests(SimpleTestCase):

    def test_ramped_threshold_interpolates_over_prep_window(self):
        self.assertEqual(ramped_threshold(50.0, 80.0, None, 24.0), 50.0)   # no event
        self.assertEqual(ramped_threshold(50.0, 80.0, 24.0, 24.0), 50.0)   # ramp just started
        self.assertEqual(ramped_threshold(50.0, 80.0, 12.0, 24.0), 65.0)   # halfway
        self.assertEqual(ramped_threshold(50.0, 80.0, 0.0, 24.0), 80.0)    # event active

    def test_graceful_countdown_clamps(self):
        self.assertEqual(graceful_countdown_s(100.0, 1200.0), 300)  # 100 Wh / 1200 W = 300 s
        self.assertEqual(graceful_countdown_s(100.0, 0.0), 3600)    # not draining -> max
        self.assertEqual(graceful_countdown_s(1.0, 5000.0), 60)     # tiny buffer -> min warning time

    def test_is_daytime_clock_or_pv(self):
        day_start, day_end = time(6, 0), time(18, 0)
        self.assertTrue(is_daytime(time(12, 0), day_start, day_end, 0.0, 10.0))    # clock day, storm zeroed PV
        self.assertTrue(is_daytime(time(5, 30), day_start, day_end, 200.0, 10.0))  # pre-dawn but panels produce
        self.assertFalse(is_daytime(time(23, 0), day_start, day_end, 0.0, 10.0))   # night


class DaytimeTests(SimpleTestCase):

    def test_surplus_turns_on_scheduled_comfort_within_headroom(self):
        breakers = [
            make_breaker(id=1, device_id='ac', priority_type='comfort', priority_degree=9,
                         load_type='motor', switch=False, peak_load_W=1500.0, mean_load_W=800.0,
                         minutes_since_on=None,
                         cycle_start=time(10, 0), cycle_end=time(16, 0)),
            make_breaker(id=2, device_id='pool', priority_type='comfort', priority_degree=1,
                         switch=False, mean_load_W=3000.0,
                         cycle_start=time(10, 0), cycle_end=time(16, 0)),
            make_breaker(id=3, device_id='grid', priority_type='ac_grid', switch=True),
        ]
        # headroom 4000 W: 'ac' enters at its 1500 W peak, 'pool' (3000 W) no longer fits
        result = decide(make_facts(breakers, pv_power_W=4000.0, mean_load_on_W=500.0))

        self.assertEqual(result.branch, 'day.surplus.comfort_on')
        by_device = actions_by_device(result)
        self.assertEqual(by_device['ac'].action, 'on')
        self.assertNotIn('pool', by_device)          # over head-room, waits for a later cycle
        self.assertEqual(by_device['grid'].action, 'off')  # no need to buy grid power

    def test_comfort_outside_schedule_window_stays_off(self):
        breakers = [
            make_breaker(id=1, device_id='tv', priority_type='comfort', switch=False,
                         cycle_start=time(18, 0), cycle_end=time(23, 0)),
        ]
        result = decide(make_facts(breakers, pv_power_W=4000.0, mean_load_on_W=500.0))
        self.assertEqual(result.actions, [])

    def test_deficit_without_saving_buys_grid(self):
        breakers = [make_breaker(id=1, device_id='grid', priority_type='ac_grid', switch=False)]
        result = decide(make_facts(
            breakers, pv_power_W=200.0, mean_load_on_W=1500.0,
            battery_stable=False, battery_capacity_percent=30.0,
        ))
        self.assertEqual(result.branch, 'day.deficit.buy_grid')
        self.assertEqual(actions_by_device(result)['grid'].action, 'on')

    def test_deficit_with_saving_keeps_best_subset(self):
        breakers = [
            make_breaker(id=1, device_id='server', priority_type='mandatory', mean_load_W=300.0),
            make_breaker(id=2, device_id='fridge', priority_type='normal', priority_degree=5, mean_load_W=400.0),
            make_breaker(id=3, device_id='tv', priority_type='comfort', priority_degree=2, mean_load_W=500.0),
        ]
        # budget = PV 1000 W - mandatory 300 W = 700 W -> fridge (400 W) stays, tv (500 W) shed
        result = decide(make_facts(
            breakers, pv_power_W=1000.0, mean_load_on_W=1200.0,
            battery_stable=False, battery_capacity_percent=30.0, power_saving=True,
        ))
        self.assertEqual(result.branch, 'day.deficit.power_saving')
        by_device = actions_by_device(result)
        self.assertEqual(by_device['tv'].action, 'off')
        self.assertNotIn('fridge', by_device)   # kept running
        self.assertNotIn('server', by_device)   # mandatory never shed


class SuddenDropTests(SimpleTestCase):

    def test_summer_drop_raises_panel_fault_alert(self):
        result = decide(make_facts(
            [], sudden_pv_drop=True, season='summer',
            pv_power_W=500.0, pv_baseline_W=3000.0,
        ))
        self.assertEqual(result.branch, 'day.sudden_drop.battery_ok')
        self.assertEqual(result.alerts[0].kind, 'panel_fault')

    def test_winter_drop_raises_weather_alert(self):
        result = decide(make_facts(
            [], sudden_pv_drop=True, season='winter',
            pv_power_W=500.0, pv_baseline_W=3000.0,
        ))
        self.assertEqual(result.alerts[0].kind, 'weather_drop')

    def test_drop_with_unstable_battery_and_no_saving_buys_grid(self):
        breakers = [make_breaker(id=1, device_id='grid', priority_type='ac_grid', switch=False)]
        result = decide(make_facts(
            breakers, sudden_pv_drop=True, season='winter',
            pv_power_W=500.0, pv_baseline_W=3000.0,
            battery_stable=False, battery_capacity_percent=25.0,
        ))
        self.assertEqual(result.branch, 'day.sudden_drop.buy_grid')
        self.assertEqual(actions_by_device(result)['grid'].action, 'on')

    def test_drop_with_saving_sheds_to_pv_budget(self):
        breakers = [
            make_breaker(id=1, device_id='tv', priority_type='comfort', mean_load_W=800.0),
            make_breaker(id=2, device_id='grid', priority_type='ac_grid', switch=False),
        ]
        result = decide(make_facts(
            breakers, sudden_pv_drop=True, season='winter',
            pv_power_W=500.0, pv_baseline_W=3000.0,
            battery_stable=False, battery_capacity_percent=25.0, power_saving=True,
        ))
        self.assertEqual(result.branch, 'day.sudden_drop.power_saving')
        self.assertEqual(actions_by_device(result)['tv'].action, 'off')


class NightTests(SimpleTestCase):

    def night_facts(self, breakers, **overrides):
        values = dict(
            is_daytime=False, local_time=time(23, 0),
            pv_power_W=0.0, pv_baseline_W=0.0,
            hours_to_morning=7.0,        # until day_start (h)
            mandatory_need_Wh=2100.0,    # servers need 300 W x 7 h (Wh)
        )
        values.update(overrides)
        return make_facts(breakers, **values)

    def test_calm_night_runs_from_battery(self):
        breakers = [make_breaker(id=1, device_id='grid', priority_type='ac_grid', switch=True)]
        result = decide(self.night_facts(breakers))
        self.assertEqual(result.branch, 'night.calm.battery')
        self.assertEqual(actions_by_device(result)['grid'].action, 'off')

    def test_sudden_draw_with_enough_reserve_stays_on_battery(self):
        result = decide(self.night_facts(
            [], sudden_draw=True, battery_remaining_Wh=4000.0,
        ))
        self.assertEqual(result.branch, 'night.sudden_draw.battery_ok')

    def test_sudden_draw_short_reserve_with_saving_trips_culprit(self):
        breakers = [
            make_breaker(id=7, device_id='heater', priority_type='comfort', cur_power_W=2000.0),
        ]
        result = decide(self.night_facts(
            breakers, sudden_draw=True, sudden_draw_culprit_id=7,
            battery_remaining_Wh=1000.0, power_saving=True,
        ))
        self.assertEqual(result.branch, 'night.sudden_draw.trip')
        trip = actions_by_device(result)['heater']
        self.assertEqual(trip.action, 'off')
        self.assertTrue(trip.lockout)
        self.assertEqual(result.alerts[0].kind, 'night_trip')

    def test_user_reenabled_culprit_is_not_tripped_again(self):
        breakers = [
            make_breaker(id=7, device_id='heater', priority_type='comfort',
                         cur_power_W=2000.0, recently_tripped=True),
            make_breaker(id=8, device_id='grid', priority_type='ac_grid', switch=False),
        ]
        result = decide(self.night_facts(
            breakers, sudden_draw=True, sudden_draw_culprit_id=7,
            battery_remaining_Wh=1000.0, power_saving=True,
        ))
        # the user insisted on this load tonight -> buy grid power instead
        self.assertEqual(result.branch, 'night.sudden_draw.buy_grid')
        self.assertEqual(actions_by_device(result)['grid'].action, 'on')
        self.assertNotIn('heater', actions_by_device(result))

    def test_sudden_draw_short_reserve_without_saving_buys_grid(self):
        breakers = [
            make_breaker(id=7, device_id='heater', priority_type='comfort', cur_power_W=2000.0),
            make_breaker(id=8, device_id='grid', priority_type='ac_grid', switch=False),
        ]
        result = decide(self.night_facts(
            breakers, sudden_draw=True, sudden_draw_culprit_id=7,
            battery_remaining_Wh=1000.0, power_saving=False,
        ))
        self.assertEqual(result.branch, 'night.sudden_draw.buy_grid')
        self.assertEqual(actions_by_device(result)['grid'].action, 'on')

    def test_mandatory_culprit_is_never_tripped(self):
        breakers = [
            make_breaker(id=7, device_id='server', priority_type='mandatory', cur_power_W=2000.0),
            make_breaker(id=8, device_id='grid', priority_type='ac_grid', switch=False),
        ]
        result = decide(self.night_facts(
            breakers, sudden_draw=True, sudden_draw_culprit_id=7,
            battery_remaining_Wh=1000.0, power_saving=True,
        ))
        self.assertEqual(result.branch, 'night.sudden_draw.buy_grid')
        self.assertNotIn('server', actions_by_device(result))
