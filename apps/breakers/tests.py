from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.organizations.models import Organization

from .models import (
    Breaker,
    BreakerAction,
    BreakerReading,
    BreakerStatus,
    TuyaCredential,
)
from .tasks import (
    poll_all_breakers,
    purge_breaker_readings,
    refresh_organization_breakers,
)

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
        return {
            'device_id': DEVICE_ID,
            'organization': self.organization.id,
            'priority_degree': 1,
            **overrides,
        }

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
        Breaker.objects.create(
            device_id=DEVICE_ID,
            organization=self.organization,
            priority_degree=1,
        )
        Breaker.objects.create(
            device_id='other-device',
            organization=self.other_organization,
            priority_degree=1,
        )

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
            device_id=DEVICE_ID,
            organization=cls.organization,
            priority_degree=1,
        )

    def test_patch_breaker_priority_degree_only(self):
        self.client.force_authenticate(self.technician)
        response = self.client.patch(
            f'/api/breakers/{DEVICE_ID}/',
            {'priority_degree': 5},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.breaker.refresh_from_db()
        self.assertEqual(self.breaker.priority_degree, 5)

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
        Breaker.objects.create(
            device_id=DEVICE_ID,
            organization=cls.organization,
            priority_degree=1,
        )

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
            device_id=DEVICE_ID,
            organization=cls.organization,
            priority_degree=1,
            priority_type='mandatory',
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
        Breaker.objects.create(
            device_id=DEVICE_ID, organization=cls.organization, priority_degree=1,
        )

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
            device_id=DEVICE_ID,
            organization=cls.organization,
            priority_degree=1,
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
        Breaker.objects.create(
            device_id=DEVICE_ID, organization=cls.organization, priority_degree=1,
        )

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
        Breaker.objects.create(
            device_id=DEVICE_ID,
            organization=cls.organization,
            priority_degree=1,
        )

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
    """The poller runs unconditionally and persists what it reads."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('powner@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Site K', phone='11', latitude=0, longitude=0, owner=cls.owner, status='active'
        )
        credential = TuyaCredential(organization=cls.organization, client_id='cid11', region='us')
        credential.client_secret = 'secret'
        credential.save()
        cls.breaker = Breaker.objects.create(
            device_id=DEVICE_ID, organization=cls.organization, priority_degree=1
        )

    def setUp(self):
        cache.clear()

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    def test_poller_reports_what_it_refreshed(self, _p, _s):
        result = refresh_organization_breakers(self.organization.id)

        self.assertEqual(result['refreshed'], 1)
        self.assertEqual(result['failed'], 0)

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    def test_poller_persists_the_rows_the_kbs_adapter_reads(self, _p, _s):
        refresh_organization_breakers(self.organization.id)

        current = BreakerStatus.objects.get(breaker=self.breaker)
        self.assertTrue(current.switch)
        self.assertTrue(current.online)
        self.assertIsNotNone(current.last_switched_on_at)
        reading = BreakerReading.objects.get(breaker=self.breaker)
        self.assertTrue(reading.switch)

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties',
           return_value=LIVE_PROPERTIES)
    def test_persisted_status_is_scaled_into_milli_units(self, _p, _s):
        refresh_organization_breakers(self.organization.id)

        current = BreakerStatus.objects.get(breaker=self.breaker)
        self.assertAlmostEqual(current.cur_voltage_mV, 213000.0)   # 2130 -> 213.0 V
        self.assertAlmostEqual(current.cur_power_mW, 958700.0)     # 9587 -> 958.7 W
        self.assertAlmostEqual(current.cur_current_mA, 4500.0)     # 4500 mA -> 4.5 A

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    def test_polling_fans_out_only_to_sites_with_credentials(self, properties, _s):
        stranger = make_user('nocred@example.com', 'home_user')
        uncredentialed = Organization.objects.create(
            name='Site L', phone='12', latitude=0, longitude=0,
            owner=stranger, status='active',
        )
        Breaker.objects.create(
            device_id='no-credential-device', organization=uncredentialed,
            priority_degree=1,
        )

        result = poll_all_breakers()

        self.assertEqual(result['organizations'], 1)
        self.assertEqual(properties.call_count, 1)

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    def test_every_status_request_reaches_the_device(self, properties, _s):
        """No cache sits in front of the endpoint: a poll never serves a read."""
        refresh_organization_breakers(self.organization.id)
        self.client.force_authenticate(self.owner)

        self.client.get(f'/api/breakers/{DEVICE_ID}/status/')
        self.client.get(f'/api/breakers/{DEVICE_ID}/status/')

        self.assertEqual(properties.call_count, 3)  # one poll + two reads

    @patch('apps.breakers.services.TuyaClient.get_device_specification', return_value=SPEC)
    @patch('apps.breakers.services.TuyaClient.get_device_properties', return_value=props(True))
    def test_raw_requests_still_persist_the_status_row(self, _p, _s):
        self.client.force_authenticate(self.owner)
        body = self.client.get(f'/api/breakers/{DEVICE_ID}/status/?raw=1').json()

        self.assertIn('raw', body)
        self.assertTrue(BreakerStatus.objects.get(breaker=self.breaker).switch)


class SimulatorStatusIngestTests(APITestCase):
    """The simulator bulk endpoint updates the adapter's source tables."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('sim-owner@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Simulator Site', phone='9', latitude=0, longitude=0,
            owner=cls.owner, status='active',
        )
        cls.breaker = Breaker.objects.create(
            device_id='sim-breaker-1',
            organization=cls.organization,
            priority_degree=2,
        )

    @staticmethod
    def payload(device_id='sim-breaker-1', **overrides):
        return [{
            'device_id': device_id,
            'timestamp': '2026-08-03T09:15:00Z',
            'switch': True,
            'countdown_1_s': 0,
            'cur_current_mA': 3500,
            'cur_power_mW': 875000,
            'cur_voltage_mV': 250000,
            'fault': '',
            'relay_status': 'last',
            'child_lock': True,
            'cycle_time': '',
            'online': True,
            **overrides,
        }]

    def test_bulk_status_creates_status_and_history(self):
        from .models import BreakerReading, BreakerStatus

        response = self.client.post(
            '/api/breakers/status/', self.payload(), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json(), {'received': 1, 'readings_created': 1})
        current = BreakerStatus.objects.get(breaker=self.breaker)
        self.assertTrue(current.switch)
        self.assertTrue(current.online)
        self.assertEqual(current.cur_power_mW, 875000)
        self.assertEqual(
            current.last_switched_on_at.isoformat(),
            '2026-08-03T09:15:00+00:00',
        )
        self.breaker.refresh_from_db()
        self.assertTrue(self.breaker.child_lock)
        self.assertTrue(
            BreakerReading.objects.filter(breaker=self.breaker).exists()
        )

    def test_ingested_history_row_mirrors_the_status_row(self):
        self.client.post('/api/breakers/status/', self.payload(), format='json')

        current = BreakerStatus.objects.get(breaker=self.breaker)
        sample = BreakerReading.objects.get(breaker=self.breaker)

        for field in BreakerStatus.SAMPLE_FIELDS:
            self.assertEqual(
                getattr(sample, field), getattr(current, field), msg=field,
            )
        self.assertEqual(sample.cur_current_mA, 3500)
        self.assertEqual(sample.cur_voltage_mV, 250000)
        self.assertTrue(sample.child_lock)

    def test_replayed_timestamp_is_idempotent(self):
        from .models import BreakerReading

        first = self.client.post(
            '/api/breakers/status/', self.payload(), format='json'
        )
        second = self.client.post(
            '/api/breakers/status/', self.payload(), format='json'
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.json()['readings_created'], 0)
        self.assertEqual(BreakerReading.objects.count(), 1)

    def test_unknown_device_rejects_whole_batch(self):
        from .models import BreakerStatus

        batch = self.payload() + self.payload(device_id='missing-device')
        response = self.client.post(
            '/api/breakers/status/', batch, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(BreakerStatus.objects.exists())

    def test_duplicate_device_in_batch_is_rejected(self):
        response = self.client.post(
            '/api/breakers/status/', self.payload() * 2, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class BreakerReadingRetentionTests(APITestCase):
    """Readings age out; current state does not."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('retention@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Site M', phone='13', latitude=0, longitude=0,
            owner=cls.owner, status='active',
        )
        cls.breaker = Breaker.objects.create(
            device_id='retention-device', organization=cls.organization,
            priority_degree=1,
        )

    def reading(self, minutes_ago):
        return BreakerReading.objects.create(
            breaker=self.breaker,
            timestamp=timezone.now() - timedelta(minutes=minutes_ago),
            switch=True,
            cur_power_mW=1000.0,
        )

    def test_readings_past_the_window_are_deleted(self):
        stale = self.reading(90)
        fresh = self.reading(30)

        result = purge_breaker_readings()

        self.assertEqual(result['deleted'], 1)
        self.assertFalse(BreakerReading.objects.filter(pk=stale.pk).exists())
        self.assertTrue(BreakerReading.objects.filter(pk=fresh.pk).exists())

    def test_purging_readings_leaves_current_state_alone(self):
        BreakerStatus.objects.create(breaker=self.breaker, switch=True, online=True)
        self.reading(120)

        purge_breaker_readings()

        self.assertTrue(BreakerStatus.objects.get(breaker=self.breaker).switch)


class StoredStatusReadTests(APITestCase):
    """Reading back what was ingested, without ever calling Tuya."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('reader@example.com', 'home_user')
        cls.stranger = make_user('stranger@example.com', 'home_user')
        cls.technician = make_user('reader-tech@example.com', 'technician')

        cls.organization = Organization.objects.create(
            name='Site N', phone='14', latitude=0, longitude=0,
            owner=cls.owner, status='active',
        )
        cls.other_organization = Organization.objects.create(
            name='Site O', phone='15', latitude=0, longitude=0,
            owner=cls.stranger, status='active',
        )
        cls.breaker = Breaker.objects.create(
            device_id='read-device', name='Heater1',
            organization=cls.organization, priority_degree=1,
            priority_type='normal', load_type='motor',
        )
        cls.other_breaker = Breaker.objects.create(
            device_id='read-device-other',
            organization=cls.other_organization, priority_degree=1,
        )
        BreakerStatus.objects.create(
            breaker=cls.breaker, switch=True, online=True,
            cur_current_mA=133, cur_power_mW=22800, cur_voltage_mV=204200,
        )
        BreakerStatus.objects.create(
            breaker=cls.other_breaker, switch=False, online=True,
        )

    def test_stored_status_matches_the_live_read_contract(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get('/api/breakers/statuses/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(len(body), 1)
        row = body[0]
        self.assertEqual(row.pop('reported_at')[:4], '2026')
        self.assertEqual(row, {
            'device_id': 'read-device',
            'name': 'Heater1',
            'organization': self.organization.id,
            'priority_type': 'normal',
            'priority': 1,
            'type': 'motor',
            'online': True,
            'is_on': True,
            'child_lock': False,
            'countdown_s': 0,
            'fault': 0,
            'voltage_V': 204.2,
            'current_A': 0.133,
            'power_W': 22.8,
            'units_resolved': True,
        })

    def test_milli_unit_columns_are_not_exposed(self):
        self.client.force_authenticate(self.owner)
        row = self.client.get('/api/breakers/statuses/').json()[0]

        for column in ('cur_power_mW', 'cur_current_mA', 'cur_voltage_mV'):
            self.assertNotIn(column, row)
        self.assertNotIn('switch', row)          # it is called is_on now
        self.assertNotIn('countdown_1_s', row)   # ... and countdown_s

    def test_unresolved_units_are_reported_per_row(self):
        BreakerStatus.objects.filter(breaker=self.breaker).update(
            units_resolved=False,
        )
        self.client.force_authenticate(self.owner)

        row = self.client.get('/api/breakers/statuses/').json()[0]

        self.assertFalse(row['units_resolved'])

    def test_a_fault_code_survives_as_a_number(self):
        BreakerStatus.objects.filter(breaker=self.breaker).update(fault='2')
        self.client.force_authenticate(self.owner)

        row = self.client.get('/api/breakers/statuses/').json()[0]

        self.assertEqual(row['fault'], 2)

    def test_is_on_filters_the_list(self):
        self.client.force_authenticate(self.technician)

        on = self.client.get('/api/breakers/statuses/?is_on=true').json()
        off = self.client.get('/api/breakers/statuses/?is_on=false').json()

        self.assertEqual([r['device_id'] for r in on], ['read-device'])
        self.assertEqual([r['device_id'] for r in off], ['read-device-other'])

    def test_status_list_is_scoped_to_the_callers_organizations(self):
        self.client.force_authenticate(self.owner)
        listed = [r['device_id'] for r in self.client.get('/api/breakers/statuses/').json()]
        self.assertEqual(listed, ['read-device'])

        self.client.force_authenticate(self.technician)
        self.assertEqual(len(self.client.get('/api/breakers/statuses/').json()), 2)

    def test_status_detail_hides_other_organizations_behind_a_404(self):
        self.client.force_authenticate(self.owner)

        mine = self.client.get('/api/breakers/statuses/read-device/')
        self.assertEqual(mine.status_code, status.HTTP_200_OK)
        self.assertEqual(mine.json()['device_id'], 'read-device')

        theirs = self.client.get('/api/breakers/statuses/read-device-other/')
        self.assertEqual(theirs.status_code, status.HTTP_404_NOT_FOUND)

    def test_status_list_requires_authentication(self):
        self.assertEqual(
            self.client.get('/api/breakers/statuses/').status_code,
            status.HTTP_401_UNAUTHORIZED,
        )


class BreakerReadingReadTests(APITestCase):
    """The per-breaker sample history endpoints."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user('history@example.com', 'home_user')
        cls.stranger = make_user('history-stranger@example.com', 'home_user')
        cls.organization = Organization.objects.create(
            name='Site P', phone='16', latitude=0, longitude=0,
            owner=cls.owner, status='active',
        )
        cls.other_organization = Organization.objects.create(
            name='Site Q', phone='17', latitude=0, longitude=0,
            owner=cls.stranger, status='active',
        )
        cls.breaker = Breaker.objects.create(
            device_id='history-device', organization=cls.organization,
            priority_degree=1,
        )
        cls.other_breaker = Breaker.objects.create(
            device_id='history-device-other',
            organization=cls.other_organization, priority_degree=1,
        )
        cls.now = timezone.now().replace(microsecond=0)
        for minutes in (30, 20, 10):
            BreakerReading.objects.create(
                breaker=cls.breaker,
                timestamp=cls.now - timedelta(minutes=minutes),
                switch=True, online=True, child_lock=False,
                countdown_1_s=minutes, fault='', relay_status='power_on',
                cycle_time='',
                cur_current_mA=minutes * 100.0,
                cur_power_mW=minutes * 1000.0,
                cur_voltage_mV=230000.0,
            )
        BreakerReading.objects.create(
            breaker=cls.other_breaker, timestamp=cls.now,
            switch=False, cur_power_mW=0.0,
        )

    def test_history_is_paginated_and_newest_first(self):
        self.client.force_authenticate(self.owner)
        body = self.client.get('/api/breakers/history-device/readings/').json()

        self.assertEqual(body['count'], 3)
        timestamps = [row['timestamp'] for row in body['results']]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))
        self.assertEqual(body['results'][0]['power_W'], 10.0)

    def test_a_sample_carries_the_whole_snapshot_not_just_power(self):
        self.client.force_authenticate(self.owner)
        row = self.client.get(
            '/api/breakers/history-device/readings/',
        ).json()['results'][0]

        self.assertEqual(row.pop('timestamp')[:4], '2026')
        self.assertEqual(row, {
            'device_id': 'history-device',
            'name': '',
            'organization': self.organization.id,
            'priority_type': 'normal',
            'priority': 1,
            'type': 'normal',
            'online': True,
            'is_on': True,
            'child_lock': False,
            'countdown_s': 10,
            'fault': 0,
            'voltage_V': 230.0,
            'current_A': 1.0,
            'power_W': 10.0,
            'units_resolved': True,
        })

    def test_a_sample_and_a_current_status_share_one_shape(self):
        """The two endpoints differ only in which timestamp they carry."""
        BreakerStatus.objects.create(breaker=self.breaker, switch=True)
        self.client.force_authenticate(self.owner)

        sample = self.client.get(
            '/api/breakers/history-device/readings/',
        ).json()['results'][0]
        current = self.client.get('/api/breakers/statuses/history-device/').json()

        self.assertEqual(set(sample) - {'timestamp'}, set(current) - {'reported_at'})

    def test_history_of_an_unreachable_breaker_is_a_404(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get('/api/breakers/history-device-other/readings/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_time_window_narrows_the_history(self):
        self.client.force_authenticate(self.owner)
        since = (self.now - timedelta(minutes=25)).isoformat()

        body = self.client.get(
            '/api/breakers/history-device/readings/', {'since': since},
        ).json()

        self.assertEqual(body['count'], 2)

    def test_unparseable_time_window_is_rejected(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(
            '/api/breakers/history-device/readings/', {'since': 'yesterday'},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('since', response.json())

    def test_flat_history_endpoint_covers_every_breaker_in_scope(self):
        self.client.force_authenticate(self.owner)

        body = self.client.get('/api/breakers/readings/').json()
        self.assertEqual(body['count'], 3)

        filtered = self.client.get(
            '/api/breakers/readings/', {'device_id': 'history-device-other'},
        ).json()
        self.assertEqual(filtered['count'], 0)
