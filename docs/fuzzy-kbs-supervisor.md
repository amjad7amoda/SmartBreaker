# Hybrid Fuzzy KBS Supervisor

## Status

Last updated: 2026-08-08.

| Repository | Path | Branch | Status |
| --- | --- | --- | --- |
| Django backend | `/home/alayham/Documents/smart/SmartBreaker` | `feature/fuzzy-kbs-supervisor` | Implemented; KBS suite, checks, and migration validation pass |
| React simulator | `/home/alayham/Documents/smart/simulation` | `feature/fuzzy-kbs-supervisor` | Implemented; unit tests, type checking, lint, and production build pass |

Both repositories remain independent Git histories. The work was layered over
the existing dirty worktrees. In particular, the existing local run documents
and the simulator's Damascus outage scenario/UI work were preserved. Nothing
was staged or committed as part of this implementation.

The default policy remains `crisp`. This is deliberate: implementing the
supervisor does not itself authorize it for a real site.

## Motivation and scientific scope

The original Tier-2 controller uses crisp thresholds. Those thresholds are
easy to audit and are appropriate for hard limits, but a normal energy
management decision often sits between labels: PV can be slightly short,
battery reserve can be merely adequate, and net power can be deteriorating at
the same time. A fuzzy supervisor combines those graded conditions without
removing the deterministic protection hierarchy.

The resulting controller is hybrid:

1. Tier-1 local safety remains authoritative.
2. Existing hard Tier-2 inverter-overload and battery-floor protection remains
   authoritative.
3. Only normal Tier-2 energy management can be supervised by fuzzy risk.
4. Every input membership, fired rule, score, transition, selected action, and
   counterfactual remains serializable for audit.

This is not autonomous machine learning. The rule base and membership
parameters are expert-defined and versioned. Any offline work is calibration
or parameter optimization against reviewed historical/simulated data. There
is no online training, runtime self-learning, automatic rule mutation, or
unreviewed model deployment.

Important limitations:

- The profile is an energy-management heuristic, not a replacement for
  electrical protection, certified relay logic, or site commissioning.
- It depends on trustworthy PV, load, SoC, capacity, rating, and baseline data.
  Invalid data produces an explicit crisp fallback.
- The reserve calculation uses current event and mandatory-energy needs; it is
  not a full stochastic forecast or battery-aging model.
- Simulator A/B metrics are evidence for calibration, not proof of field
  performance. Real activation remains gated by a shadow observation period.

## Control order and policy modes

```text
Tier-1 active?
  yes -> mirror Tier-1 safety result; fuzzy controller does not advance
  no  -> hard inverter overload or battery-floor branch?
           yes -> execute hard crisp protection; fuzzy controller does not advance
           no  -> crisp policy: execute crisp only
                  fuzzy_shadow: execute crisp; store fuzzy counterfactual only
                  fuzzy_active: execute fuzzy when valid; store crisp counterfactual
                                execute crisp when fuzzy inputs are invalid
```

Policy semantics:

| Policy | Executed result | Audit comparison | Fuzzy state |
| --- | --- | --- | --- |
| `crisp` | Existing crisp controller | None | Not created or advanced |
| `fuzzy_shadow` | Existing crisp controller | Fuzzy branch/actions stored as JSON | Advanced on valid normal cycles |
| `fuzzy_active` | Fuzzy normal controller | Crisp branch/actions stored as JSON | Advanced on valid normal cycles |

A shadow action is never persisted as a `BreakerAction`. Only actions in the
executed result can create pending device work.

For Django cycles, Tier-1 selection, fuzzy-state advancement, and decision
persistence share one organization row lock. This prevents a concurrent
Tier-1 activation from leaving behind an advanced fuzzy state for a cycle that
ultimately persisted the safety interlock instead.

## Mamdani profile `mamdani-v1`

The implementation is dependency-free and deterministic. It uses minimum
conjunction, maximum aggregation, and centroid defuzzification.

### Inputs

Let:

- `P_pv` be current usable PV power in watts.
- `P_load` be current AC load in watts.
- `P_rating` be the configured inverter rating in watts.
- `C` be configured battery capacity in Wh.
- `SOC` be battery state of charge in percent.
- `E_mandatory` be the expected mandatory and event-required energy until
  morning in Wh.
- `N_night` be `night_reserve_percent`.
- `N_event` be the current event stability target when an event is active or
  upcoming, otherwise zero.

The normalized inputs are:

```text
power_balance_ratio =
    (P_pv - P_load) / P_rating

reserve_target_percent =
    max(N_night, N_event, 100 * E_mandatory / C)

battery_reserve_margin =
    (SOC - reserve_target_percent) / 100

net_power_trend =
    ((P_pv - P_load) - (P_pv_baseline - P_load_baseline)) / P_rating
```

Values are clamped to `[-1, 1]` for membership evaluation while the original
derived values remain in the audit payload. A non-positive capacity/rating,
non-finite required value, physically impossible negative power/energy or
percentage, missing current PV/load sensor, missing baseline, or invalid
nighttime horizon invalidates the evaluation.

The high-risk power-saving budget is also recorded. In daytime it is bounded
current PV. At night it is bounded PV plus usable reserve energy spread over
the hours until morning. Breaker selection still accounts for mandatory/event
reservation, health, priority, schedule, and motor inrush.

### Membership functions

`L(a,b)` is a left shoulder equal to 1 at or below `a`, linear to 0 at
`b`. `T(a,b,c)` is a triangle with its peak at `b`. `R(a,b)` is a
right shoulder equal to 0 at or below `a`, linear to 1 at `b`.

| Input | Linguistic term | Function |
| --- | --- | --- |
| Power balance | deficit | `L(-0.25, 0.00)` |
| Power balance | balanced | `T(-0.25, 0.00, 0.25)` |
| Power balance | surplus | `R(0.00, 0.25)` |
| Battery reserve | short | `L(-0.10, 0.10)` |
| Battery reserve | adequate | `T(-0.10, 0.10, 0.30)` |
| Battery reserve | ample | `R(0.10, 0.30)` |
| Net-power trend | falling | `L(-0.15, 0.00)` |
| Net-power trend | steady | `T(-0.15, 0.00, 0.15)` |
| Net-power trend | rising | `R(0.00, 0.15)` |

The output universe is risk from 0 to 100:

| Output term | Function |
| --- | --- |
| low | `L(25, 45)` |
| watch | `T(25, 50, 75)` |
| high | `R(55, 75)` |

### Expert rule table

Each cell is the consequent for the named trend, yielding 3 × 3 × 3 = 27
rules. Rows and their iteration order are part of the versioned profile.

| Power balance | Battery reserve | Falling | Steady | Rising |
| --- | --- | --- | --- | --- |
| deficit | short | high | high | high |
| deficit | adequate | high | high | watch |
| deficit | ample | high | watch | watch |
| balanced | short | high | high | watch |
| balanced | adequate | high | watch | low |
| balanced | ample | watch | low | low |
| surplus | short | high | watch | watch |
| surplus | adequate | watch | low | low |
| surplus | ample | low | low | low |

For a rule, firing strength is the minimum of its three antecedent
memberships. Consequents of the same term are combined by maximum. The
aggregated output is sampled over 0–100 in deterministic 0.25-point increments
and its centroid is returned as `risk_score`. The evaluation also contains
all input values, memberships, non-zero fired rules and strengths, aggregated
strengths, inferred band, active hysteretic band, profile version, validity,
and fallback reason.

## Hysteresis

The organization-scoped controller stores its current band, candidate band,
consecutive valid cycle count, last score, last evaluation time, and profile
version.

Transitions are:

- Enter `high` immediately at score ≥ 75.
- Enter `high` after two consecutive valid cycles at score ≥ 65.
- Leave `high` after two consecutive valid cycles at score ≤ 55. A
  sufficiently low recovery (≤ 35) may land directly in `low`; otherwise it
  lands in `watch`.
- Enter `low` immediately at score ≤ 25.
- Enter `low` after two consecutive valid cycles at score ≤ 35.
- Leave `low` at score ≥ 45. A score ≥ 75 can move directly to `high`;
  otherwise the controller returns to `watch`.
- Values between candidate thresholds clear the candidate count, preventing
  boundary noise from accumulating non-consecutive evidence.
- Invalid fuzzy input executes the crisp policy and does not change the
  candidate count or last valid evaluation timestamp.
- A gap strictly greater than twice the configured cycle interval, or a
  profile-version change, resets the working state to `watch`.

Freshness uses controller execution time, not telemetry event time. This is
essential in the accelerated React simulator, where five real seconds advance
the physical timestamp by five simulated minutes.

The decision record includes transition evidence such as
`confirming_high_entry`, `confirmed_high_entry`,
`confirming_high_exit`, `confirmed_high_exit`, `immediate_low_entry`,
`low_exit`, `invalid_hold`, and `stale_reset`.

## Band behavior

Protection and eligibility rules remain deterministic:

- `low`: during the day, restore eligible scheduled comfort loads within
  inverter headroom and request grid OFF. At night, retain load states and
  request grid OFF.
- `watch`: preserve current load and grid states.
- `high` with power saving: keep the best priority subset inside the
  day/night safe-energy budget and request grid OFF.
- `high` without power saving: request grid ON. If the closed grid breaker is
  confirmed de-energized, use the existing priority shedding and grid-outage
  alert behavior.

Mandatory and event-required loads are excluded from shedding, and an
event-required load still receives its authoritative ON intent. Breaker
online/fault/lockout checks, motor inrush estimates, priority selection,
grid-failure handling, and night sudden-draw culprit eligibility remain in
force. PV-drop detection still produces its seasonal diagnostic alert; normal
energy response uses the fuzzy trend rather than a single drop Boolean.

## Meaning of a consecutive cycle

A consecutive cycle means a valid Tier-2 fuzzy evaluation, not a telemetry
push, render tick, or elapsed wall-clock interval by itself.

| Environment | Tier-2 interval | Clock acceleration | One cycle | Two cycles |
| --- | --- | --- | --- | --- |
| React default | 5 real seconds | 60× | 5 real seconds / 5 simulated minutes | 10 real seconds / 10 simulated minutes |
| Production default | 300 seconds | 1× operational clock | about 5 minutes | about 10 minutes |

Production timing is subject to dispatcher and worker delay. Severe risk
(score ≥ 75), Tier-1 safety, and hard Tier-2 protection do not wait for
two-cycle confirmation.

## Database and API changes

Migration `0009_fuzzy_kbs_supervisor` adds:

- `KBSSettings.tier2_policy`: `crisp`, `fuzzy_shadow`, or
  `fuzzy_active`; default `crisp`.
- `KBSDecision.policy`; default `crisp`.
- `KBSDecision.counterfactual`; JSON default `{}`.
- `KBSControllerState`; one-to-one with organization, holding the persistent
  hysteresis fields.

The settings endpoint accepts the policy plus the simulator's battery
capacity, night reserve, and inverter rating. Run-cycle, simulator-state, and
decision-audit responses expose policy, fuzzy evaluation, and counterfactual.
Simulator state also exposes the controller state and `mamdani-v1` metadata.
A skipped observing/no-data cycle returns the same top-level fields with empty
evidence. The organization-scoped simulator reset removes controller state
along with decision and telemetry history.

Affected endpoints:

- `PATCH /api/kbs/settings/?organization=<id>`
- `POST /api/kbs/sim/run-cycle/`
- `GET /api/kbs/sim/state/?organization=<id>`
- `POST /api/kbs/sim/reset/`
- `GET /api/kbs/decision-logs/`
- `GET /api/kbs/decision-logs/<event-id>/`

## React simulator changes

The React repository is the sole simulator for this feature; the legacy
`SmartBreaker/simulator/sim.js` was not used.

The simulator now:

- synchronizes `tier2_policy` and fuzzy sizing settings with Django;
- provides policy selectors in configuration and Scenario Lab;
- displays active policy, profile, risk score/band, transition, memberships,
  fallback reason, and counterfactual branch/actions on the dashboard;
- records fuzzy bands, fallbacks, counterfactual branches, and band
  transitions as scenario observations;
- includes deterministic scenarios for immediate high entry, confirmed
  moderate high entry, two-cycle recovery, boundary noise, invalid-input crisp
  fallback, shadow comparison, hard battery authority, and Tier-1 authority;
- retains the existing deterministic scenarios, including the Damascus
  evening utility-failure scenario;
- measures grid-import Wh, minimum battery SoC, time below reserve target,
  optional-load served Wh, mandatory OFF commands, action count, and command
  reversals; and
- offers an A/B flow that loads the same deterministic scenario, resets the
  backend, runs `crisp`, resets again, runs `fuzzy_active`, and presents
  `fuzzy - crisp` metric differences before restoring the original policy.

## Implementation log

| Subsystem | Implemented work |
| --- | --- |
| Pure engine | Versioned membership functions, explicit 27-rule table, Mamdani min/max inference, centroid score, validation, deterministic payload |
| Controller | Pure hysteresis transition function plus organization-scoped persistent state and stale/profile reset |
| Django adapter | Tier-1/hard-protection precedence, crisp/shadow/active routing, safe fallback, counterfactual serialization, executed-action isolation |
| Persistence | Migration 0009, policy/counterfactual decision fields, controller state, reset integration |
| APIs/audit | Policy and evidence in settings/run/state/history responses; controller metadata and reset counts |
| React state | Policy synchronization, fuzzy evidence ingestion, scenario observations, seven accumulated metrics, two-run A/B orchestration |
| React UI | Configuration/Scenario Lab selectors, dashboard supervisor card, comparison table, timing explanation |
| Scenarios | Six dedicated fuzzy scenarios plus hard-protection and Tier-1-authority coverage; existing Damascus changes retained |
| Tests | Numeric boundaries, all 27 consequents, centroid, min/max inference, hysteresis, fallback, persistence, shadow isolation, migration, APIs, UI, timing, scenario evaluation, and metric differences |

## Verification results

Results from 2026-08-08:

| Check | Result |
| --- | --- |
| Django KBS suite | 116 passed |
| Focused fuzzy/settings/audit tests | 40 passed |
| Django system check | Passed, no issues |
| Migration drift | `makemigrations --check --dry-run`: no changes |
| Migration forward/default test | Passed |
| Python compilation | Passed |
| React Vitest | 20 passed; 2 opt-in live tests intentionally skipped |
| React TypeScript | `npm run typecheck`: passed |
| React ESLint | `npm run lint`: passed |
| React production build | `npm run build`: passed; 1,611 modules transformed |
| Diff whitespace check | Passed in both repositories |
| Development migration | Applied the pending migration set, including `0009`, to the dedicated local simulator database and restored its policy to `crisp` after testing |
| Live browser/backend scenarios | 8 selected fuzzy/safety scenarios passed against Django and the Tier-1 bridge |
| Live Scenario Lab A/B | Passed; reset, crisp run, second reset, fuzzy-active run, metric comparison, and policy restoration completed |
| Repository-wide Django suite | 216/217 passed; unrelated pre-existing `apps.telemetry.tests.ReadingIngestTests.test_missing_timestamp_is_rejected` errors because its assertion expects a list-shaped validation body |

The isolated telemetry test fails in the same way and no telemetry code was
changed by this feature.

### Crisp-versus-fuzzy metrics

The Scenario Lab collects comparable values for every A/B run:

| Metric | Direction used during review |
| --- | --- |
| Grid-import Wh | Lower is better, subject to optional-service constraint |
| Minimum battery SoC | Must not regress below safety/reserve limits |
| Time below reserve target | Must not increase |
| Optional-load served Wh | Higher is better, subject to grid-use constraint |
| Mandatory OFF commands | Must remain zero |
| Action count | Diagnostic operational burden |
| Command reversals | Lower measures reduced chatter |

One live A/B run used `fuzzy-boundary-noise` for 18 real seconds at 60×. The
values below are a functional verification sample, not a statistically
meaningful calibration dataset:

| Metric | Crisp | Fuzzy active | Δ fuzzy − crisp |
| --- | ---: | ---: | ---: |
| Grid-import Wh | 0.00 | 0.00 | 0.00 |
| Minimum battery SoC (%) | 58.824 | 58.824 | 0.000 |
| Time below reserve target (simulated s) | 0.00 | 0.00 | 0.00 |
| Optional-load served Wh | 227.475 | 224.992 | -2.483 |
| Mandatory OFF commands | 0 | 0 | 0 |
| Action count | 1 | 1 | 0 |
| Command reversals | 0 | 0 | 0 |

The fuzzy run retained 98.91% of crisp optional service, with identical grid,
minimum-SoC, reserve, safety-command, action, and reversal measurements.
Because both runs used zero grid energy and zero command reversals, this
sample cannot demonstrate either the energy-benefit gate or the 30% reversal
reduction gate. Those gates still require a larger scenario set and shadow
dataset.

The eight live cases covered immediate high entry, two-cycle moderate-high
entry, two-cycle recovery, SoC/power-balance noise, invalid-input fallback,
shadow comparison, hard battery protection, and combined Tier-1 precedence.
The timing run exposed and then verified that stale-state age must use
controller execution time rather than accelerated telemetry timestamps.

## Shadow rollout and activation gates

Recommended rollout:

1. Deploy the migration and code with every site still on `crisp`.
2. Select a small reviewed cohort and switch it to `fuzzy_shadow`.
3. Verify valid-input rate, rule coverage, stale resets, action deltas, and
   mandatory/event protection daily.
4. Accumulate at least 14 days and 500 valid shadow decisions per activation
   decision.
5. Run deterministic boundary-noise and safety scenarios, then compare field
   shadow counterfactuals against crisp execution.
6. Activate `fuzzy_active` only after every gate is met, starting with a
   reversible pilot cohort.

Required gates:

- Zero mandatory-load OFF commands.
- No regression in hard-protection behavior or timing.
- No additional battery-floor or overload violations.
- At least 30% fewer command reversals in boundary-noise scenarios.
- Either at least 5% lower grid energy without optional service falling below
  98% of crisp, or at least 10% higher optional service with grid use no more
  than 2% above crisp.
- At least 14 days and 500 valid decisions in `fuzzy_shadow` before real-site
  activation.

Current activation status: **not approved**. The implementation, offline
tests, selected live cases, and one functional A/B sample are complete, but
the performance thresholds and required shadow duration have not been met.

## Rollback

Immediate rollback is a settings change to `tier2_policy=crisp`. Crisp mode
does not consult or advance fuzzy controller state, so no schema rollback is
required. Existing audit rows remain available. Pending actions already
created by an executed active decision are operational records and should be
handled through the normal action-resolution workflow; changing policy does
not erase them.

Before a later reactivation, allow the controller's stale-state rule to reset
it to `watch`, or explicitly clear the organization state through the
simulator reset in a simulator environment. Do not use the simulator reset on
a real organization.

## Primary scientific resources

- [Teo et al., IEEE ISGT Asia 2016](https://doi.org/10.1109/ISGT-Asia.2016.7796362)
- [Yahyaoui et al., 2014](https://doi.org/10.1016/j.enconman.2013.07.091)
- [Arcos-Avilés et al., IEEE Transactions on Smart Grid](https://doi.org/10.1109/TSG.2016.2555245)
- [Fuzzy demand-side EMS, 2023](https://doi.org/10.1016/j.ecmx.2023.100354)
- [Hybrid fuzzy PV microgrid EMS, 2026](https://doi.org/10.1016/j.compeleceng.2026.110977)
