import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.organizations.models import Organization

from .models import Reading


class ReadingIngestTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            email='pi-owner@example.com', role='home_user', password='UserPass123!'
        )
        self.org = Organization.objects.create(
            name='Solar Site', phone='12345', latitude='40.123456',
            longitude='23.654321', owner=self.user,
        )
        self.url = reverse('reading-ingest')

    def _post(self, payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type='application/json')

    def test_receives_single_reading(self):
        resp = self._post({
            'organization': self.org.id,
            'timestamp': '2026-07-26T10:00:00Z',
            'grid_voltage_V': 230.5,
            'battery_capacity_percent': 87,
            'device_status_flags': '10010000',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json(), {'received': 1})
        self.assertEqual(Reading.objects.filter(organization=self.org).count(), 1)
        print(f"Received {resp.json()['received']} reading -> stored in DB")

    def test_receives_batch_of_readings(self):
        batch = [
            {'organization': self.org.id, 'timestamp': '2026-07-26T10:00:01Z', 'ac_output_active_power_W': 1500},
            {'organization': self.org.id, 'timestamp': '2026-07-26T10:00:02Z', 'ac_output_active_power_W': 1520},
            {'organization': self.org.id, 'timestamp': '2026-07-26T10:00:03Z', 'ac_output_active_power_W': 1490},
        ]
        resp = self._post(batch)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json(), {'received': 3})
        self.assertEqual(Reading.objects.filter(organization=self.org).count(), 3)
        print(f"Received a batch of {resp.json()['received']} readings -> stored in DB")

    def test_resent_batch_is_idempotent(self):
        batch = [
            {'organization': self.org.id, 'timestamp': '2026-07-26T10:00:01Z'},
            {'organization': self.org.id, 'timestamp': '2026-07-26T10:00:02Z'},
        ]
        self._post(batch)
        self._post(batch)  # resend after a simulated reconnect
        self.assertEqual(Reading.objects.filter(organization=self.org).count(), 2)
        print("Resent batch ignored duplicates -> no double-counting")

    def test_missing_timestamp_is_rejected(self):
        resp = self._post({'organization': self.org.id})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('timestamp', resp.json()[0])

    def _post_capturing_cycles(self, payload):
        with patch('apps.kbs.tasks.run_kbs_cycle_for_org.delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self._post(payload)
        return resp, delay

    def test_accepted_batch_triggers_one_kbs_cycle_per_site(self):
        other_user = User.objects.create_user(
            email='second-owner@example.com', role='home_user', password='UserPass123!'
        )
        other_org = Organization.objects.create(
            name='Second Site', phone='67890', latitude='41.123456',
            longitude='24.654321', owner=other_user,
        )
        _, delay = self._post_capturing_cycles([
            {'organization': self.org.id, 'timestamp': '2026-07-26T10:00:01Z'},
            {'organization': self.org.id, 'timestamp': '2026-07-26T10:00:02Z'},
            {'organization': other_org.id, 'timestamp': '2026-07-26T10:00:02Z'},
        ])
        self.assertEqual(
            sorted(call.args[0] for call in delay.call_args_list),
            sorted([self.org.id, other_org.id]),
        )
        print('Batch from 2 sites -> 1 KBS cycle queued per site')

    def test_rejected_batch_triggers_no_cycle(self):
        resp, delay = self._post_capturing_cycles({'organization': self.org.id})
        self.assertEqual(resp.status_code, 400)
        delay.assert_not_called()
        print('Invalid reading -> no KBS cycle queued')


class ReadingReadTests(APITestCase):
    """Browsing the inverter samples that the Pi already pushed."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            email='reader@example.com', role='home_user', password='pw',
            is_active=True, must_set_password=False,
        )
        cls.stranger = User.objects.create_user(
            email='reader-stranger@example.com', role='home_user', password='pw',
            is_active=True, must_set_password=False,
        )
        cls.technician = User.objects.create_user(
            email='reader-tech@example.com', role='technician', password='pw',
            is_active=True, must_set_password=False,
        )
        cls.org = Organization.objects.create(
            name='Read Site', phone='1', latitude=0, longitude=0, owner=cls.owner,
        )
        cls.other_org = Organization.objects.create(
            name='Other Site', phone='2', latitude=0, longitude=0, owner=cls.stranger,
        )
        cls.now = timezone.now().replace(microsecond=0)
        for minutes, watts in ((30, 1000), (20, 1500), (10, 2000)):
            Reading.objects.create(
                organization=cls.org,
                timestamp=cls.now - timedelta(minutes=minutes),
                ac_output_active_power_W=watts,
                battery_capacity_percent=90 - minutes,
            )
        Reading.objects.create(
            organization=cls.other_org, timestamp=cls.now,
            ac_output_active_power_W=42,
        )

    def test_listing_is_paginated_and_scoped_to_own_sites(self):
        self.client.force_authenticate(self.owner)
        body = self.client.get('/api/telemetry/readings/').json()

        self.assertEqual(body['count'], 3)
        self.assertEqual(body['results'][0]['ac_output_active_power_W'], 2000)
        self.assertEqual(body['results'][0]['organization_name'], 'Read Site')

    def test_technician_sees_every_site(self):
        self.client.force_authenticate(self.technician)
        self.assertEqual(self.client.get('/api/telemetry/readings/').json()['count'], 4)

    def test_listing_requires_authentication(self):
        self.assertEqual(self.client.get('/api/telemetry/readings/').status_code, 401)

    def test_ingest_stays_open_to_the_unauthenticated_edge_agent(self):
        response = self.client.post(
            '/api/telemetry/readings/',
            {'organization': self.org.id, 'timestamp': '2026-08-04T10:00:00Z'},
            format='json',
        )
        self.assertEqual(response.status_code, 201)

    def test_time_window_narrows_the_listing(self):
        self.client.force_authenticate(self.owner)
        since = (self.now - timedelta(minutes=25)).isoformat()

        body = self.client.get('/api/telemetry/readings/', {'since': since}).json()

        self.assertEqual(body['count'], 2)

    def test_latest_returns_one_row_per_site(self):
        self.client.force_authenticate(self.technician)
        body = self.client.get('/api/telemetry/readings/latest/').json()

        self.assertEqual(len(body), 2)
        by_org = {row['organization']: row for row in body}
        self.assertEqual(by_org[self.org.id]['ac_output_active_power_W'], 2000)
        self.assertEqual(by_org[self.other_org.id]['ac_output_active_power_W'], 42)

    def test_latest_can_be_narrowed_to_one_site(self):
        self.client.force_authenticate(self.owner)
        body = self.client.get(
            '/api/telemetry/readings/latest/', {'organization': self.org.id},
        ).json()

        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]['battery_capacity_percent'], 80)
