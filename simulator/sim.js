/* SmartBreaker Simulator engine.
 *
 * Combines the real NASA POWER climatology in data.js (monthly irradiance,
 * cloud, precipitation, humidity, temperature per city) with sun-position
 * astronomy and the user's inputs to produce a live PV curve, a full inverter
 * reading, and per-breaker behavior — and can push both to the KBS backend.
 *
 * Every variable carries a comment with its meaning and unit.
 */

'use strict';

// ============================= constants ==================================

const TICK_MS = 100;                 // real time between simulation ticks (ms)
const PERFORMANCE_RATIO = 0.85;      // PV system losses: wiring, dirt, temperature (fraction of ideal yield)
const CHARGE_EFFICIENCY = 0.95;      // battery round-trip charge efficiency (fraction)
const MPPT_VOLTAGE_V = 330;          // typical PV string voltage at the MPPT while producing (V)
const AC_VOLTAGE_V = 230;            // nominal AC output voltage (V)
const HEATSINK_TAU_S = 300;          // time constant for heatsink temperature drift toward its target (s)

// Weather label -> fraction of clear-sky PV production that survives it.
const WEATHER_PV_FACTOR = {
  sunny: 1.0,          // clear sky: full clear-sky production
  partly_cloudy: 0.75, // scattered clouds
  cloudy: 0.45,        // solid cloud deck
  rainy: 0.25,         // rain clouds
  foggy: 0.20,         // fog blocks most direct light
  storm: 0.10,         // storm front
};

// ============================= state ======================================

const state = {
  simMs: Date.now(),      // simulated wall-clock time (ms since epoch)
  scale: 60,              // simulated seconds that pass per real second (s/s)
  running: true,          // simulation clock running (flag)

  city: 'Damascus',       // selected city (name, must exist in SOLAR_DATA)
  weather: 'sunny',       // current weather label (see WEATHER_PV_FACTOR)
  weatherAuto: true,      // pick weather automatically from climate data (flag)
  nextWeatherPickMs: 0,   // sim time of the next automatic weather re-roll (ms since epoch)

  maxPvW: 4000,           // user-set maximum PV production of the array (W)
  pvThresholdW: 80,       // inverter uses PV only when production is at/above this (W)
  maxInverterW: 4000,     // maximum continuous AC output of the inverter (W)
  gridAvailable: true,    // the state grid actually has electricity right now; OFF = outage: a closed grid breaker takes in nothing (flag)

  batteryCapacityWh: 5000, // usable battery energy at 100% (Wh)
  batterySocWh: 3000,      // energy currently stored (Wh)
  batteryNominalV: 24,     // nominal bank voltage (V)
  batteryVoltageOverrideV: null, // tester-supplied sensor voltage; null uses the physical charge/load model (V|null)

  heatsinkC: 25,          // current heatsink temperature (degC)

  breakers: [],           // list of breaker objects (see addBreaker)
  breakerSeq: 0,          // id counter for new breakers (count)

  push: { enabled: false, baseUrl: 'http://127.0.0.1:8000', orgId: 1, intervalS: 5, lastRealMs: 0, status: 'off' },

  kbs: {
    connected: false,      // KBS closed-loop control active (flag)
    cycleS: 60,            // K: real seconds between KBS decision cycles (s)
    lastCycleRealMs: 0,    // real time of the previous cycle trigger (ms)
    status: 'disconnected',// human-readable loop status (text)
    branch: '–',           // decision-tree branch of the last cycle (text)
    log: [],               // recent loop events, newest first (list of {t, text, cls})
    countdowns: [],        // armed delayed switch-offs: {deviceId, fireAtSimMs, reason}
    lastAlertTs: '',       // created_at of the newest alert already logged (ISO text)
  },

  tier1: {
    connected: false,      // local Tier-1 bridge evaluation enabled (flag)
    baseUrl: 'http://127.0.0.1:8788', // loopback-only Python bridge URL (text)
    intervalMs: 500,       // real-time cadence of local safety evaluation (ms)
    lastEvalRealMs: 0,     // performance clock of the latest request (ms)
    busy: false,           // prevents overlapping bridge requests (flag)
    status: 'disconnected',// bridge connection/evaluation status (text)
    situation: '',         // latest non-empty Tier1Result situation, or '' (text)
    log: [],               // recent Tier-1 messages, newest first
    config: {
      heatsink_temp_limit_C: 70,
      overload_fraction: 1.05,
      battery_low_voltage_V: 24,
      battery_low_margin_V: 0.5,
      battery_critical_margin_V: 0.1,
      battery_shutdown_buffer_percent: 2,
      grid_present_min_V: 100,
    },
  },

  scenario: {
    definition: null,      // selected scenario object, or null (SMARTBREAKER_SCENARIOS item)
    active: false,         // scenario timer/timeline currently running (flag)
    hasRun: false,         // selected setup already ran and must be reloaded before another run
    startedRealMs: 0,      // performance clock at scenario start (ms)
    startedSimMs: 0,       // simulated clock at scenario start (ms since epoch)
    nextEventIndex: 0,     // next scenario timeline event to apply (index)
    overrides: {},         // forced sensor values for deterministic fault injection
    events: [],            // per-load copy of timeline events, editable without mutating scenario definitions
    batteryVoltageDeterminesSoc: false, // true when this scenario's chosen voltage must derive usable charge
    observations: null,    // Tier-1/Tier-2 outputs collected during this run
    originalBaseUrl: '',   // backend URL restored after an offline scenario (text)
    log: [],               // scenario timeline, newest first
  },

  evidence: {
    entries: [],           // actual Python-engine facts/rules/commands plus executor outcomes
    filter: 'all',         // all | T1 | T2 | EXECUTOR
    sequence: 0,           // stable newest-first row id (count)
    lastTier1FactsRealMs: 0, // throttle repetitive Tier-1 fact snapshots (ms)
  },

  lastRealMs: performance.now(), // real timestamp of the previous tick (ms)
};

// ============================= helpers ====================================

const $ = (id) => document.getElementById(id);

function initDashboardNavigation() {
  const shell = $('dashboard-shell');
  const pageTitle = $('dashboard-page-title');
  const destinations = {
    overview: {
      button: $('nav-overview'),
      view: $('view-overview'),
      title: 'Overview',
    },
    observations: {
      button: $('nav-kbs-observations'),
      view: $('view-kbs-observations'),
      title: 'KBS observations',
    },
  };
  if (!shell || !pageTitle || Object.values(destinations).some((item) => !item.button || !item.view)) return;

  function activate(name, focusTab = false) {
    const selected = destinations[name] ?? destinations.overview;
    for (const item of Object.values(destinations)) {
      const active = item === selected;
      item.view.hidden = !active;
      item.view.classList.toggle('active', active);
      item.button.classList.toggle('active', active);
      if (active) item.button.setAttribute('aria-current', 'page');
      else item.button.removeAttribute('aria-current');
    }
    pageTitle.textContent = selected.title;
    shell.scrollTop = 0;
    if (focusTab) selected.button.focus();
  }

  destinations.overview.button.addEventListener('click', () => activate('overview'));
  destinations.observations.button.addEventListener('click', () => activate('observations'));
  activate('overview');
}

function initSimulationInputSheet() {
  const sheet = $('dashboard-column-left');
  const backdrop = $('simulation-inputs-backdrop');
  const headerButton = $('btn-open-inputs');
  const closeButton = $('btn-close-inputs');
  if (!sheet || !backdrop || !headerButton || !closeButton) return;

  let returnFocus = headerButton;

  function setOpen(open, restoreFocus = true) {
    if (open) returnFocus = document.activeElement || headerButton;
    document.body.classList.toggle('input-sheet-open', open);
    sheet.setAttribute('aria-hidden', String(!open));
    backdrop.hidden = !open;
    headerButton.setAttribute('aria-expanded', String(open));
    if (open) {
      window.setTimeout(() => closeButton.focus(), 0);
    } else if (restoreFocus && returnFocus instanceof HTMLElement) returnFocus.focus();
  }

  headerButton.addEventListener('click', () => setOpen(true));
  closeButton.addEventListener('click', () => setOpen(false));
  backdrop.addEventListener('click', () => setOpen(false));
  document.addEventListener('keydown', (event) => {
    if (!document.body.classList.contains('input-sheet-open')) return;
    if (event.key === 'Escape') {
      setOpen(false);
      return;
    }
    if (event.key === 'Tab') {
      const focusable = [...sheet.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )].filter((element) => element.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
}

function cityRow(monthNum) {
  // The real-climatology row for the selected city and a month (1-12).
  return SOLAR_DATA.find((r) => r.city === state.city && r.month === monthNum);
}

function seasonForMonth(monthNum) {
  // Two-season Syrian split used by the simulator: May-Oct summer, Nov-Apr winter.
  return monthNum >= 5 && monthNum <= 10 ? 'summer' : 'winter';
}

// ============================= solar model ================================

function solarDeclinationRad(dayOfYear) {
  // Earth's axial tilt seen by the sun on a given day (radians). Cooper's formula.
  return (23.45 * Math.PI / 180) * Math.sin(2 * Math.PI * (284 + dayOfYear) / 365);
}

function solarElevationSin(date, latitudeDeg) {
  // sin(solar elevation) for a local datetime; <=0 means the sun is below the horizon.
  // Local clock time is used as solar time — a small approximation fine for a simulator.
  const start = new Date(date.getFullYear(), 0, 0);                    // Jan 0 of the sim year
  const dayOfYear = Math.floor((date - start) / 86400000);             // 1-366 (days)
  const decl = solarDeclinationRad(dayOfYear);                         // solar declination (rad)
  const lat = latitudeDeg * Math.PI / 180;                             // site latitude (rad)
  const hours = date.getHours() + date.getMinutes() / 60 + date.getSeconds() / 3600; // local clock (h)
  const hourAngle = (hours - 12) * 15 * Math.PI / 180;                 // sun's angle from local noon (rad)
  return Math.sin(lat) * Math.sin(decl) + Math.cos(lat) * Math.cos(decl) * Math.cos(hourAngle);
}

function dayShapeIntegralH(date, latitudeDeg) {
  // Integral of max(sin(elevation),0) over the whole day (hours). Used to
  // normalize the bell shape so its area matches the real daily energy.
  let sum = 0; // running integral (dimensionless-hours)
  for (let m = 0; m < 24 * 60; m += 5) {
    const t = new Date(date.getFullYear(), date.getMonth(), date.getDate(), 0, m);
    sum += Math.max(solarElevationSin(t, latitudeDeg), 0) * (5 / 60);
  }
  return sum;
}

let shapeCache = { key: '', integralH: 0, sunriseH: null, sunsetH: null }; // per-day cache of the shape integral and sun window

function daySolar(date, row) {
  // Clear-sky PV power right now plus the day's sun window, from real data + astronomy.
  const key = `${state.city}|${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
  if (shapeCache.key !== key) {
    const integralH = dayShapeIntegralH(date, row.latitude_deg); // area under the elevation bell (h)
    let sunriseH = null, sunsetH = null;                         // local clock hours of sunrise/sunset (h)
    for (let m = 0; m < 24 * 60; m += 5) {
      const t = new Date(date.getFullYear(), date.getMonth(), date.getDate(), 0, m);
      const up = solarElevationSin(t, row.latitude_deg) > 0;
      if (up && sunriseH === null) sunriseH = m / 60;
      if (up) sunsetH = m / 60;
    }
    shapeCache = { key, integralH, sunriseH, sunsetH };
  }
  // Daily clear-sky energy of the array: GHI (kWh/m2/day, numerically "peak sun
  // hours") x array size x performance ratio -> Wh for a maxPvW-sized array.
  const clearDayWh = state.maxPvW * row.clearsky_ghi_kwh_m2_day * PERFORMANCE_RATIO; // (Wh)
  const shapeNow = Math.max(solarElevationSin(date, row.latitude_deg), 0);           // bell height now (0-1)
  const clearSkyW = shapeCache.integralH > 0
    ? Math.min((clearDayWh / shapeCache.integralH) * shapeNow, state.maxPvW)         // clear-sky production now (W)
    : 0;
  return { clearSkyW, sunriseH: shapeCache.sunriseH, sunsetH: shapeCache.sunsetH };
}

function currentPvW(date, row) {
  // Actual PV production now: clear-sky level scaled by the active weather (W).
  const { clearSkyW } = daySolar(date, row);
  return clearSkyW * (WEATHER_PV_FACTOR[state.weather] ?? 1.0);
}

// ============================= weather model ==============================

function allowedWeather(row) {
  // Weather labels plausible for this city+month, from the real climate row.
  const options = ['sunny', 'partly_cloudy'];                    // always possible
  if (row.cloud_amount_percent >= 25) options.push('cloudy');
  if (row.precip_mm_day >= 0.3) options.push('rainy');
  if (row.precip_mm_day >= 1.5) options.push('storm');
  if (row.humidity_percent >= 65) options.push('foggy');
  return options;
}

function pickAutoWeather(row, date) {
  // Randomly pick a weather label weighted by the month's real cloud/precip/
  // humidity averages, so e.g. a Latakia January is often rainy and a Deir
  // Ezzour July almost always sunny.
  const cloud = row.cloud_amount_percent;   // average cloud amount (%)
  const weights = {
    sunny: Math.max(100 - cloud, 5),                                  // clear share (weight)
    partly_cloudy: cloud * 0.55,                                      // scattered-cloud share (weight)
    cloudy: cloud * 0.45,                                             // overcast share (weight)
    rainy: row.precip_mm_day * 18,                                    // rain share scales with real mm/day (weight)
    storm: row.precip_mm_day >= 1.5 ? row.precip_mm_day * 4 : 0,      // storms only in wet months (weight)
    foggy: row.humidity_percent >= 65 && date.getHours() <= 9 ? (row.humidity_percent - 60) : 0, // fog: humid mornings (weight)
  };
  const total = Object.values(weights).reduce((a, b) => a + b, 0);    // sum of weights
  let roll = Math.random() * total;                                   // random point in the weight space
  for (const [label, w] of Object.entries(weights)) {
    roll -= w;
    if (roll <= 0) return label;
  }
  return 'sunny';
}

// ============================= breakers ===================================

function addBreaker(preset = {}) {
  state.breakerSeq += 1;
  state.breakers.push({
    deviceId: preset.deviceId ?? `sim-breaker-${state.breakerSeq}`, // hardware id sent to the backend (text)
    priorityType: preset.priorityType ?? 'normal',                  // mandatory | normal | comfort | ac_grid
    priorityDegree: preset.priorityDegree ?? 1,                     // importance inside the category (positive int)
    loadType: preset.loadType ?? 'normal',                          // motor | normal
    peakW: preset.peakW ?? 800,                                     // draw during the initial peak phase (W)
    normalW: preset.normalW ?? 300,                                 // steady draw after the peak phase (W)
    peakMinutes: preset.peakMinutes ?? 15,                          // how long the peak phase lasts after switch-on (min)
    switchOn: preset.switchOn ?? false,                             // relay position (flag)
    online: preset.online ?? true,                                  // reachable on the network (flag)
    fault: preset.fault ?? '',                                      // fault flags; empty = healthy (text)
    onSinceMs: null,                                                // sim time of the last OFF->ON (ms since epoch)
  });
  renderBreakerTable();
}

function breakerDrawW(b) {
  // Power this breaker pulls right now (W). Motor and normal loads both use
  // the peak/normal pair; the peak phase runs peakMinutes after switch-on.
  if (!b.switchOn || !b.online || b.priorityType === 'ac_grid') return 0;
  const inPeak = b.onSinceMs !== null
    && (state.simMs - b.onSinceMs) < b.peakMinutes * 60000;          // still inside the peak phase (flag)
  return inPeak ? b.peakW : b.normalW;
}

function gridBreakerOn() {
  // True when an AC-grid breaker exists, is ON and reachable -> site buys grid power.
  return state.breakers.some((b) => b.priorityType === 'ac_grid' && b.switchOn && b.online);
}

// ============================= power flow =================================

function batteryVoltageRange() {
  // The configured protection floor represents 0% *usable* charge. Energy
  // below that voltage is deliberately unavailable because discharging it
  // would damage the bank. The full point follows the existing 24 V model and
  // scales for 12/48 V banks.
  const scale = state.batteryNominalV / 24;
  const floorV = state.tier1.config.battery_low_voltage_V;
  const fullV = Math.max(27.4 * scale, floorV + 0.1);
  return { floorV, fullV, scale };
}

function batterySocFromVoltage(voltageV) {
  const { floorV, fullV } = batteryVoltageRange();
  return Math.min(Math.max((voltageV - floorV) / (fullV - floorV), 0), 1);
}

function synchronizeBatteryChargeToVoltage(voltageV) {
  const socFrac = batterySocFromVoltage(voltageV);
  state.batterySocWh = state.batteryCapacityWh * socFrac;
  if ($('inp-batsoc')) $('inp-batsoc').value = (socFrac * 100).toFixed(1);
  return socFrac;
}

function batteryVoltageV(socFrac, dischargeW) {
  // Linear usable-charge curve plus sag proportional to discharge power.
  // This is the forward form of batterySocFromVoltage(), so tester-selected
  // voltage and reported capacity no longer contradict one another.
  const { floorV, fullV, scale } = batteryVoltageRange();
  const openCircuit = floorV + (fullV - floorV) * socFrac;
  const sag = 1.2 * scale * (dischargeW / Math.max(state.batteryCapacityWh, 1)); // load sag, ~1.2 V at 1C on 24 V (V)
  return openCircuit - sag;
}

function stepPower(simDtS, row) {
  // One simulation step of the whole electrical site. simDtS: sim seconds elapsed (s).
  const date = new Date(state.simMs);
  const forced = state.scenario.definition ? state.scenario.overrides : {}; // deterministic scenario sensor overrides
  const modeledPvW = currentPvW(date, row);                          // weather/astronomy PV model (W)
  const pvW = forced.pvW ?? modeledPvW;                              // scenario may inject an exact PV value (W)
  const pvUsableW = pvW >= state.pvThresholdW ? pvW : 0;             // inverter only harvests PV above the threshold (W)
  const loadW = state.breakers.reduce((sum, b) => sum + breakerDrawW(b), 0); // total AC load (W)
  const gridOn = gridBreakerOn();                                    // AC-grid breaker closed (flag)
  const gridSupplying = gridOn && state.gridAvailable;               // grid actually delivering power; a closed breaker during an outage takes in nothing (flag)

  let chargeW = 0;     // power flowing into the battery (W)
  let dischargeW = 0;  // power drained from the battery (W)
  if (gridSupplying) {
    // Grid covers the loads; all usable PV charges the battery.
    chargeW = pvUsableW;
  } else {
    const netW = pvUsableW - loadW;                                  // PV surplus (+) or deficit (-) (W)
    if (netW >= 0) chargeW = netW;
    else dischargeW = -netW;
  }

  // Integrate battery energy over the step.
  const dtH = simDtS / 3600;                                         // step length (h)
  state.batterySocWh += chargeW * CHARGE_EFFICIENCY * dtH - dischargeW * dtH;
  state.batterySocWh = Math.min(Math.max(state.batterySocWh, 0), state.batteryCapacityWh);
  const voltageOverrideV = forced.batteryVoltageV ?? state.batteryVoltageOverrideV;
  const voltageDeterminesSoc = state.scenario.definition
    ? state.scenario.batteryVoltageDeterminesSoc
    : Number.isFinite(state.batteryVoltageOverrideV);
  if (Number.isFinite(voltageOverrideV) && voltageDeterminesSoc) {
    // A tester-selected voltage is authoritative. Keep the paired capacity
    // fact synchronized on every frame instead of reporting an impossible
    // combination such as 24.05 V and 80% usable charge.
    state.batterySocWh = state.batteryCapacityWh * batterySocFromVoltage(voltageOverrideV);
  }
  const socFrac = state.batterySocWh / state.batteryCapacityWh;      // state of charge (0-1)
  const empty = !gridSupplying && dischargeW > 0 && state.batterySocWh <= 0;  // battery exhausted with no working grid: blackout (flag)

  // Heatsink drifts toward ambient + load-dependent heating.
  const targetC = row.temp_C + 30 * (loadW / state.maxInverterW) + 8 * (pvW / Math.max(state.maxPvW, 1)); // steady-state temperature (degC)
  state.heatsinkC += (targetC - state.heatsinkC) * Math.min(simDtS / HEATSINK_TAU_S, 1);

  const vBat = voltageOverrideV ?? batteryVoltageV(socFrac, dischargeW); // bank voltage, optionally tester-controlled (V)
  const heatsinkC = forced.heatsinkC ?? state.heatsinkC;             // reported heatsink temperature (degC)
  const gridVoltageV = forced.gridVoltageV ?? (gridSupplying ? AC_VOLTAGE_V : 0); // sensed grid input (V)
  const chargeCurrentA = forced.batteryChargeCurrentA ?? (chargeW / Math.max(vBat, 0.1)); // battery charge current (A)
  const dischargeCurrentA = forced.batteryDischargeCurrentA ?? (dischargeW / Math.max(vBat, 0.1)); // battery discharge current (A)
  return {
    pvW, pvUsableW, loadW, gridOn, gridSupplying,
    chargeW, dischargeW, chargeCurrentA, dischargeCurrentA,
    socFrac, vBat, heatsinkC, gridVoltageV, empty,
  };
}

function buildReading(flow) {
  // The full inverter snapshot in exactly the shape of the backend Reading model.
  const date = new Date(state.simMs);
  return {
    organization: state.push.orgId,
    timestamp: date.toISOString(),
    grid_voltage_V: +flow.gridVoltageV.toFixed(2),                    // grid input sensed by the inverter (V)
    grid_freq_Hz: flow.gridVoltageV >= 100 ? 50 : 0,                  // (Hz)
    ac_output_voltage_V: AC_VOLTAGE_V,                                // (V)
    ac_output_freq_Hz: 50,                                            // (Hz)
    ac_output_apparent_power_VA: Math.round(flow.loadW / 0.95),       // active power / power factor (VA)
    ac_output_active_power_W: Math.round(flow.loadW),                 // (W)
    output_load_percent: Math.round(100 * flow.loadW / state.maxInverterW), // (% of rating)
    bus_voltage_V: 360,                                               // DC bus while running (V)
    battery_voltage_V: +flow.vBat.toFixed(2),                         // (V)
    battery_charge_current_A: +flow.chargeCurrentA.toFixed(2),       // (A)
    battery_capacity_percent: Math.round(flow.socFrac * 100),         // (%)
    heatsink_temp_C: +flow.heatsinkC.toFixed(1),                      // (degC)
    pv_input_current_A: +(flow.pvW > 0 ? flow.pvW / MPPT_VOLTAGE_V : 0).toFixed(2), // (A)
    pv_input_voltage_V: flow.pvW > 0 ? MPPT_VOLTAGE_V : 0,            // (V)
    battery_voltage_scc_V: +flow.vBat.toFixed(2),                     // solar charge controller's battery reading (V)
    battery_discharge_current_A: +flow.dischargeCurrentA.toFixed(2), // (A)
    device_status_flags: '00010000',                                  // static status bits (text)
    battery_voltage_offset_fans_on: 0,                                // (V)
    eeprom_version: 'sim-1',                                          // (text)
    pv_charging_power_W: Math.round(flow.pvUsableW),                  // PV power the inverter actually harvests (W)
    device_status_flags2: '00',                                       // (text)
  };
}

function buildBreakerStatuses() {
  // Per-breaker live states in exactly the shape of POST /api/breakers/status/.
  return state.breakers.map((b) => {
    const drawW = breakerDrawW(b);                                    // current draw (W)
    return {
      device_id: b.deviceId,
      timestamp: new Date(state.simMs).toISOString(),
      switch: b.switchOn,
      countdown_1_s: 0,
      cur_current_mA: Math.round(drawW / AC_VOLTAGE_V * 1000),        // device reports mA
      cur_power_mW: Math.round(drawW * 1000),                         // device reports mW
      cur_voltage_mV: AC_VOLTAGE_V * 1000,                            // device reports mV
      fault: b.fault,
      relay_status: 'last',
      child_lock: false,
      cycle_time: '',
      online: b.online,
    };
  });
}

// ============================= main loop ==================================

function tick() {
  const realNow = performance.now();                                  // real clock (ms)
  const realDtS = (realNow - state.lastRealMs) / 1000;                // real seconds since last tick (s)
  state.lastRealMs = realNow;
  if (!state.running) return render(null);

  const simDtS = realDtS * state.scale;                               // simulated seconds this tick (s)
  state.simMs += simDtS * 1000;
  advanceScenarioTimeline();

  const date = new Date(state.simMs);
  const row = cityRow(date.getMonth() + 1);                           // real climate row for this city+month
  if (!row) return;

  // Season auto-correction: the select always snaps to the datetime's season.
  $('inp-season').value = seasonForMonth(date.getMonth() + 1);

  // Automatic weather: re-roll every 30-90 simulated minutes.
  if (state.weatherAuto && state.simMs >= state.nextWeatherPickMs) {
    state.weather = pickAutoWeather(row, date);
    state.nextWeatherPickMs = state.simMs + (30 + Math.random() * 60) * 60000;
    $('inp-weather').value = state.weather;
  }

  const flow = stepPower(simDtS, row);
  tier1Loop(flow);
  maybePush(flow);
  kbsCountdownCheck();
  kbsLoop();
  render(flow, row, date);
  updateScenarioRun();
}

// ============================= Tier-1 edge loop ===========================

function tier1Payload(flow) {
  // Translate the simulated site into the exact dataclasses consumed by the
  // real dependency-free edge/tier1_kbs.py evaluator.
  return {
    inverter: {
      ac_output_active_power_W: flow.loadW,
      heatsink_temp_C: flow.heatsinkC,
      battery_voltage_V: flow.vBat,
      battery_capacity_percent: flow.socFrac * 100,
      battery_charge_current_A: flow.chargeCurrentA,
      battery_discharge_current_A: flow.dischargeCurrentA,
      grid_voltage_V: flow.gridVoltageV,
      pv_charging_power_W: flow.pvUsableW,
    },
    breakers: state.breakers.map((b) => ({
      device_id: b.deviceId,
      priority_type: b.priorityType,
      priority_degree: b.priorityDegree,
      switch: b.switchOn,
      online: b.online,
      cur_power_W: breakerDrawW(b),
    })),
    config: {
      ...state.tier1.config,
      max_inverter_power_W: state.maxInverterW,
      battery_capacity_Wh: state.batteryCapacityWh,
    },
  };
}

async function tier1Loop(flow) {
  const t1 = state.tier1;
  if (!t1.connected || t1.busy) return;
  const realNow = performance.now();
  if (realNow - t1.lastEvalRealMs < t1.intervalMs) return;
  t1.lastEvalRealMs = realNow;
  t1.busy = true;
  try {
    const response = await fetch(`${t1.baseUrl}/evaluate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(tier1Payload(flow)),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail ?? `HTTP ${response.status}`);
    if (result.engine !== 'edge.tier1_kbs.evaluate' || !result.facts) {
      throw new Error('Tier-1 bridge did not return real-engine provenance/facts');
    }

    const previous = t1.situation;
    t1.situation = result.situation ?? '';
    t1.status = `evaluated @ ${new Date().toLocaleTimeString()}`;
    scenarioObserve('tier1_evaluation', { situation: t1.situation });
    const firstFact = state.evidence.lastTier1FactsRealMs === 0;
    if (firstFact || t1.situation !== previous ||
        realNow - state.evidence.lastTier1FactsRealMs >= 2000) {
      evidenceLog('T1', 'FACT', tier1FactsSummary(result.facts), {
        engine: result.engine,
        facts: result.facts,
      });
      state.evidence.lastTier1FactsRealMs = realNow;
    }
    if (firstFact || t1.situation !== previous) {
      evidenceLog(
        'T1', 'RULE',
        t1.situation
          ? `Actual Tier-1 rule fired: ${t1.situation}`
          : 'No Tier-1 safety rule fired for this fact snapshot.',
        { engine: result.engine, situation: t1.situation },
      );
    }
    if (t1.situation && t1.situation !== previous) {
      tier1Log(`situation → ${t1.situation}`, 'alert');
      scenarioLog(`T1 situation → ${t1.situation}`, 'event');
    } else if (!t1.situation && previous) {
      tier1Log(`danger cleared (${previous})`);
      scenarioLog(`T1 danger cleared (${previous})`, 'event');
    }
    applyTier1Commands(result.commands ?? []);
    if (result.notify && result.commands?.length) tier1Log(result.notify, 'alert');
  } catch (err) {
    t1.status = `error: ${err.message}`;
    scenarioObserve('tier1_error', { message: err.message });
    evidenceLog('T1', 'ERROR', `Tier-1 bridge error: ${err.message}`);
  } finally {
    t1.busy = false;
  }
}

function applyTier1Commands(commands) {
  let changed = false;
  for (const command of commands) {
    const b = state.breakers.find((item) => item.deviceId === command.device_id);
    if (!b) continue;
    scenarioObserve('tier1_command', { ...command });
    evidenceLog(
      'T1', 'COMMAND',
      `${command.device_id} → ${command.action.toUpperCase()}` +
        `${command.countdown_s > 0 ? ` in ${command.countdown_s}s` : ' immediately'} · ${command.reason}`,
      { engine: 'edge.tier1_kbs.evaluate', command },
    );
    if (command.action === 'off' && command.countdown_s > 0) {
      const armed = state.kbs.countdowns.some(
        (item) => item.source === 'T1' && item.deviceId === command.device_id
      );
      if (!armed) {
        state.kbs.countdowns.push({
          source: 'T1', deviceId: command.device_id,
          fireAtSimMs: state.simMs + command.countdown_s * 1000,
          reason: command.reason, actionId: null,
        });
        tier1Log(`OFF in ${Math.round(command.countdown_s / 60)} min — ${command.device_id} — ${command.reason}`, 'action');
        evidenceLog('EXECUTOR', 'SCHEDULED', `Tier-1 countdown armed for ${command.device_id}`, command);
      }
      continue;
    }
    if (command.action === 'off' && b.switchOn) {
      b.switchOn = false;
      tier1Log(`OFF ${command.device_id} — ${command.reason}`, 'action');
      evidenceLog('EXECUTOR', 'APPLIED', `Applied Tier-1 command: ${command.device_id} OFF`, command);
      changed = true;
    } else if (command.action === 'on' && !b.switchOn) {
      b.switchOn = true;
      b.onSinceMs = state.simMs;
      tier1Log(`ON ${command.device_id} — ${command.reason}`, 'action');
      evidenceLog('EXECUTOR', 'APPLIED', `Applied Tier-1 command: ${command.device_id} ON`, command);
      changed = true;
    }
  }
  if (changed) renderBreakerTable();
}

function tier1Log(text, cls = '') {
  const d = new Date(state.simMs);
  state.tier1.log.unshift({ t: d.toLocaleTimeString(), text, cls });
  state.tier1.log = state.tier1.log.slice(0, 20);
  $('tier1-log').innerHTML = state.tier1.log.map((entry) =>
    `<div class="entry ${entry.cls}"><span class="t">${entry.t}</span>${entry.text}</div>`
  ).join('');
}

// ============================= engine evidence ============================

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}

function tier1FactsSummary(facts) {
  const inverter = facts?.inverter ?? {};
  return `load ${Number(inverter.ac_output_active_power_W ?? 0).toFixed(0)} W · ` +
    `heatsink ${Number(inverter.heatsink_temp_C ?? 0).toFixed(1)} °C · ` +
    `battery ${Number(inverter.battery_voltage_V ?? 0).toFixed(2)} V · ` +
    `grid ${Number(inverter.grid_voltage_V ?? 0).toFixed(0)} V · ` +
    `PV ${Number(inverter.pv_charging_power_W ?? 0).toFixed(0)} W`;
}

function tier2FactsSummary(facts) {
  if (!facts) return 'No fact snapshot was returned (cycle skipped).';
  return `${facts.is_daytime ? 'day' : 'night'} · ` +
    `load ${Number(facts.load_power_W ?? 0).toFixed(0)} W · ` +
    `PV ${Number(facts.pv_power_W ?? 0).toFixed(0)} W · ` +
    `battery ${facts.battery_capacity_percent ?? 'unknown'}% / ${facts.battery_voltage_V ?? 'unknown'} V · ` +
    `heat_high=${Boolean(facts.heat_high)} · overload=${Boolean(facts.overload)} · ` +
    `grid_failed=${Boolean(facts.grid_failed)} · sudden_drop=${Boolean(facts.sudden_pv_drop)} · ` +
    `sudden_draw=${Boolean(facts.sudden_draw)}`;
}

function evidenceLog(source, kind, summary, raw = null) {
  state.evidence.sequence += 1;
  state.evidence.entries.unshift({
    id: state.evidence.sequence,
    simTime: new Date(state.simMs).toLocaleString(),
    source, kind, summary, raw,
  });
  state.evidence.entries = state.evidence.entries.slice(0, 250);
  renderEvidence();
}

function renderEvidence() {
  const entries = state.evidence.filter === 'all'
    ? state.evidence.entries
    : state.evidence.entries.filter((entry) => entry.source === state.evidence.filter);
  $('tbl-evidence').querySelector('tbody').innerHTML = entries.map((entry) => {
    const sourceClass = entry.source.toLowerCase();
    const kindClass = entry.kind.toLowerCase();
    const raw = entry.raw === null ? '' :
      `<details class="evidence-raw"><summary>Raw engine data</summary>` +
      `<pre>${escapeHtml(JSON.stringify(entry.raw, null, 2))}</pre></details>`;
    return `<tr>` +
      `<td>${escapeHtml(entry.simTime)}</td>` +
      `<td><span class="evidence-source ${sourceClass}">${escapeHtml(entry.source)}</span></td>` +
      `<td><span class="evidence-kind ${kindClass}">${escapeHtml(entry.kind)}</span></td>` +
      `<td><div class="evidence-summary">${escapeHtml(entry.summary)}</div>${raw}</td>` +
      `</tr>`;
  }).join('');
}

// ============================= KBS closed loop ============================

let kbsBusy = false; // guards against overlapping cycle requests (flag)

async function kbsLoop() {
  // Every K real seconds: trigger one KBS cycle on the server, fetch its
  // pending breaker actions, apply them to the simulated breakers, and ACK.
  const k = state.kbs;
  if (!k.connected || kbsBusy) return;
  const realNow = performance.now();                                  // real clock (ms)
  if (realNow - k.lastCycleRealMs < k.cycleS * 1000) return;
  k.lastCycleRealMs = realNow;
  kbsBusy = true;
  try {
    const base = state.push.baseUrl, org = state.push.orgId;
    const runResponse = await fetch(`${base}/api/kbs/sim/run-cycle/`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ organization: org }),
    });
    const run = await runResponse.json();
    if (!runResponse.ok) throw new Error(run.detail ?? `run-cycle HTTP ${runResponse.status}`);
    if (run.engine !== 'apps.kbs.engine.run_cycle') {
      throw new Error('Tier-2 endpoint did not return real-engine provenance');
    }
    evidenceLog('T2', 'FACT', tier2FactsSummary(run.facts), {
      engine: run.engine,
      facts: run.facts,
    });
    evidenceLog(
      'T2', 'RULE',
      run.branch
        ? `Actual Tier-2 branch fired: ${run.branch}`
        : `Tier-2 cycle skipped: ${run.detail ?? 'no decision'}`,
      { engine: run.engine, branch: run.branch },
    );
    for (const command of (run.actions ?? [])) {
      evidenceLog(
        'T2', 'COMMAND',
        `${command.device_id} → ${command.action.toUpperCase()}` +
          `${command.countdown_s > 0 ? ` in ${command.countdown_s}s` : ' immediately'} · ${command.reason}`,
        { engine: run.engine, command },
      );
    }
    const stateResponse = await fetch(`${base}/api/kbs/sim/state/?organization=${org}`);
    const st = await stateResponse.json();
    if (!stateResponse.ok) throw new Error(st.detail ?? `state HTTP ${stateResponse.status}`);
    const appliedIds = applyKbsActions(st.pending_actions ?? []);     // BreakerAction ids we executed
    if (appliedIds.length) {
      await fetch(`${base}/api/kbs/sim/ack/`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_ids: appliedIds }),
      });
    }
    for (const a of (st.recent_alerts ?? []).slice().reverse()) {     // oldest first so the log reads chronologically
      if (a.created_at > k.lastAlertTs) {
        k.lastAlertTs = a.created_at;
        kbsLog(`ALERT [${a.severity}] ${a.message}`, 'alert');
        scenarioObserve('tier2_alert', { ...a });
        evidenceLog('T2', 'ALERT', `[${a.severity}] ${a.kind} · ${a.message}`, a);
      }
    }
    k.branch = run.branch ?? '(skipped)';
    scenarioObserve('tier2_branch', { branch: k.branch });
    k.status = `cycle @ ${new Date().toLocaleTimeString()}`;
    kbsLog(`cycle → ${k.branch}`);
  } catch (err) {
    k.status = `error: ${err.message}`;
    scenarioObserve('backend_error', { message: err.message });
    scenarioLog(`Tier-2/backend error: ${err.message}`, 'fail');
    evidenceLog('T2', 'ERROR', `Tier-2/backend error: ${err.message}`);
  } finally {
    kbsBusy = false;
  }
}

function applyKbsActions(actions) {
  // Flip the simulated breakers to the states the KBS decided; returns the
  // applied BreakerAction ids so they can be ACKed.
  const applied = []; // ids executed this round
  for (const a of actions) {
    const b = state.breakers.find((x) => x.deviceId === a.device_id);
    if (!b) continue; // breaker only exists in the DB, nothing to flip here
    scenarioObserve('tier2_action_received', { ...a });
    // A local safety situation owns non-grid load state.  Keep a Tier-2 ON
    // pending (un-ACKed) until Tier-1 reports that the danger has cleared.
    if (a.action === 'on' && b.priorityType !== 'ac_grid' && state.tier1.situation) {
      kbsLog(`BLOCKED ON ${a.device_id} - Tier-1 ${state.tier1.situation} still active`, 'alert');
      scenarioObserve('tier2_action_blocked', { ...a });
      evidenceLog(
        'EXECUTOR', 'BLOCKED',
        `Did not apply Tier-2 ${a.device_id} ON because Tier-1 ${state.tier1.situation} is active.`,
        a,
      );
      continue;
    }
    if (a.action === 'on') {
      if (!b.switchOn) { b.switchOn = true; b.onSinceMs = state.simMs; } // peak phase restarts on every OFF->ON
      kbsLog(`ON  ${a.device_id} - ${a.reason}`, 'action');
      evidenceLog('EXECUTOR', 'APPLIED', `Applied Tier-2 command: ${a.device_id} ON`, a);
    } else if (a.countdown_s > 0) {
      // Delayed shutdown (battery protection): arm the device countdown in
      // simulated time — the breaker keeps running until it fires. The action
      // is ACKed only when the countdown fires, so the server's pending-action
      // dedupe prevents re-arming the same shutdown every cycle.
      if (state.kbs.countdowns.some((c) => c.source === 'T2' && c.deviceId === a.device_id)) continue; // already armed locally
      state.kbs.countdowns.push({
        source: 'T2',
        deviceId: a.device_id,
        fireAtSimMs: state.simMs + a.countdown_s * 1000,              // countdown counts simulated seconds (ms since epoch)
        reason: a.reason,
        actionId: a.id,                                               // BreakerAction pk, ACKed after firing (unitless)
      });
      kbsLog(`OFF in ${Math.round(a.countdown_s / 60)} min - ${a.device_id} - ${a.reason}`, 'action');
      evidenceLog('EXECUTOR', 'SCHEDULED', `Tier-2 countdown armed for ${a.device_id}`, a);
      continue; // not ACKed yet
    } else {
      b.switchOn = false;
      kbsLog(`OFF ${a.device_id} - ${a.reason}`, 'action');
      evidenceLog('EXECUTOR', 'APPLIED', `Applied Tier-2 command: ${a.device_id} OFF`, a);
    }
    applied.push(a.id);
    scenarioObserve('tier2_action_applied', { ...a });
  }
  if (applied.length) renderBreakerTable();
  return applied;
}

function kbsCountdownCheck() {
  // Fire armed countdowns whose simulated deadline passed, then ACK them.
  const due = state.kbs.countdowns.filter((c) => state.simMs >= c.fireAtSimMs);
  if (!due.length) return;
  state.kbs.countdowns = state.kbs.countdowns.filter((c) => state.simMs < c.fireAtSimMs);
  for (const c of due) {
    const b = state.breakers.find((x) => x.deviceId === c.deviceId);
    if (b && b.switchOn) {
      b.switchOn = false;
      const log = c.source === 'T1' ? tier1Log : kbsLog;
      log(`countdown fired: ${c.deviceId} OFF - ${c.reason}`, 'action');
      evidenceLog('EXECUTOR', 'APPLIED', `${c.source} countdown fired: ${c.deviceId} OFF`, c);
      if (c.source === 'T2') {
        scenarioObserve('tier2_action_applied', {
          id: c.actionId, device_id: c.deviceId, action: 'off',
          countdown_s: Math.round((c.fireAtSimMs - state.scenario.startedSimMs) / 1000),
          reason: c.reason,
        });
      }
    }
  }
  const tier2Ids = due.filter((c) => c.source === 'T2' && c.actionId).map((c) => c.actionId);
  if (tier2Ids.length) {
    fetch(`${state.push.baseUrl}/api/kbs/sim/ack/`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action_ids: tier2Ids }),
    }).catch(() => {});
  }
  renderBreakerTable();
}

function kbsLog(text, cls = '') {
  // Prepend one entry to the on-page KBS log (kept short).
  const d = new Date(state.simMs);
  state.kbs.log.unshift({ t: d.toLocaleTimeString(), text, cls });
  state.kbs.log = state.kbs.log.slice(0, 20);
  $('kbs-log').innerHTML = state.kbs.log.map((e) =>
    `<div class="entry ${e.cls}"><span class="t">${e.t}</span>${e.text}</div>`).join('');
}

async function patchKbsSettings(payload) {
  // Push a settings change (K, mode, power saving, data source) to the server.
  try {
    const response = await fetch(`${state.push.baseUrl}/api/kbs/settings/?organization=${state.push.orgId}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail ?? `settings HTTP ${response.status}`);
    }
  } catch (err) {
    state.kbs.status = `settings error: ${err.message}`;
    scenarioObserve('backend_error', { message: err.message });
  }
}

// ============================= backend push ===============================

async function maybePush(flow) {
  const p = state.push;
  if (!p.enabled) return;
  const realNow = performance.now();                                  // real clock (ms)
  if (realNow - p.lastRealMs < p.intervalS * 1000) return;
  p.lastRealMs = realNow;
  try {
    const r1 = await fetch(`${p.baseUrl}/api/telemetry/readings/`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildReading(flow)),
    });
    const r2 = await fetch(`${p.baseUrl}/api/breakers/status/`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildBreakerStatuses()),
    });
    p.status = `pushed @ ${new Date().toLocaleTimeString()} (telemetry ${r1.status}, breakers ${r2.status})`;
  } catch (err) {
    p.status = `error: ${err.message}`;
    scenarioObserve('backend_error', { message: err.message });
  }
}

// ============================= rendering ==================================

function render(flow, row, date) {
  if (!flow) return;
  $('out-simclock').textContent = `${date.toLocaleString()} .${String(date.getMilliseconds()).padStart(3, '0')}`;
  const solar = daySolar(date, row);
  const isDay = solar.clearSkyW > 0;                                  // sun above horizon (flag)
  $('out-daynight').textContent = isDay ? 'day' : 'night';
  $('out-sunwindow').textContent = solar.sunriseH !== null
    ? `${fmtH(solar.sunriseH)} – ${fmtH(solar.sunsetH)}` : '–';
  $('out-climate').textContent =
    `GHI ${row.ghi_kwh_m2_day} kWh/m²/day · clear-sky ${row.clearsky_ghi_kwh_m2_day} · ` +
    `cloud ${row.cloud_amount_percent}% · rain ${row.precip_mm_day} mm/day · ${row.temp_C}°C`;

  $('out-pv').textContent = Math.round(flow.pvW);
  $('out-load').textContent = Math.round(flow.loadW);
  const socEl = $('out-soc');
  socEl.textContent = `${Math.round(flow.socFrac * 100)}% · ${flow.vBat.toFixed(1)} V`;
  socEl.className = flow.socFrac < 0.25 ? 'crit' : flow.socFrac < 0.5 ? 'warn' : '';
  if (document.activeElement !== $('inp-batvoltage')) {
    $('inp-batvoltage').value = flow.vBat.toFixed(2);
  }
  if ($('inp-batsoc').disabled) $('inp-batsoc').value = (flow.socFrac * 100).toFixed(1);
  const sourceEl = $('out-source');
  sourceEl.textContent = flow.empty ? 'BLACKOUT (battery empty)'
    : flow.gridSupplying ? 'grid'
    : flow.gridOn ? (flow.dischargeW > 0 ? 'battery (grid is OUT!)' : 'solar (grid is OUT!)')
    : flow.dischargeW > 0 ? 'battery' : 'solar';
  sourceEl.className = flow.empty || flow.gridOn && !flow.gridSupplying ? 'crit' : '';

  $('out-reading').textContent = JSON.stringify(buildReading(flow), null, 1);
  $('out-push').textContent = state.push.enabled ? state.push.status : 'off';
  $('out-kbs-status').textContent = state.kbs.connected ? state.kbs.status : 'disconnected';
  $('out-kbs-branch').textContent = state.kbs.branch;
  $('out-tier1-status').textContent = state.tier1.connected ? state.tier1.status : 'disconnected';
  $('out-tier1-situation').textContent = state.tier1.situation || 'none';
  $('out-tier1-situation').className = state.tier1.situation ? 'crit' : '';

  drawChart(row, date, flow);
  updateBreakerDraws();
}

function fmtH(h) {
  // 6.25 (hours) -> "06:15" (clock text)
  const hh = Math.floor(h), mm = Math.round((h - hh) * 60);
  return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`;
}

function drawChart(row, date, flow) {
  // Day chart: clear-sky PV curve, weather-scaled curve, and the "now" marker.
  const cv = $('pv-chart'), ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;                                  // canvas size (px)
  ctx.clearRect(0, 0, W, H);
  const maxW = Math.max(state.maxPvW, 100);                           // vertical scale ceiling (W)
  const wf = WEATHER_PV_FACTOR[state.weather] ?? 1;                   // active weather factor (fraction)

  ctx.strokeStyle = '#26313c'; ctx.lineWidth = 1;                     // hour grid
  for (let h = 0; h <= 24; h += 3) {
    const x = (h / 24) * W;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillStyle = '#5a6672'; ctx.font = '10px sans-serif';
    ctx.fillText(`${h}h`, x + 3, H - 5);
  }

  const curve = (factor, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.beginPath();
    for (let m = 0; m <= 24 * 60; m += 10) {
      const t = new Date(date.getFullYear(), date.getMonth(), date.getDate(), 0, m);
      const { clearSkyW } = daySolar(t, row);
      const x = (m / (24 * 60)) * W;
      const y = H - (clearSkyW * factor / maxW) * (H - 18) - 14;
      m === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
  };
  curve(1, '#3f6f89');   // clear-sky ceiling
  curve(wf, '#7fd1a8');  // with current weather

  const nowH = date.getHours() + date.getMinutes() / 60;              // now on the x axis (h)
  const x = (nowH / 24) * W;
  const y = H - (flow.pvW / maxW) * (H - 18) - 14;
  ctx.strokeStyle = '#f0b25e'; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  ctx.fillStyle = '#f0b25e'; ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
}

// -------- breaker table --------

function renderBreakerTable() {
  const tbody = $('tbl-breakers').querySelector('tbody');
  tbody.innerHTML = '';
  state.breakers.forEach((b, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input type="text" value="${b.deviceId}" data-f="deviceId"></td>
      <td><select data-f="priorityType">
        ${['mandatory', 'normal', 'comfort', 'ac_grid'].map((v) =>
          `<option ${v === b.priorityType ? 'selected' : ''}>${v}</option>`).join('')}
      </select></td>
      <td><input type="number" min="1" value="${b.priorityDegree}" data-f="priorityDegree"></td>
      <td><select data-f="loadType">
        ${['normal', 'motor'].map((v) =>
          `<option ${v === b.loadType ? 'selected' : ''}>${v}</option>`).join('')}
      </select></td>
      <td><input type="number" min="0" value="${b.peakW}" data-f="peakW"></td>
      <td><input type="number" min="0" value="${b.normalW}" data-f="normalW"></td>
      <td><input type="number" min="0" value="${b.peakMinutes}" data-f="peakMinutes"></td>
      <td><input type="checkbox" ${b.switchOn ? 'checked' : ''} data-f="switchOn"></td>
      <td><input type="checkbox" ${b.online ? 'checked' : ''} data-f="online"></td>
      <td><input type="text" value="${b.fault}" placeholder="ok" size="8" data-f="fault"></td>
      <td class="draw-cell"><span class="draw">0 W</span></td>
      <td><button class="danger" data-remove>x</button></td>`;
    tr.querySelectorAll('[data-f]').forEach((el) => {
      el.addEventListener('change', () => {
        const f = el.dataset.f;
        if (el.type === 'checkbox') {
          if (f === 'switchOn' && el.checked && !b.switchOn) b.onSinceMs = state.simMs; // OFF->ON starts the peak phase
          b[f] = el.checked;
        } else if (el.type === 'number') b[f] = parseFloat(el.value) || 0;
        else b[f] = el.value;
      });
    });
    tr.querySelector('[data-remove]').addEventListener('click', () => {
      state.breakers.splice(i, 1);
      renderBreakerTable();
    });
    tbody.appendChild(tr);
  });
}

function updateBreakerDraws() {
  const rows = $('tbl-breakers').querySelectorAll('tbody tr');
  state.breakers.forEach((b, i) => {
    const cell = rows[i]?.querySelector('.draw');
    if (!cell) return;
    const drawW = breakerDrawW(b);                                    // current draw (W)
    const inPeak = drawW > 0 && drawW === b.peakW && b.peakW !== b.normalW; // showing peak-phase draw (flag)
    cell.textContent = b.priorityType === 'ac_grid'
      ? (b.switchOn ? (state.gridAvailable ? 'grid ON' : 'grid ON (no input!)') : 'grid off')
      : `${drawW} W${inPeak ? ' (peak)' : ''}`;
    cell.className = 'draw' + (inPeak ? ' peak' : '');
  });
}

// ============================= wiring =====================================

function initControls() {
  initDashboardNavigation();
  initSimulationInputSheet();

  // city list
  const cities = [...new Set(SOLAR_DATA.map((r) => r.city))];
  $('inp-city').innerHTML = cities.map((c) => `<option>${c}</option>`).join('');
  $('inp-city').value = state.city;
  $('inp-city').addEventListener('change', (e) => { state.city = e.target.value; refreshWeatherOptions(); });

  // datetime
  const setDatetimeInput = () => {
    const d = new Date(state.simMs);
    const pad = (n, l = 2) => String(n).padStart(l, '0');
    $('inp-datetime').value =
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  setDatetimeInput();
  $('inp-datetime').addEventListener('change', (e) => {
    const d = new Date(e.target.value);
    if (!isNaN(d)) { d.setMilliseconds(parseInt($('inp-ms').value) || 0); state.simMs = d.getTime(); }
  });
  $('inp-ms').addEventListener('change', () => {
    const d = new Date(state.simMs);
    d.setMilliseconds(parseInt($('inp-ms').value) || 0);
    state.simMs = d.getTime();
  });
  $('inp-scale').addEventListener('change', (e) => { state.scale = parseFloat(e.target.value) || 1; });
  $('btn-playpause').addEventListener('click', () => {
    state.running = !state.running;
    $('btn-playpause').textContent = state.running ? 'Pause' : 'Play';
    if (!state.running) setDatetimeInput(); // expose the frozen time for hand editing
  });

  // season: user picks, but the datetime immediately corrects it (see tick)
  $('inp-season').addEventListener('change', () => {
    $('inp-season').value = seasonForMonth(new Date(state.simMs).getMonth() + 1);
  });

  // weather
  refreshWeatherOptions();
  $('inp-weather').addEventListener('change', (e) => { state.weather = e.target.value; });
  $('inp-weather-auto').addEventListener('change', (e) => {
    state.weatherAuto = e.target.checked;
    state.nextWeatherPickMs = 0; // re-roll immediately when re-enabled
  });

  // power
  $('inp-maxpv').addEventListener('change', (e) => { state.maxPvW = parseFloat(e.target.value) || 0; });
  $('inp-pvthreshold').addEventListener('change', (e) => { state.pvThresholdW = parseFloat(e.target.value) || 0; });
  $('inp-maxinv').addEventListener('change', (e) => { state.maxInverterW = parseFloat(e.target.value) || 1; });
  $('inp-grid-available').addEventListener('change', (e) => { state.gridAvailable = e.target.checked; });
  $('inp-batcap').addEventListener('change', (e) => {
    state.batteryCapacityWh = parseFloat(e.target.value) || 1;
    state.batterySocWh = Math.min(state.batterySocWh, state.batteryCapacityWh);
  });
  $('inp-batsoc').addEventListener('change', (e) => {
    state.batterySocWh = state.batteryCapacityWh * (parseFloat(e.target.value) || 0) / 100;
  });
  $('inp-batnom').addEventListener('change', (e) => {
    const previousNominalV = state.batteryNominalV;
    state.batteryNominalV = parseInt(e.target.value);
    if (previousNominalV > 0 && state.batteryNominalV !== previousNominalV) {
      const scaledFloorV = state.tier1.config.battery_low_voltage_V *
        state.batteryNominalV / previousNominalV;
      state.tier1.config.battery_low_voltage_V = scaledFloorV;
      $('inp-kbs-batfloor').value = scaledFloorV.toFixed(1);
      patchKbsSettings({ battery_low_voltage_V: scaledFloorV });
    }
    const forcedVoltage = state.scenario.definition
      ? state.scenario.overrides.batteryVoltageV : state.batteryVoltageOverrideV;
    if (Number.isFinite(forcedVoltage) &&
        (!state.scenario.definition || state.scenario.batteryVoltageDeterminesSoc)) {
      synchronizeBatteryChargeToVoltage(forcedVoltage);
    }
    renderScenarioBatteryControl();
  });
  $('inp-batvoltage-auto').addEventListener('change', (e) => {
    if (state.scenario.definition) {
      e.target.checked = false;
      return;
    }
    $('inp-batvoltage').disabled = e.target.checked;
    $('inp-batsoc').disabled = !e.target.checked;
    if (e.target.checked) {
      state.batteryVoltageOverrideV = null;
    } else {
      const voltageV = parseFloat($('inp-batvoltage').value);
      state.batteryVoltageOverrideV = Number.isFinite(voltageV) ? voltageV : 0;
      synchronizeBatteryChargeToVoltage(state.batteryVoltageOverrideV);
    }
  });
  $('inp-batvoltage').addEventListener('input', (e) => {
    const voltageV = parseFloat(e.target.value);
    if (!Number.isFinite(voltageV)) return;
    if (state.scenario.definition) state.scenario.overrides.batteryVoltageV = voltageV;
    else state.batteryVoltageOverrideV = voltageV;
    if (state.scenario.definition) state.scenario.batteryVoltageDeterminesSoc = true;
    synchronizeBatteryChargeToVoltage(voltageV);
  });

  // push
  $('inp-baseurl').addEventListener('change', (e) => { state.push.baseUrl = e.target.value.replace(/\/$/, ''); });
  $('inp-orgid').addEventListener('change', (e) => { state.push.orgId = parseInt(e.target.value) || 1; });
  $('inp-pushint').addEventListener('change', (e) => { state.push.intervalS = parseFloat(e.target.value) || 5; });
  $('inp-push-enabled').addEventListener('change', (e) => {
    state.push.enabled = e.target.checked;
    state.push.status = e.target.checked ? 'waiting for first push…' : 'off';
  });

  $('btn-add-breaker').addEventListener('click', () => addBreaker());

  // KBS closed loop
  $('inp-kbs-connected').addEventListener('change', async (e) => {
    state.kbs.connected = e.target.checked;
    if (state.kbs.connected) {
      // Only alerts raised from now on are interesting; skip the backlog so a
      // page reload does not replay alerts from earlier runs.
      try {
        const st = await (await fetch(
          `${state.push.baseUrl}/api/kbs/sim/state/?organization=${state.push.orgId}`)).json();
        state.kbs.lastAlertTs = st.recent_alerts?.[0]?.created_at ?? '';
      } catch { /* server unreachable: the first cycle will report it */ }
      if (!state.push.enabled) {                       // the KBS needs data flowing
        $('inp-push-enabled').checked = true;
        state.push.enabled = true;
        state.push.status = 'waiting for first push…';
      }
      patchKbsSettings({
        data_source: 'simulator',
        cycle_seconds: state.kbs.cycleS,
        mode: $('inp-kbs-mode').value,
        power_saving: $('inp-kbs-powersaving').checked,
      });
      // first cycle ~6 s after connect, so a couple of pushes land first
      state.kbs.lastCycleRealMs = performance.now() - state.kbs.cycleS * 1000 + 6000;
      state.kbs.status = 'connected, first cycle soon…';
      kbsLog('KBS connected (data source: simulator)');
    } else {
      state.kbs.status = 'disconnected';
      kbsLog('KBS disconnected');
    }
  });
  $('inp-kbs-k').addEventListener('change', (e) => {
    state.kbs.cycleS = Math.max(parseFloat(e.target.value) || 60, 5);
    patchKbsSettings({ cycle_seconds: Math.round(state.kbs.cycleS) });
  });
  $('inp-kbs-powersaving').addEventListener('change', (e) => {
    patchKbsSettings({ power_saving: e.target.checked });
  });
  $('inp-kbs-batfloor').addEventListener('change', (e) => {
    const floorV = parseFloat(e.target.value) || 0;
    state.tier1.config.battery_low_voltage_V = floorV;
    patchKbsSettings({ battery_low_voltage_V: floorV });
    const forcedVoltage = state.scenario.definition
      ? state.scenario.overrides.batteryVoltageV : state.batteryVoltageOverrideV;
    if (Number.isFinite(forcedVoltage) &&
        (!state.scenario.definition || state.scenario.batteryVoltageDeterminesSoc)) {
      synchronizeBatteryChargeToVoltage(forcedVoltage);
    }
    renderScenarioBatteryControl();
  });
  $('inp-kbs-mode').addEventListener('change', (e) => {
    patchKbsSettings({ mode: e.target.value });
  });

  // Tier-1 local bridge
  $('inp-tier1-url').addEventListener('change', (e) => {
    state.tier1.baseUrl = e.target.value.replace(/\/$/, '');
  });
  $('inp-tier1-connected').addEventListener('change', (e) => {
    state.tier1.connected = e.target.checked;
    state.tier1.status = e.target.checked ? 'connected, waiting for evaluation…' : 'disconnected';
    state.tier1.situation = '';
    state.tier1.lastEvalRealMs = 0;
    tier1Log(e.target.checked ? 'Tier-1 bridge connected' : 'Tier-1 bridge disconnected');
  });

  $('inp-evidence-source').addEventListener('change', (e) => {
    state.evidence.filter = e.target.value;
    renderEvidence();
  });
  $('btn-evidence-clear').addEventListener('click', () => {
    state.evidence.entries = [];
    state.evidence.lastTier1FactsRealMs = 0;
    renderEvidence();
  });

  initScenarioControls();
}

// ============================= scenario runner ============================

function freshScenarioObservations() {
  return {
    tier1Evaluations: 0,
    tier1Situations: [],
    tier1Commands: [],
    tier1Errors: [],
    tier2Branches: [],
    tier2ActionsReceived: [],
    tier2ActionsApplied: [],
    tier2ActionsBlocked: [],
    tier2Alerts: [],
    backendErrors: [],
  };
}

function cloneScenarioEvents(events = []) {
  return events.map((event) => ({
    ...event,
    changes: {
      ...(event.changes ?? {}),
      overrides: { ...(event.changes?.overrides ?? {}) },
      state: { ...(event.changes?.state ?? {}) },
      breakers: Object.fromEntries(Object.entries(event.changes?.breakers ?? {}).map(
        ([deviceId, patch]) => [deviceId, { ...patch }]
      )),
    },
  }));
}

function expectedBatteryVoltageState(voltageV) {
  const cfg = state.tier1.config;
  if (voltageV <= cfg.battery_low_voltage_V + cfg.battery_critical_margin_V) {
    return { label: 'CRITICAL · immediate Tier-1 shedding', className: 'crit' };
  }
  if (voltageV <= cfg.battery_low_voltage_V + cfg.battery_low_margin_V) {
    return { label: 'LOW · protection countdown', className: 'warn' };
  }
  return { label: 'ABOVE LOW-BATTERY TRIGGER', className: '' };
}

function scenarioBatteryControlValue(scenario, control) {
  if (control.source === 'event') {
    const events = state.scenario.definition === scenario ? state.scenario.events : scenario.events;
    return events?.[control.eventIndex]?.changes?.overrides?.batteryVoltageV;
  }
  return state.scenario.definition === scenario
    ? state.scenario.overrides.batteryVoltageV : scenario.setup.overrides?.batteryVoltageV;
}

function scenarioEventDisplayLabel(event, eventIndex) {
  const control = state.scenario.definition?.batteryControl;
  if (control?.source === 'event' && control.eventIndex === eventIndex) {
    const voltageV = event.changes?.overrides?.batteryVoltageV;
    if (Number.isFinite(voltageV)) return `${control.eventLabel} ${voltageV} V`;
  }
  return event.label;
}

function renderScenarioBatteryControl() {
  const panel = $('panel-scenario-inputs');
  if (!panel) return;
  const scenario = state.scenario.definition;
  const control = scenario?.batteryControl;
  panel.hidden = !control;
  if (!control) return;

  const voltageV = Number(scenarioBatteryControlValue(scenario, control));
  const input = $('inp-scenario-battery-voltage');
  $('lbl-scenario-battery-voltage').textContent = control.label;
  input.min = String(control.min ?? 0);
  input.max = String(control.max ?? 100);
  input.step = String(control.step ?? 0.01);
  input.value = Number.isFinite(voltageV) ? String(voltageV) : '';
  input.disabled = Boolean(
    state.scenario.active && control.source === 'event' &&
    state.scenario.nextEventIndex > control.eventIndex
  );

  const socFrac = Number.isFinite(voltageV) ? batterySocFromVoltage(voltageV) : 0;
  const usableWh = socFrac * state.batteryCapacityWh;
  $('out-scenario-battery-capacity').textContent = Number.isFinite(voltageV)
    ? `${(socFrac * 100).toFixed(1)}% · ${Math.round(usableWh)} Wh` : '—';
  const voltageState = expectedBatteryVoltageState(voltageV);
  const stateOutput = $('out-scenario-battery-state');
  stateOutput.textContent = voltageState.label;
  stateOutput.className = voltageState.className;
  const { floorV, fullV } = batteryVoltageRange();
  $('out-scenario-battery-note').textContent =
    `${control.note} Usable charge is derived between the ${floorV.toFixed(2)} V protection floor ` +
    `and ${fullV.toFixed(2)} V full-charge point, then sent with the selected voltage.`;
}

function updateScenarioBatteryControl(value) {
  const scenario = state.scenario.definition;
  const control = scenario?.batteryControl;
  const voltageV = Number(value);
  if (!control || !Number.isFinite(voltageV)) return;
  if (control.source === 'event') {
    const event = state.scenario.events[control.eventIndex];
    if (!event) return;
    event.changes.overrides.batteryVoltageV = voltageV;
    if (state.scenario.nextEventIndex <= control.eventIndex) {
      $('out-scenario-next').textContent =
        `${scenarioEventDisplayLabel(event, control.eventIndex)} at +${event.atSimS}s simulated`;
    }
  } else {
    state.scenario.overrides.batteryVoltageV = voltageV;
    synchronizeBatteryChargeToVoltage(voltageV);
    $('inp-batvoltage').value = voltageV.toFixed(2);
  }
  renderScenarioBatteryControl();
}

function initScenarioControls() {
  $('inp-scenario').innerHTML = SMARTBREAKER_SCENARIOS.map((scenario) =>
    `<option value="${scenario.id}">[${scenario.tier}] ${scenario.name.replace(/^.*·\s*/, '')}</option>`
  ).join('');
  $('btn-scenario-load').addEventListener('click', () => loadScenario($('inp-scenario').value));
  $('btn-scenario-run').addEventListener('click', () => startScenario());
  $('btn-scenario-stop').addEventListener('click', () => finishScenario(false));
  $('inp-scenario').addEventListener('change', () => showScenarioPreview($('inp-scenario').value, true));
  $('inp-scenario-battery-voltage').addEventListener('change', (event) => {
    updateScenarioBatteryControl(event.target.value);
  });
  showScenarioPreview($('inp-scenario').value);
}

function scenarioDefaultDateTime(scenario) {
  const [date, timeValue = '00:00:00'] = scenario.setup.localDateTime.split('T');
  return { date, time: timeValue.slice(0, 8) };
}

function showScenarioPreview(id, resetDate = true) {
  const scenario = SMARTBREAKER_SCENARIOS.find((item) => item.id === id);
  if (!scenario || state.scenario.active) return;
  $('out-scenario-description').textContent = scenario.description;
  if (resetDate || !$('inp-scenario-date').value) {
    const defaults = scenarioDefaultDateTime(scenario);
    $('inp-scenario-date').value = defaults.date;
    $('inp-scenario-time').value = defaults.time;
  }
  $('inp-scenario-date').disabled = Boolean(scenario.dateLocked);
  $('inp-scenario-time').disabled = Boolean(scenario.timeLocked);
  const lockReason = scenario.dateLocked
    ? 'This scenario date is fixed because its ScheduledEvent is stored in Django.'
    : 'Choose any start date; the Python facts engine derives season and day/night from it.';
  $('inp-scenario-date').title = lockReason;
  $('inp-scenario-time').title = lockReason;
  if (state.scenario.definition?.id !== id) $('panel-scenario-inputs').hidden = true;
}

function restoreScenarioBackend() {
  if (!state.scenario.originalBaseUrl) return;
  state.push.baseUrl = state.scenario.originalBaseUrl;
  $('inp-baseurl').value = state.push.baseUrl;
  state.scenario.originalBaseUrl = '';
}

function loadScenario(id) {
  if (state.scenario.active) finishScenario(false);
  restoreScenarioBackend();
  const scenario = SMARTBREAKER_SCENARIOS.find((item) => item.id === id);
  if (!scenario) return;
  const setup = scenario.setup;
  const defaults = scenarioDefaultDateTime(scenario);
  const selectedDate = scenario.dateLocked
    ? defaults.date : ($('inp-scenario-date').value || defaults.date);
  const selectedTime = scenario.timeLocked
    ? defaults.time : ($('inp-scenario-time').value || defaults.time);

  state.scenario.definition = scenario;
  state.scenario.active = false;
  state.scenario.hasRun = false;
  state.scenario.startedRealMs = 0;
  state.scenario.startedSimMs = 0;
  state.scenario.nextEventIndex = 0;
  state.scenario.overrides = { ...(setup.overrides ?? {}) };
  state.scenario.events = cloneScenarioEvents(scenario.events);
  state.scenario.batteryVoltageDeterminesSoc = Boolean(scenario.batteryControl);
  state.scenario.observations = freshScenarioObservations();
  state.scenario.log = [];
  state.scenario.originalBaseUrl = state.push.baseUrl;

  state.running = false;
  state.simMs = new Date(`${selectedDate}T${selectedTime}`).getTime();
  state.scale = setup.scale;
  state.weather = setup.weather;
  state.weatherAuto = setup.weatherAuto;
  state.nextWeatherPickMs = Number.POSITIVE_INFINITY;
  state.maxPvW = setup.maxPvW;
  state.pvThresholdW = setup.pvThresholdW;
  state.maxInverterW = setup.maxInverterW;
  state.gridAvailable = setup.gridAvailable;
  state.batteryCapacityWh = setup.batteryCapacityWh;
  state.batterySocWh = setup.batteryCapacityWh * setup.batterySocPercent / 100;
  state.batteryNominalV = setup.batteryNominalV;
  state.batteryVoltageOverrideV = null;
  state.heatsinkC = setup.heatsinkC;
  state.breakers = (setup.breakers ?? []).map((breaker) => ({
    ...breaker,
    peakMinutes: breaker.peakMinutes ?? 15,
    fault: breaker.fault ?? '',
    onSinceMs: breaker.switchOn ? state.simMs - 60 * 60000 : null,
  }));
  state.breakerSeq = state.breakers.length;

  state.push.intervalS = setup.pushIntervalS;
  state.push.enabled = false;
  state.push.lastRealMs = 0;
  state.push.status = 'off';
  if (setup.backendOffline) state.push.baseUrl = 'http://127.0.0.1:8999';

  state.kbs.connected = false;
  state.kbs.cycleS = setup.tier2CycleS;
  state.kbs.lastCycleRealMs = 0;
  state.kbs.branch = '–';
  state.kbs.status = 'disconnected';
  state.kbs.log = [];
  state.kbs.countdowns = [];
  state.kbs.lastAlertTs = new Date().toISOString();
  $('kbs-log').innerHTML = '';

  state.tier1.connected = false;
  state.tier1.situation = '';
  state.tier1.status = 'disconnected';
  state.tier1.lastEvalRealMs = 0;
  state.tier1.log = [];
  state.tier1.config.battery_low_voltage_V = setup.batteryFloorV;
  if (state.scenario.batteryVoltageDeterminesSoc &&
      Number.isFinite(state.scenario.overrides.batteryVoltageV)) {
    synchronizeBatteryChargeToVoltage(state.scenario.overrides.batteryVoltageV);
  }
  $('tier1-log').innerHTML = '';

  state.evidence.entries = [];
  state.evidence.lastTier1FactsRealMs = 0;
  renderEvidence();

  shapeCache.key = '';
  syncScenarioControls(setup);
  renderScenarioBatteryControl();
  renderBreakerTable();
  $('inp-scenario').value = id;
  $('out-scenario-description').textContent = scenario.description;
  $('out-scenario-status').textContent = `Loaded · ${scenario.durationRealS}s real time`;
  $('out-scenario-phase').textContent = state.scenario.events.length
    ? 'BEFORE DISTURBANCE' : 'MONITORING';
  $('out-scenario-next').textContent = state.scenario.events.length
    ? `${scenarioEventDisplayLabel(state.scenario.events[0], 0)} at +${state.scenario.events[0].atSimS}s simulated`
    : 'No injected disturbance';
  scenarioLog(`Loaded ${scenario.name}`, 'event');
  renderScenarioExpectations(false);
}

function syncScenarioControls(setup) {
  const date = new Date(state.simMs);
  const pad = (value, width = 2) => String(value).padStart(width, '0');
  $('inp-datetime').value =
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  $('inp-ms').value = '0';
  $('inp-scale').value = state.scale;
  $('btn-playpause').textContent = 'Play';
  $('inp-weather-auto').checked = state.weatherAuto;
  refreshWeatherOptions();
  $('inp-weather').value = state.weather;
  $('inp-season').value = seasonForMonth(date.getMonth() + 1);
  $('inp-maxpv').value = state.maxPvW;
  $('inp-pvthreshold').value = state.pvThresholdW;
  $('inp-maxinv').value = state.maxInverterW;
  $('inp-grid-available').checked = state.gridAvailable;
  $('inp-batcap').value = state.batteryCapacityWh;
  const forcedBatteryVoltage = state.scenario.overrides.batteryVoltageV;
  const testerControlsVoltage = Number.isFinite(forcedBatteryVoltage) &&
    state.scenario.batteryVoltageDeterminesSoc;
  $('inp-batsoc').value = (state.batterySocWh / state.batteryCapacityWh * 100).toFixed(1);
  $('inp-batsoc').disabled = testerControlsVoltage;
  $('inp-batvoltage').value = testerControlsVoltage
    ? forcedBatteryVoltage.toFixed(2)
    : batteryVoltageV(state.batterySocWh / state.batteryCapacityWh, 0).toFixed(2);
  $('inp-batvoltage').disabled = !testerControlsVoltage;
  $('inp-batvoltage-auto').checked = !testerControlsVoltage;
  $('inp-batvoltage-auto').disabled = true;
  $('inp-batnom').value = state.batteryNominalV;
  $('inp-baseurl').value = state.push.baseUrl;
  $('inp-pushint').value = state.push.intervalS;
  $('inp-push-enabled').checked = false;
  $('inp-kbs-connected').checked = false;
  $('inp-kbs-k').value = state.kbs.cycleS;
  $('inp-kbs-powersaving').checked = setup.powerSaving;
  $('inp-kbs-batfloor').value = setup.batteryFloorV;
  $('inp-kbs-mode').value = 'active';
  $('inp-tier1-connected').checked = false;
}

function startScenario() {
  if (!state.scenario.definition) {
    loadScenario($('inp-scenario').value);
  } else if (state.scenario.hasRun) {
    loadScenario(state.scenario.definition.id);
  }
  const scenario = state.scenario.definition;
  if (!scenario) return;
  if (state.scenario.active) return;

  state.scenario.active = true;
  state.scenario.hasRun = true;
  state.scenario.startedRealMs = performance.now();
  state.scenario.startedSimMs = state.simMs;
  state.scenario.nextEventIndex = 0;
  state.scenario.observations = freshScenarioObservations();
  state.running = true;
  state.lastRealMs = performance.now();
  $('btn-playpause').textContent = 'Pause';

  state.tier1.connected = Boolean(scenario.setup.tier1);
  state.tier1.status = state.tier1.connected ? 'connected, waiting for evaluation…' : 'disconnected';
  state.tier1.lastEvalRealMs = 0;
  $('inp-tier1-connected').checked = state.tier1.connected;

  state.kbs.connected = Boolean(scenario.setup.tier2);
  state.kbs.status = state.kbs.connected ? 'connected, collecting baseline…' : 'disconnected';
  state.kbs.lastCycleRealMs = performance.now() - state.kbs.cycleS * 1000 + 6000;
  state.push.enabled = state.kbs.connected;
  state.push.lastRealMs = 0;
  state.push.status = state.push.enabled ? 'waiting for first push…' : 'off';
  $('inp-kbs-connected').checked = state.kbs.connected;
  $('inp-push-enabled').checked = state.push.enabled;

  if (state.kbs.connected) {
    patchKbsSettings({
      data_source: 'simulator', cycle_seconds: Math.round(state.kbs.cycleS),
      mode: 'active', power_saving: scenario.setup.powerSaving,
      battery_low_voltage_V: scenario.setup.batteryFloorV,
    });
  }
  $('out-scenario-status').textContent = `Running · 0/${scenario.durationRealS}s`;
  $('out-scenario-phase').textContent = state.scenario.events.length
    ? 'BEFORE DISTURBANCE' : 'MONITORING';
  scenarioLog('Scenario started', 'event');
  renderScenarioExpectations(false);
}

function finishScenario(completed) {
  if (!state.scenario.definition) return;
  state.scenario.active = false;
  state.running = false;
  state.push.enabled = false;
  state.kbs.connected = false;
  state.tier1.connected = false;
  $('btn-playpause').textContent = 'Play';
  $('inp-push-enabled').checked = false;
  $('inp-kbs-connected').checked = false;
  $('inp-tier1-connected').checked = false;
  const results = renderScenarioExpectations(true);
  const passed = results.every((result) => result === 'pass');
  const label = completed ? (passed ? 'COMPLETE' : 'INCOMPLETE') : 'STOPPED';
  $('out-scenario-status').textContent = `${label} · ${state.scenario.definition.name}`;
  $('out-scenario-phase').textContent = completed ? 'FINISHED' : 'STOPPED';
  $('out-scenario-next').textContent = '-';
  scenarioLog(
    completed
      ? (passed
          ? 'All expected outputs were observed from the real engines.'
          : 'One or more expected real-engine outputs were not observed.')
      : 'Scenario stopped by user',
    completed && passed ? 'pass' : 'fail',
  );
  renderScenarioBatteryControl();
  restoreScenarioBackend();
}

function advanceScenarioTimeline() {
  const runtime = state.scenario;
  if (!runtime.active || !runtime.definition) return;
  const elapsedSimS = (state.simMs - runtime.startedSimMs) / 1000;
  const events = runtime.events;
  while (runtime.nextEventIndex < events.length &&
         elapsedSimS >= events[runtime.nextEventIndex].atSimS) {
    const event = events[runtime.nextEventIndex];
    applyScenarioChanges(event.changes ?? {});
    scenarioLog(`+${event.atSimS}s · ${scenarioEventDisplayLabel(event, runtime.nextEventIndex)}`, 'event');
    runtime.nextEventIndex += 1;
    $('out-scenario-phase').textContent = event.phase ?? 'DURING DISTURBANCE';
    const next = events[runtime.nextEventIndex];
    $('out-scenario-next').textContent = next
      ? `${scenarioEventDisplayLabel(next, runtime.nextEventIndex)} at +${next.atSimS}s simulated`
      : 'No more injected events';
  }
}

function applyScenarioChanges(changes) {
  if (changes.overrides) {
    Object.assign(state.scenario.overrides, changes.overrides);
    if (state.scenario.batteryVoltageDeterminesSoc &&
        Number.isFinite(changes.overrides.batteryVoltageV)) {
      synchronizeBatteryChargeToVoltage(changes.overrides.batteryVoltageV);
      $('inp-batvoltage').value = changes.overrides.batteryVoltageV.toFixed(2);
    }
  }
  if (changes.state) {
    Object.assign(state, changes.state);
  }
  if (changes.breakers) {
    for (const [deviceId, patch] of Object.entries(changes.breakers)) {
      const breaker = state.breakers.find((item) => item.deviceId === deviceId);
      if (!breaker) continue;
      if (patch.switchOn && !breaker.switchOn) breaker.onSinceMs = state.simMs;
      Object.assign(breaker, patch);
    }
    renderBreakerTable();
  }
  if (changes.backend === 'offline') {
    state.push.baseUrl = 'http://127.0.0.1:8999';
  } else if (changes.backend === 'online' && state.scenario.originalBaseUrl) {
    state.push.baseUrl = state.scenario.originalBaseUrl;
  }
  $('inp-baseurl').value = state.push.baseUrl;
  renderScenarioBatteryControl();
}

function updateScenarioRun() {
  const runtime = state.scenario;
  if (!runtime.active || !runtime.definition) return;
  const elapsedRealS = (performance.now() - runtime.startedRealMs) / 1000;
  $('out-scenario-status').textContent =
    `Running · ${Math.min(Math.floor(elapsedRealS), runtime.definition.durationRealS)}` +
    `/${runtime.definition.durationRealS}s`;
  if (elapsedRealS >= runtime.definition.durationRealS) finishScenario(true);
}

function scenarioObserve(type, value) {
  const runtime = state.scenario;
  if (!runtime.active || !runtime.observations) return;
  const observations = runtime.observations;
  if (type === 'tier1_evaluation') {
    observations.tier1Evaluations += 1;
    if (value.situation && !observations.tier1Situations.includes(value.situation)) {
      observations.tier1Situations.push(value.situation);
    }
  } else if (type === 'tier1_command') observations.tier1Commands.push(value);
  else if (type === 'tier1_error') observations.tier1Errors.push(value);
  else if (type === 'tier2_branch') observations.tier2Branches.push(value.branch);
  else if (type === 'tier2_action_received') observations.tier2ActionsReceived.push(value);
  else if (type === 'tier2_action_applied') observations.tier2ActionsApplied.push(value);
  else if (type === 'tier2_action_blocked') observations.tier2ActionsBlocked.push(value);
  else if (type === 'tier2_alert') observations.tier2Alerts.push(value);
  else if (type === 'backend_error') observations.backendErrors.push(value);
  renderScenarioExpectations(false);
}

function scenarioLog(text, cls = '') {
  if (!state.scenario.definition) return;
  const elapsed = state.scenario.startedSimMs
    ? Math.max((state.simMs - state.scenario.startedSimMs) / 1000, 0) : 0;
  state.scenario.log.unshift({ t: `+${Math.round(elapsed)}s`, text, cls });
  state.scenario.log = state.scenario.log.slice(0, 40);
  $('scenario-log').innerHTML = state.scenario.log.map((entry) =>
    `<div class="entry ${entry.cls}"><span class="t">${entry.t}</span>${entry.text}</div>`
  ).join('');
}

function expectationResult(expectation, final) {
  const observations = state.scenario.observations ?? freshScenarioObservations();
  const pendingOrFail = () => final ? 'fail' : 'pending';
  const matchAction = (action, wanted) =>
    action.device_id === wanted.deviceId && action.action === wanted.action;

  if (expectation.type === 'tier1_idle') {
    if (!final) return 'pending';
    return observations.tier1Evaluations > 0 &&
      observations.tier1Situations.length === 0 && observations.tier1Commands.length === 0
      ? 'pass' : 'fail';
  }
  if (expectation.type === 'tier1_situation') {
    return observations.tier1Situations.includes(expectation.value) ? 'pass' : pendingOrFail();
  }
  if (expectation.type === 'tier1_action') {
    const matches = observations.tier1Commands.filter((action) => action.action === expectation.action);
    const allDevices = expectation.devices.every(
      (deviceId) => matches.some((action) => action.device_id === deviceId)
    );
    const countdownOkay = expectation.countdown === 'positive'
      ? matches.filter((action) => expectation.devices.includes(action.device_id)).every((action) => action.countdown_s > 0)
      : expectation.countdown === 'zero'
        ? matches.filter((action) => expectation.devices.includes(action.device_id)).every((action) => action.countdown_s === 0)
        : true;
    return allDevices && countdownOkay ? 'pass' : pendingOrFail();
  }
  if (expectation.type === 'tier1_action_absent') {
    if (!final) return 'pending';
    return observations.tier1Commands.some((action) => matchAction(action, expectation)) ? 'fail' : 'pass';
  }
  if (expectation.type === 'tier2_branch') {
    return observations.tier2Branches.some((branch) => expectation.values.includes(branch))
      ? 'pass' : pendingOrFail();
  }
  if (expectation.type === 'tier2_action') {
    const collection = expectation.stage === 'blocked'
      ? observations.tier2ActionsBlocked
      : expectation.stage === 'received'
        ? observations.tier2ActionsReceived : observations.tier2ActionsApplied;
    const found = collection.find((action) => matchAction(action, expectation));
    if (!found) return pendingOrFail();
    if (expectation.countdown === 'positive' && !(found.countdown_s > 0)) return pendingOrFail();
    return 'pass';
  }
  if (expectation.type === 'tier2_action_absent') {
    if (!final) return 'pending';
    return observations.tier2ActionsReceived.some((action) => matchAction(action, expectation))
      ? 'fail' : 'pass';
  }
  if (expectation.type === 'tier2_alert') {
    const acceptedKinds = expectation.kinds ?? [expectation.kind];
    return observations.tier2Alerts.some((alert) => acceptedKinds.includes(alert.kind))
      ? 'pass' : pendingOrFail();
  }
  if (expectation.type === 'backend_error') {
    return observations.backendErrors.length ? 'pass' : pendingOrFail();
  }
  if (expectation.type === 'breaker_state') {
    if (!final) return 'pending';
    const breaker = state.breakers.find((item) => item.deviceId === expectation.deviceId);
    return breaker && breaker.switchOn === expectation.switchOn ? 'pass' : 'fail';
  }
  return final ? 'fail' : 'pending';
}

function renderScenarioExpectations(final) {
  const scenario = state.scenario.definition;
  if (!scenario) {
    $('scenario-expectations').innerHTML = '';
    return [];
  }
  const results = scenario.expectations.map((expectation) => expectationResult(expectation, final));
  $('scenario-expectations').innerHTML = scenario.expectations.map((expectation, index) =>
    `<div class="expectation ${results[index]}">${expectation.label}</div>`
  ).join('');
  return results;
}

function refreshWeatherOptions() {
  // Only weather labels plausible for the selected city+month are selectable.
  const row = cityRow(new Date(state.simMs).getMonth() + 1);
  const allowed = row ? allowedWeather(row) : Object.keys(WEATHER_PV_FACTOR);
  $('inp-weather').innerHTML = Object.keys(WEATHER_PV_FACTOR).map((w) =>
    `<option value="${w}" ${allowed.includes(w) ? '' : 'disabled'}>${w}${allowed.includes(w) ? '' : ' (off-season)'}</option>`
  ).join('');
  if (!allowed.includes(state.weather)) state.weather = allowed[0];
  $('inp-weather').value = state.weather;
}

// ============================= boot =======================================

initControls();
// A sensible default site: servers (mandatory), fridge (normal), AC (comfort motor), grid breaker.
addBreaker({ deviceId: 'sim-servers', priorityType: 'mandatory', priorityDegree: 5, normalW: 300, peakW: 300, switchOn: true });
addBreaker({ deviceId: 'sim-fridge', priorityType: 'normal', priorityDegree: 3, loadType: 'motor', peakW: 600, normalW: 150, switchOn: true });
addBreaker({ deviceId: 'sim-ac-unit', priorityType: 'comfort', priorityDegree: 2, loadType: 'motor', peakW: 1800, normalW: 900 });
addBreaker({ deviceId: 'sim-grid', priorityType: 'ac_grid', priorityDegree: 1, peakW: 0, normalW: 0 });

// Optional automation hook for repeatable browser smoke tests, for example:
//   /?scenario=t1-overheat&autorun=1
const query = new URLSearchParams(window.location.search);
const requestedScenario = query.get('scenario');
if (requestedScenario && SMARTBREAKER_SCENARIOS.some((item) => item.id === requestedScenario)) {
  $('inp-scenario').value = requestedScenario;
  showScenarioPreview(requestedScenario, true);
  loadScenario(requestedScenario);
  if (query.get('autorun') === '1') setTimeout(startScenario, 100);
}
setInterval(tick, TICK_MS);
