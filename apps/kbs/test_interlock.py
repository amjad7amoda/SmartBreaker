"""Tier-1/Tier-2 safety interlock behavior and normal-path regression tests."""

import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.breakers.models import Breaker
from apps.organizations.models import Organization

from .adapters.django import DjangoKBSAdapter
from .engine.rules import decide
from .models import BreakerAction, KBSDecision, Tier1SafetyState
from .tests import make_breaker, make_facts


class Tier1Tier2InterlockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            email='interlock-owner@example.com',
            password='pw',
            role='home_user',
            is_active=True,
            must_set_password=False,
        )
        cls.organization = Organization.objects.create(
            name='Interlock Site',
            phone='20',
            latitude=33.5138,
            longitude=36.2765,
            owner=owner,
            status='active',
        )
        cls.breaker = Breaker.objects.create(
            device_id='interlock-load',
            organization=cls.organization,
            priority_type='comfort',
            priority_degree=1,
        )

    def setUp(self):
        self.adapter = DjangoKBSAdapter()
        self.facts = make_facts(
            [make_breaker(
                id=self.breaker.id,
                device_id=self.breaker.device_id,
                priority_type='comfort',
                switch=True,
            )],
            organization_id=self.organization.id,
        )

    def _activate(self, action_status='pending'):
        source = KBSDecision.objects.create(
            organization=self.organization,
            tier='tier1',
            event_type='decision',
            engine='edge.tier1_kbs.evaluate',
            branch='inverter_overheat',
            occurred_at=self.facts.now,
        )
        BreakerAction.objects.create(
            decision=source,
            breaker=self.breaker,
            device_id=self.breaker.device_id,
            action='off',
            countdown_s=0,
            reason='tier1: inverter overheating',
            status=action_status,
        )
        return Tier1SafetyState.objects.create(
            organization=self.organization,
            source_decision=source,
            active=True,
            situation='inverter_overheat',
            episode_id=uuid.uuid4(),
            commands=[{
                'device_id': self.breaker.device_id,
                'action': 'off',
                'countdown_s': 0,
                'reason': 'tier1: inverter overheating',
            }],
            source_occurred_at=self.facts.now,
            activated_at=timezone.now(),
        )

    def test_active_hold_bypasses_normal_rules_and_mirrors_tier1(self):
        self._activate()

        result = self.adapter.make_decision(
            self.organization, self.facts, decide,
        )
        decision = self.adapter.persist_result(
            self.organization, self.facts, result,
        )

        self.assertEqual(result.branch, 'tier1_interlock.inverter_overheat')
        self.assertEqual(
            [(item.device_id, item.action, item.reason) for item in result.actions],
            [(
                self.breaker.device_id,
                'off',
                'tier1: inverter overheating',
            )],
        )
        mirrored = decision.actions.get()
        self.assertEqual(mirrored.action, 'off')
        self.assertEqual(mirrored.status, 'suppressed_duplicate')
        self.assertFalse(any(
            step['code'].startswith('tier2.guard.')
            for step in decision.trace
        ))

    def test_inactive_hold_leaves_normal_tier2_result_unchanged(self):
        Tier1SafetyState.objects.create(
            organization=self.organization,
            active=False,
            situation='',
            commands=[],
        )
        expected = decide(self.facts)

        actual = self.adapter.make_decision(
            self.organization, self.facts, decide,
        )

        self.assertEqual(actual, expected)
        self.assertEqual(actual.branch, 'day.surplus.comfort_on')

    def test_persistence_rechecks_hold_to_close_activation_race(self):
        normal_result = decide(self.facts)
        self._activate()

        decision = self.adapter.persist_result(
            self.organization, self.facts, normal_result,
        )

        self.assertEqual(decision.branch, 'tier1_interlock.inverter_overheat')
        self.assertEqual(decision.actions.get().action, 'off')
