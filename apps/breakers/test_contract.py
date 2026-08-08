from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.organizations.models import Organization

from .models import Breaker
from .serializers import BreakerSerializer, BreakerUpdateSerializer


class BreakerContractCompatibilityTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        owner = get_user_model().objects.create_user(
            email='contract-owner@example.com',
            password='pw',
            role='home_user',
            is_active=True,
            must_set_password=False,
        )
        cls.organization = Organization.objects.create(
            name='Contract Site',
            phone='101',
            latitude=33.51,
            longitude=36.29,
            owner=owner,
            status='active',
        )
        cls.breaker = Breaker.objects.create(
            name='Clinic AC',
            device_id='contract-breaker',
            organization=cls.organization,
            priority_type='normal',
            priority_degree=1,
        )

    def test_legacy_backend_write_keys_update_canonical_kbs_fields(self):
        serializer = BreakerUpdateSerializer(
            self.breaker,
            data={
                'type': 'motor',
                'priority': 7,
                'protected': True,
                'peak_load': 1800,
                'mean_load': 900,
            },
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.breaker.refresh_from_db()

        self.assertEqual(self.breaker.load_type, 'motor')
        self.assertEqual(self.breaker.priority_degree, 7)
        self.assertEqual(self.breaker.priority_type, 'mandatory')
        self.assertEqual(self.breaker.peak_load_W, 1800)
        self.assertEqual(self.breaker.mean_load_W, 900)

    def test_response_exposes_backend_aliases_and_canonical_kbs_fields(self):
        body = BreakerSerializer(self.breaker).data

        self.assertEqual(body['name'], 'Clinic AC')
        self.assertEqual(body['load_type'], body['type'])
        self.assertEqual(body['priority_degree'], body['priority'])
        self.assertEqual(body['peak_load_W'], body['peak_load'])
        self.assertEqual(body['mean_load_W'], body['mean_load'])
        self.assertEqual(
            body['protected'],
            body['priority_type'] == 'mandatory',
        )
