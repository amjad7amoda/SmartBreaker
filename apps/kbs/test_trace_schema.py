"""Schema and determinism checks shared across Tier-2 branches."""

from django.test import SimpleTestCase

from .engine.rules import decide
from .tests import make_breaker, make_facts


class Tier2TraceSchemaTests(SimpleTestCase):
    def test_representative_branches_emit_deterministic_schema_v1_paths(self):
        scenarios = (
            {'heat_high': True, 'overload': True, 'heatsink_temp_C': 85.0},
            {'battery_low': True},
            {},
            {'pv_power_W': 100.0, 'battery_stable': False},
            {'sudden_pv_drop': True},
            {'is_daytime': False, 'sudden_draw': True},
            {'is_daytime': False, 'mandatory_need_Wh': 9e9},
            {'grid_failed': True, 'pv_power_W': 100.0, 'battery_stable': False},
        )
        breakers = [
            make_breaker(
                id=1, device_id='trace-comfort', priority_type='comfort',
            ),
            make_breaker(
                id=2, device_id='trace-grid', priority_type='ac_grid',
                switch=False,
            ),
        ]
        for overrides in scenarios:
            with self.subTest(overrides=overrides):
                facts = make_facts(breakers, **overrides)
                first = decide(facts)
                second = decide(facts)
                self.assertEqual(first.trace_version, 1)
                self.assertEqual(first.trace, second.trace)
                self.assertTrue(first.trace)
                for step in first.trace:
                    self.assertEqual(set(step), {
                        'code', 'kind', 'outcome', 'summary', 'evidence',
                    })
                    self.assertIsInstance(step['evidence'], dict)
                self.assertEqual(first.trace[-1]['kind'], 'branch') if not (
                    first.actions or first.alerts
                ) else self.assertIn(first.trace[-1]['kind'], ('output', 'alert'))

    def test_raw_thresholds_are_present_in_guard_evidence(self):
        result = decide(make_facts(
            [], heat_high=True, overload=False, heatsink_temp_C=85.0,
            heatsink_temp_limit_C=70.0,
        ))
        stress = result.trace[0]
        self.assertEqual(stress['evidence']['heat_actual'], 85.0)
        self.assertEqual(stress['evidence']['heat_threshold'], 70.0)
