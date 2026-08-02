/* Deterministic scenarios for exercising the real Tier-1 and Tier-2 engines.
 *
 * Scenarios contain data only.  sim.js owns execution, observations and
 * PASS/FAIL evaluation.  Keeping the cases declarative makes it easy to add a
 * new disturbance without adding another custom control path.
 */

'use strict';

const SCENARIO_BASE_BREAKERS = [
  { deviceId: 'sim-servers', priorityType: 'mandatory', priorityDegree: 5, loadType: 'normal', peakW: 300, normalW: 300, switchOn: true, online: true },
  { deviceId: 'sim-fridge', priorityType: 'normal', priorityDegree: 3, loadType: 'normal', peakW: 400, normalW: 400, switchOn: true, online: true },
  { deviceId: 'sim-ac-unit', priorityType: 'comfort', priorityDegree: 2, loadType: 'normal', peakW: 500, normalW: 500, switchOn: false, online: true },
  { deviceId: 'sim-event-load', priorityType: 'normal', priorityDegree: 8, loadType: 'normal', peakW: 700, normalW: 700, switchOn: false, online: true },
  { deviceId: 'sim-grid', priorityType: 'ac_grid', priorityDegree: 1, loadType: 'normal', peakW: 0, normalW: 0, switchOn: false, online: true },
];

function scenarioBreakers(overrides = {}) {
  return SCENARIO_BASE_BREAKERS.map((breaker) => ({
    ...breaker,
    ...(overrides[breaker.deviceId] ?? {}),
  }));
}

const SCENARIO_BASE_DAY = {
  localDateTime: '2026-07-15T12:00:00',
  scale: 60,
  weather: 'sunny',
  weatherAuto: false,
  maxPvW: 4000,
  pvThresholdW: 80,
  maxInverterW: 4000,
  gridAvailable: true,
  batteryCapacityWh: 5000,
  batterySocPercent: 80,
  batteryNominalV: 24,
  heatsinkC: 40,
  pushIntervalS: 1,
  tier2CycleS: 5,
  batteryFloorV: 24,
  powerSaving: false,
  breakers: scenarioBreakers(),
  overrides: { pvW: 2500, heatsinkC: 40, batteryVoltageV: 26.5 },
};

const SMARTBREAKER_SCENARIOS = [
  {
    id: 't1-normal',
    name: 'T1 · normal operation',
    tier: 'Tier-1',
    description: 'Healthy inverter, battery and grid. Tier-1 must remain idle.',
    durationRealS: 5,
    setup: { ...SCENARIO_BASE_DAY, tier1: true, tier2: false },
    events: [],
    expectations: [
      { type: 'tier1_idle', label: 'Tier-1 evaluates but emits no safety situation' },
    ],
  },
  {
    id: 't1-overheat',
    name: 'T1 · inverter overheat',
    tier: 'Tier-1',
    description: 'Starts healthy, then injects an 80 °C heatsink so you can compare the real Tier-1 facts before and during overheating.',
    durationRealS: 9,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: true,
      tier2: false,
      breakers: scenarioBreakers({ 'sim-ac-unit': { switchOn: true } }),
      overrides: { pvW: 1000, heatsinkC: 40, batteryVoltageV: 26 },
    },
    events: [
      { atSimS: 180, phase: 'DURING OVERHEAT', label: 'Heatsink rises from 40 °C to 80 °C', changes: { overrides: { heatsinkC: 80 } } },
    ],
    expectations: [
      { type: 'tier1_situation', value: 'inverter_overheat', label: 'Tier-1 detects inverter_overheat' },
      { type: 'tier1_action', action: 'off', devices: ['sim-ac-unit', 'sim-fridge'], label: 'Comfort and normal loads receive immediate OFF commands' },
      { type: 'tier1_action_absent', deviceId: 'sim-servers', action: 'off', label: 'Mandatory server never receives OFF' },
    ],
  },
  {
    id: 't1-overload',
    name: 'T1 · mild overload',
    tier: 'Tier-1',
    description: 'A 1200 W load on a 1000 W inverter should shed only the 500 W comfort load.',
    durationRealS: 6,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: true,
      tier2: false,
      maxInverterW: 1000,
      breakers: scenarioBreakers(),
      overrides: { pvW: 500, heatsinkC: 40, batteryVoltageV: 26 },
    },
    events: [
      { atSimS: 180, phase: 'DURING OVERLOAD', label: '500 W comfort load switches ON and pushes total load to 1200 W', changes: { breakers: { 'sim-ac-unit': { switchOn: true } } } },
    ],
    expectations: [
      { type: 'tier1_situation', value: 'inverter_overload', label: 'Tier-1 detects inverter_overload' },
      { type: 'tier1_action', action: 'off', devices: ['sim-ac-unit'], label: 'Least-important comfort load is shed' },
      { type: 'tier1_action_absent', deviceId: 'sim-fridge', action: 'off', label: 'Normal load survives the mild overload' },
    ],
  },
  {
    id: 't1-battery-critical',
    name: 'T1 · battery critical',
    tier: 'Tier-1',
    description: 'Battery at the voltage floor must shed non-mandatory loads immediately.',
    batteryControl: {
      source: 'event',
      eventIndex: 0,
      label: 'Critical-event battery voltage (V)',
      eventLabel: 'Battery voltage changes to tester target',
      min: 20,
      max: 30,
      step: 0.01,
      note: 'You choose the voltage injected when the critical event begins.',
    },
    durationRealS: 6,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: true,
      tier2: false,
      breakers: scenarioBreakers({ 'sim-ac-unit': { switchOn: true } }),
      overrides: { pvW: 0, heatsinkC: 40, batteryVoltageV: 26, batteryChargeCurrentA: 0, batteryDischargeCurrentA: 49 },
    },
    events: [
      { atSimS: 180, phase: 'BATTERY CRITICAL', label: 'Battery voltage collapses from 26 V to 24.05 V', changes: { overrides: { batteryVoltageV: 24.05 } } },
    ],
    expectations: [
      { type: 'tier1_situation', value: 'battery_critical', label: 'Tier-1 detects battery_critical' },
      { type: 'tier1_action', action: 'off', devices: ['sim-ac-unit', 'sim-fridge'], countdown: 'zero', label: 'Shutdown commands are immediate' },
    ],
  },
  {
    id: 't1-battery-countdown',
    name: 'T1 · low-battery countdown',
    tier: 'Tier-1',
    description: 'Battery near its floor should arm delayed OFF commands rather than cutting instantly.',
    batteryControl: {
      source: 'event',
      eventIndex: 0,
      label: 'Low-battery event voltage (V)',
      eventLabel: 'Battery voltage changes to tester target',
      min: 20,
      max: 30,
      step: 0.01,
      note: 'Choose a value near the configured floor to test low versus critical handling.',
    },
    durationRealS: 7,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: true,
      tier2: false,
      breakers: scenarioBreakers({ 'sim-ac-unit': { switchOn: true } }),
      overrides: { pvW: 0, heatsinkC: 40, batteryVoltageV: 26, batteryChargeCurrentA: 0, batteryDischargeCurrentA: 49 },
    },
    events: [
      { atSimS: 180, phase: 'BATTERY LOW', label: 'Battery voltage falls from 26 V to 24.4 V', changes: { overrides: { batteryVoltageV: 24.4 } } },
    ],
    expectations: [
      { type: 'tier1_situation', value: 'battery_low', label: 'Tier-1 detects battery_low' },
      { type: 'tier1_action', action: 'off', devices: ['sim-ac-unit', 'sim-fridge'], countdown: 'positive', label: 'Loads receive positive countdowns' },
    ],
  },
  {
    id: 't1-grid-outage',
    name: 'T1 · grid outage with thin battery',
    tier: 'Tier-1',
    description: 'A dead grid with the grid breaker closed and a thin battery must trigger local shedding.',
    batteryControl: {
      source: 'current',
      label: 'Battery voltage during outage (V)',
      min: 20,
      max: 30,
      step: 0.01,
      note: 'This live battery value determines whether the bank is thin when grid voltage disappears.',
    },
    durationRealS: 6,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: true,
      tier2: false,
      gridAvailable: true,
      breakers: scenarioBreakers({
        'sim-ac-unit': { switchOn: true },
        'sim-grid': { switchOn: true },
      }),
      overrides: { pvW: 0, heatsinkC: 40, batteryVoltageV: 24.9, batteryChargeCurrentA: 0, batteryDischargeCurrentA: 49, gridVoltageV: 230 },
    },
    events: [
      { atSimS: 180, phase: 'GRID OUTAGE', label: 'State-grid input drops from 230 V to 0 V', changes: { state: { gridAvailable: false }, overrides: { gridVoltageV: 0 } } },
    ],
    expectations: [
      { type: 'tier1_situation', value: 'grid_outage', label: 'Tier-1 detects grid_outage' },
      { type: 'tier1_action_absent', deviceId: 'sim-grid', action: 'off', label: 'Grid breaker stays ON for automatic recovery' },
    ],
  },
  {
    id: 't2-day-surplus',
    name: 'T2 · daytime solar surplus',
    tier: 'Tier-2',
    description: 'Strong PV should run the scheduled comfort load without buying grid electricity.',
    durationRealS: 13,
    setup: { ...SCENARIO_BASE_DAY, tier1: false, tier2: true, overrides: { pvW: 3000, heatsinkC: 40, batteryVoltageV: 26.5 } },
    events: [],
    expectations: [
      { type: 'tier2_branch', values: ['day.surplus.comfort_on'], label: 'Tier-2 takes the daytime surplus branch' },
      { type: 'tier2_action', deviceId: 'sim-ac-unit', action: 'on', stage: 'applied', label: 'Scheduled comfort load is switched ON' },
    ],
  },
  {
    id: 't2-day-deficit-grid',
    name: 'T2 · daytime deficit buys grid',
    tier: 'Tier-2',
    description: 'Weak PV and an unstable battery should close the AC-grid breaker.',
    durationRealS: 13,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: false,
      tier2: true,
      batterySocPercent: 30,
      overrides: { pvW: 100, heatsinkC: 40, batteryVoltageV: 25.5 },
    },
    events: [],
    expectations: [
      { type: 'tier2_branch', values: ['day.deficit.buy_grid'], label: 'Tier-2 takes the deficit/grid branch' },
      { type: 'tier2_action', deviceId: 'sim-grid', action: 'on', stage: 'applied', label: 'Grid breaker is switched ON' },
    ],
  },
  {
    id: 't2-power-saving',
    name: 'T2 · power-saving subset',
    tier: 'Tier-2',
    description: 'With limited PV, keep the normal load and shed the less-important comfort load.',
    durationRealS: 13,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: false,
      tier2: true,
      powerSaving: true,
      batterySocPercent: 30,
      breakers: scenarioBreakers({ 'sim-ac-unit': { switchOn: true } }),
      overrides: { pvW: 800, heatsinkC: 40, batteryVoltageV: 25.5 },
    },
    events: [],
    expectations: [
      { type: 'tier2_branch', values: ['day.deficit.power_saving'], label: 'Tier-2 takes the power-saving branch' },
      { type: 'tier2_action', deviceId: 'sim-ac-unit', action: 'off', stage: 'applied', label: 'Comfort load is shed' },
      { type: 'tier2_action_absent', deviceId: 'sim-fridge', action: 'off', label: 'Higher-priority fridge is kept running' },
    ],
  },
  {
    id: 't2-summer-pv-drop',
    name: 'T2 · sudden PV drop with seasonal diagnosis',
    tier: 'Tier-2',
    description: 'Build a 3000 W baseline, then collapse PV to 300 W. The Python facts engine derives the season from your selected date.',
    durationRealS: 14,
    setup: { ...SCENARIO_BASE_DAY, tier1: false, tier2: true, overrides: { pvW: 3000, heatsinkC: 40, batteryVoltageV: 26.5 } },
    events: [
      { atSimS: 180, phase: 'DURING PV DROP', label: 'PV production suddenly falls from 3000 W to 300 W', changes: { overrides: { pvW: 300 } } },
    ],
    expectations: [
      { type: 'tier2_branch', values: ['day.sudden_drop.battery_ok'], label: 'Tier-2 detects the sudden drop while battery is stable' },
      { type: 'tier2_alert', kinds: ['panel_fault', 'weather_drop'], label: 'Actual engine raises its season-appropriate PV-drop alert' },
    ],
  },
  {
    id: 't2-battery-protection',
    name: 'T2 · battery protection',
    tier: 'Tier-2',
    description: 'Low battery voltage should schedule shutdown countdowns and switch the grid on.',
    batteryControl: {
      source: 'current',
      label: 'Battery protection test voltage (V)',
      min: 20,
      max: 30,
      step: 0.01,
      note: 'This is the live voltage sent to Tier-2 when the scenario starts.',
    },
    durationRealS: 13,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: false,
      tier2: true,
      breakers: scenarioBreakers({ 'sim-ac-unit': { switchOn: true } }),
      overrides: { pvW: 0, heatsinkC: 40, batteryVoltageV: 24.4, batteryChargeCurrentA: 0, batteryDischargeCurrentA: 49 },
    },
    events: [],
    expectations: [
      { type: 'tier2_branch', values: ['protect_battery'], label: 'Tier-2 takes protect_battery' },
      { type: 'tier2_action', deviceId: 'sim-ac-unit', action: 'off', countdown: 'positive', stage: 'received', label: 'Comfort load receives a countdown shutdown' },
      { type: 'tier2_action', deviceId: 'sim-grid', action: 'on', stage: 'applied', label: 'Grid takes over immediately' },
    ],
  },
  {
    id: 't2-night-trip',
    name: 'T2 · night sudden-draw trip',
    tier: 'Tier-2',
    description: 'At night, introduce a large comfort load that endangers the mandatory morning reserve.',
    durationRealS: 14,
    setup: {
      ...SCENARIO_BASE_DAY,
      localDateTime: '2026-07-15T23:00:00',
      tier1: false,
      tier2: true,
      powerSaving: true,
      batterySocPercent: 20,
      overrides: { pvW: 0, heatsinkC: 35, batteryVoltageV: 25.2 },
    },
    events: [
      { atSimS: 180, phase: 'DURING SUDDEN DRAW', label: 'A 2000 W comfort load is switched ON', changes: { breakers: { 'sim-ac-unit': { switchOn: true, peakW: 2000, normalW: 2000 } } } },
    ],
    expectations: [
      { type: 'tier2_branch', values: ['night.sudden_draw.trip'], label: 'Tier-2 takes the night trip branch' },
      { type: 'tier2_action', deviceId: 'sim-ac-unit', action: 'off', stage: 'applied', label: 'Sudden-draw culprit is switched OFF' },
      { type: 'tier2_alert', kind: 'night_trip', label: 'User receives a night_trip alert' },
    ],
  },
  {
    id: 't2-grid-outage',
    name: 'T2 · state-grid outage fallback',
    tier: 'Tier-2',
    description: 'Grid breaker is closed but voltage is absent; Tier-2 must shed while leaving it closed.',
    durationRealS: 13,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: false,
      tier2: true,
      gridAvailable: false,
      batterySocPercent: 30,
      breakers: scenarioBreakers({ 'sim-ac-unit': { switchOn: true }, 'sim-grid': { switchOn: true } }),
      overrides: { pvW: 100, heatsinkC: 40, batteryVoltageV: 25.5, gridVoltageV: 0 },
    },
    events: [],
    expectations: [
      { type: 'tier2_branch', values: ['day.deficit.grid_out.shed'], label: 'Tier-2 takes the grid-out shedding branch' },
      { type: 'tier2_alert', kind: 'grid_outage', label: 'Critical grid_outage alert is raised' },
    ],
  },
  {
    id: 't2-scheduled-event',
    name: 'T2 · scheduled event requirement',
    tier: 'Tier-2',
    dateLocked: true,
    timeLocked: true,
    description: 'During the seeded event, its normally-OFF event breaker must be treated as mandatory and switched ON.',
    durationRealS: 13,
    setup: {
      ...SCENARIO_BASE_DAY,
      localDateTime: '2026-08-15T12:00:00',
      tier1: false,
      tier2: true,
      overrides: { pvW: 3000, heatsinkC: 40, batteryVoltageV: 26.5 },
    },
    events: [],
    expectations: [
      { type: 'tier2_action', deviceId: 'sim-event-load', action: 'on', stage: 'applied', label: 'Event-required breaker is switched ON' },
    ],
  },
  {
    id: 'combined-precedence',
    name: 'T1 + T2 · safety precedence',
    tier: 'Integrated',
    description: 'Tier-1 sheds an overheated site immediately; a later Tier-2 ON command is blocked while heat remains high.',
    durationRealS: 14,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: true,
      tier2: true,
      breakers: scenarioBreakers({ 'sim-ac-unit': { switchOn: true } }),
      overrides: { pvW: 3000, heatsinkC: 40, batteryVoltageV: 26.5 },
    },
    events: [
      { atSimS: 180, phase: 'DURING OVERHEAT', label: 'Heatsink rises from 40 °C to 80 °C', changes: { overrides: { heatsinkC: 80 } } },
    ],
    expectations: [
      { type: 'tier1_situation', value: 'inverter_overheat', label: 'Tier-1 detects and handles the overheat first' },
      { type: 'tier1_action', action: 'off', devices: ['sim-ac-unit'], label: 'Tier-1 switches the comfort load OFF' },
      { type: 'tier2_action', deviceId: 'sim-ac-unit', action: 'on', stage: 'blocked', label: 'Tier-2 restoration is blocked while Tier-1 danger remains' },
    ],
  },
  {
    id: 'combined-backend-outage',
    name: 'T1 + T2 · backend unavailable',
    tier: 'Integrated',
    description: 'Route Tier-2 to an unavailable local port and verify Tier-1 protection still works.',
    durationRealS: 8,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: true,
      tier2: true,
      backendOffline: true,
      breakers: scenarioBreakers({ 'sim-ac-unit': { switchOn: true } }),
      overrides: { pvW: 1000, heatsinkC: 40, batteryVoltageV: 26 },
    },
    events: [
      { atSimS: 120, phase: 'BACKEND OFFLINE · OVERHEAT', label: 'Heatsink rises to 80 °C while Tier-2 is unreachable', changes: { overrides: { heatsinkC: 80 } } },
    ],
    expectations: [
      { type: 'backend_error', label: 'The simulator observes a Tier-2/backend connection error' },
      { type: 'tier1_situation', value: 'inverter_overheat', label: 'Tier-1 still detects inverter_overheat' },
      { type: 'tier1_action', action: 'off', devices: ['sim-ac-unit', 'sim-fridge'], label: 'Local safety shedding still executes' },
    ],
  },
  {
    id: 'combined-recovery',
    name: 'T1 + T2 · danger clears and control returns',
    tier: 'Integrated',
    description: 'Keep heat high through one Tier-2 cycle, then clear it and verify normal restoration resumes.',
    durationRealS: 17,
    setup: {
      ...SCENARIO_BASE_DAY,
      tier1: true,
      tier2: true,
      breakers: scenarioBreakers({ 'sim-ac-unit': { switchOn: true } }),
      overrides: { pvW: 3000, heatsinkC: 40, batteryVoltageV: 26.5 },
    },
    events: [
      { atSimS: 180, phase: 'DURING OVERHEAT', label: 'Heatsink rises from 40 °C to 80 °C', changes: { overrides: { heatsinkC: 80 } } },
      { atSimS: 600, phase: 'RECOVERY', label: 'Heatsink returns to a safe 40 °C', changes: { overrides: { heatsinkC: 40 } } },
    ],
    expectations: [
      { type: 'tier1_situation', value: 'inverter_overheat', label: 'Tier-1 initially protects the site' },
      { type: 'tier2_action', deviceId: 'sim-ac-unit', action: 'on', stage: 'blocked', label: 'An early Tier-2 ON is blocked during danger' },
      { type: 'tier2_action', deviceId: 'sim-ac-unit', action: 'on', stage: 'applied', label: 'Tier-2 restores the load after the danger clears' },
    ],
  },
];
