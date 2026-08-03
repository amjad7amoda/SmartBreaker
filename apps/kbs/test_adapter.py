from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.breakers.models import Breaker, BreakerStatus
from apps.organizations.models import Organization
from apps.telemetry.models import Reading

from .adapters.django import DjangoKBSAdapter
from .engine.facts import SystemFacts
from .engine.rules import ActionIntent, AlertIntent, RuleResult
from .models import Alert, BreakerAction, KBSDecision, KBSSettings
from .weather import WeatherContext


class DjangoKBSAdapterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            email='adapter-owner@example.com',
            password='pw',
            role='home_user',
            is_active=True,
            must_set_password=False,
        )
        cls.organization = Organization.objects.create(
            name='Adapter Site',
            phone='10',
            latitude=33.5138,
            longitude=36.2765,
            owner=owner,
            status='active',
        )
        cls.settings = KBSSettings.objects.create(
            organization=cls.organization,
            mode='active',
            data_source='simulator',
        )
        cls.breaker = Breaker.objects.create(
            device_id='adapter-load',
            organization=cls.organization,
            priority_type='normal',
            priority_degree=3,
            mean_load_W=800,
        )
        cls.cycle_time = datetime(
            2026, 8, 3, 9, 15, tzinfo=datetime_timezone.utc
        )
        BreakerStatus.objects.create(
            breaker=cls.breaker,
            switch=True,
            online=True,
            cur_power_mW=875000,
            last_switched_on_at=cls.cycle_time - timedelta(minutes=5),
        )
        Reading.objects.create(
            organization=cls.organization,
            timestamp=cls.cycle_time - timedelta(minutes=5),
            grid_voltage_V=0,
            ac_output_active_power_W=900,
            battery_voltage_V=26,
            battery_capacity_percent=70,
            battery_charge_current_A=1,
            battery_discharge_current_A=20,
            heatsink_temp_C=40,
            pv_charging_power_W=1200,
        )
        Reading.objects.create(
            organization=cls.organization,
            timestamp=cls.cycle_time,
            grid_voltage_V=0,
            ac_output_active_power_W=1000,
            battery_voltage_V=26,
            battery_capacity_percent=70,
            battery_charge_current_A=1,
            battery_discharge_current_A=20,
            heatsink_temp_C=41,
            pv_charging_power_W=1300,
        )

    def setUp(self):
        self.adapter = DjangoKBSAdapter()

    def test_simulator_clock_uses_latest_telemetry_timestamp(self):
        resolved = self.adapter.resolve_cycle_time(
            self.organization, self.settings
        )
        self.assertEqual(resolved, self.cycle_time)

    @patch(
        'apps.kbs.adapters.django.get_weather_context',
        return_value=WeatherContext(
            season='summer', condition='clear', sunrise=None, sunset=None
        ),
    )
    def test_build_facts_translates_orm_rows_to_pure_contract(self, _weather):
        facts = self.adapter.build_facts(
            self.organization, self.settings, self.cycle_time
        )

        self.assertIsInstance(facts, SystemFacts)
        self.assertIsInstance(facts.breakers, tuple)
        self.assertEqual(facts.organization_id, self.organization.id)
        self.assertEqual(facts.load_power_W, 1000)
        self.assertEqual(facts.pv_power_W, 1300)
        self.assertEqual(facts.breakers[0].cur_power_W, 875)
        self.assertEqual(facts.breakers[0].minutes_since_on, 5)

    @patch(
        'apps.kbs.adapters.django.get_weather_context',
        return_value=WeatherContext(
            season='summer', condition='clear', sunrise=None, sunset=None
        ),
    )
    def test_persist_result_owns_database_side_effects(self, _weather):
        facts = self.adapter.build_facts(
            self.organization, self.settings, self.cycle_time
        )
        result = RuleResult(
            branch='test.adapter',
            actions=[ActionIntent(
                breaker_id=self.breaker.id,
                device_id=self.breaker.device_id,
                action='off',
                reason='adapter boundary test',
                lockout=True,
            )],
            alerts=[AlertIntent(
                kind='night_trip',
                severity='warning',
                message='adapter boundary test',
            )],
        )

        decision = self.adapter.persist_result(
            self.organization, facts, result
        )

        self.assertIsInstance(decision, KBSDecision)
        self.assertEqual(decision.facts['organization_id'], self.organization.id)
        self.assertEqual(BreakerAction.objects.count(), 1)
        self.assertEqual(Alert.objects.count(), 1)
        self.breaker.refresh_from_db()
        self.assertTrue(self.breaker.locked_out)
        self.assertEqual(self.breaker.lockout_reason, 'adapter boundary test')
