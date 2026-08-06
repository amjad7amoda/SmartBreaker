"""Migration coverage for populated Tier-1 safety history."""

from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class Tier1SafetyInterlockMigrationTests(TransactionTestCase):
    migrate_from = ('kbs', '0007_canonical_tier2_engine')
    migrate_to = ('kbs', '0008_tier1_safety_interlock')

    @staticmethod
    def _targets(executor, kbs_target):
        return [
            target for target in executor.loader.graph.leaf_nodes()
            if target[0] != 'kbs'
        ] + [kbs_target]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        old_targets = self._targets(executor, self.migrate_from)
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state(old_targets).apps

        User = old_apps.get_model('accounts', 'User')
        Organization = old_apps.get_model('organizations', 'Organization')
        Breaker = old_apps.get_model('breakers', 'Breaker')
        EdgeDevice = old_apps.get_model('kbs', 'EdgeDevice')
        Decision = old_apps.get_model('kbs', 'KBSDecision')
        Action = old_apps.get_model('kbs', 'BreakerAction')

        owner = User.objects.create(
            email='interlock-migration@example.com',
            role='home_user',
            is_active=True,
            password='!',
        )
        organization = Organization.objects.create(
            name='Interlock migration site',
            phone='1',
            latitude='33.510000',
            longitude='36.290000',
            owner=owner,
            status='active',
        )
        breaker = Breaker.objects.create(
            device_id='migration-load',
            organization=organization,
            priority_type='comfort',
        )
        edge = EdgeDevice.objects.create(
            organization=organization,
            name='Migration edge',
            secret_hash='!',
        )
        now = timezone.now()
        first = Decision.objects.create(
            organization=organization,
            edge_device=edge,
            tier='tier1',
            event_type='decision',
            branch='inverter_overheat',
            engine='edge.tier1_kbs.evaluate',
            occurred_at=now,
        )
        Action.objects.create(
            decision=first,
            breaker=breaker,
            device_id=breaker.device_id,
            action='off',
            reason='first command',
        )
        latest_confirmed = Decision.objects.create(
            organization=organization,
            edge_device=edge,
            tier='tier1',
            event_type='decision',
            branch='inverter_overheat',
            engine='edge.tier1_kbs.evaluate',
            occurred_at=now + timedelta(seconds=1),
        )
        Decision.objects.create(
            organization=organization,
            edge_device=edge,
            tier='tier1',
            event_type='error',
            branch='evaluation_error',
            engine='edge.tier1_kbs.evaluate',
            occurred_at=now + timedelta(seconds=2),
        )
        self.organization_id = organization.id
        self.latest_confirmed_id = latest_confirmed.id

        executor = MigrationExecutor(connection)
        new_targets = self._targets(executor, self.migrate_to)
        executor.migrate(new_targets)
        self.apps = executor.loader.project_state(new_targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_backfill_creates_one_state_from_latest_confirmed_event(self):
        SafetyState = self.apps.get_model('kbs', 'Tier1SafetyState')

        states = SafetyState.objects.filter(
            organization_id=self.organization_id,
        )
        self.assertEqual(states.count(), 1)
        state = states.get()
        self.assertTrue(state.active)
        self.assertEqual(state.situation, 'inverter_overheat')
        self.assertEqual(state.source_decision_id, self.latest_confirmed_id)
        self.assertEqual(state.commands, [{
            'device_id': 'migration-load',
            'action': 'off',
            'countdown_s': 0,
            'reason': 'first command',
        }])
