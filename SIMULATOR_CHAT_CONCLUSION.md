# SmartBreaker Browser Simulator — Implementation Conclusion

## 1. Purpose

This document summarizes the browser-simulator work completed during this
conversation. The objective was to create a practical environment for testing
the two SmartBreaker knowledge-based systems (KBS):

- **Tier-1:** the local edge safety engine in `edge/tier1_kbs.py`.
- **Tier-2:** the site-level Django KBS in `apps/kbs/engine/`.

The most important design rule was that the browser must not pretend a rule
fired or manufacture a successful command. The Python KBS implementations
remain the source of truth. Browser JavaScript generates physical conditions,
sends facts to the engines, executes returned commands, and presents the
evidence.

## 2. Final architecture

```text
Browser simulator
  |
  |-- simulated inverter and breaker facts
  |
  |-- Tier-1 request --> local loopback bridge
  |                       |
  |                       `--> edge.tier1_kbs.evaluate(...)
  |
  `-- telemetry + cycle request --> Django simulator API
                                  |
                                  `--> apps.kbs.engine.run_cycle(...)

Python engine responses
  |
  |-- normalized fact snapshot
  |-- fired situation / decision branch
  |-- commands and countdowns
  `-- alerts
        |
        `--> browser evidence log and command executor
```

The browser therefore has three responsibilities:

1. Model the solar site and inject deterministic scenario conditions.
2. Call the real Python entry points through their local APIs.
3. Display and execute what the engines actually returned.

It does **not** reproduce the Tier-1 or Tier-2 rule trees in JavaScript.

## 3. Tier-1 browser integration

### Local bridge

`edge/simulator_bridge.py` was added as a small, dependency-free HTTP adapter.
It binds to `127.0.0.1:8788` and exposes:

- `GET /health`
- `POST /evaluate`

The evaluation endpoint:

1. Validates the browser JSON payload.
2. Converts it into the real `InverterState`, `BreakerState`, and
   `Tier1Config` dataclasses.
3. Calls `edge.tier1_kbs.evaluate`.
4. Returns the real situation, notification, and commands.
5. Returns normalized facts and the provenance value
   `edge.tier1_kbs.evaluate` for the evidence panel.

The bridge is only an adapter. No alternative safety rules were implemented
inside it.

### Tier-1 browser loop

When Tier-1 is connected, the simulator evaluates the physical snapshot about
every 500 milliseconds of real time. Returned commands are applied to the
matching simulated breakers, including real countdown behavior when the
engine requests a delayed action.

## 4. Tier-2 browser integration

The existing Django simulator endpoints are used for the complete Tier-2
closed loop:

1. The browser posts inverter and breaker telemetry.
2. It calls `/api/kbs/sim/run-cycle/`.
3. Django calls the real `apps.kbs.engine.run_cycle` function.
4. The response includes the real decision branch, fact snapshot, and
   generated actions.
5. The browser fetches pending actions from `/api/kbs/sim/state/`.
6. It executes those actions against the simulated breakers.
7. It acknowledges applied actions through `/api/kbs/sim/ack/`.

`apps/kbs/views.py` was extended to expose engine provenance and the stored
decision facts in simulator responses. The underlying facts, rules, and action
selection algorithms were not duplicated in the frontend.

## 5. Command execution and Tier precedence

The browser executor now represents an actual closed loop instead of displaying
commands without applying them.

- Immediate `on` and `off` actions change breaker state.
- Delayed shutdowns are armed as countdowns in simulated time.
- Applied actions are acknowledged to Django.
- Executor events are logged separately from engine decisions.

Tier-1 owns local safety. While a Tier-1 danger is active, a Tier-2 command
cannot switch a non-grid load back on. Safe Tier-2 actions, such as shedding a
load or connecting the grid, can still run. Tier-2 control resumes after the
Tier-1 danger clears.

This makes integrated scenarios able to demonstrate safety precedence and
recovery rather than two engines independently changing the same breaker.

## 6. Deterministic scenario suite

`simulator/scenarios.js` contains scenario definitions, physical starting
conditions, timed disturbances, and expected engine outputs. It does not
contain replacement rule logic.

### Tier-1 scenarios

- Normal operation
- Inverter overheat
- Inverter overload
- Critically low battery
- Low-battery countdown
- State-grid outage

### Tier-2 scenarios

- Daytime solar surplus
- Daytime deficit and grid purchase
- Power-saving load selection
- Sudden PV drop with season-dependent diagnosis
- Battery protection
- Night sudden-draw trip
- Grid-outage fallback
- Scheduled-event requirement

### Integrated scenarios

- Tier-1 precedence over Tier-2
- Tier-1 operation while the backend is unavailable
- Return of control to Tier-2 after a local danger clears

Scenario checks use observations collected from real engine responses. The UI
labels them:

- `WAITING` before the expected output arrives.
- `OBSERVED` when the engine actually returns the expected output.
- `MISSING` when the scenario finishes without that output.

The internal scenario definition may describe an expected branch or command,
but it cannot create that observation itself.

## 7. Before-event and during-event presentation

Fault scenarios were changed to begin in a safe state and inject the fault
later. For example, the inverter-overheat scenario now behaves as follows:

```text
BEFORE DISTURBANCE
  heatsink = 40 °C
  Tier-1 fact snapshot is recorded
  no safety rule is expected to fire

at +180 simulated seconds
  heatsink changes to 80 °C

DURING OVERHEAT
  a new fact snapshot is recorded
  the real inverter_overheat situation is returned
  returned breaker commands are displayed and executed
```

The top command bar shows the current phase and next injected event so the
operator can compare conditions before, during, and after a disturbance.

## 8. Scenario date and time

The scenario runner now provides separate start-date and start-time controls.
The selected values become the actual simulated clock used in telemetry sent
to Tier-2. This matters because the Python facts engine derives properties such
as day/night and season from timestamps.

Most scenarios accept a custom date and time. The scheduled-event scenario is
locked to its seeded timestamp because its `ScheduledEvent` is stored in the
Django database.

## 9. Facts, rules, commands, and executor evidence

The simulator includes a unified evidence stream with these sources:

- `T1`: data returned by `edge.tier1_kbs.evaluate`.
- `T2`: data returned by `apps.kbs.engine.run_cycle`.
- `EXECUTOR`: what the browser did with a returned command.

Evidence row types include:

- `FACT`
- `RULE`
- `COMMAND`
- `ALERT`
- `SCHEDULED`
- `APPLIED`
- `BLOCKED`
- `ERROR`

Each fact or decision entry can expose **Raw engine data**, allowing the exact
payload to be inspected. If an endpoint does not return the expected engine
provenance, the simulator records an error rather than showing a successful
result.

## 10. Simulator database bootstrap

The management command below prepares the dedicated browser-simulator site:

```powershell
python manage.py seed_simulator --reset-history
```

It creates or updates these hardware identifiers:

```text
sim-servers
sim-fridge
sim-ac-unit
sim-event-load
sim-grid
```

It also:

- Configures active simulator-mode KBS settings.
- Creates the scheduled event used by the event scenario.
- Restores deterministic initial breaker states.
- Optionally clears old simulator telemetry, decisions, actions, and alerts.

The command was improved to reuse an existing simulator organization when all
`sim-*` devices already belong to that same clearly identifiable simulator
site. It still refuses to move globally unique device IDs between unrelated
organizations or to choose between devices split across organizations.

In the current local database, the simulator site is organization `1`.

## 11. Frontend layout refactor

The simulator presentation was reorganized from one long scrolling document
into an observability dashboard.

### Top command bar

The always-visible top area contains:

- Scenario selector
- Scenario date and time
- Load, run, and stop controls
- Current status
- Current phase
- Next event
- Scenario description

### Left column — simulation inputs

- Simulated clock and time scale
- City, season, and weather
- PV, inverter, battery, and grid settings
- Backend telemetry configuration

### Center column — live site

- Actual engine-output checks
- Scenario event timeline
- Live PV, load, battery, and source metrics
- PV production graph
- Breaker state and manual controls

### Right column — KBS observability

- Unified fact/rule/command evidence
- Tier-1 connection, status, situation, and log
- Tier-2 settings, status, branch, and log
- Collapsible raw inverter payload

On desktop, the browser page is fixed to the viewport. The three columns remain
visible together and scroll independently where necessary. Smaller screens
fall back to a responsive stacked layout.

All existing DOM IDs were preserved, so the presentation refactor did not
require changes to simulator behavior or KBS code.

## 12. Files involved

### Browser presentation and orchestration

- `simulator/index.html`
- `simulator/style.css`
- `simulator/sim.js`
- `simulator/scenarios.js`
- `simulator/README.md`

### Real-engine adapters and simulator API evidence

- `edge/simulator_bridge.py`
- `edge/test_simulator_bridge.py`
- `apps/kbs/views.py`

### Database preparation

- `apps/kbs/management/commands/seed_simulator.py`

The existing Tier-1 and Tier-2 facts/rules implementations remain the decision
source. They were not rewritten in JavaScript for the simulator.

## 13. How to run everything

### Infrastructure and database

From the repository root:

```powershell
docker compose up -d postgres redis
python manage.py migrate
python manage.py seed_simulator --reset-history
```

### Local services

Use three terminals from the repository root.

Terminal 1 — Django and Tier-2:

```powershell
python manage.py runserver
```

Terminal 2 — real Tier-1 bridge:

```powershell
python -m edge.simulator_bridge
```

Terminal 3 — browser assets:

```powershell
python -m http.server 8791 --directory simulator
```

Open:

```text
http://127.0.0.1:8791
```

Use organization ID `1` for the currently seeded local simulator site.

## 14. Recommended testing workflow

1. Reset simulator history when deterministic database state is important.
2. Open the browser simulator.
3. Choose a scenario and select its date and time.
4. Click **Load**.
5. Inspect the safe starting state and `BEFORE DISTURBANCE` phase.
6. Click **Run scenario**.
7. Watch the center timeline and breaker state.
8. Monitor facts, fired rules, commands, and executor results on the right.
9. Expand **Raw engine data** when detailed verification is needed.
10. Treat `MISSING` as a real-engine output that did not arrive, not merely a
    visual failure.

## 15. Validation completed

The completed work was checked with:

- 15 Tier-1 and bridge unit tests.
- 31 Tier-2 KBS tests.
- Django system checks.
- Migration drift checks.
- Seeder execution against PostgreSQL.
- A real browser Tier-1 overheat scenario:
  - Safe pre-event facts recorded.
  - Real `inverter_overheat` situation recorded.
  - Two returned command rows recorded.
  - All 3 expected outputs observed.
- A database-backed Tier-2 daytime-surplus scenario:
  - Real `day.surplus.comfort_on` branch recorded.
  - Returned command applied by the executor.
  - Both expected outputs observed.
- Browser date-control testing:
  - Custom date/time loaded into the simulated clock.
  - Scheduled-event date/time remained locked.
- Dashboard layout testing:
  - Top command bar positioned above the workspace.
  - Left, center, and right columns visible together.
  - Desktop page fixed to the viewport.
  - Column scrolling works independently.
  - No duplicated DOM IDs.
  - Every ID required by `simulator/sim.js` remains present.

## 16. Final result

The SmartBreaker browser simulator is now both a deterministic test driver and
an observability interface for the real two-tier KBS architecture. It can show
the physical conditions before a fault, inject the disturbance, record the
facts evaluated by Python, identify the rule or branch that actually fired,
display the commands returned by the engine, and show whether those commands
were applied, scheduled, or blocked.

This creates a clear separation of concerns:

- **Python facts and rules decide.**
- **Scenario definitions produce repeatable physical conditions.**
- **The browser executes and explains the result.**

