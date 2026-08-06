from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase

from .services import run_cycle
from .tests import make_facts


class FakeAdapter:
    def __init__(self, mode='active', facts=None):
        self.settings = SimpleNamespace(mode=mode)
        self.facts = facts
        self.calls = []
        self.decision = SimpleNamespace(branch='stored')

    def get_settings(self, organization):
        self.calls.append(('settings', organization.id))
        return self.settings

    def resolve_cycle_time(self, organization, settings, requested_now=None):
        self.calls.append(('time', requested_now))
        return requested_now

    def build_facts(self, organization, settings, cycle_time):
        self.calls.append(('facts', cycle_time))
        return self.facts

    def persist_result(self, organization, facts, result):
        self.calls.append(('persist', result.branch))
        self.result = result
        return self.decision


class RunCycleServiceTests(TestCase):
    def setUp(self):
        self.organization = SimpleNamespace(id=7)
        self.now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    def test_active_cycle_uses_adapter_around_pure_engine(self):
        adapter = FakeAdapter(facts=make_facts([]))

        decision = run_cycle(
            self.organization,
            now=self.now,
            adapter=adapter,
        )

        self.assertIs(decision, adapter.decision)
        self.assertEqual(
            adapter.calls,
            [
                ('settings', 7),
                ('time', self.now),
                ('facts', self.now),
                ('persist', 'day.surplus.comfort_on'),
            ],
        )

    def test_observing_mode_never_builds_or_persists(self):
        adapter = FakeAdapter(mode='observing', facts=make_facts([]))

        decision = run_cycle(self.organization, now=self.now, adapter=adapter)

        self.assertIsNone(decision)
        self.assertEqual(adapter.calls, [('settings', 7)])

    def test_missing_facts_never_persists(self):
        adapter = FakeAdapter(facts=None)

        decision = run_cycle(self.organization, now=self.now, adapter=adapter)

        self.assertIsNone(decision)
        self.assertEqual(
            adapter.calls,
            [('settings', 7), ('time', self.now), ('facts', self.now)],
        )
