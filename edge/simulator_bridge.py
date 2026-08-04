"""Loopback browser bridge for the real, audited Tier-1 evaluator."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from .audit import AuditedTier1Service
    from .tier1_kbs import BreakerState, InverterState, Tier1Config, evaluate
except ImportError:
    from audit import AuditedTier1Service
    from tier1_kbs import BreakerState, InverterState, Tier1Config, evaluate


DEFAULT_PORT = 8788
MAX_BODY_BYTES = 256 * 1024
ALLOWED_ORIGINS = {
    'http://127.0.0.1:8791', 'http://localhost:8791', 'null',
}


class PayloadError(ValueError):
    pass


def _dataclass_kwargs(cls, value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadError(f'{label} must be a JSON object')
    allowed = {item.name for item in fields(cls)}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PayloadError(f'unknown {label} field(s): {", ".join(unknown)}')
    return value


def evaluate_payload(payload: Any, audit_service=None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PayloadError('request body must be a JSON object')
    if 'inverter' not in payload or 'breakers' not in payload:
        raise PayloadError('request requires inverter and breakers')
    if not isinstance(payload['breakers'], list):
        raise PayloadError('breakers must be a JSON array')
    try:
        inverter = InverterState(
            **_dataclass_kwargs(InverterState, payload['inverter'], 'inverter')
        )
        breakers = [
            BreakerState(**_dataclass_kwargs(BreakerState, item, f'breakers[{index}]'))
            for index, item in enumerate(payload['breakers'])
        ]
        config = Tier1Config(
            **_dataclass_kwargs(Tier1Config, payload.get('config', {}), 'config')
        )
    except TypeError as exc:
        raise PayloadError(str(exc)) from exc
    evaluated = (
        audit_service.evaluate(inverter, breakers, config)
        if audit_service is not None else evaluate(inverter, breakers, config)
    )
    result = asdict(evaluated)
    result['engine'] = 'edge.tier1_kbs.evaluate'
    result['facts'] = {
        'inverter': asdict(inverter),
        'breakers': [asdict(breaker) for breaker in breakers],
        'config': asdict(config),
    }
    return result


class Tier1BridgeHandler(BaseHTTPRequestHandler):
    server_version = 'SmartBreakerTier1Bridge/2.0'

    def _cors_origin(self):
        origin = self.headers.get('Origin')
        return origin if origin in ALLOWED_ORIGINS else None

    def _send_json(self, status, body):
        encoded = json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        origin = self._cors_origin()
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.end_headers()
        self.wfile.write(encoded)

    def _payload(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length <= 0 or length > MAX_BODY_BYTES:
            raise PayloadError('request body is empty or too large')
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        origin = self._cors_origin()
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path == '/health':
            counts = self.server.audit_service.store.counts()
            self._send_json(200, {
                'status': 'ok', 'engine': 'tier1', 'audit_queue': counts,
            })
            return
        self._send_json(404, {'detail': 'not found'})

    def do_POST(self):  # noqa: N802
        try:
            payload = self._payload()
            if self.path == '/evaluate':
                self._send_json(200, evaluate_payload(payload, self.server.audit_service))
                return
            if self.path == '/action-result':
                self.server.audit_service.update_action(
                    payload.get('action_id'), payload.get('status'),
                    payload.get('resulting_state'), payload.get('executed_at'),
                    payload.get('failure_reason', ''),
                )
                self._send_json(200, {'updated': True, 'action_id': payload.get('action_id')})
                return
            self._send_json(404, {'detail': 'not found'})
        except (PayloadError, ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {'detail': str(exc)})
        except Exception as exc:
            self._send_json(500, {'detail': f'Tier-1 bridge failed: {exc}'})

    def log_message(self, fmt, *args):
        print(f'[tier1-bridge] {self.address_string()} - {fmt % args}')


def main():
    parser = argparse.ArgumentParser(description='SmartBreaker local Tier-1 simulator bridge')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument(
        '--audit-db', default=os.getenv(
            'SMARTBREAKER_EDGE_AUDIT_DB', str(Path(__file__).with_name('tier1_audit.sqlite3')),
        ),
    )
    args = parser.parse_args()
    service = AuditedTier1Service(
        args.audit_db,
        upload_base_url=os.getenv('SMARTBREAKER_BACKEND_URL'),
        device_token=os.getenv('SMARTBREAKER_DEVICE_TOKEN'),
        auto_start=True,
    )
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Tier1BridgeHandler)
    server.audit_service = service
    print(f'Tier-1 bridge listening on http://127.0.0.1:{args.port}')
    print('Endpoints: GET /health, POST /evaluate, POST /action-result')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nTier-1 bridge stopped')
    finally:
        server.server_close()
        service.close()


if __name__ == '__main__':
    main()
