"""Local HTTP bridge between the browser simulator and the real Tier-1 KBS.

The bridge deliberately has no Django or third-party dependencies.  It binds
to the loopback interface only, accepts a JSON snapshot, calls the same
``evaluate`` function that will run on the Raspberry Pi, and returns the
result as JSON.  It is a development/test adapter, not a production device
API.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

try:
    from .tier1_kbs import BreakerState, InverterState, Tier1Config, evaluate
except ImportError:  # Allows ``python edge/simulator_bridge.py`` from the repo root.
    from tier1_kbs import BreakerState, InverterState, Tier1Config, evaluate


DEFAULT_PORT = 8788
MAX_BODY_BYTES = 256 * 1024
ALLOWED_ORIGINS = {
    "http://127.0.0.1:8791",
    "http://localhost:8791",
    "null",  # Supports opening simulator/index.html directly from file://.
}


class PayloadError(ValueError):
    """The browser sent a malformed Tier-1 evaluation payload."""


def _dataclass_kwargs(cls, value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadError(f"{label} must be a JSON object")
    allowed = {item.name for item in fields(cls)}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PayloadError(f"unknown {label} field(s): {', '.join(unknown)}")
    return value


def evaluate_payload(payload: Any) -> dict[str, Any]:
    """Validate one browser payload and evaluate it with the real Tier-1 code."""
    if not isinstance(payload, dict):
        raise PayloadError("request body must be a JSON object")
    if "inverter" not in payload or "breakers" not in payload:
        raise PayloadError("request requires inverter and breakers")
    if not isinstance(payload["breakers"], list):
        raise PayloadError("breakers must be a JSON array")

    try:
        inverter = InverterState(
            **_dataclass_kwargs(InverterState, payload["inverter"], "inverter")
        )
        breakers = [
            BreakerState(**_dataclass_kwargs(BreakerState, item, f"breakers[{index}]"))
            for index, item in enumerate(payload["breakers"])
        ]
        config = Tier1Config(
            **_dataclass_kwargs(Tier1Config, payload.get("config", {}), "config")
        )
    except TypeError as exc:
        raise PayloadError(str(exc)) from exc

    result = asdict(evaluate(inverter, breakers, config))
    # Return the normalized dataclass inputs as evidence. The browser displays
    # these exact facts next to the situation/commands produced by the real
    # edge evaluator; no rule decision is recreated in JavaScript.
    result['engine'] = 'edge.tier1_kbs.evaluate'
    result['facts'] = {
        'inverter': asdict(inverter),
        'breakers': [asdict(breaker) for breaker in breakers],
        'config': asdict(config),
    }
    return result


class Tier1BridgeHandler(BaseHTTPRequestHandler):
    server_version = "SmartBreakerTier1Bridge/1.0"

    def _cors_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        return origin if origin in ALLOWED_ORIGINS else None

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(204)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "engine": "tier1"})
            return
        self._send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/evaluate":
            self._send_json(404, {"detail": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise PayloadError("request body is empty or too large")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            self._send_json(200, evaluate_payload(payload))
        except (PayloadError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"detail": str(exc)})
        except Exception as exc:  # Keep the local test bridge responsive.
            self._send_json(500, {"detail": f"Tier-1 evaluation failed: {exc}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[tier1-bridge] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SmartBreaker local Tier-1 simulator bridge")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Tier1BridgeHandler)
    print(f"Tier-1 bridge listening on http://127.0.0.1:{args.port}")
    print("Health check: /health   Evaluation endpoint: POST /evaluate")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nTier-1 bridge stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
