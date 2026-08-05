"""Loopback browser bridge for the real, audited Tier-1 evaluator."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
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


class Tier1ConfigResolver:
    """Cache backend-owned site thresholds without making Tier-1 network-dependent."""

    CONFIG_PATH = '/api/kbs/edge/tier1-config/'

    def __init__(self, backend_url='', device_token='', timeout_s=3.0,
                 cache_ttl_s=30.0, transport=None):
        self.backend_url = (backend_url or '').rstrip('/')
        self.device_token = device_token or ''
        self.timeout_s = timeout_s
        self.cache_ttl_s = max(float(cache_ttl_s), 0.0)
        self.transport = transport
        self._lock = threading.RLock()
        self._cached = None
        self._expires_at = 0.0
        self._last_error = ''

    def _get(self, path):
        request = urllib.request.Request(
            f'{self.backend_url}{path}', method='GET',
            headers={'Authorization': f'Device {self.device_token}'},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode('utf-8'))

    def _authoritative_values(self):
        if not self.backend_url or not self.device_token:
            return {}
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now < self._expires_at:
                return dict(self._cached)
        try:
            response = (
                self.transport(self.CONFIG_PATH)
                if self.transport is not None else self._get(self.CONFIG_PATH)
            )
            config = response.get('config') if isinstance(response, dict) else None
            if not isinstance(config, dict):
                raise ValueError('Tier-1 config response requires a config object')
            config = dict(_dataclass_kwargs(Tier1Config, config, 'backend config'))
            Tier1Config(**config)
        except (OSError, TypeError, ValueError, KeyError, urllib.error.URLError) as exc:
            with self._lock:
                self._last_error = str(exc)
                return dict(self._cached or {})
        with self._lock:
            self._cached = config
            self._expires_at = now + self.cache_ttl_s
            self._last_error = ''
            return dict(config)

    def resolve(self, request_config):
        merged = dict(request_config)
        merged.update(self._authoritative_values())
        return Tier1Config(**_dataclass_kwargs(Tier1Config, merged, 'config'))

    def status(self):
        with self._lock:
            return {
                'source': 'backend' if self._cached is not None else 'request_fallback',
                'max_inverter_power_W': (
                    self._cached.get('max_inverter_power_W')
                    if self._cached is not None else None
                ),
                'last_error': self._last_error,
            }


def evaluate_payload(payload: Any, audit_service=None, config_resolver=None) -> dict[str, Any]:
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
        request_config = _dataclass_kwargs(
            Tier1Config, payload.get('config', {}), 'config'
        )
        config = (
            config_resolver.resolve(request_config)
            if config_resolver is not None
            else Tier1Config(**request_config)
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
                'config': self.server.config_resolver.status(),
            })
            return
        self._send_json(404, {'detail': 'not found'})

    def do_POST(self):  # noqa: N802
        try:
            payload = self._payload()
            if self.path == '/evaluate':
                self._send_json(200, evaluate_payload(
                    payload, self.server.audit_service, self.server.config_resolver,
                ))
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
    backend_url = os.getenv('SMARTBREAKER_BACKEND_URL')
    device_token = os.getenv('SMARTBREAKER_DEVICE_TOKEN')
    service = AuditedTier1Service(
        args.audit_db,
        upload_base_url=backend_url,
        device_token=device_token,
        auto_start=True,
    )
    config_resolver = Tier1ConfigResolver(backend_url, device_token)
    config_resolver.resolve({})
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Tier1BridgeHandler)
    server.audit_service = service
    server.config_resolver = config_resolver
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
