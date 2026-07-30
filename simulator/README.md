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

## Pushing to the KBS backend

The push panel POSTs the generated inverter reading to `/api/telemetry/readings/` and
all breaker states to `/api/breakers/status/` on the configured server, using
**simulated** timestamps — so the KBS engine sees the simulated world.
`config/settings/development.py` enables CORS for this. Run the backend with
migrations applied and an Organization whose id matches the panel's value.
