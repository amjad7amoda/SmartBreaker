"""Durable Tier-1 evaluation audit queue (stdlib-only, Raspberry-Pi safe)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .tier1_kbs import TRACE_VERSION, Tier1Result, evaluate
except ImportError:
    from tier1_kbs import TRACE_VERSION, Tier1Result, evaluate


ACTION_STATUSES = {
    'pending', 'scheduled', 'applied', 'blocked', 'failed', 'noop',
    'suppressed_duplicate', 'superseded',
}


def _utc_now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class Tier1AuditStore:
    """SQLite event/action store using WAL so evaluation writes stay brief."""

    def __init__(self, path, uploaded_retention_days=7):
        self.path = str(Path(path))
        self.uploaded_retention_days = uploaded_retention_days
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA synchronous=NORMAL')
            self._db.execute('PRAGMA foreign_keys=ON')
            self._create_schema()

    def _create_schema(self):
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                situation TEXT NOT NULL,
                engine TEXT NOT NULL,
                trace_version INTEGER NOT NULL,
                trace_json TEXT NOT NULL,
                facts_json TEXT NOT NULL,
                notify TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                command_signature TEXT NOT NULL,
                upload_state TEXT NOT NULL DEFAULT 'pending',
                rejection_reason TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT
            );
            CREATE INDEX IF NOT EXISTS edge_events_upload_idx
                ON events(upload_state, occurred_at);
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
                device_id TEXT NOT NULL,
                action TEXT NOT NULL,
                countdown_s INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                resulting_state INTEGER,
                executed_at TEXT,
                failure_reason TEXT NOT NULL DEFAULT '',
                result_upload_state TEXT NOT NULL DEFAULT 'not_ready'
            );
            CREATE INDEX IF NOT EXISTS edge_actions_event_idx ON actions(event_id);
            CREATE TABLE IF NOT EXISTS evaluator_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                active_signature TEXT NOT NULL DEFAULT '',
                active_situation TEXT NOT NULL DEFAULT ''
            );
            INSERT OR IGNORE INTO evaluator_state(singleton) VALUES (1);
        ''')
        self._db.commit()

    def active_state(self):
        with self._lock:
            row = self._db.execute(
                'SELECT active_signature, active_situation FROM evaluator_state WHERE singleton=1'
            ).fetchone()
        return row['active_signature'], row['active_situation']

    def set_active_state(self, signature='', situation=''):
        with self._lock:
            self._db.execute(
                'UPDATE evaluator_state SET active_signature=?, active_situation=? WHERE singleton=1',
                (signature, situation),
            )
            self._db.commit()

    def save_result(self, result, facts, event_type='decision', occurred_at=None):
        occurred_at = occurred_at or _utc_now()
        event_id = str(uuid.uuid4())
        signature = _json({
            'situation': result.situation,
            'commands': [
                [command.device_id, command.action, command.countdown_s, command.reason]
                for command in result.commands
            ],
        })
        action_rows = []
        for command in result.commands:
            command.action_id = str(uuid.uuid4())
            command.status = command.status or 'pending'
            action_rows.append((
                command.action_id, event_id, command.device_id, command.action,
                command.countdown_s, command.reason, command.status,
            ))
        result.event_id = event_id
        result.event_type = event_type
        result.upload_state = 'pending'
        with self._lock:
            self._db.execute(
                '''INSERT INTO events(
                    event_id,event_type,situation,engine,trace_version,trace_json,
                    facts_json,notify,occurred_at,command_signature,upload_state
                ) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending')''',
                (
                    event_id, event_type, result.situation, 'edge.tier1_kbs.evaluate',
                    result.trace_version, _json(result.trace), _json(facts), result.notify,
                    occurred_at.isoformat(), signature,
                ),
            )
            self._db.executemany(
                '''INSERT INTO actions(
                    action_id,event_id,device_id,action,countdown_s,reason,status
                ) VALUES(?,?,?,?,?,?,?)''',
                action_rows,
            )
            self._db.commit()
        return result

    def update_action(self, action_id, status, resulting_state=None,
                      executed_at=None, failure_reason=''):
        if status not in ACTION_STATUSES:
            raise ValueError(f'unknown action status: {status}')
        if resulting_state is not None and type(resulting_state) is not bool:
            raise ValueError('resulting_state must be boolean or None')
        if executed_at is None and status in {
            'applied', 'blocked', 'failed', 'noop', 'suppressed_duplicate',
        }:
            executed_at = _utc_now()
        if isinstance(executed_at, datetime):
            executed_at = executed_at.isoformat()
        with self._lock:
            cursor = self._db.execute(
                '''UPDATE actions SET status=?, resulting_state=?, executed_at=?,
                   failure_reason=?, result_upload_state='pending' WHERE action_id=?''',
                (
                    status,
                    None if resulting_state is None else int(resulting_state),
                    executed_at, str(failure_reason or ''), str(action_id),
                ),
            )
            self._db.commit()
        if cursor.rowcount != 1:
            raise KeyError(f'unknown action_id: {action_id}')

    def pending_events(self, limit=25):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE upload_state='pending' ORDER BY occurred_at LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._event_payload(row) for row in rows]

    def _event_payload(self, row):
        actions = self._db.execute(
            'SELECT * FROM actions WHERE event_id=? ORDER BY rowid', (row['event_id'],)
        ).fetchall()
        return {
            'event_id': row['event_id'], 'event_type': row['event_type'],
            'situation': row['situation'], 'branch': row['situation'],
            'engine': row['engine'], 'trace_version': row['trace_version'],
            'trace': json.loads(row['trace_json']), 'facts': json.loads(row['facts_json']),
            'notify': row['notify'], 'occurred_at': row['occurred_at'],
            'actions': [{
                'action_id': action['action_id'], 'device_id': action['device_id'],
                'action': action['action'], 'countdown_s': action['countdown_s'],
                'reason': action['reason'], 'status': action['status'],
                'resulting_state': (
                    None if action['resulting_state'] is None
                    else bool(action['resulting_state'])
                ),
                'executed_at': action['executed_at'],
                'failure_reason': action['failure_reason'],
            } for action in actions],
        }

    def pending_action_results(self, limit=100):
        with self._lock:
            rows = self._db.execute(
                '''SELECT actions.* FROM actions JOIN events USING(event_id)
                   WHERE actions.result_upload_state='pending'
                     AND events.upload_state='uploaded'
                   ORDER BY actions.rowid LIMIT ?''',
                (limit,),
            ).fetchall()
        return [{
            'action_id': row['action_id'], 'status': row['status'],
            'resulting_state': None if row['resulting_state'] is None else bool(row['resulting_state']),
            'executed_at': row['executed_at'], 'failure_reason': row['failure_reason'],
        } for row in rows]

    def mark_event(self, event_id, state, reason=''):
        if state not in ('uploaded', 'rejected'):
            raise ValueError('event state must be uploaded or rejected')
        uploaded_at = _utc_now().isoformat() if state == 'uploaded' else None
        with self._lock:
            self._db.execute(
                'UPDATE events SET upload_state=?, rejection_reason=?, uploaded_at=? WHERE event_id=?',
                (state, reason, uploaded_at, str(event_id)),
            )
            self._db.commit()

    def mark_action_result_uploaded(self, action_id):
        with self._lock:
            self._db.execute(
                "UPDATE actions SET result_upload_state='uploaded' WHERE action_id=?",
                (str(action_id),),
            )
            self._db.commit()

    def counts(self):
        with self._lock:
            return {
                state: self._db.execute(
                    'SELECT COUNT(*) FROM events WHERE upload_state=?', (state,)
                ).fetchone()[0]
                for state in ('pending', 'uploaded', 'rejected')
            }

    def cleanup(self, now=None):
        now = now or _utc_now()
        cutoff = (now - timedelta(days=self.uploaded_retention_days)).isoformat()
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM events WHERE upload_state='uploaded' AND uploaded_at < ?", (cutoff,)
            )
            self._db.commit()
            return cursor.rowcount

    def close(self):
        with self._lock:
            self._db.close()


class AuditedTier1Service:
    """Transition-aware evaluator plus non-blocking, retryable uploader."""

    def __init__(self, db_path, upload_base_url=None, device_token=None,
                 batch_size=25, timeout_s=3.0, retry_min_s=1.0,
                 retry_max_s=60.0, uploaded_retention_days=7,
                 evaluator=evaluate, auto_start=False):
        self.store = Tier1AuditStore(db_path, uploaded_retention_days)
        self.upload_base_url = (upload_base_url or '').rstrip('/')
        self.device_token = device_token or ''
        self.batch_size = max(1, min(int(batch_size), 100))
        self.timeout_s = timeout_s
        self.retry_min_s = retry_min_s
        self.retry_max_s = retry_max_s
        self.evaluator = evaluator
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        if auto_start and self.upload_base_url and self.device_token:
            self.start()

    def evaluate(self, inverter, breakers, cfg=None, occurred_at=None):
        occurred_at = occurred_at or _utc_now()
        facts = {
            'inverter': asdict(inverter),
            'breakers': [asdict(breaker) for breaker in breakers],
            'config': asdict(cfg) if cfg is not None else {},
        }
        try:
            result = self.evaluator(inverter, breakers, cfg)
        except Exception as exc:
            result = Tier1Result(
                situation='evaluation_error', event_type='error',
                notify=f'Tier-1 evaluation failed: {exc}',
                trace=[{
                    'code': 'tier1.evaluator.error', 'kind': 'error',
                    'outcome': 'error', 'summary': 'Tier-1 evaluator raised an exception.',
                    'evidence': {'error_type': type(exc).__name__, 'message': str(exc)},
                }],
            )
            self.store.save_result(result, facts, 'error', occurred_at)
            self._wake.set()
            return result

        signature = _json({
            'situation': result.situation,
            'commands': [
                [command.device_id, command.action, command.countdown_s, command.reason]
                for command in result.commands
            ],
        })
        active_signature, active_situation = self.store.active_state()
        if result.situation:
            if signature != active_signature:
                self.store.save_result(result, facts, 'decision', occurred_at)
                self.store.set_active_state(signature, result.situation)
                self._wake.set()
        elif active_situation:
            result.event_type = 'clear'
            result.trace.append({
                'code': 'tier1.transition.clear', 'kind': 'transition',
                'outcome': 'selected',
                'summary': f'Tier-1 danger cleared: {active_situation}.',
                'evidence': {'previous_situation': active_situation},
            })
            self.store.save_result(result, facts, 'clear', occurred_at)
            self.store.set_active_state()
            self._wake.set()
        return result

    def update_action(self, *args, **kwargs):
        self.store.update_action(*args, **kwargs)
        self._wake.set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._upload_loop, name='tier1-audit-uploader', daemon=True,
        )
        self._thread.start()

    def _upload_loop(self):
        delay = self.retry_min_s
        while not self._stop.is_set():
            self._wake.wait(delay)
            self._wake.clear()
            try:
                changed = self.flush_once()
                delay = self.retry_min_s if changed else min(delay * 2, self.retry_max_s)
            except (OSError, ValueError, urllib.error.URLError):
                delay = min(delay * 2, self.retry_max_s)

    def _post(self, path, body):
        request = urllib.request.Request(
            f'{self.upload_base_url}{path}',
            data=json.dumps(body).encode('utf-8'), method='POST',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Device {self.device_token}',
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode('utf-8'))

    def flush_once(self, transport=None):
        if not self.upload_base_url or not self.device_token:
            return False
        post = transport or self._post
        changed = False
        events = self.store.pending_events(self.batch_size)
        if events:
            response = post('/api/kbs/edge/decision-events/', {'events': events})
            by_id = {str(item.get('event_id')): item for item in response.get('results', [])}
            for event in events:
                outcome = by_id.get(event['event_id'])
                if not outcome:
                    continue
                if outcome.get('status') in ('created', 'duplicate'):
                    self.store.mark_event(event['event_id'], 'uploaded')
                    changed = True
                elif outcome.get('status') == 'rejected':
                    self.store.mark_event(
                        event['event_id'], 'rejected', str(outcome.get('detail') or '')
                    )
                    changed = True
        action_results = self.store.pending_action_results(self.batch_size * 4)
        if action_results:
            response = post('/api/kbs/edge/action-results/', {'results': action_results})
            by_id = {str(item.get('action_id')): item for item in response.get('results', [])}
            for result in action_results:
                if by_id.get(result['action_id'], {}).get('status') == 'updated':
                    self.store.mark_action_result_uploaded(result['action_id'])
                    changed = True
        self.store.cleanup()
        return changed

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.store.close()
