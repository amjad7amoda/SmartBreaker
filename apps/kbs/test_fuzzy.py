from dataclasses import replace
from datetime import timedelta
from itertools import product
from unittest import TestCase

from .engine.fuzzy import (
    POWER_TERMS, PROFILE_VERSION, RESERVE_TERMS, RULE_TABLE, TREND_TERMS,
    ControllerSnapshot, advance_controller, evaluate_fuzzy, fuzzify_inputs,
    infer_risk, left_shoulder, output_membership, right_shoulder, triangle,
)
from .engine.rules import decide_fuzzy
from .tests import make_breaker, make_facts


class MembershipFunctionTests(TestCase):
    def test_piecewise_boundaries_are_inclusive_and_linear(self):
        self.assertEqual(left_shoulder(-0.25, -0.25, 0), 1)
        self.assertEqual(left_shoulder(0, -0.25, 0), 0)
        self.assertEqual(left_shoulder(-0.125, -0.25, 0), .5)
        self.assertEqual(triangle(-.25, -.25, 0, .25), 0)
        self.assertEqual(triangle(0, -.25, 0, .25), 1)
        self.assertEqual(triangle(.25, -.25, 0, .25), 0)
        self.assertEqual(right_shoulder(0, 0, .25), 0)
        self.assertEqual(right_shoulder(.125, 0, .25), .5)
        self.assertEqual(right_shoulder(.25, 0, .25), 1)

    def test_all_input_membership_breakpoints(self):
        negative = fuzzify_inputs(-.25, -.10, -.15)
        zero = fuzzify_inputs(0, .10, 0)
        positive = fuzzify_inputs(.25, .30, .15)
        self.assertEqual(negative['power_balance'], {
            'deficit': 1, 'balanced': 0, 'surplus': 0,
        })
        self.assertEqual(zero['power_balance']['balanced'], 1)
        self.assertEqual(positive['power_balance']['surplus'], 1)
        self.assertEqual(negative['battery_reserve']['adequate'], 0)
        self.assertEqual(zero['battery_reserve']['adequate'], 1)
        self.assertEqual(positive['battery_reserve']['ample'], 1)
        self.assertEqual(negative['net_power_trend']['falling'], 1)
        self.assertEqual(zero['net_power_trend']['steady'], 1)
        self.assertEqual(positive['net_power_trend']['rising'], 1)

    def test_output_boundaries_cover_the_complete_universe(self):
        for score in range(101):
            self.assertGreater(
                max(output_membership(term, score) for term in ('low', 'watch', 'high')),
                0,
            )


class MamdaniInferenceTests(TestCase):
    def test_versioned_rule_table_is_the_full_cartesian_product(self):
        self.assertEqual(PROFILE_VERSION, 'mamdani-v1')
        self.assertEqual(len(RULE_TABLE), 27)
        self.assertEqual(
            {(power, reserve, trend) for power, reserve, trend, _ in RULE_TABLE},
            set(product(POWER_TERMS, RESERVE_TERMS, TREND_TERMS)),
        )
        self.assertTrue(all(rule[3] in ('low', 'watch', 'high') for rule in RULE_TABLE))

    def test_all_27_expert_rule_consequents(self):
        expected = {
            ('deficit', 'short'): ('high', 'high', 'high'),
            ('deficit', 'adequate'): ('high', 'high', 'watch'),
            ('deficit', 'ample'): ('high', 'watch', 'watch'),
            ('balanced', 'short'): ('high', 'high', 'watch'),
            ('balanced', 'adequate'): ('high', 'watch', 'low'),
            ('balanced', 'ample'): ('watch', 'low', 'low'),
            ('surplus', 'short'): ('high', 'watch', 'watch'),
            ('surplus', 'adequate'): ('watch', 'low', 'low'),
            ('surplus', 'ample'): ('low', 'low', 'low'),
        }
        actual = {
            (power, reserve): tuple(
                consequent
                for row_power, row_reserve, _trend, consequent in RULE_TABLE
                if row_power == power and row_reserve == reserve
            )
            for power in POWER_TERMS
            for reserve in RESERVE_TERMS
        }
        self.assertEqual(actual, expected)

    def test_known_rules_and_centroid_outputs(self):
        high = infer_risk(-1, -1, -1)
        watch = infer_risk(0, .10, 0)
        low = infer_risk(1, 1, 1)
        self.assertEqual(high['fired_rules'][0]['rule_id'], 1)
        self.assertEqual(high['fired_rules'][0]['then'], 'high')
        self.assertAlmostEqual(high['risk_score'], 82.088, places=3)
        self.assertAlmostEqual(watch['risk_score'], 50.0, places=3)
        self.assertAlmostEqual(low['risk_score'], 17.912, places=3)

    def test_min_conjunction_and_max_consequent_aggregation(self):
        inferred = infer_risk(-.125, 0, -.075)
        self.assertTrue(inferred['fired_rules'])
        for rule in inferred['fired_rules']:
            self.assertGreater(rule['strength'], 0)
            self.assertLessEqual(rule['strength'], 1)
        for term, strength in inferred['aggregated_strengths'].items():
            expected = max(
                [rule['strength'] for rule in inferred['fired_rules'] if rule['then'] == term]
                or [0],
            )
            self.assertEqual(strength, expected)

    def test_fact_calculation_includes_event_mandatory_and_night_targets(self):
        facts = make_facts(
            [], event_upcoming=True, stability_threshold_percent=80,
            battery_capacity_percent=70, battery_remaining_Wh=3500,
            battery_capacity_Wh=5000, night_reserve_percent=30,
            mandatory_need_Wh=2000, pv_power_W=2000, load_power_W=1000,
            pv_baseline_W=1500, load_baseline_W=1200,
        )
        evaluation = evaluate_fuzzy(facts)
        self.assertTrue(evaluation['valid'])
        self.assertEqual(evaluation['inputs']['reserve_target_percent'], 80)
        self.assertEqual(evaluation['inputs']['battery_reserve_margin'], -.1)
        self.assertEqual(evaluation['inputs']['power_balance_ratio'], .2)
        self.assertEqual(evaluation['inputs']['net_power_trend'], .14)

    def test_missing_and_nonfinite_inputs_are_explicit_fallbacks(self):
        missing = evaluate_fuzzy(make_facts([], pv_baseline_W=None))
        invalid = evaluate_fuzzy(make_facts([], battery_capacity_percent=float('nan')))
        sensor_missing = evaluate_fuzzy(make_facts([], pv_power_valid=False))
        impossible = evaluate_fuzzy(make_facts(
            [], battery_capacity_percent=101, load_power_W=-1,
        ))
        invalid_night = evaluate_fuzzy(make_facts(
            [], is_daytime=False, hours_to_morning=0,
        ))
        self.assertFalse(missing['valid'])
        self.assertIn('invalid_pv_baseline_W', missing['fallback_reason'])
        self.assertFalse(invalid['valid'])
        self.assertIn('invalid_battery_capacity_percent', invalid['fallback_reason'])
        self.assertIn('missing_pv_power', sensor_missing['fallback_reason'])
        self.assertIn(
            'out_of_range_battery_capacity_percent',
            impossible['fallback_reason'],
        )
        self.assertIn('negative_load_power_W', impossible['fallback_reason'])
        self.assertIn(
            'invalid_hours_to_morning', invalid_night['fallback_reason'],
        )

    def test_replay_is_byte_for_byte_deterministic(self):
        facts = make_facts([], pv_power_W=740, load_power_W=1600)
        self.assertEqual(evaluate_fuzzy(facts), evaluate_fuzzy(facts))


def evaluation(score, valid=True):
    return {
        'valid': valid,
        'risk_score': score if valid else None,
        'fallback_reason': None if valid else 'missing',
    }


class HysteresisTests(TestCase):
    def setUp(self):
        self.now = make_facts([]).now

    def advance(self, state, score, seconds=5, valid=True):
        return advance_controller(
            state, evaluation(score, valid), self.now + timedelta(seconds=seconds), 5,
        )

    def test_high_entry_is_immediate_at_75_and_confirmed_at_65(self):
        immediate, evidence = self.advance(ControllerSnapshot(), 75)
        self.assertEqual(immediate.current_band, 'high')
        self.assertEqual(evidence['transition'], 'immediate_high_entry')

        first, _ = self.advance(ControllerSnapshot(), 70)
        self.assertEqual((first.current_band, first.candidate_band, first.consecutive_cycles),
                         ('watch', 'high', 1))
        self.now += timedelta(seconds=5)
        second, evidence = self.advance(first, 70)
        self.assertEqual(second.current_band, 'high')
        self.assertEqual(evidence['transition'], 'confirmed_high_entry')

    def test_high_recovery_requires_two_cycles_at_or_below_55(self):
        state = ControllerSnapshot(current_band='high')
        first, _ = self.advance(state, 40)
        self.assertEqual(first.current_band, 'high')
        self.now += timedelta(seconds=5)
        second, evidence = self.advance(first, 40)
        self.assertEqual(second.current_band, 'watch')
        self.assertEqual(evidence['transition'], 'confirmed_high_exit')

    def test_two_low_risk_recovery_cycles_can_move_high_directly_to_low(self):
        state = ControllerSnapshot(current_band='high')
        first, _ = self.advance(state, 20)
        self.assertEqual(first.current_band, 'high')
        self.now += timedelta(seconds=5)
        second, evidence = self.advance(first, 20)
        self.assertEqual(second.current_band, 'low')
        self.assertEqual(evidence['transition'], 'confirmed_high_exit')

    def test_low_entry_and_exit_thresholds(self):
        immediate, _ = self.advance(ControllerSnapshot(), 25)
        self.assertEqual(immediate.current_band, 'low')
        first, _ = self.advance(ControllerSnapshot(), 30)
        self.now += timedelta(seconds=5)
        confirmed, _ = self.advance(first, 30)
        self.assertEqual(confirmed.current_band, 'low')
        exited, evidence = self.advance(confirmed, 45)
        self.assertEqual(exited.current_band, 'watch')
        self.assertEqual(evidence['transition'], 'low_exit')

    def test_boundary_noise_does_not_advance_candidates(self):
        state = ControllerSnapshot()
        for index, score in enumerate((64.9, 65.1, 64.9, 65.1), start=1):
            self.now += timedelta(seconds=5)
            state, _ = self.advance(state, score)
            self.assertEqual(state.current_band, 'watch')
            self.assertLessEqual(state.consecutive_cycles, 1)

    def test_invalid_input_holds_candidate_and_valid_timestamp(self):
        prior = ControllerSnapshot(
            candidate_band='high', consecutive_cycles=1,
            last_risk_score=70, last_evaluated_at=self.now,
        )
        next_state, evidence = advance_controller(
            prior, evaluation(None, valid=False), self.now + timedelta(seconds=5), 5,
        )
        self.assertEqual(next_state, prior)
        self.assertFalse(evidence['advanced'])

    def test_stale_state_resets_to_watch_before_evaluation(self):
        prior = ControllerSnapshot(
            current_band='high', last_risk_score=80,
            last_evaluated_at=self.now,
        )
        next_state, evidence = advance_controller(
            prior, evaluation(70), self.now + timedelta(seconds=11), 5,
        )
        self.assertEqual(next_state.current_band, 'watch')
        self.assertEqual(next_state.candidate_band, 'high')
        self.assertEqual(next_state.consecutive_cycles, 1)
        self.assertTrue(evidence['stale_reset'])


class FuzzyDecisionBehaviorTests(TestCase):
    def test_hard_protection_remains_authoritative(self):
        result = decide_fuzzy(
            make_facts([], battery_low=True), 'low',
            evaluation={'profile_version': PROFILE_VERSION, 'risk_score': 10},
        )
        self.assertEqual(result.branch, 'protect_battery')

    def test_watch_preserves_state_but_event_requirement_remains_authoritative(self):
        event = make_breaker(
            id=4, device_id='event', priority_type='comfort',
            event_required=True, switch=False,
        )
        result = decide_fuzzy(make_facts([event]), 'watch')
        self.assertEqual(result.branch, 'fuzzy.watch.preserve')
        self.assertEqual([(item.device_id, item.action) for item in result.actions],
                         [('event', 'on')])

    def test_high_power_saving_never_sheds_mandatory_or_event_loads(self):
        breakers = [
            make_breaker(id=1, device_id='server', priority_type='mandatory'),
            make_breaker(id=2, device_id='event', priority_type='comfort', event_required=True),
            make_breaker(id=3, device_id='tv', priority_type='comfort'),
        ]
        result = decide_fuzzy(
            make_facts(breakers, power_saving=True), 'high',
            evaluation={'inputs': {'safe_budget_W': 100}},
        )
        self.assertEqual([item.device_id for item in result.actions], ['tv'])
