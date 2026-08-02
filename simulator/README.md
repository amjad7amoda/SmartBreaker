# SmartBreaker Simulator

A standalone web page (HTML/CSS/JS, no build step) that simulates a whole
solar site — PV production, inverter, battery, and smart breakers — for
testing the KBS engine.

## Run

Open `index.html` directly in a browser (works from `file://` because the
climate data is embedded in `data.js`), or serve the folder:

```bash
python -m http.server 8791 --directory simulator
```

Then open <http://127.0.0.1:8791>. Serving the folder is recommended because
the Tier-1 bridge CORS policy explicitly trusts this local simulator origin.

## Run the two-tier scenario suite

The scenario runner drives the browser's physical model while evaluating the
**real** Python rule engines:

- Tier-1 is called through a loopback-only, dependency-free HTTP bridge.
- Tier-2 is called through the existing Django simulator endpoints.
- The browser applies both engines' actions, records their returned evidence,
  enforces Tier-1 safety precedence, and marks declared checks OBSERVED or
  MISSING. JavaScript does not decide which rule fired or invent commands.

### 1. Start infrastructure and prepare the simulator database

From the repository root:

```bash
docker compose up -d postgres redis
python manage.py migrate
python manage.py seed_simulator --reset-history
```

The command prints the simulator **Organization id**. Enter that value in the
browser's `Organization id` field. The seed command creates these device ids:

```text
sim-servers
sim-fridge
sim-ac-unit
sim-event-load
sim-grid
```

It also creates the scheduled event used by the event scenario. The
`--reset-history` option is deliberately limited to the dedicated simulator
site; it clears old simulator readings, decisions, actions and alerts so a
repeat run is deterministic. It never targets an arbitrary organization.

### 2. Start the three local processes

Use three terminals from the repository root:

```bash
# Terminal 1: Django / Tier-2
python manage.py runserver

# Terminal 2: the real Tier-1 evaluator on loopback
python -m edge.simulator_bridge

# Terminal 3: browser simulator static server
python -m http.server 8791 --directory simulator
```

Celery worker/beat are not required for browser scenarios: the browser invokes
the Tier-2 cycle endpoint every configured K seconds. The ordinary production
dispatcher still uses Celery beat.

### 3. Run a scenario

1. Open <http://127.0.0.1:8791>.
2. Enter the Organization id printed by `seed_simulator`.
3. Choose a scenario in **Two-tier scenario runner**.
4. Choose the scenario date and time. A locked field means the scenario relies
   on seeded date-specific data, such as a scheduled event.
5. Select **Load** to inspect the safe **BEFORE EVENT** state and its next
   scheduled disturbance.
6. If **Tester-controlled condition** appears, choose the battery voltage to
   inject. The simulator shows the usable charge derived from that voltage and
   whether it is above, low, or critical relative to the configured floor.
7. Select **Run scenario** and watch the phase change when the event occurs.
8. Review **Facts, fired rules & commands**. Each row is based on the response
   from `edge.tier1_kbs.evaluate`, `apps.kbs.engine.run_cycle`, or the command
   executor. Expand **Raw engine data** to inspect the exact fact snapshot and
   returned rule/command payload.

The source labels above the evidence table show the expected Python entry
points. If a response does not contain matching provenance and facts, the
simulator treats it as an engine error instead of displaying a successful
result.

Running the same scenario again reloads its browser state automatically. For a
completely clean server-side repeat (especially alert-cooldown or night-lockout
cases), run `python manage.py seed_simulator --reset-history` first.

### Scenario categories

- **Tier-1:** normal, overheat, overload, battery critical/low and grid outage.
- **Tier-2:** solar surplus, grid purchase, power-saving subset, sudden PV
  drop, battery protection, night trip, grid outage and scheduled event.
- **Integrated:** Tier-1 precedence, operation during backend loss, and return
  of control to Tier-2 after danger clears.

Scenario fault injection is intentionally deterministic: it can force reported
PV power, heatsink temperature, battery voltage/current and grid voltage while
leaving the normal manual simulator mode available.

Battery-critical, low-battery countdown, thin-battery grid-outage, and Tier-2
battery-protection scenarios expose their battery voltage to the tester. When
the tester controls voltage, usable charge is derived on the browser side from
the configured protection floor (0% usable) to the bank's full-charge voltage
(100%). Both values are then sent to the real engines. This prevents a scenario
from reporting a critical voltage together with an unrelated high capacity.
Values outside the scenario's expected trigger range are allowed for boundary
testing; the corresponding expected engine output will remain **MISSING** if
the real rule does not fire.

The simulator uses a standard dashboard layout:

- The unified header contains the scenario filter, date/time filter, run
  actions, status, and the **Simulation inputs** CTA.
- **Simulation inputs** opens a right-side action sheet containing simulated
  time, environment, electrical-model, scenario-condition, and backend fields.
- The sidebar **Overview** tab contains power flow, scenario status, KBS
  observability, and the complete breaker table. The breaker table has no
  independent horizontal or vertical scroll area.
- The sidebar **KBS observations** tab contains the detailed facts, fired
  rules, commands, alerts, and raw inverter payload.

## Real data

`data/solar_data.csv` holds **real monthly climatology from the NASA POWER
API** (~2001–2020 averages) for the 7 supported Syrian cities — per city and
month: all-sky and clear-sky solar irradiance (kWh/m²/day), cloud amount (%),
precipitation (mm/day), temperature (°C), humidity (%), the two-season label
(May–Oct = summer, Nov–Apr = winter) and the dominant weather.
`data.js` is the same table embedded as JavaScript.

Regenerate both (requires internet):

```bash
python simulator/tools/fetch_solar_data.py   # hits power.larc.nasa.gov, rewrites the CSV and data.js
```

## How the PV value is computed

1. Sun-position astronomy (declination + hour angle for the city's latitude)
   gives the day's sunrise/sunset and a bell-shaped elevation curve.
2. The bell is scaled so its daily area equals the **real** clear-sky yield:
   `max PV (W) × clear-sky GHI (kWh/m²/day) × 0.85 performance ratio`.
3. The current weather multiplies it: sunny ×1.0, partly cloudy ×0.75,
   cloudy ×0.45, rainy ×0.25, foggy ×0.20, storm ×0.10.
4. The inverter only harvests PV above the user-set threshold (default 80 W).

Season always auto-corrects from the datetime; auto-weather rolls every
30–90 simulated minutes with probabilities weighted by the month's real
cloud/precip/humidity, and manual weather choices incompatible with the
city+month are disabled.

## Breakers

Each breaker: priority type (mandatory/normal/comfort/ac_grid), degree, load
type (motor/normal), peak W, normal W, peak time (default **15 min** — the
draw stays at peak W that long after switch-on, then drops to normal W),
switch, online, fault. The `ac_grid` breaker supplies the site from the grid
while ON (battery then only charges from PV).

## Grid electricity availability

The **grid electricity available** checkbox models the state grid itself, which
is separate from the AC-grid *breaker*:

| grid breaker | grid available | result |
|---|---|---|
| off | – | site runs on solar/battery |
| on | yes | grid supplies the loads, PV charges the battery |
| on | **no** | breaker is closed but **nothing comes in** — the inverter reports 0 V grid voltage and the site still runs on solar/battery |

The KBS senses the third case from the reading (breaker ON + no grid voltage)
and, even with power saving off, keeps the breaker closed (so supply resumes
by itself when the grid returns) while shedding comfort/normal loads by
priority. The readout shows `battery (grid is OUT!)` and the breaker row shows
`grid ON (no input!)`.

## Closed-loop KBS control

Tick **connected** in the *KBS control* panel and the simulator runs the full
loop: every **K** real seconds (editable, synced to `KBSSettings.cycle_seconds`)
it triggers a KBS cycle on the server, fetches the pending breaker actions,
**applies them to the simulated breakers** (immediate on/off, or an armed
countdown in simulated time for battery-protection shutdowns), ACKs them, and
logs every decision branch, action and alert in the panel. Connecting also
enables the data push and switches the site's `data_source` to `simulator`.
A breaker switched off and back on (by the user or the KBS) re-enters its
peak phase for its full peak time.

When Tier-1 is connected, it evaluates every 500 ms of real time. While it
reports an active safety situation, a Tier-2 command cannot switch a
non-grid load back ON. Tier-2 OFF commands and an AC-grid ON command are still
allowed. Once Tier-1 reports that the danger cleared, pending Tier-2 control
can resume.

## Pushing to the KBS backend

The push panel POSTs the generated inverter reading to `/api/telemetry/readings/` and
all breaker states to `/api/breakers/status/` on the configured server, using
**simulated** timestamps — so the KBS engine sees the simulated world.
`config/settings/development.py` enables CORS for this. Run the backend with
migrations applied and an Organization whose id matches the panel's value.
