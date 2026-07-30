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

  lastRealMs: performance.now(), // real timestamp of the previous tick (ms)
};

// ============================= helpers ====================================

const $ = (id) => document.getElementById(id);

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

function batteryVoltageV(socFrac, dischargeW) {
  // Simple bank voltage model: linear open-circuit curve over state of charge
  // plus sag proportional to discharge power. Scaled from a 24 V bank.
  const scale = state.batteryNominalV / 24;                           // 12/24/48 V bank scaling (ratio)
  const openCircuit = (22.4 + 5.0 * socFrac) * scale;                 // resting voltage: 22.4 V empty -> 27.4 V full on 24 V (V)
  const sag = 1.2 * scale * (dischargeW / Math.max(state.batteryCapacityWh, 1)); // load sag, ~1.2 V at 1C on 24 V (V)
  return openCircuit - sag;
}

function stepPower(simDtS, row) {
  // One simulation step of the whole electrical site. simDtS: sim seconds elapsed (s).
  const date = new Date(state.simMs);
  const pvW = currentPvW(date, row);                                 // PV production now (W)
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
  const socFrac = state.batterySocWh / state.batteryCapacityWh;      // state of charge (0-1)
  const empty = !gridSupplying && dischargeW > 0 && state.batterySocWh <= 0;  // battery exhausted with no working grid: blackout (flag)

  // Heatsink drifts toward ambient + load-dependent heating.
  const targetC = row.temp_C + 30 * (loadW / state.maxInverterW) + 8 * (pvW / Math.max(state.maxPvW, 1)); // steady-state temperature (degC)
  state.heatsinkC += (targetC - state.heatsinkC) * Math.min(simDtS / HEATSINK_TAU_S, 1);

  const vBat = batteryVoltageV(socFrac, dischargeW);                 // bank voltage now (V)
  return { pvW, pvUsableW, loadW, gridOn, gridSupplying, chargeW, dischargeW, socFrac, vBat, empty };
}

function buildReading(flow) {
  // The full inverter snapshot in exactly the shape of the backend Reading model.
  const date = new Date(state.simMs);
  return {
    organization: state.push.orgId,
    timestamp: date.toISOString(),
    grid_voltage_V: flow.gridSupplying ? AC_VOLTAGE_V : 0,            // grid voltage is only sensed when the breaker is closed AND the state grid actually has power (V)
    grid_freq_Hz: flow.gridSupplying ? 50 : 0,                        // (Hz)
    ac_output_voltage_V: AC_VOLTAGE_V,                                // (V)
    ac_output_freq_Hz: 50,                                            // (Hz)
    ac_output_apparent_power_VA: Math.round(flow.loadW / 0.95),       // active power / power factor (VA)
    ac_output_active_power_W: Math.round(flow.loadW),                 // (W)
    output_load_percent: Math.round(100 * flow.loadW / state.maxInverterW), // (% of rating)
    bus_voltage_V: 360,                                               // DC bus while running (V)
    battery_voltage_V: +flow.vBat.toFixed(2),                         // (V)
    battery_charge_current_A: +(flow.chargeW / flow.vBat).toFixed(2), // (A)
    battery_capacity_percent: Math.round(flow.socFrac * 100),         // (%)
    heatsink_temp_C: +state.heatsinkC.toFixed(1),                     // (degC)
    pv_input_current_A: +(flow.pvW > 0 ? flow.pvW / MPPT_VOLTAGE_V : 0).toFixed(2), // (A)
    pv_input_voltage_V: flow.pvW > 0 ? MPPT_VOLTAGE_V : 0,            // (V)
    battery_voltage_scc_V: +flow.vBat.toFixed(2),                     // solar charge controller's battery reading (V)
    battery_discharge_current_A: +(flow.dischargeW / flow.vBat).toFixed(2), // (A)
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
  maybePush(flow);
  kbsCountdownCheck();
  kbsLoop();
  render(flow, row, date);
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
    const run = await (await fetch(`${base}/api/kbs/sim/run-cycle/`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ organization: org }),
    })).json();
    const st = await (await fetch(`${base}/api/kbs/sim/state/?organization=${org}`)).json();
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
      }
    }
    k.branch = run.branch ?? '(skipped)';
    k.status = `cycle @ ${new Date().toLocaleTimeString()}`;
    kbsLog(`cycle → ${k.branch}`);
  } catch (err) {
    k.status = `error: ${err.message}`;
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
    if (a.action === 'on') {
      if (!b.switchOn) { b.switchOn = true; b.onSinceMs = state.simMs; } // peak phase restarts on every OFF->ON
      kbsLog(`ON  ${a.device_id} — ${a.reason}`, 'action');
    } else if (a.countdown_s > 0) {
      // Delayed shutdown (battery protection): arm the device countdown in
      // simulated time — the breaker keeps running until it fires. The action
      // is ACKed only when the countdown fires, so the server's pending-action
      // dedupe prevents re-arming the same shutdown every cycle.
      if (state.kbs.countdowns.some((c) => c.deviceId === a.device_id)) continue; // already armed locally
      state.kbs.countdowns.push({
        deviceId: a.device_id,
        fireAtSimMs: state.simMs + a.countdown_s * 1000,              // countdown counts simulated seconds (ms since epoch)
        reason: a.reason,
        actionId: a.id,                                               // BreakerAction pk, ACKed after firing (unitless)
      });
      kbsLog(`OFF in ${Math.round(a.countdown_s / 60)} min — ${a.device_id} — ${a.reason}`, 'action');
      continue; // not ACKed yet
    } else {
      b.switchOn = false;
      kbsLog(`OFF ${a.device_id} — ${a.reason}`, 'action');
    }
    applied.push(a.id);
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
      kbsLog(`countdown fired: ${c.deviceId} OFF — ${c.reason}`, 'action');
    }
  }
  fetch(`${state.push.baseUrl}/api/kbs/sim/ack/`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action_ids: due.map((c) => c.actionId).filter(Boolean) }),
  }).catch(() => {});
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
    await fetch(`${state.push.baseUrl}/api/kbs/settings/?organization=${state.push.orgId}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    state.kbs.status = `settings error: ${err.message}`;
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
  $('inp-batnom').addEventListener('change', (e) => { state.batteryNominalV = parseInt(e.target.value); });

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
    patchKbsSettings({ battery_low_voltage_V: parseFloat(e.target.value) || 0 });
  });
  $('inp-kbs-mode').addEventListener('change', (e) => {
    patchKbsSettings({ mode: e.target.value });
  });
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
setInterval(tick, TICK_MS);
