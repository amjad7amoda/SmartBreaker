import json

from django.test import TestCase
from django.urls import reverse

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
