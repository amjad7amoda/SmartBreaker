"""Prove a populated Backend V1 schema upgrades into the merged contract."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BackendKBSMergeMigrationTests(TransactionTestCase):
    migrate_from = ('breakers', '0005_breaker_name')
    migrate_to = ('breakers', '0006_merge_backend_kbs')

    @staticmethod
    def _old_targets(executor):
        return [
            target
            for target in executor.loader.graph.leaf_nodes()
            if target[0] not in ('breakers', 'kbs')
        ] + [BackendKBSMergeMigrationTests.migrate_from]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        # Django's test runner creates the database at the newest graph first.
        # Remove both parallel feature branches so the fixture below is built
        # against the exact Backend V1 leaf, not a partially rolled-back merge.
        executor.migrate([('kbs', None), ('breakers', None)])
        executor = MigrationExecutor(connection)
        old_targets = self._old_targets(executor)
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state(old_targets).apps

        User = old_apps.get_model('accounts', 'User')
        Organization = old_apps.get_model('organizations', 'Organization')
        Breaker = old_apps.get_model('breakers', 'Breaker')
        DeviceAction = old_apps.get_model('breakers', 'BreakerAction')

        owner = User.objects.create(
            email='backend-migration@example.com',
            role='home_user',
            is_active=True,
            password='!',
        )
        organization = Organization.objects.create(
            name='Backend V1 Site',
            phone='102',
            latitude='33.510000',
            longitude='36.290000',
            owner=owner,
            status='active',
        )
        breaker = Breaker.objects.create(
            name='Protected freezer',
            device_id='backend-v1-breaker',
            organization=organization,
            type='motor',
            priority=8,
            protected=True,
            peak_load='1200.00',
            mean_load='350.00',
        )
        device_action = DeviceAction.objects.create(
            breaker=breaker,
            action='switch_off',
            source='kbs',
            reason='pre-merge action',
            confirmed=True,
        )
        self.breaker_id = breaker.id
        self.device_action_id = device_action.id

        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        self.apps = executor.loader.project_state(
            executor.loader.graph.leaf_nodes(),
        ).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_backend_data_and_action_audit_survive_canonicalization(self):
        Breaker = self.apps.get_model('breakers', 'Breaker')
        DeviceAction = self.apps.get_model('breakers', 'BreakerAction')
        BreakerStatus = self.apps.get_model('breakers', 'BreakerStatus')

        breaker = Breaker.objects.get(pk=self.breaker_id)
        self.assertEqual(breaker.name, 'Protected freezer')
        self.assertEqual(breaker.load_type, 'motor')
        self.assertEqual(breaker.priority_degree, 8)
        self.assertEqual(breaker.priority_type, 'mandatory')
        self.assertEqual(breaker.peak_load_W, 1200)
        self.assertEqual(breaker.mean_load_W, 350)
        self.assertFalse(breaker.locked_out)
        self.assertTrue(
            DeviceAction.objects.filter(pk=self.device_action_id).exists(),
        )
        self.assertFalse(BreakerStatus.objects.filter(breaker=breaker).exists())
