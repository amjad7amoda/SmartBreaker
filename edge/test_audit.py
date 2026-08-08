"""Contract tests for durable Tier-1 decision auditing."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from edge.audit import AuditedTier1Service
from edge.tier1_kbs import BreakerState, InverterState, Tier1Result


def danger_site(switch=True):
    return [
        BreakerState(
            'comfort-load', 'comfort', switch=switch, online=True,
            cur_power_W=500,
        ),
        BreakerState(
            'protected-load', 'mandatory', switch=True, online=True,
            cur_power_W=300,
        ),
    ]


class AuditedTier1ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / 'tier1-audit.sqlite3'
        self.service = AuditedTier1Service(self.db_path)

    def tearDown(self):
        self.service.close()
        self.tempdir.cleanup()

    def test_saves_transitions_and_command_changes_but_suppresses_repeats(self):
        hot = InverterState(heatsink_temp_C=80, ac_output_active_power_W=800)

        first = self.service.evaluate(hot, danger_site())
        repeated = self.service.evaluate(hot, danger_site())
        changed = self.service.evaluate(hot, danger_site(switch=False))
        cleared = self.service.evaluate(InverterState(), danger_site(switch=False))
        idle = self.service.evaluate(InverterState(), danger_site(switch=False))

        self.assertTrue(first.event_id)
        self.assertEqual(repeated.event_id, '')
        self.assertTrue(changed.event_id)
        self.assertEqual(cleared.event_type, 'clear')
        self.assertTrue(cleared.event_id)
        self.assertEqual(idle.event_id, '')
        events = self.service.store.pending_events()
        self.assertEqual([event['event_type'] for event in events], [
            'decision', 'decision', 'clear',
        ])
        self.assertEqual(events[-1]['trace'][-1]['code'], 'tier1.transition.clear')

    def test_pending_event_and_active_signature_survive_restart(self):
        hot = InverterState(heatsink_temp_C=80, ac_output_active_power_W=800)
        first = self.service.evaluate(hot, danger_site())
        self.service.close()

        self.service = AuditedTier1Service(self.db_path)
        repeated = self.service.evaluate(hot, danger_site())

        self.assertEqual(repeated.event_id, '')
        self.assertEqual(
            [event['event_id'] for event in self.service.store.pending_events()],
            [first.event_id],
        )

    def test_danger_does_not_clear_only_because_no_more_commands_are_needed(self):
        low = InverterState(
            battery_voltage_V=24.4,
            battery_discharge_current_A=10.0,
        )

        first = self.service.evaluate(low, danger_site())
        still_dangerous = self.service.evaluate(low, danger_site(switch=False))
        cleared = self.service.evaluate(InverterState(), danger_site(switch=False))

        self.assertEqual(first.situation, 'battery_low')
        self.assertEqual(still_dangerous.situation, 'battery_low')
        self.assertNotEqual(still_dangerous.event_type, 'clear')
        self.assertEqual(cleared.event_type, 'clear')

    def test_evaluator_errors_are_durable_events(self):
        def broken_evaluator(*args, **kwargs):
            raise RuntimeError('sensor decode failed')

        self.service.close()
        self.service = AuditedTier1Service(
            self.db_path, evaluator=broken_evaluator,
        )
        result = self.service.evaluate(InverterState(), danger_site())

        self.assertEqual(result.event_type, 'error')
        self.assertEqual(result.situation, 'evaluation_error')
        event = self.service.store.pending_events()[0]
        self.assertEqual(event['trace'][0]['outcome'], 'error')
        self.assertIn('sensor decode failed', event['notify'])

    def test_action_updates_and_partial_upload_results_are_idempotent(self):
        self.service.close()
        self.service = AuditedTier1Service(
            self.db_path, upload_base_url='https://audit.invalid',
            device_token='device.secret',
        )
        first = self.service.evaluate(
            InverterState(heatsink_temp_C=80), danger_site(),
        )
        action_id = first.commands[0].action_id
        self.service.update_action(
            action_id, 'scheduled', resulting_state=True,
        )
        self.service.evaluate(InverterState(), danger_site())
        event_ids = [event['event_id'] for event in self.service.store.pending_events()]
        calls = []

        def transport(path, body):
            calls.append((path, body))
            if path.endswith('decision-events/'):
                return {'results': [
                    {'event_id': event_ids[0], 'status': 'created'},
                    {
                        'event_id': event_ids[1], 'status': 'rejected',
                        'detail': 'immutable conflict',
                    },
                ]}
            return {'results': [
                {'action_id': action_id, 'status': 'updated'},
            ]}

        self.assertTrue(self.service.flush_once(transport=transport))
        self.assertEqual(self.service.store.counts(), {
            'pending': 0, 'uploaded': 1, 'rejected': 1,
        })
        self.assertEqual(calls[0][0], '/api/kbs/edge/decision-events/')
        self.assertEqual(calls[0][1]['events'][0]['actions'][0]['status'], 'scheduled')
        self.assertEqual(calls[1][0], '/api/kbs/edge/action-results/')
        self.assertFalse(self.service.flush_once(transport=transport))

    def test_cleanup_only_removes_expired_uploaded_events(self):
        hot = InverterState(heatsink_temp_C=80)
        uploaded = self.service.evaluate(hot, danger_site())
        self.service.store.mark_event(uploaded.event_id, 'uploaded')
        self.service.evaluate(InverterState(), danger_site())
        rejected_id = self.service.store.pending_events()[0]['event_id']
        self.service.store.mark_event(rejected_id, 'rejected', 'keep')
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with self.service.store._lock:
            self.service.store._db.execute(
                'UPDATE events SET uploaded_at=? WHERE event_id=?',
                (old, uploaded.event_id),
            )
            self.service.store._db.commit()

        self.assertEqual(self.service.store.cleanup(), 1)
        self.assertEqual(self.service.store.counts()['rejected'], 1)


class TraceSchemaTests(unittest.TestCase):
    def test_trace_is_deterministic_and_schema_complete(self):
        service_result = None
        with tempfile.TemporaryDirectory() as directory:
            service = AuditedTier1Service(Path(directory) / 'trace.sqlite3')
            try:
                service_result = service.evaluate(
                    InverterState(heatsink_temp_C=80), danger_site(),
                )
            finally:
                service.close()

        for step in service_result.trace:
            self.assertEqual(set(step), {
                'code', 'kind', 'outcome', 'summary', 'evidence',
            })
        self.assertEqual(service_result.trace_version, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
