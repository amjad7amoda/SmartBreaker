from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from apps.breakers.models import (
    Breaker,
    BreakerAction as DeviceAction,
    BreakerStatus,
    TuyaCredential,
)
from apps.organizations.models import Organization

from .executor import confirm_action, execute_action
from .models import (
    BreakerAction,
    KBSDecision,
    KBSSettings,
    Tier1SafetyState,
)


def properties(is_on, countdown=0):
    return {
        'properties': [
            {'code': 'switch_1', 'value': is_on},
            {'code': 'child_lock', 'value': False},
            {'code': 'countdown_1', 'value': countdown},
            {'code': 'fault', 'value': 0},
            {'code': 'online_state', 'value': 'online'},
        ],
    }


class RealSiteKBSExecutorTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            email='executor-owner@example.com',
            password='pw',
            role='home_user',
            is_active=True,
            must_set_password=False,
        )
        cls.organization = Organization.objects.create(
            name='Executor Site',
            phone='103',
            latitude=33.51,
            longitude=36.29,
            owner=owner,
            status='active',
        )
        credential = TuyaCredential(
            organization=cls.organization,
            client_id='executor-client',
            region='us',
        )
        credential.client_secret = 'secret'
        credential.save()
        cls.settings = KBSSettings.objects.create(
            organization=cls.organization,
            mode='active',
            data_source='real',
        )
        cls.breaker = Breaker.objects.create(
            name='Executor load',
            device_id='executor-breaker',
            organization=cls.organization,
            priority_type='comfort',
            priority_degree=2,
        )
        BreakerStatus.objects.create(
            breaker=cls.breaker,
            switch=True,
            online=True,
        )

    def setUp(self):
        cache.clear()
        self.decision = KBSDecision.objects.create(
            organization=self.organization,
            tier='tier2',
            branch='test.executor',
            facts={},
        )

    def action(self, action, countdown_s=0):
        return BreakerAction.objects.create(
            decision=self.decision,
            breaker=self.breaker,
            device_id=self.breaker.device_id,
            action=action,
            countdown_s=countdown_s,
            reason='executor integration test',
        )

    @patch(
        'apps.breakers.services.TuyaClient.get_device_specification',
        return_value={'status': []},
    )
    @patch(
        'apps.breakers.services.TuyaClient.get_device_properties',
        return_value=properties(False),
    )
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_immediate_intent_executes_through_backend_and_updates_both_audits(
        self, send, _properties, _specification,
    ):
        action = self.action('off')

        self.assertEqual(execute_action(action.id), 'applied')

        action.refresh_from_db()
        self.assertEqual(action.status, 'applied')
        self.assertFalse(action.resulting_state)
        send.assert_called_once_with(
            self.breaker.device_id,
            [{'code': 'switch', 'value': False}],
        )
        device_action = DeviceAction.objects.get()
        self.assertEqual(device_action.source, 'kbs')
        self.assertEqual(device_action.action, 'switch_off')
        self.assertTrue(device_action.confirmed)

    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_active_tier1_interlock_blocks_an_unsafe_on_without_tuya(
        self, send,
    ):
        Tier1SafetyState.objects.create(
            organization=self.organization,
            active=True,
            situation='inverter_overheat',
            commands=[],
        )
        action = self.action('on')

        self.assertEqual(execute_action(action.id), 'blocked')

        action.refresh_from_db()
        self.assertEqual(action.status, 'blocked')
        self.assertIn('Tier-1 safety interlock', action.failure_reason)
        send.assert_not_called()
        device_action = DeviceAction.objects.get()
        self.assertFalse(device_action.confirmed)
        self.assertIn('inverter_overheat', device_action.reason)

    @patch(
        'apps.breakers.services.TuyaClient.get_device_specification',
        return_value={'status': []},
    )
    @patch(
        'apps.breakers.services.TuyaClient.get_device_properties',
        side_effect=[properties(True), properties(True, countdown=45)],
    )
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_second_based_countdown_is_scheduled_then_confirmed(
        self, send, _properties, _specification,
    ):
        action = self.action('off', countdown_s=45)

        self.assertEqual(execute_action(action.id), 'scheduled')
        action.refresh_from_db()
        self.assertEqual(action.status, 'scheduled')
        send.assert_called_once_with(
            self.breaker.device_id,
            [{'code': 'countdown_1', 'value': 45}],
        )

        with patch(
            'apps.breakers.services.TuyaClient.get_device_properties',
            return_value=properties(False),
        ):
            self.assertEqual(confirm_action(action.id), 'applied')
        action.refresh_from_db()
        self.assertEqual(action.status, 'applied')
        self.assertFalse(action.resulting_state)

    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_simulator_intent_stays_pending_for_simulator_ack(self, send):
        self.settings.data_source = 'simulator'
        self.settings.save(update_fields=['data_source'])
        action = self.action('off')

        self.assertEqual(execute_action(action.id), 'pending')

        action.refresh_from_db()
        self.assertEqual(action.status, 'pending')
        send.assert_not_called()
