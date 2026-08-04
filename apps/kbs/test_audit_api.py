"""Persistent decision audit ingestion and history API tests."""

import uuid

from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.breakers.models import Breaker
from apps.organizations.models import Organization

from .models import BreakerAction, EdgeDevice, KBSDecision


class DecisionAuditApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            email='audit-owner@example.com', password='Pass123!',
            role='home_user', is_active=True,
        )
        cls.other_owner = User.objects.create_user(
            email='audit-other@example.com', password='Pass123!',
            role='home_user', is_active=True,
        )
        cls.technician = User.objects.create_user(
            email='audit-tech@example.com', password='Pass123!',
            role='technician', is_active=True,
        )
        cls.organization = Organization.objects.create(
            name='Audited Home', phone='1', latitude='33.510000',
            longitude='36.290000', owner=cls.owner, status='active',
        )
        cls.other_organization = Organization.objects.create(
            name='Other Audited Home', phone='2', latitude='34.000000',
            longitude='37.000000', owner=cls.other_owner, status='active',
        )
        cls.breaker = Breaker.objects.create(
            device_id='audit-comfort-load', organization=cls.organization,
            priority_type='comfort',
        )
        cls.other_breaker = Breaker.objects.create(
            device_id='audit-other-load', organization=cls.other_organization,
            priority_type='normal',
        )
        cls.device = EdgeDevice(
            organization=cls.organization, name='Audit edge', secret_hash='',
        )
        cls.device.set_secret('correct-secret')
        cls.device.save()
        cls.other_device = EdgeDevice(
            organization=cls.other_organization, name='Other edge', secret_hash='',
        )
        cls.other_device.set_secret('other-secret')
        cls.other_device.save()

    def setUp(self):
        self.event_id = uuid.uuid4()
        self.action_id = uuid.uuid4()
        self.payload = {
            'event_id': str(self.event_id),
            'event_type': 'decision',
            'situation': 'inverter_overheat',
            'branch': 'inverter_overheat',
            'engine': 'edge.tier1_kbs.evaluate',
            'trace_version': 1,
            'trace': [{
                'code': 'tier1.guard.inverter_overheat', 'kind': 'guard',
                'outcome': 'passed', 'summary': 'Overheat detected.',
                'evidence': {
                    'actual': 80, 'operator': '>=', 'threshold': 70,
                    'unit': 'C',
                },
            }],
            'facts': {'inverter': {'heatsink_temp_C': 80}},
            'occurred_at': timezone.now().isoformat(),
            'actions': [{
                'action_id': str(self.action_id),
                'device_id': self.breaker.device_id,
                'action': 'off', 'countdown_s': 0,
                'reason': 'Protect inverter', 'status': 'pending',
            }],
        }

    def _device_headers(self, device=None, secret='correct-secret'):
        device = device or self.device
        return {
            'HTTP_AUTHORIZATION': f'Device {device.device_id}.{secret}',
        }

    @staticmethod
    def _jwt_headers(user):
        token = RefreshToken.for_user(user).access_token
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def _upload(self, payload=None, **headers):
        return self.client.post(
            '/api/kbs/edge/decision-events/',
            {'events': [payload or self.payload]}, format='json', **headers,
        )

    def test_device_authentication_ingests_idempotently_and_rejects_conflicts(self):
        invalid = self._upload(**self._device_headers(secret='wrong-secret'))
        created = self._upload(**self._device_headers())
        duplicate = self._upload(**self._device_headers())
        conflict_payload = {**self.payload, 'facts': {'tampered': True}}
        conflict = self._upload(conflict_payload, **self._device_headers())

        self.assertIn(invalid.status_code, (401, 403))
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()['results'][0]['status'], 'created')
        self.assertEqual(duplicate.json()['results'][0]['status'], 'duplicate')
        self.assertEqual(conflict.json()['results'][0]['status'], 'rejected')
        self.assertEqual(KBSDecision.objects.filter(event_id=self.event_id).count(), 1)
        decision = KBSDecision.objects.get(event_id=self.event_id)
        self.assertEqual(decision.organization, self.organization)
        self.assertEqual(decision.tier, 'tier1')
        self.assertEqual(decision.actions.get().device_id, self.breaker.device_id)

    def test_partial_batch_rejection_does_not_discard_valid_event(self):
        response = self.client.post(
            '/api/kbs/edge/decision-events/',
            {'events': [self.payload, {'event_id': 'not-a-uuid'}]},
            format='json', **self._device_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['accepted'], 1)
        self.assertEqual(response.json()['rejected'], 1)
        self.assertTrue(KBSDecision.objects.filter(event_id=self.event_id).exists())

    def test_action_results_are_device_scoped_and_sync_legacy_executed(self):
        self._upload(**self._device_headers())
        body = {'results': [{
            'action_id': str(self.action_id), 'status': 'applied',
            'resulting_state': False, 'executed_at': timezone.now().isoformat(),
        }]}
        isolated = self.client.post(
            '/api/kbs/edge/action-results/', body, format='json',
            **self._device_headers(self.other_device, 'other-secret'),
        )
        updated = self.client.post(
            '/api/kbs/edge/action-results/', body, format='json',
            **self._device_headers(),
        )

        self.assertEqual(isolated.json()['results'][0]['status'], 'rejected')
        self.assertEqual(updated.json()['results'][0]['status'], 'updated')
        action = BreakerAction.objects.get(action_id=self.action_id)
        self.assertEqual(action.status, 'applied')
        self.assertTrue(action.executed)
        self.assertFalse(action.resulting_state)

    def test_device_snapshot_and_audit_survive_hardware_deletion(self):
        self._upload(**self._device_headers())
        breaker_id = self.breaker.pk
        Breaker.objects.filter(pk=breaker_id).delete()

        action = BreakerAction.objects.get(action_id=self.action_id)
        self.assertIsNone(action.breaker)
        self.assertEqual(action.device_id, 'audit-comfort-load')
        self.assertTrue(KBSDecision.objects.filter(event_id=self.event_id).exists())

    def test_history_permissions_filters_pagination_and_legacy_label(self):
        self._upload(**self._device_headers())
        other_decision = KBSDecision.objects.create(
            organization=self.other_organization, tier='tier2',
            branch='night.grid_on', facts={}, trace=[], trace_version=0,
        )

        owner_response = self.client.get(
            '/api/kbs/decision-logs/?tier=tier1&has_actions=true&page_size=1',
            **self._jwt_headers(self.owner),
        )
        other_owner_detail = self.client.get(
            f'/api/kbs/decision-logs/{self.event_id}/',
            **self._jwt_headers(self.other_owner),
        )
        technician_response = self.client.get(
            '/api/kbs/decision-logs/', **self._jwt_headers(self.technician),
        )
        legacy_detail = self.client.get(
            f'/api/kbs/decision-logs/{other_decision.event_id}/',
            **self._jwt_headers(self.technician),
        )

        self.assertEqual(owner_response.status_code, 200)
        self.assertEqual(owner_response.json()['count'], 1)
        self.assertEqual(owner_response.json()['results'][0]['tier'], 'tier1')
        self.assertEqual(other_owner_detail.status_code, 404)
        self.assertEqual(technician_response.json()['count'], 2)
        self.assertTrue(legacy_detail.json()['legacy'])
        self.assertEqual(legacy_detail.json()['trace'], [])

