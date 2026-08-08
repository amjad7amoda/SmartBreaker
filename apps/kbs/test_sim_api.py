import csv
import tempfile
from datetime import timedelta
from pathlib import Path

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.breakers.models import Breaker, BreakerReading, BreakerStatus
from apps.organizations.models import Organization
from apps.telemetry.models import Reading

from .climate import ClimateDataError, load_climate_rows
from .models import (
    Alert, BreakerAction, KBSControllerState, KBSDecision, KBSSettings, ScheduledEvent,
    Tier1SafetyState,
)


class ClimateDataTests(APITestCase):
    def tearDown(self):
        load_climate_rows.cache_clear()

    def test_source_has_seven_complete_cities_and_known_mappings(self):
        rows = load_climate_rows()
        cities = {row['city'] for row in rows}
        self.assertEqual(len(cities), 7)
        for city in cities:
            self.assertEqual({row['month'] for row in rows if row['city'] == city}, set(range(1, 13)))
        damascus_july = next(row for row in rows if row['city'] == 'Damascus' and row['month'] == 7)
        latakia_january = next(row for row in rows if row['city'] == 'Latakia' and row['month'] == 1)
        self.assertEqual(damascus_july['typical_weather'], 'sunny')
        self.assertEqual(latakia_january['typical_weather'], 'rainy')

    def test_api_filters_without_copying_or_reducing_supported_city_list(self):
        response = self.client.get('/api/kbs/sim/climate/?city=Damascus&month=7')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['count'], 1)
        self.assertEqual(len(response.json()['cities']), 7)
        self.assertEqual(response.json()['rows'][0]['typical_weather'], 'sunny')

    def test_malformed_or_incomplete_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.csv'
            with path.open('w', newline='', encoding='utf-8') as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    'city', 'latitude_deg', 'longitude_deg', 'month', 'season',
                    'typical_weather', 'ghi_kwh_m2_day', 'clearsky_ghi_kwh_m2_day',
                    'cloud_amount_percent', 'precip_mm_day', 'temp_C', 'humidity_percent',
                ])
                writer.writerow(['Damascus', 33.51, 36.29, 7, 'summer', 'sunny', 8.19, 8.35, 17.4, .01, 26.4, 33.1])
            with self.assertRaises(ClimateDataError):
                load_climate_rows(str(path))


class SimulatorApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(email='sim-api@example.com', password='Pass123!', is_active=True)
        cls.sim_org = Organization.objects.create(
            name='Simulator A', phone='1', latitude='33.510000', longitude='36.290000',
            owner=owner, status='active',
        )
        cls.other_org = Organization.objects.create(
            name='Simulator B', phone='2', latitude='35.520000', longitude='35.790000',
            owner=owner, status='active',
        )
        cls.real_org = Organization.objects.create(
            name='Production Site', phone='3', latitude='34.730000', longitude='36.710000',
            owner=owner, status='active',
        )
        KBSSettings.objects.create(organization=cls.sim_org, data_source='simulator', mode='active')
        KBSSettings.objects.create(organization=cls.other_org, data_source='simulator', mode='active')
        KBSSettings.objects.create(organization=cls.real_org, data_source='real', mode='active')
        cls.breaker = Breaker.objects.create(
            device_id='sim-api-load', organization=cls.sim_org, priority_type='comfort',
            priority_degree=1, child_lock=True, locked_out=True, lockout_reason='safety',
            locked_at=timezone.now(),
        )
        cls.other_breaker = Breaker.objects.create(
            device_id='sim-other-load', organization=cls.other_org, priority_type='normal',
        )
        BreakerStatus.objects.create(
            breaker=cls.breaker, switch=True, online=True, countdown_1_s=60, child_lock=True,
            cur_power_mW=500000,
        )
        BreakerStatus.objects.create(breaker=cls.other_breaker, switch=True, online=True)
        now = timezone.now()
        Reading.objects.create(
            organization=cls.sim_org, timestamp=now, pv_charging_power_W=1500,
            battery_voltage_V=25.4,
        )
        Reading.objects.create(organization=cls.other_org, timestamp=now, pv_charging_power_W=999)
        BreakerReading.objects.create(breaker=cls.breaker, timestamp=now, switch=True, cur_power_mW=500000)
        BreakerReading.objects.create(breaker=cls.other_breaker, timestamp=now, switch=True)
        decision = KBSDecision.objects.create(
            organization=cls.sim_org, branch='day.surplus.comfort_on',
            facts={'weather_condition': 'clear'},
        )
        BreakerAction.objects.create(
            decision=decision, breaker=cls.breaker, action='off', countdown_s=20, reason='test',
        )
        Alert.objects.create(
            organization=cls.sim_org, kind='battery_low', severity='warning', message='test alert',
        )
        Alert.objects.create(
            organization=cls.other_org, kind='grid_outage', severity='critical', message='keep me',
        )
        event = ScheduledEvent.objects.create(
            organization=cls.sim_org, name='Preserved event',
            start_at=now + timedelta(hours=1), end_at=now + timedelta(hours=2),
        )
        event.required_breakers.add(cls.breaker)

    def test_state_expands_identity_telemetry_breakers_metadata_and_lockouts(self):
        response = self.client.get('/api/kbs/sim/state/?organization=' + str(self.sim_org.id))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['organization']['name'], 'Simulator A')
        self.assertEqual(body['latest_telemetry']['pv_charging_power_W'], 1500)
        self.assertEqual(body['metadata']['engine'], 'apps.kbs.services.run_cycle')
        self.assertEqual(body['settings']['max_inverter_power_W'], 5000)
        self.assertEqual(body['settings']['tier2_policy'], 'crisp')
        self.assertEqual(body['policy'], 'crisp')
        self.assertEqual(body['metadata']['fuzzy_profile'], 'mamdani-v1')
        self.assertEqual(body['controller_state']['current_band'], 'watch')
        self.assertFalse(body['tier1_safety']['active'])
        self.assertEqual(body['breakers'][0]['device_id'], 'sim-api-load')
        self.assertTrue(body['breakers'][0]['locked_out'])
        self.assertEqual(len(body['pending_actions']), 1)

    def test_reset_requires_confirmation_and_rejects_real_organizations(self):
        missing = self.client.post('/api/kbs/sim/reset/', {'organization': self.sim_org.id}, format='json')
        denied = self.client.post('/api/kbs/sim/reset/', {'organization': self.real_org.id, 'confirm': True}, format='json')
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(Reading.objects.filter(organization=self.sim_org).exists())

    def test_reset_is_scoped_and_preserves_definitions_settings_and_events(self):
        Tier1SafetyState.objects.create(
            organization=self.sim_org,
            active=True,
            situation='inverter_overheat',
            commands=[],
        )
        KBSControllerState.objects.create(
            organization=self.sim_org, current_band='high',
        )
        response = self.client.post(
            '/api/kbs/sim/reset/', {'organization': self.sim_org.id, 'confirm': True}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Reading.objects.filter(organization=self.sim_org).exists())
        self.assertFalse(BreakerReading.objects.filter(breaker=self.breaker).exists())
        self.assertFalse(KBSDecision.objects.filter(organization=self.sim_org).exists())
        self.assertFalse(Alert.objects.filter(organization=self.sim_org).exists())
        self.assertFalse(Tier1SafetyState.objects.filter(
            organization=self.sim_org,
        ).exists())
        self.assertFalse(KBSControllerState.objects.filter(
            organization=self.sim_org,
        ).exists())
        self.assertTrue(Reading.objects.filter(organization=self.other_org).exists())
        self.assertTrue(Alert.objects.filter(organization=self.other_org).exists())
        self.assertTrue(Breaker.objects.filter(pk=self.breaker.pk).exists())
        self.assertTrue(KBSSettings.objects.filter(organization=self.sim_org).exists())
        self.assertTrue(ScheduledEvent.objects.filter(organization=self.sim_org).exists())
        self.breaker.refresh_from_db()
        status_row = BreakerStatus.objects.get(breaker=self.breaker)
        self.assertFalse(self.breaker.locked_out)
        self.assertFalse(self.breaker.child_lock)
        self.assertEqual(status_row.countdown_1_s, 0)
        self.assertFalse(status_row.child_lock)

    def test_policy_setting_validates_and_synchronizes(self):
        response = self.client.patch(
            '/api/kbs/settings/?organization=' + str(self.sim_org.id),
            {
                'tier2_policy': 'fuzzy_shadow',
                'battery_capacity_Wh': 7200,
                'night_reserve_percent': 35,
                'max_inverter_power_W': 4400,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['updated']['tier2_policy'], 'fuzzy_shadow')
        settings = KBSSettings.objects.get(organization=self.sim_org)
        self.assertEqual(settings.tier2_policy, 'fuzzy_shadow')
        self.assertEqual(settings.battery_capacity_Wh, 7200)
        self.assertEqual(settings.night_reserve_percent, 35)
        self.assertEqual(settings.max_inverter_power_W, 4400)
        invalid = self.client.patch(
            '/api/kbs/settings/?organization=' + str(self.sim_org.id),
            {'tier2_policy': 'learn_itself'},
            format='json',
        )
        self.assertEqual(invalid.status_code, 400)

    def test_explicit_on_override_clears_lockout_and_records_state(self):
        timestamp = '2026-08-03T09:15:00Z'
        response = self.client.post('/api/kbs/sim/breaker-override/', {
            'organization': self.sim_org.id,
            'device_id': self.breaker.device_id,
            'switch': True,
            'timestamp': timestamp,
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.breaker.refresh_from_db()
        current = BreakerStatus.objects.get(breaker=self.breaker)
        self.assertTrue(current.switch)
        self.assertEqual(current.countdown_1_s, 0)
        self.assertFalse(self.breaker.locked_out)
        self.assertFalse(self.breaker.child_lock)
        self.assertTrue(BreakerReading.objects.filter(
            breaker=self.breaker, timestamp='2026-08-03T09:15:00Z',
        ).exists())

    def test_override_cannot_cross_organization_boundary(self):
        response = self.client.post('/api/kbs/sim/breaker-override/', {
            'organization': self.sim_org.id,
            'device_id': self.other_breaker.device_id,
            'switch': False,
            'timestamp': '2026-08-03T09:15:00Z',
        }, format='json')
        self.assertEqual(response.status_code, 404)
        self.assertTrue(BreakerStatus.objects.get(breaker=self.other_breaker).switch)

    def test_ack_cannot_revive_a_superseded_action(self):
        action = BreakerAction.objects.get(
            decision__organization=self.sim_org,
        )
        action.status = 'superseded'
        action.failure_reason = 'Tier-1 safety started'
        action.save()

        response = self.client.post(
            '/api/kbs/sim/ack/',
            {'action_ids': [action.id]},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['acknowledged'], 0)
        self.assertEqual(response.json()['ignored_resolved'], 1)
        action.refresh_from_db()
        self.assertEqual(action.status, 'superseded')

    def test_simulator_ack_cannot_resolve_a_real_site_action(self):
        decision = KBSDecision.objects.create(
            organization=self.real_org,
            branch='test.real-action-isolation',
            facts={},
        )
        action = BreakerAction.objects.create(
            decision=decision,
            device_id='real-site-load',
            action='off',
            reason='must be resolved by the backend executor',
        )

        response = self.client.post(
            '/api/kbs/sim/ack/',
            {'action_ids': [action.id]},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['acknowledged'], 0)
        action.refresh_from_db()
        self.assertEqual(action.status, 'pending')
