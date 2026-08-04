"""Data-migration coverage for canonical Tier-2 provenance."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class Tier2ProvenanceMigrationTests(TransactionTestCase):
    migrate_from = ('kbs', '0006_decision_trace_audit')
    migrate_to = ('kbs', '0007_canonical_tier2_engine')

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
        Decision = old_apps.get_model('kbs', 'KBSDecision')
        owner = User.objects.create(
            email='provenance-migration@example.com', role='home_user',
            is_active=True, password='!',
        )
        organization = Organization.objects.create(
            name='Provenance migration site', phone='1',
            latitude='33.510000', longitude='36.290000',
            owner=owner, status='active',
        )
        self.current_event = Decision.objects.create(
            organization=organization, tier='tier2', trace_version=1,
            engine='apps.kbs.engine.rules.decide', branch='current.trace',
        ).event_id
        self.legacy_event = Decision.objects.create(
            organization=organization, tier='tier2', trace_version=0,
            engine='legacy.apps.kbs.services.run_cycle', branch='legacy.trace',
        ).event_id

        executor = MigrationExecutor(connection)
        new_targets = self._targets(executor, self.migrate_to)
        executor.migrate(new_targets)
        self.apps = executor.loader.project_state(new_targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_only_current_traced_aliases_are_canonicalized(self):
        Decision = self.apps.get_model('kbs', 'KBSDecision')
        current = Decision.objects.get(event_id=self.current_event)
        legacy = Decision.objects.get(event_id=self.legacy_event)
        self.assertEqual(current.engine, 'apps.kbs.services.run_cycle')
        self.assertEqual(legacy.engine, 'legacy.apps.kbs.services.run_cycle')
