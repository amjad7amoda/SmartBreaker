# SmartBreaker KBS Engine Architecture

This document describes how the KBS is connected to the backend. The
authoritative inventory of facts and decision rules is
[KBS_FACTS_AND_RULES.md](KBS_FACTS_AND_RULES.md); changes to reasoning must be
made there and in the pure engine together.

## 1. Architecture

Tier-2 is split into three boundaries:

```text
Celery / API / simulator
          |
          v
apps/kbs/services.py             application orchestration
          |
          +----> apps/kbs/adapters/django.py
          |        DB, clock, weather and persistence
          |
          v
apps/kbs/engine/                 pure decision engine
  facts.py                       immutable input contract
  derived.py                     pure calculations
  grouping.py                    pure grouping/subset policies
  rules.py                       decide(facts) -> RuleResult
```

The engine does not import Django, ORM models, Celery, HTTP clients, a clock,
or backend services. It does not use Experta. It receives immutable Python
dataclasses and returns intent dataclasses. This keeps the existing KBS logic
portable and makes it possible to replace Django or any external service
without rewriting the rules.

Tier-1 remains separate in `edge/tier1_kbs.py`. It is the dependency-free
Raspberry Pi safety layer for decisions that cannot wait for the server.

## 2. Pure engine contract

`apps/kbs/engine/facts.py` defines the only input types:

- `SystemFacts`: the complete snapshot for one decision cycle.
- `BreakerFacts`: immutable state and configuration for one breaker.
- `facts_to_dict()`: converts the snapshot to JSON-safe audit data.

`apps/kbs/engine/rules.py` exposes:

```python
result = decide(facts)
```

The returned `RuleResult` contains:

- `branch`: the single decision-tree branch selected this cycle.
- `actions`: `ActionIntent` values describing desired breaker states.
- `alerts`: `AlertIntent` values describing user/technician notifications.

These are descriptions only. The engine never writes a model or commands a
device itself.

## 3. Adapter contract

`DjangoKBSAdapter` is the anti-corruption layer between the KBS vocabulary and
the backend. `run_cycle()` depends on these four adapter operations:

```python
settings = adapter.get_settings(organization)
cycle_time = adapter.resolve_cycle_time(
    organization, settings, requested_now=now
)
facts = adapter.build_facts(organization, settings, cycle_time)
decision = adapter.persist_result(organization, facts, result)
```

Responsibilities are deliberately divided as follows:

| Concern | Owner |
|---|---|
| ORM queries and joins | Django adapter |
| raw-unit conversion (`mW -> W`, inverter registers) | Django adapter |
| real or simulator clock selection | Django adapter |
| weather/backend service calls | Django adapter through `apps/kbs/weather.py` |
| construction of frozen facts | Django adapter |
| fact-derived calculations | pure helpers in `engine/derived.py`, called by the adapter |
| branch selection and action/alert intent | pure engine |
| decisions, actions, alerts, lockouts and deduplication | Django adapter |
| active/observing lifecycle and call ordering | application service |
| periodic scheduling | Celery tasks |

To move the KBS to another backend, implement these four operations and pass
that adapter to `run_cycle(organization, adapter=...)`. The engine remains
unchanged.

## 4. Decision-cycle pipeline

```text
run_kbs_cycles (Celery dispatcher)
  -> run_kbs_cycle_for_org
     -> services.run_cycle
        1. load site settings through adapter
        2. stop if mode is observing
        3. resolve real/simulator cycle time
        4. adapter builds SystemFacts
        5. engine decide(facts)
        6. adapter persists decision, actions, alerts and lockouts
```

The simulator can trigger the same application service through
`POST /api/kbs/sim/run-cycle/`; it does not have a second copy of the rules.

For `data_source='real'`, the cycle normally uses the server clock. For
`data_source='simulator'`, it anchors to the newest inverter telemetry
timestamp. An explicit `now` supplied to `run_cycle()` takes precedence.

If there is no cycle timestamp or no telemetry in the required window, the
service skips the cycle and persists no misleading decision.

## 5. Backend model vocabulary

The canonical breaker names shared by models, serializers, migrations, the
adapter, simulator and KBS are:

| Meaning | Canonical field |
|---|---|
| priority category | `priority_type` |
| priority inside a category | `priority_degree` |
| motor or normal load | `load_type` |
| motor/startup power | `peak_load_W` |
| settled power | `mean_load_W` |
| KBS user-release lock | `locked_out`, `lockout_reason`, `locked_at` |
| physical device button lock | `child_lock` |

The former names `priority`, `type`, `peak_load`, `mean_load`, and
`protected` must not be reintroduced. Migration
`breakers/0002_kbs_breaker_fields.py` preserves and renames their data.
`breakers/0004_merge_kbs_tuya.py` joins the KBS and Tuya migration branches so
both feature lines have one migration head.

## 6. Simulator and edge integration

The simulator/Pi sends breaker snapshots as a non-empty JSON array to:

```text
POST /api/breakers/status/
```

The endpoint performs one atomic bulk ingest:

- validates every device before writing anything;
- rejects duplicate or unknown `device_id` values;
- updates each `BreakerStatus` current-state row;
- copies the physical `child_lock` state to `Breaker`;
- stamps `last_switched_on_at` on an OFF-to-ON transition;
- appends an idempotent `BreakerReading` keyed by breaker and timestamp.

The endpoint currently uses `AllowAny`, consistent with the telemetry ingest
path. Device-token authentication is required before exposing it outside a
trusted simulator/LAN environment.

Other KBS simulator endpoints are:

| Endpoint | Purpose |
|---|---|
| `POST /api/telemetry/readings/` | ingest inverter telemetry |
| `POST /api/kbs/sim/run-cycle/` | run one Tier-2 cycle |
| `GET /api/kbs/sim/state/` | retrieve settings, latest decision, pending actions and alerts |
| `POST /api/kbs/sim/ack/` | mark applied actions executed |
| `PATCH /api/kbs/settings/` | change mode, cadence, source and power-saving setting |

## 7. Extension seams

- `apps/kbs/engine/grouping.py` owns pure selection policies. Both functions
  consume `BreakerFacts` and return selected `BreakerFacts`; they must not
  query models.
- `apps/kbs/weather.py` is the backend weather-service seam. The Django
  adapter calls it and converts its result into fact values. Network logic
  belongs here or behind another adapter, never in `engine/`.
- `apps/kbs/services.py` accepts an injected adapter, which is the seam for
  tests and alternative backends.

## 8. Tests

Pure engine and edge tests require no database:

```bash
python3 -m unittest \
  apps.kbs.tests \
  apps.kbs.test_services \
  edge.test_tier1_kbs \
  edge.test_simulator_bridge
```

Database-bound adapter and ingestion tests use the self-contained SQLite
settings in `config/settings/test.py`:

```bash
DJANGO_SETTINGS_MODULE=config.settings.test \
  python manage.py test \
  apps.breakers.tests.SimulatorStatusIngestTests \
  apps.kbs.test_adapter

DJANGO_SETTINGS_MODULE=config.settings.test python manage.py check
DJANGO_SETTINGS_MODULE=config.settings.test \
  python manage.py makemigrations --check --dry-run
```

The key boundary invariant is easy to audit:

```bash
rg -n "django|apps\." apps/kbs/engine
```

Apart from documentation text, that command should find no backend imports.

## 9. Deliberately separate future work

- observing-phase learning for `mean_load_W`, `peak_load_W`, and reserve
  thresholds;
- real weather-provider integration behind `apps/kbs/weather.py`;
- authenticated device ingestion;
- edge/Pi command delivery and production deployment concerns.

None of those changes should require edits to the rule engine unless they
introduce a genuinely new fact or decision rule. If they do, update
`KBS_FACTS_AND_RULES.md` first so the documented contract remains the source
of truth.
