from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.breakers.models import Breaker, BreakerStatus
from apps.organizations.models import Organization

from .adapters.django import DjangoKBSAdapter
from .engine.fuzzy import PROFILE_VERSION
from .engine.rules import decide
from .models import (
    BreakerAction, KBSControllerState, KBSSettings, Tier1SafetyState,
)
from .tests import make_breaker, make_facts


def fuzzy_evaluation(score=82.088, valid=True):
    return {
        'profile_version': PROFILE_VERSION,
        'valid': valid,
        'fallback_reason': None if valid else 'invalid_pv_baseline_W',
        'inputs': {'safe_budget_W': 100},
        'memberships': {},
        'fired_rules': [],
        'aggregated_strengths': {},
        'risk_score': score if valid else None,
        'inferred_band': 'high' if valid else None,
    }


class FuzzyPolicyPersistenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            email='fuzzy-policy@example.com', password='pw', is_active=True,
        )
        cls.organization = Organization.objects.create(
            name='Fuzzy policy site', phone='20', latitude=33.5,
            longitude=36.2, owner=owner, status='active',
        )
        cls.settings = KBSSettings.objects.create(
            organization=cls.organization, mode='active',
            data_source='simulator', cycle_seconds=5,
        )
        cls.grid = Breaker.objects.create(
            device_id='fuzzy-grid', organization=cls.organization,
            priority_type='ac_grid',
        )
        cls.comfort = Breaker.objects.create(
            device_id='fuzzy-comfort', organization=cls.organization,
            priority_type='comfort', mean_load_W=500,
        )
        BreakerStatus.objects.create(
            breaker=cls.grid, switch=False, online=True,
        )
        BreakerStatus.objects.create(
            breaker=cls.comfort, switch=True, online=True,
        )

    def setUp(self):
        self.adapter = DjangoKBSAdapter()
        self.facts = make_facts([
            make_breaker(
                id=self.grid.id, device_id=self.grid.device_id,
                priority_type='ac_grid', switch=False,
            ),
            make_breaker(
                id=self.comfort.id, device_id=self.comfort.device_id,
                priority_type='comfort', switch=True, mean_load_W=500,
            ),
        ])

    def select(self, policy):
        self.settings.tier2_policy = policy
        self.settings.save(update_fields=['tier2_policy'])

    def test_crisp_does_not_create_or_advance_fuzzy_state(self):
        self.select('crisp')
        result = self.adapter.make_decision(self.organization, self.facts, decide)
        decision = self.adapter.persist_result(self.organization, self.facts, result)
        self.assertEqual(decision.policy, 'crisp')
        self.assertNotIn('fuzzy_evaluation', decision.facts)
        self.assertFalse(KBSControllerState.objects.exists())

    @patch('apps.kbs.adapters.django.evaluate_fuzzy', return_value=fuzzy_evaluation())
    def test_shadow_actions_are_json_only(self, _evaluate):
        self.select('fuzzy_shadow')
        result = self.adapter.make_decision(self.organization, self.facts, decide)
        self.assertEqual(result.branch, 'day.surplus.comfort_on')
        self.assertEqual(result.counterfactual['branch'], 'fuzzy.high.buy_grid')
        self.assertEqual(result.counterfactual['actions'][0]['device_id'], 'fuzzy-grid')
        decision = self.adapter.persist_result(self.organization, self.facts, result)
        self.assertEqual(decision.policy, 'fuzzy_shadow')
        self.assertEqual(BreakerAction.objects.count(), 0)

    @patch('apps.kbs.adapters.django.evaluate_fuzzy', return_value=fuzzy_evaluation())
    def test_active_persists_only_fuzzy_actions_and_crisp_counterfactual(self, _evaluate):
        self.select('fuzzy_active')
        result = self.adapter.make_decision(self.organization, self.facts, decide)
        decision = self.adapter.persist_result(self.organization, self.facts, result)
        self.assertEqual(decision.branch, 'fuzzy.high.buy_grid')
        self.assertEqual(decision.counterfactual['branch'], 'day.surplus.comfort_on')
        self.assertEqual(
            list(BreakerAction.objects.values_list('device_id', 'action')),
            [('fuzzy-grid', 'on')],
        )

    @patch(
        'apps.kbs.adapters.django.evaluate_fuzzy',
        return_value=fuzzy_evaluation(valid=False),
    )
    def test_invalid_active_input_falls_back_without_advancing_state(self, _evaluate):
        self.select('fuzzy_active')
        result = self.adapter.make_decision(self.organization, self.facts, decide)
        decision = self.adapter.persist_result(self.organization, self.facts, result)
        state = KBSControllerState.objects.get(organization=self.organization)
        self.assertEqual(decision.branch, 'day.surplus.comfort_on')
        self.assertEqual(
            decision.facts['fuzzy_evaluation']['fallback_reason'],
            'invalid_pv_baseline_W',
        )
        self.assertEqual(state.consecutive_cycles, 0)
        self.assertIsNone(state.last_evaluated_at)

    @patch('apps.kbs.adapters.django.evaluate_fuzzy', return_value=fuzzy_evaluation())
    def test_hard_battery_protection_bypasses_fuzzy_state(self, _evaluate):
        self.select('fuzzy_active')
        facts = replace(
            self.facts, battery_low=True, battery_voltage_V=24.4,
            battery_draw_W=1200,
        )
        result = self.adapter.make_decision(self.organization, facts, decide)
        decision = self.adapter.persist_result(self.organization, facts, result)
        self.assertEqual(decision.branch, 'protect_battery')
        self.assertEqual(
            decision.facts['fuzzy_evaluation']['fallback_reason'],
            'hard_protection_authoritative',
        )
        self.assertFalse(KBSControllerState.objects.exists())

    @patch('apps.kbs.adapters.django.evaluate_fuzzy', return_value=fuzzy_evaluation())
    def test_tier1_interlock_bypasses_fuzzy_state(self, evaluate):
        self.select('fuzzy_active')
        Tier1SafetyState.objects.create(
            organization=self.organization,
            active=True,
            situation='inverter_overheat',
            commands=[{
                'device_id': self.comfort.device_id,
                'action': 'off',
                'countdown_s': 0,
                'reason': 'Tier-1 owns the active danger',
            }],
        )
        result = self.adapter.make_decision(
            self.organization, self.facts, decide,
        )
        decision = self.adapter.persist_result(
            self.organization, self.facts, result,
        )
        self.assertEqual(decision.branch, 'tier1_interlock.inverter_overheat')
        self.assertEqual(
            decision.facts['fuzzy_evaluation']['fallback_reason'],
            'tier1_interlock_authoritative',
        )
        self.assertFalse(KBSControllerState.objects.exists())
        evaluate.assert_not_called()

    @patch(
        'apps.kbs.adapters.django.evaluate_fuzzy',
        return_value=fuzzy_evaluation(score=70),
    )
    def test_two_moderate_cycles_persist_and_confirm_high(self, _evaluate):
        self.select('fuzzy_shadow')
        first = self.adapter.make_decision(self.organization, self.facts, decide)
        self.adapter.persist_result(self.organization, self.facts, first)
        state = KBSControllerState.objects.get(organization=self.organization)
        self.assertEqual(
            (state.current_band, state.candidate_band, state.consecutive_cycles),
            ('watch', 'high', 1),
        )
        # The React plant advances five simulated minutes per five-real-second
        # controller cycle; this must still be a consecutive controller cycle.
        later = replace(self.facts, now=self.facts.now + timedelta(minutes=5))
        second = self.adapter.make_decision(self.organization, later, decide)
        self.adapter.persist_result(self.organization, later, second)
        state.refresh_from_db()
        self.assertEqual(state.current_band, 'high')
        self.assertEqual(state.consecutive_cycles, 0)
