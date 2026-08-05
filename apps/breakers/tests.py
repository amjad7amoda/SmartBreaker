import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django_celery_beat.models import PeriodicTask
from rest_framework import status
from rest_framework.test import APITestCase

from apps.organizations.models import Organization

from . import scheduling
from .models import Breaker, BreakerAction, TuyaCredential
from .tasks import refresh_organization_breakers

User = get_user_model()

DEVICE_ID = 'eb53222ee822689d7dh2g3'
ONLINE_PROPERTIES = {'properties': [
    {'code': 'switch_1', 'value': False},
    {'code': 'fault', 'value': 0},
    {'code': 'online_state', 'value': 'online'},
]}
OFFLINE_PROPERTIES = {'properties': [{'code': 'online_state', 'value': 'offline'}]}


def make_user(email, role):
    return User.objects.create_user(
        email=email, password='pw', role=role, is_active=True, must_set_password=False
    )


class BreakerApiTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('owner@example.com', 'home_user')
        cls.other_owner = make_user('other@example.com', 'home_user')
        cls.technician = make_user('tech@example.com', 'technician')
        cls.admin = make_user('admin@example.com', 'admin')

        cls.organization = Organization.objects.create(
            name='Site A', phone='1', latitude=0, longitude=0, owner=cls.owner, status='active'
        )
        cls.other_organization = Organization.objects.create(
            name='Site B', phone='2', latitude=0, longitude=0, owner=cls.other_owner, status='active'
        )
        credential = TuyaCredential(organization=cls.organization, client_id='cid', region='us')
        credential.client_secret = 'secret'
        credential.save()

    def create_payload(self, **overrides):
        return {'device_id': DEVICE_ID, 'organization': self.organization.id, 'priority': 1, **overrides}

    # --- permissions ---------------------------------------------------

    def test_home_user_cannot_create_breaker(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post('/api/breakers/', self.create_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_home_user_cannot_read_credentials(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(
            self.client.get('/api/breakers/tuya-credentials/').status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_credential_response_never_exposes_the_secret(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get('/api/breakers/tuya-credentials/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('client_secret', response.json()[0])

    def test_home_user_only_sees_breakers_of_own_organizations(self):
        Breaker.objects.create(device_id=DEVICE_ID, organization=self.organization, priority=1)
        Breaker.objects.create(device_id='other-device', organization=self.other_organization, priority=1)

        self.client.force_authenticate(self.owner)
        listed = [b['device_id'] for b in self.client.get('/api/breakers/').json()]
        self.assertEqual(listed, [DEVICE_ID])

        self.client.force_authenticate(self.technician)
        self.assertEqual(len(self.client.get('/api/breakers/').json()), 2)

    # --- creation ------------------------------------------------------

    @patch('apps.breakers.serializers.TuyaClient.get_device_properties', return_value=ONLINE_PROPERTIES)
    def test_technician_creates_breaker_after_tuya_verification(self, mocked):
        self.client.force_authenticate(self.technician)
        response = self.client.post('/api/breakers/', self.create_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()['tuya'], {
            'verified': True, 'online': True, 'switch_on': False, 'fault': 0,
        })
        mocked.assert_called_once_with(DEVICE_ID)
        self.assertTrue(Breaker.objects.filter(device_id=DEVICE_ID).exists())

    @patch('apps.breakers.serializers.TuyaClient.get_device_properties', return_value=OFFLINE_PROPERTIES)
    def test_offline_device_is_registered_with_a_warning(self, _mocked):
        self.client.force_authenticate(self.technician)
        response = self.client.post('/api/breakers/', self.create_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.json()['tuya']['online'])
        self.assertIn('warning', response.json()['tuya'])

    def test_creation_rejected_when_organization_has_no_credentials(self):
        self.client.force_authenticate(self.technician)
        response = self.client.post(
            '/api/breakers/', self.create_payload(organization=self.other_organization.id)
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('organization', response.json())

    @patch('apps.breakers.serializers.TuyaClient.get_device_properties')
    def test_device_outside_the_project_is_a_client_error(self, mocked):
        from .tuya import TuyaDeviceError
        mocked.side_effect = TuyaDeviceError(1106, 'permission deny')

        self.client.force_authenticate(self.technician)
        response = self.client.post('/api/breakers/', self.create_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('device_id', response.json())

    @patch('apps.breakers.serializers.TuyaClient.get_device_properties')
    def test_bad_credentials_are_a_server_error_not_a_validation_error(self, mocked):
        from .tuya import TuyaAuthError
        mocked.side_effect = TuyaAuthError(1004, 'sign invalid')

        self.client.force_authenticate(self.technician)
        response = self.client.post('/api/breakers/', self.create_payload())
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)


class UpdateTests(APITestCase):
    """Partial updates were never exercised; these probe them directly."""

    @classmethod
    def setUpTestData(cls):
        cls.technician = make_user('tech2@example.com', 'technician')
        cls.owner = make_user('owner2@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Site C', phone='3', latitude=0, longitude=0, owner=cls.owner, status='active'
        )
        credential = TuyaCredential(organization=cls.organization, client_id='cid2', region='us')
        credential.client_secret = 'secret'
        credential.save()
        cls.credential = credential
        cls.breaker = Breaker.objects.create(
            device_id=DEVICE_ID, organization=cls.organization, priority=1
        )

    def test_patch_breaker_priority_only(self):
        self.client.force_authenticate(self.technician)
        response = self.client.patch(f'/api/breakers/{DEVICE_ID}/', {'priority': 5}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_patch_breaker_cannot_change_device_id(self):
        self.client.force_authenticate(self.technician)
        response = self.client.patch(
            f'/api/breakers/{DEVICE_ID}/', {'device_id': 'hijacked'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.breaker.refresh_from_db()
        self.assertEqual(self.breaker.device_id, DEVICE_ID)

    @patch('apps.breakers.serializers.TuyaClient.verify')
    def test_patch_credential_region_without_resending_secret(self, mocked_verify):
        self.client.force_authenticate(self.technician)
        response = self.client.patch(
            f'/api/breakers/tuya-credentials/{self.credential.id}/', {'region': 'eu'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # The stored secret must be re-verified against the new region.
        mocked_verify.assert_called_once()
        self.credential.refresh_from_db()
        self.assertEqual(self.credential.region, 'eu')
        self.assertEqual(self.credential.client_secret, 'secret')

    @patch('apps.breakers.serializers.TuyaClient.verify')
    def test_credential_creation_requires_a_secret(self, _mocked_verify):
        organization = Organization.objects.create(
            name='Site D', phone='4', latitude=0, longitude=0, owner=self.owner, status='active'
        )
        self.client.force_authenticate(self.technician)
        response = self.client.post('/api/breakers/tuya-credentials/', {
            'organization': organization.id, 'client_id': 'cid3', 'region': 'us',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('client_secret', response.json())


SPEC = {'status': [
    {'code': 'cur_voltage', 'values': '{"unit":"V","min":0,"max":5000,"scale":1,"step":1}'},
    {'code': 'cur_current', 'values': '{"unit":"mA","min":0,"max":30000,"scale":0,"step":1}'},
    {'code': 'cur_power', 'values': '{"unit":"W","min":0,"max":50000,"scale":1,"step":1}'},
]}
LIVE_PROPERTIES = {'properties': [
    {'code': 'switch_1', 'value': True},
    {'code': 'cur_voltage', 'value': 2130},
    {'code': 'cur_current', 'value': 4500},
    {'code': 'cur_power', 'value': 9587},
    {'code': 'fault', 'value': 0},
    {'code': 'online_state', 'value': 'online'},
]}


class StatusTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('sowner@example.com', 'home_user')
        cls.stranger = make_user('stranger@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Site E', phone='5', latitude=0, longitude=0, owner=cls.owner, status='active'
        )
        credential = TuyaCredential(organization=cls.organization, client_id='cid5', region='us')
        credential.client_secret = 'secret'
        credential.save()
        Breaker.objects.create(device_id=DEVICE_ID, organization=cls.organization, priority=1)

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=LIVE_PROPERTIES)
    def test_values_are_scaled_using_the_device_specification(self, _props, _spec):
        self.client.force_authenticate(self.owner)
        body = self.client.get(f'/api/breakers/{DEVICE_ID}/status/').json()

        self.assertTrue(body['units_resolved'])
        self.assertEqual(body['voltage_V'], 213.0)   # 2130, scale 1
        self.assertEqual(body['current_A'], 4.5)     # 4500 mA, scale 0
        self.assertEqual(body['power_W'], 958.7)     # 9587, scale 1
        self.assertTrue(body['is_on'])
        self.assertTrue(body['online'])
        self.assertNotIn('raw', body)

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=LIVE_PROPERTIES)
    def test_raw_properties_are_opt_in(self, _props, _spec):
        self.client.force_authenticate(self.owner)
        body = self.client.get(f'/api/breakers/{DEVICE_ID}/status/?raw=1').json()
        self.assertEqual(len(body['raw']), 6)

    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=LIVE_PROPERTIES)
    def test_unresolved_units_are_flagged_instead_of_guessed(self, _props):
        from .tuya import TuyaError
        with patch('apps.breakers.services.TuyaClient.get_device_specification',
                   side_effect=TuyaError(1106, 'permission deny')):
            self.client.force_authenticate(self.owner)
            body = self.client.get(f'/api/breakers/{DEVICE_ID}/status/').json()

        self.assertFalse(body['units_resolved'])
        self.assertEqual(body['voltage_V'], 2130)  # raw, deliberately not scaled

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=LIVE_PROPERTIES)
    def test_specification_is_fetched_once_then_cached(self, _props, spec):
        self.client.force_authenticate(self.owner)
        self.client.get(f'/api/breakers/{DEVICE_ID}/status/')
        self.client.get(f'/api/breakers/{DEVICE_ID}/status/')
        self.assertEqual(spec.call_count, 1)

    def test_status_of_another_users_breaker_is_not_found(self):
        self.client.force_authenticate(self.stranger)
        response = self.client.get(f'/api/breakers/{DEVICE_ID}/status/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


def props(switch_on, child_lock=False, countdown=0):
    return {'properties': [
        {'code': 'switch_1', 'value': switch_on},
        {'code': 'child_lock', 'value': child_lock},
        {'code': 'countdown_1', 'value': countdown},
        {'code': 'online_state', 'value': 'online'},
    ]}


class SwitchTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('cowner@example.com', 'home_user')
        cls.stranger = make_user('cstranger@example.com', 'home_user')
        cls.technician = make_user('ctech@example.com', 'technician')
        cls.organization = Organization.objects.create(
            name='Site F', phone='6', latitude=0, longitude=0, owner=cls.owner, status='active'
        )
        credential = TuyaCredential(organization=cls.organization, client_id='cid6', region='us')
        credential.client_secret = 'secret'
        credential.save()
        Breaker.objects.create(
            device_id=DEVICE_ID, organization=cls.organization, priority=1, protected=True
        )

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_owner_can_switch_on_and_result_is_confirmed(self, send, _p, _s):
        self.client.force_authenticate(self.owner)
        response = self.client.post(f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'on'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body['requested'], 'on')
        self.assertTrue(body['confirmed'])
        self.assertTrue(body['status']['is_on'])
        send.assert_called_once_with(DEVICE_ID, [{'code': 'switch', 'value': True}])

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(False))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_protected_breaker_can_still_be_switched_off(self, send, _p, _s):
        self.client.force_authenticate(self.technician)
        response = self.client.post(f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'off'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()['confirmed'])
        send.assert_called_once_with(DEVICE_ID, [{'code': 'switch', 'value': False}])

    @patch('apps.breakers.services.CONFIRM_RETRY_DELAY', 0)
    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(False))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_relay_that_never_moved_is_reported_unconfirmed(self, _send, properties, _s):
        self.client.force_authenticate(self.owner)
        response = self.client.post(f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'on'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()['confirmed'])
        self.assertEqual(properties.call_count, 2)  # initial read plus one retry

    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_offline_device_is_a_client_error(self, send):
        from .tuya import TuyaDeviceError
        send.side_effect = TuyaDeviceError(2007, 'device is offline')

        self.client.force_authenticate(self.owner)
        response = self.client.post(f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'on'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_stranger_cannot_switch_someone_elses_breaker(self):
        self.client.force_authenticate(self.stranger)
        response = self.client.post(f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'off'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_state_is_rejected(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'toggle'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ActionLogTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('aowner@example.com', 'home_user')
        cls.stranger = make_user('astranger@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Site J', phone='10', latitude=0, longitude=0, owner=cls.owner, status='active'
        )
        credential = TuyaCredential(organization=cls.organization, client_id='cid10', region='us')
        credential.client_secret = 'secret'
        credential.save()
        Breaker.objects.create(device_id=DEVICE_ID, organization=cls.organization, priority=1)

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_switching_records_an_action_with_the_actor_and_reason(self, _send, _p, _s):
        self.client.force_authenticate(self.owner)
        self.client.post(
            f'/api/breakers/{DEVICE_ID}/switch/',
            {'state': 'on', 'reason': 'manual test'}, format='json',
        )

        action = BreakerAction.objects.get()
        self.assertEqual(action.action, 'switch_on')
        self.assertEqual(action.source, 'manual')
        self.assertEqual(action.reason, 'manual test')
        self.assertEqual(action.actor, self.owner)
        self.assertTrue(action.confirmed)
        # The Tuya status is snapshotted because it is persisted nowhere else.
        self.assertTrue(action.breaker_status['is_on'])

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_the_latest_reading_is_copied_into_the_action(self, _send, _p, _s):
        from django.utils import timezone
        from apps.telemetry.models import Reading

        Reading.objects.create(
            organization=self.organization, timestamp=timezone.now(), output_load_percent=73.0
        )

        self.client.force_authenticate(self.owner)
        self.client.post(f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'on'}, format='json')

        action = BreakerAction.objects.get()
        self.assertEqual(action.telemetry['output_load_percent'], 73.0)

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_an_action_is_still_recorded_when_no_telemetry_has_arrived(self, _send, _p, _s):
        self.client.force_authenticate(self.owner)
        self.client.post(f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'on'}, format='json')

        self.assertIsNone(BreakerAction.objects.get().telemetry)

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(True, countdown=1800))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_actions_are_listed_and_filterable(self, _send, _p, _s):
        self.client.force_authenticate(self.owner)
        self.client.post(f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'on'}, format='json')
        self.client.post(f'/api/breakers/{DEVICE_ID}/countdown/', {'minutes': 30}, format='json')

        listed = self.client.get('/api/breakers/actions/').json()
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0]['device_id'], DEVICE_ID)

        filtered = self.client.get('/api/breakers/actions/?action=countdown_set').json()
        self.assertEqual([a['action'] for a in filtered], ['countdown_set'])

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_a_stranger_cannot_see_another_organizations_actions(self, _send, _p, _s):
        self.client.force_authenticate(self.owner)
        self.client.post(f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'on'}, format='json')
        action_id = BreakerAction.objects.get().id

        self.client.force_authenticate(self.stranger)
        self.assertEqual(self.client.get('/api/breakers/actions/').json(), [])
        self.assertEqual(
            self.client.get(f'/api/breakers/actions/{action_id}/').status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_actions_cannot_be_written_through_the_api(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post('/api/breakers/actions/', {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class SigningTests(APITestCase):
    """The body must be signed byte-for-byte as it is sent."""

    def test_body_changes_the_signature(self):
        from .tuya import TuyaClient

        credential = TuyaCredential(client_id='cid', region='us')
        credential.client_secret = 'secret'
        client = TuyaClient(credential)

        empty = client.sign('POST', '/p', '1', 'tok', '')
        with_body = client.sign('POST', '/p', '1', 'tok', '{"commands":[]}')
        self.assertNotEqual(empty, with_body)


class ChildLockTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('lowner@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Site G', phone='7', latitude=0, longitude=0, owner=cls.owner, status='active'
        )
        credential = TuyaCredential(organization=cls.organization, client_id='cid7', region='us')
        credential.client_secret = 'secret'
        credential.save()
        cls.breaker = Breaker.objects.create(
            device_id=DEVICE_ID, organization=cls.organization, priority=1
        )

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           side_effect=[props(True, child_lock=False), props(True, child_lock=True)])
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_enabling_child_lock_writes_to_tuya_and_persists(self, send, _p, _s):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            f'/api/breakers/{DEVICE_ID}/child-lock/', {'enabled': True}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()['confirmed'])
        send.assert_called_once_with(DEVICE_ID, [{'code': 'child_lock', 'value': True}])
        self.breaker.refresh_from_db()
        self.assertTrue(self.breaker.child_lock)

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(True, child_lock=True))
    def test_status_read_reconciles_a_change_made_in_the_tuya_app(self, _p, _s):
        self.assertFalse(self.breaker.child_lock)

        self.client.force_authenticate(self.owner)
        body = self.client.get(f'/api/breakers/{DEVICE_ID}/status/').json()

        self.assertTrue(body['child_lock'])
        self.breaker.refresh_from_db()
        self.assertTrue(self.breaker.child_lock)

    def test_child_lock_cannot_be_changed_by_a_plain_patch(self):
        technician = make_user('ltech@example.com', 'technician')
        self.client.force_authenticate(technician)
        response = self.client.patch(
            f'/api/breakers/{DEVICE_ID}/', {'child_lock': True}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.breaker.refresh_from_db()
        self.assertFalse(self.breaker.child_lock)

    @patch('apps.breakers.services.CONFIRM_RETRY_DELAY', 0)
    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(True, child_lock=False))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_lock_that_never_engaged_is_reported_unconfirmed(self, _send, _p, _s):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            f'/api/breakers/{DEVICE_ID}/child-lock/', {'enabled': True}, format='json'
        )
        self.assertFalse(response.json()['confirmed'])

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(False, child_lock=True))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_locking_an_already_locked_breaker_says_so_and_sends_nothing(self, send, _p, _s):
        self.client.force_authenticate(self.owner)
        body = self.client.post(
            f'/api/breakers/{DEVICE_ID}/child-lock/', {'enabled': True}, format='json'
        ).json()

        self.assertTrue(body['confirmed'])
        self.assertFalse(body['changed'])
        self.assertIn('already child-locked', body['reason'])
        send.assert_not_called()
        # A request that changed nothing is not an action worth logging.
        self.assertFalse(BreakerAction.objects.filter(action='child_lock_on').exists())

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(True, child_lock=False))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_unlocking_an_already_unlocked_breaker_says_so_and_sends_nothing(self, send, _p, _s):
        self.client.force_authenticate(self.owner)
        body = self.client.post(
            f'/api/breakers/{DEVICE_ID}/child-lock/', {'enabled': False}, format='json'
        ).json()

        self.assertTrue(body['confirmed'])
        self.assertFalse(body['changed'])
        self.assertIn('already unlocked', body['reason'])
        send.assert_not_called()
        self.assertFalse(BreakerAction.objects.filter(action='child_lock_off').exists())


class CountdownTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('downer@example.com', 'home_user')
        cls.stranger = make_user('dstranger@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Site I', phone='9', latitude=0, longitude=0, owner=cls.owner, status='active'
        )
        credential = TuyaCredential(organization=cls.organization, client_id='cid9', region='us')
        credential.client_secret = 'secret'
        credential.save()
        Breaker.objects.create(device_id=DEVICE_ID, organization=cls.organization, priority=1)

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(True, countdown=1800))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_countdown_is_sent_to_tuya_in_seconds(self, send, _p, _s):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            f'/api/breakers/{DEVICE_ID}/countdown/', {'minutes': 30}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body['confirmed'])
        self.assertEqual(body['remaining_s'], 1800)
        self.assertEqual(body['action'], 'off')  # the breaker is on, so it will open
        self.assertIsNotNone(body['switches_at'])
        send.assert_called_once_with(DEVICE_ID, [{'code': 'countdown_1', 'value': 1800}])

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(True, countdown=0))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_zero_minutes_cancels_the_countdown(self, send, _p, _s):
        self.client.force_authenticate(self.owner)
        body = self.client.post(
            f'/api/breakers/{DEVICE_ID}/countdown/', {'minutes': 0}, format='json'
        ).json()

        self.assertTrue(body['confirmed'])
        self.assertIsNone(body['switches_at'])
        self.assertIsNone(body['action'])
        send.assert_called_once_with(DEVICE_ID, [{'code': 'countdown_1', 'value': 0}])

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(False, countdown=1800))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_countdown_on_an_open_breaker_schedules_a_close(self, send, _p, _s):
        """The countdown toggles, so on an off breaker it is a delayed switch-on."""
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            f'/api/breakers/{DEVICE_ID}/countdown/', {'minutes': 30}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body['confirmed'])
        self.assertEqual(body['action'], 'on')
        send.assert_called_once_with(DEVICE_ID, [{'code': 'countdown_1', 'value': 1800}])

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(True, child_lock=True, countdown=1800))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_child_locked_countdown_is_accepted_with_a_warning(self, send, _p, _s):
        self.client.force_authenticate(self.owner)
        body = self.client.post(
            f'/api/breakers/{DEVICE_ID}/countdown/', {'minutes': 30}, format='json'
        ).json()

        self.assertTrue(body['confirmed'])
        self.assertIn('warning', body)
        send.assert_called_once_with(DEVICE_ID, [{'code': 'countdown_1', 'value': 1800}])

    @patch('apps.breakers.services.CONFIRM_RETRY_DELAY', 0)
    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(True, countdown=0))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_countdown_the_device_never_started_is_reported_unconfirmed(self, _send, _p, _s):
        self.client.force_authenticate(self.owner)
        body = self.client.post(
            f'/api/breakers/{DEVICE_ID}/countdown/', {'minutes': 30}, format='json'
        ).json()
        self.assertFalse(body['confirmed'])

    def test_countdown_beyond_the_tuya_cap_is_rejected(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            f'/api/breakers/{DEVICE_ID}/countdown/', {'minutes': 1441}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_stranger_cannot_schedule_someone_elses_breaker(self):
        self.client.force_authenticate(self.stranger)
        response = self.client.post(
            f'/api/breakers/{DEVICE_ID}/countdown/', {'minutes': 30}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ChildLockLockoutTests(APITestCase):
    """The lock opens the relay and blocks commands until released."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('gowner@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Site H', phone='8', latitude=0, longitude=0, owner=cls.owner, status='active'
        )
        credential = TuyaCredential(organization=cls.organization, client_id='cid8', region='us')
        credential.client_secret = 'secret'
        credential.save()
        Breaker.objects.create(device_id=DEVICE_ID, organization=cls.organization, priority=1)

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           side_effect=[props(True, child_lock=False), props(False, child_lock=True)])
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_enabling_the_lock_warns_that_the_load_is_cut(self, send, _p, _s):
        self.client.force_authenticate(self.owner)
        body = self.client.post(
            f'/api/breakers/{DEVICE_ID}/child-lock/', {'enabled': True}, format='json'
        ).json()

        self.assertTrue(body['confirmed'])
        self.assertIn('warning', body)
        self.assertFalse(body['status']['is_on'])
        # Only the lock is written; no attempt is made to fight the hardware.
        self.assertEqual(send.call_count, 1)

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           side_effect=[props(False, child_lock=True), props(True, child_lock=False)])
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_releasing_the_lock_carries_no_warning(self, _send, _p, _s):
        self.client.force_authenticate(self.owner)
        body = self.client.post(
            f'/api/breakers/{DEVICE_ID}/child-lock/', {'enabled': False}, format='json'
        ).json()
        self.assertTrue(body['confirmed'])
        self.assertNotIn('warning', body)

    @patch('apps.breakers.services.CONFIRM_RETRY_DELAY', 0)
    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=props(False, child_lock=True))
    @patch('apps.breakers.services.TuyaClient.send_commands')
    def test_switching_a_locked_breaker_explains_why_it_failed(self, _send, _p, _s):
        self.client.force_authenticate(self.owner)
        body = self.client.post(
            f'/api/breakers/{DEVICE_ID}/switch/', {'state': 'on'}, format='json'
        ).json()

        self.assertFalse(body['confirmed'])
        self.assertIn('child-locked', body['reason'])


class OrganizationPollingTests(APITestCase):
    """The poller is created by traffic and torn down by silence."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('powner@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Site K', phone='11', latitude=0, longitude=0, owner=cls.owner, status='active'
        )
        credential = TuyaCredential(organization=cls.organization, client_id='cid11', region='us')
        credential.client_secret = 'secret'
        credential.save()
        Breaker.objects.create(
            device_id=DEVICE_ID, organization=cls.organization, priority=1
        )

    def setUp(self):
        cache.clear()

    @property
    def schedule(self):
        return PeriodicTask.objects.get(name=scheduling.schedule_name(self.organization.id))

    def test_first_authenticated_request_creates_the_organizations_poller(self):
        self.assertEqual(PeriodicTask.objects.count(), 0)

        self.client.force_authenticate(self.owner)
        self.client.get('/api/breakers/')

        task = self.schedule
        self.assertTrue(task.enabled)
        self.assertEqual(task.task, scheduling.TASK_NAME)
        self.assertEqual(task.interval.every, 30)
        self.assertEqual(task.interval.period, 'seconds')
        self.assertEqual(json.loads(task.args), [self.organization.id])

    def test_anonymous_traffic_starts_nothing(self):
        self.client.get('/api/breakers/')
        self.assertEqual(PeriodicTask.objects.count(), 0)

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    def test_active_poller_caches_every_breaker_status(self, _p, _s):
        scheduling.touch_organization(self.organization.id)

        result = refresh_organization_breakers(self.organization.id)

        self.assertEqual(result['refreshed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertTrue(scheduling.cached_status(DEVICE_ID)['is_on'])

    def test_poller_switches_itself_off_once_the_organization_goes_idle(self):
        scheduling.ensure_schedule(self.organization.id)  # no activity marker == idle

        result = refresh_organization_breakers(self.organization.id)

        self.assertEqual(result['stopped'], 'idle')
        self.assertFalse(self.schedule.enabled)

    def test_a_returning_user_restarts_a_stopped_poller(self):
        scheduling.ensure_schedule(self.organization.id)
        scheduling.disable_schedule(self.organization.id)
        cache.clear()  # the re-check window has elapsed

        self.client.force_authenticate(self.owner)
        self.client.get('/api/breakers/')

        self.assertTrue(self.schedule.enabled)

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties')
    def test_status_endpoint_is_served_from_the_poller_cache(self, properties, _s):
        scheduling.cache_status(DEVICE_ID, {'device_id': DEVICE_ID, 'is_on': True, 'from_cache': 1})

        self.client.force_authenticate(self.owner)
        body = self.client.get(f'/api/breakers/{DEVICE_ID}/status/').json()

        self.assertEqual(body['from_cache'], 1)
        properties.assert_not_called()

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    def test_raw_requests_bypass_the_cache_and_do_not_poison_it(self, _p, _s):
        self.client.force_authenticate(self.owner)
        body = self.client.get(f'/api/breakers/{DEVICE_ID}/status/?raw=1').json()

        self.assertIn('raw', body)
        self.assertIsNone(scheduling.cached_status(DEVICE_ID))
