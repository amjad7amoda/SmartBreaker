from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class FuzzySupervisorMigrationTests(TransactionTestCase):
    migrate_from = ('kbs', '0008_tier1_safety_interlock')
    migrate_to = ('kbs', '0009_fuzzy_kbs_supervisor')

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
        Settings = old_apps.get_model('kbs', 'KBSSettings')
        Decision = old_apps.get_model('kbs', 'KBSDecision')
        owner = User.objects.create(
            email='fuzzy-migration@example.com', role='home_user',
            is_active=True, password='!',
        )
        organization = Organization.objects.create(
            name='Fuzzy migration site', phone='31', latitude='33.5',
            longitude='36.2', owner=owner, status='active',
        )
        Settings.objects.create(organization=organization)
        Decision.objects.create(organization=organization, branch='legacy-crisp')
        self.organization_id = organization.id

        executor = MigrationExecutor(connection)
        new_targets = self._targets(executor, self.migrate_to)
        executor.migrate(new_targets)
        self.apps = executor.loader.project_state(new_targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_defaults_and_state_contract(self):
        Settings = self.apps.get_model('kbs', 'KBSSettings')
        Decision = self.apps.get_model('kbs', 'KBSDecision')
        State = self.apps.get_model('kbs', 'KBSControllerState')
        self.assertEqual(
            Settings.objects.get(organization_id=self.organization_id).tier2_policy,
            'crisp',
        )
        decision = Decision.objects.get(organization_id=self.organization_id)
        self.assertEqual(decision.policy, 'crisp')
        self.assertEqual(decision.counterfactual, {})
        state = State.objects.create(organization_id=self.organization_id)
        self.assertEqual(state.current_band, 'watch')
        self.assertEqual(state.candidate_band, '')
        self.assertEqual(state.consecutive_cycles, 0)
        self.assertEqual(state.profile_version, 'mamdani-v1')
