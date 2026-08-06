"""Regression tests for live-state ordering and Tier-2 engine provenance."""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.breakers.models import Breaker
from apps.organizations.models import Organization

from .adapters.django import DjangoKBSAdapter
from .contracts import LEGACY_TIER2_ENGINE, TIER2_ENGINE
from .engine.rules import RuleResult
from .models import BreakerAction, KBSDecision, KBSSettings
from .tasks import run_kbs_cycles
from .tests import make_facts


class Tier2ProvenanceContractTests(APITestCase):
    def setUp(self):
        owner = User.objects.create_user(
            email='provenance-owner@example.com', password='Pass123!',
            role='home_user', is_active=True,
        )
        self.organization = Organization.objects.create(
            name='Provenance Site', phone='1', latitude='33.510000',
            longitude='36.290000', owner=owner, status='active',
        )
        self.breaker = Breaker.objects.create(
            device_id='provenance-load', organization=self.organization,
            priority_type='normal',
        )

    def test_live_state_uses_tier2_receipt_order_not_historical_event_time(self):
        legacy = KBSDecision.objects.create(
            organization=self.organization, tier='tier2', trace_version=0,
            engine=LEGACY_TIER2_ENGINE, branch='legacy.future',
            occurred_at=timezone.now() + timedelta(days=30),
        )
        current = KBSDecision.objects.create(
            organization=self.organization, tier='tier2', trace_version=1,
            engine=TIER2_ENGINE, branch='historical.playback',
            occurred_at=timezone.now() - timedelta(days=30),
        )
        tier1 = KBSDecision.objects.create(
            organization=self.organization, tier='tier1', trace_version=1,
            engine='edge.tier1_kbs.evaluate', branch='inverter_overheat',
            occurred_at=timezone.now() + timedelta(days=60),
        )
        BreakerAction.objects.create(
            decision=current, breaker=self.breaker,
            device_id=self.breaker.device_id, action='off', reason='Tier-2 action',
        )
        BreakerAction.objects.create(
            decision=tier1, breaker=self.breaker,
            device_id=self.breaker.device_id, action='on', reason='Tier-1 upload',
        )

        response = self.client.get(
            f'/api/kbs/sim/state/?organization={self.organization.id}',
        )

        self.assertEqual(response.status_code, 200)
        decision = response.json()['latest_decision']
        self.assertEqual(decision['event_id'], str(current.event_id))
        self.assertEqual(decision['engine'], TIER2_ENGINE)
        self.assertEqual(decision['branch'], 'historical.playback')
        self.assertFalse(decision['legacy'])
        self.assertIn('occurred_at', decision)
        self.assertIn('received_at', decision)
        self.assertNotEqual(decision['event_id'], str(legacy.event_id))
        self.assertEqual(
            [action['reason'] for action in response.json()['pending_actions']],
            ['Tier-2 action'],
        )

    def test_adapter_persists_the_service_wrapper_as_canonical_engine(self):
        facts = make_facts([], organization_id=self.organization.id)
        decision = DjangoKBSAdapter().persist_result(
            self.organization, facts,
            RuleResult(branch='contract.test', trace_version=1, trace=[]),
        )
        self.assertEqual(decision.engine, TIER2_ENGINE)

    @patch('apps.kbs.tasks.run_kbs_cycle_for_org.delay')
    def test_recent_tier1_upload_does_not_delay_a_due_tier2_cycle(self, delay):
        KBSSettings.objects.create(
            organization=self.organization, mode='active', cycle_seconds=300,
        )
        KBSDecision.objects.create(
            organization=self.organization, tier='tier1',
            engine='edge.tier1_kbs.evaluate', branch='battery_low',
        )

        self.assertEqual(run_kbs_cycles(), 1)
        delay.assert_called_once_with(self.organization.id)
