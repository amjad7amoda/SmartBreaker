"""Migration coverage proving legacy audit rows are preserved in place."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DecisionAuditMigrationTests(TransactionTestCase):
    migrate_from = ('kbs', '0005_kbssettings_grid_present_min_v_alter_alert_kind_and_more')
    migrate_to = ('kbs', '0006_decision_trace_audit')

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
        Decision = old_apps.get_model('kbs', 'KBSDecision')
        Action = old_apps.get_model('kbs', 'BreakerAction')

        owner = User.objects.create(
            email='migration-owner@example.com', role='home_user',
            is_active=True, password='!',
        )
        organization = Organization.objects.create(
            name='Legacy audit site', phone='1', latitude='33.510000',
            longitude='36.290000', owner=owner, status='active',
        )
        breaker = Breaker.objects.create(
            device_id='legacy-audit-load', organization=organization,
            priority_type='comfort',
        )
        for index, executed in enumerate((False, True), start=1):
            decision = Decision.objects.create(
                organization=organization, branch=f'legacy.branch.{index}',
                facts={'legacy': index},
            )
            Action.objects.create(
                decision=decision, breaker=breaker, action='off',
                countdown_s=0, reason='legacy action', executed=executed,
            )

        executor = MigrationExecutor(connection)
        new_targets = self._targets(executor, self.migrate_to)
        executor.migrate(new_targets)
        self.apps = executor.loader.project_state(new_targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_legacy_rows_receive_distinct_ids_and_version_zero(self):
        Decision = self.apps.get_model('kbs', 'KBSDecision')
        Action = self.apps.get_model('kbs', 'BreakerAction')

        decisions = list(Decision.objects.order_by('branch'))
        actions = list(Action.objects.order_by('created_at'))
        self.assertEqual(len(decisions), 2)
        self.assertEqual(len({decision.event_id for decision in decisions}), 2)
        self.assertTrue(all(decision.tier == 'tier2' for decision in decisions))
        self.assertTrue(all(decision.event_type == 'decision' for decision in decisions))
        self.assertTrue(all(decision.trace_version == 0 for decision in decisions))
        self.assertTrue(all(decision.trace == [] for decision in decisions))
        self.assertTrue(all(
            decision.engine == 'legacy.apps.kbs.services.run_cycle'
            for decision in decisions
        ))
        self.assertEqual(len({action.action_id for action in actions}), 2)
        self.assertEqual([action.status for action in actions], ['pending', 'applied'])
        self.assertEqual(
            [action.device_id for action in actions],
            ['legacy-audit-load', 'legacy-audit-load'],
        )

