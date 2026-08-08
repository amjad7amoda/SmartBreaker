"""Unit tests for the Pi-side Tier-1 safety KBS.

Dependency-free, like the module itself: run with ``python -m unittest`` from
the repo root, or ``python edge/test_tier1_kbs.py``.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tier1_kbs import (  # noqa: E402
    BreakerState,
    InverterState,
    Tier1Config,
    evaluate,
    graceful_countdown_s,
)


def site(**overrides):
    """A default site: servers (mandatory), fridge (normal), TV (comfort), grid breaker."""
    breakers = [
        BreakerState('servers', 'mandatory', 5, switch=True, cur_power_W=300),
        BreakerState('fridge', 'normal', 5, switch=True, cur_power_W=400),
        BreakerState('tv', 'comfort', 2, switch=True, cur_power_W=500),
        BreakerState('grid', 'ac_grid', 1, switch=overrides.pop('grid_on', False)),
    ]
    return breakers


def off_ids(result):
    """device_ids commanded OFF, in the order Tier-1 issued them."""
    return [c.device_id for c in result.commands if c.action == 'off']


class OverheatTests(unittest.TestCase):

    def test_overheat_sheds_comfort_then_normal_never_mandatory(self):
        inv = InverterState(ac_output_active_power_W=1200, heatsink_temp_C=75)
        result = evaluate(inv, site())
        self.assertEqual(result.situation, 'inverter_overheat')
        self.assertEqual(off_ids(result), ['tv', 'fridge'])   # comfort first, mandatory absent
        self.assertTrue(all(c.countdown_s == 0 for c in result.commands))  # immediate
        self.assertIn('heatsink', result.notify)


class OverloadTests(unittest.TestCase):

    def test_overload_sheds_only_until_within_rating(self):
        cfg = Tier1Config(max_inverter_power_W=1000)
        # 1200 W on a 1000 W inverter: shedding the 500 W TV is enough
        inv = InverterState(ac_output_active_power_W=1200)
        result = evaluate(inv, site(), cfg)
        self.assertEqual(result.situation, 'inverter_overload')
        self.assertEqual(off_ids(result), ['tv'])   # fridge survives — no blackout for a mild overload

    def test_rating_boundary_matches_tier2(self):
        cfg = Tier1Config(max_inverter_power_W=4000)
        result = evaluate(
            InverterState(ac_output_active_power_W=4000), site(), cfg,
        )
        self.assertEqual(result.situation, 'inverter_overload')

    def test_mild_load_does_nothing(self):
        inv = InverterState(ac_output_active_power_W=1200)
        self.assertEqual(evaluate(inv, site()).situation, '')  # 1200 W on the 5000 W default: fine


class BatteryTests(unittest.TestCase):

    def test_low_battery_arms_countdowns_sized_by_buffer_over_draw(self):
        cfg = Tier1Config(battery_capacity_Wh=5000, battery_shutdown_buffer_percent=2.0)
        # 24.4 V is within the 0.5 V margin of the 24.0 V floor; not charging
        inv = InverterState(
            ac_output_active_power_W=1200, battery_voltage_V=24.4,
            battery_discharge_current_A=49.2,  # 24.4 V x 49.2 A ~ 1200 W
        )
        result = evaluate(inv, site(), cfg)
        self.assertEqual(result.situation, 'battery_low')
        self.assertEqual(off_ids(result), ['tv', 'fridge'])
        # 100 Wh buffer / ~1200 W draw ~= 300 s (exact value follows the measured draw)
        for c in result.commands:
            self.assertAlmostEqual(c.countdown_s, 300, delta=5)
        self.assertIn('switch off in ~4 min', result.notify)

    def test_charging_battery_is_not_protected(self):
        inv = InverterState(
            ac_output_active_power_W=1200, battery_voltage_V=24.4,
            battery_charge_current_A=10.0,  # the bank is recovering on its own
        )
        self.assertEqual(evaluate(inv, site()).situation, '')

    def test_at_the_floor_sheds_immediately_without_countdown(self):
        inv = InverterState(
            ac_output_active_power_W=1200, battery_voltage_V=24.05,
            battery_discharge_current_A=49.0,
        )
        result = evaluate(inv, site())
        self.assertEqual(result.situation, 'battery_critical')
        self.assertTrue(all(c.countdown_s == 0 for c in result.commands))

    def test_low_battery_hold_remains_active_after_all_loads_are_off(self):
        breakers = site()
        for breaker in breakers:
            if breaker.priority_type in ('comfort', 'normal'):
                breaker.switch = False
        result = evaluate(
            InverterState(
                battery_voltage_V=24.4,
                battery_discharge_current_A=10.0,
            ),
            breakers,
        )

        self.assertEqual(result.situation, 'battery_low')
        self.assertEqual(result.commands, [])
        self.assertIn('hold stays active', result.notify)

    def test_countdown_clamps(self):
        cfg = Tier1Config()
        self.assertEqual(graceful_countdown_s(100.0, 1200.0, cfg), 300)
        self.assertEqual(graceful_countdown_s(100.0, 0.0, cfg), 3600)   # not draining -> max
        self.assertEqual(graceful_countdown_s(1.0, 5000.0, cfg), 60)    # tiny buffer -> min warning


class GridOutageTests(unittest.TestCase):

    def test_dead_grid_with_low_battery_sheds_and_keeps_breaker_on(self):
        # grid breaker closed, grid voltage 0, battery thin but above the floor
        inv = InverterState(
            ac_output_active_power_W=1200, grid_voltage_V=0.0,
            battery_voltage_V=24.9, battery_discharge_current_A=48.0,
            pv_charging_power_W=0.0,
        )
        result = evaluate(inv, site(grid_on=True))
        self.assertEqual(result.situation, 'grid_outage')
        self.assertEqual(off_ids(result), ['tv', 'fridge'])   # by priority
        self.assertNotIn('grid', off_ids(result))             # breaker stays ON to resume automatically
        self.assertNotIn('servers', off_ids(result))          # mandatory untouched

    def test_dead_grid_with_healthy_battery_is_left_to_tier2(self):
        inv = InverterState(
            ac_output_active_power_W=1200, grid_voltage_V=0.0,
            battery_voltage_V=26.5, battery_discharge_current_A=45.0,
        )
        self.assertEqual(evaluate(inv, site(grid_on=True)).situation, '')

    def test_live_grid_is_not_an_outage(self):
        inv = InverterState(
            ac_output_active_power_W=1200, grid_voltage_V=230.0,
            battery_voltage_V=24.9, battery_discharge_current_A=48.0,
        )
        self.assertEqual(evaluate(inv, site(grid_on=True)).situation, '')

    def test_outage_hold_remains_active_after_all_sheddable_loads_are_off(self):
        breakers = site(grid_on=True)
        for breaker in breakers:
            if breaker.priority_type in ('comfort', 'normal'):
                breaker.switch = False
        result = evaluate(
            InverterState(
                ac_output_active_power_W=300,
                grid_voltage_V=0.0,
                battery_voltage_V=24.9,
                battery_discharge_current_A=12.0,
            ),
            breakers,
        )

        self.assertEqual(result.situation, 'grid_outage')
        self.assertEqual(result.commands, [])
        self.assertIn('hold stays active', result.notify)


class PriorityTests(unittest.TestCase):

    def test_degree_orders_within_a_category(self):
        breakers = [
            BreakerState('comfort-hi', 'comfort', 9, switch=True, cur_power_W=100),
            BreakerState('comfort-lo', 'comfort', 1, switch=True, cur_power_W=100),
            BreakerState('normal-lo', 'normal', 1, switch=True, cur_power_W=100),
        ]
        result = evaluate(InverterState(heatsink_temp_C=80), breakers)
        self.assertEqual(off_ids(result), ['comfort-lo', 'comfort-hi', 'normal-lo'])

    def test_offline_breakers_are_not_commanded(self):
        breakers = [BreakerState('tv', 'comfort', 1, switch=True, online=False, cur_power_W=500)]
        result = evaluate(InverterState(heatsink_temp_C=80), breakers)
        self.assertEqual(off_ids(result), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
