# SmartBreaker KBS vs. Experta

## Full logic comparison, engine lifecycle, and rule-firing behavior

Reviewed against the repository implementation on **2026-08-03**.

## 1. Executive conclusion

SmartBreaker's KBS and Experta can both be called rule-based systems in the
broad sense, but they use fundamentally different execution models:

- **SmartBreaker Tier-2 is a deterministic decision procedure.** It receives
  one immutable `SystemFacts` snapshot, follows explicit Python `if`/`elif`
  control flow, selects one primary branch, and returns action and alert
  intents. It has no working memory, RETE network, activation set, agenda,
  salience, or automatic forward chaining.
- **SmartBreaker Tier-1 is an even smaller first-match safety decision
  procedure.** It evaluates one live inverter/breaker snapshot locally and
  returns immediately when the first safety condition matches.
- **Experta is a production-rule inference engine.** Facts are declared into
  working memory. Pattern matching creates rule activations. Activations are
  placed on an agenda, conflict resolution chooses the next activation, its
  right-hand side fires, and any fact changes can create or remove further
  activations. The cycle continues until the agenda is empty, a firing limit
  is reached, or the engine is halted.

The most accurate description of the current SmartBreaker implementation is:

> A two-tier, rules-as-code control system with immutable snapshots, explicit
> safety precedence, pure decision functions, and persisted action intents.

It is a valid KBS architecture, but it is **not an Experta-style inference
engine**, and the repository deliberately does not depend on Experta.

## 2. Source map

The key SmartBreaker sources are:

| Concern | Source |
|---|---|
| Tier-2 immutable fact contract | [`apps/kbs/engine/facts.py`](apps/kbs/engine/facts.py) |
| Tier-2 calculations | [`apps/kbs/engine/derived.py`](apps/kbs/engine/derived.py) |
| Tier-2 grouping policies | [`apps/kbs/engine/grouping.py`](apps/kbs/engine/grouping.py) |
| Tier-2 branch selection | [`apps/kbs/engine/rules.py`](apps/kbs/engine/rules.py) |
| Application-cycle orchestration | [`apps/kbs/services.py`](apps/kbs/services.py) |
| ORM-to-fact and result persistence adapter | [`apps/kbs/adapters/django.py`](apps/kbs/adapters/django.py) |
| Periodic Celery dispatch | [`apps/kbs/tasks.py`](apps/kbs/tasks.py) |
| Decisions, actions, alerts, and settings | [`apps/kbs/models.py`](apps/kbs/models.py) |
| Tier-1 local safety engine | [`edge/tier1_kbs.py`](edge/tier1_kbs.py) |
| Existing engine architecture | [`KBS_ENGINE.md`](KBS_ENGINE.md) |
| Existing fact/rule inventory | [`KBS_FACTS_AND_RULES.md`](KBS_FACTS_AND_RULES.md) |

The Experta comparison is based on its official documentation and source
repository:

- [Experta: The Basics](https://experta.readthedocs.io/en/latest/thebasics.html)
- [Experta: Rule and pattern reference](https://experta.readthedocs.io/en/latest/reference.html)
- [Experta source repository](https://github.com/nilp0inter/experta)
- [Experta changelog](https://github.com/nilp0inter/experta/blob/develop/CHANGELOG.rst)

## 3. Architectural comparison

### 3.1 SmartBreaker Tier-2

```text
Celery, API, or simulator
          |
          v
services.run_cycle(organization)
          |
          +--> load KBS settings
          +--> choose real/simulator cycle time
          +--> Django adapter queries telemetry and breaker state
          +--> adapter derives one frozen SystemFacts snapshot
          |
          v
engine.rules.decide(facts)
          |
          +--> explicit safety precedence
          +--> exactly one primary branch code
          +--> zero or more ActionIntent values
          +--> zero or more AlertIntent values
          |
          v
adapter.persist_result(...)
          |
          +--> KBSDecision audit row
          +--> pending BreakerAction rows
          +--> Alert rows
          +--> optional breaker lockout
          |
          v
edge/simulator executor applies actions and acknowledges them
          |
          v
new telemetry is evaluated in a later KBS cycle
```

The pure engine does not query the database, read a clock, call weather APIs,
write models, or command hardware. It maps:

```python
SystemFacts -> RuleResult(branch, actions, alerts)
```

### 3.2 Experta

```text
instantiate KnowledgeEngine
          |
          v
reset()
  +--> InitialFact
  +--> facts yielded by @DefFacts
          |
          v
declare facts into working memory
          |
          v
RETE/pattern matcher finds all matching rule/fact combinations
          |
          v
matching combinations become Activations on the Agenda
          |
          v
salience + conflict strategy choose the top activation
          |
          v
fire the rule's RHS method
          |
          +--> may declare, retract, modify, or duplicate facts
          |
          v
matcher updates activations and agenda
          |
          +--> repeat until agenda empty, halted, or firing limit reached
```

Experta separates a rule into:

- **LHS:** patterns/conditional elements that match facts.
- **RHS:** the Python method executed when that particular activation fires.

## 4. Full side-by-side comparison

| Dimension | SmartBreaker KBS | Experta |
|---|---|---|
| Core paradigm | Explicit decision tree / policy function | Production-rule inference engine |
| Main entry point | `decide(facts)` for Tier-2; `evaluate(...)` for Tier-1 | `KnowledgeEngine.reset()`, `declare(...)`, `run()` |
| Fact representation | Frozen typed dataclasses: one `SystemFacts` containing a tuple of `BreakerFacts` | `Fact` objects, which are dictionary-like and may be subclassed |
| Fact organization | One complete snapshot constructed before reasoning | Many individual facts in engine working memory |
| Fact lifetime | Snapshot exists only for that function call; the next cycle builds a new snapshot | Facts stay in working memory until reset, retract, or modify |
| Immutability | Dataclasses are frozen; nested breakers are a tuple | Declared values are frozen internally because matching depends on immutability |
| Rule representation | Normal Python functions and `if`/`elif` conditions | Methods decorated with `@Rule(...)` |
| Condition language | Python expressions and helper calls | Patterns, `AND`, `OR`, `NOT`, `TEST`, `EXISTS`, `FORALL`, field constraints, `MATCH`, and `AS` |
| Matching | Direct evaluation of booleans and loops | Matcher finds rule/fact combinations; Experta introduced RETE matching in version 1.0 |
| Agenda | None | Yes; matching combinations become activations on an agenda |
| Activation | None as an engine object | A rule plus matching facts and bindings |
| Conflict resolution | Encoded directly by source-code order and early `return` | Higher `salience` first, then the configured strategy orders competing activations |
| Priority | Structural: inverter gate, battery gate, then day/night tree | Declarative: usually rule salience plus strategy |
| Number of primary decisions per run | One Tier-2 `branch`; one Tier-1 `situation` or none | Potentially many rule activations may fire in one `run()` |
| Same rule firing multiple times | A helper can loop over breakers, but the branch itself is selected once | A rule can fire once per valid matching fact combination |
| Forward chaining | Across external KBS cycles only: actions affect telemetry, which is read next cycle | Native within one `run()`: RHS fact changes can activate more rules immediately |
| Fact mutation during inference | None; `decide()` does not change `facts` | Native `declare`, `retract`, `modify`, and `duplicate` operations |
| Re-evaluation | A fresh scheduled/API cycle reconstructs every fact | Incremental agenda updates happen after fact changes during the same run |
| Stopping condition | Reaching a `return` from one explicit path | Empty agenda, firing limit, or explicit halt |
| Outputs | Structured `RuleResult`, `ActionIntent`, and `AlertIntent` | Whatever each RHS method does; structured results require application design |
| Side effects | Kept outside the pure engine and persisted by the Django adapter | RHS methods may perform side effects unless the application deliberately isolates them |
| Persistence | Built-in application audit: `KBSDecision`, facts JSON, actions, alerts | No SmartBreaker-specific persistence; must be implemented around the engine |
| Action delivery | Pending action rows are consumed/applied later and acknowledged | Not provided by Experta; the application must command devices and track results |
| Deduplication | Pending same-breaker/same-action intents are suppressed for 10 minutes; alerts have a 5-minute cooldown | No domain-specific action/alert deduplication by default |
| Scheduling | Celery checks each site's `cycle_seconds`; simulator can call an API cycle | Experta does not schedule domain cycles by itself |
| Determinism | Very high: identical snapshot produces the same ordered result | Can be deterministic, but behavior depends on all activations, salience, conflict strategy, and RHS fact changes |
| Explainability | One stored branch code plus full input snapshot and intent reasons | Agenda/watchers can explain activations, but an application must persist a domain-level trace |
| Safety precedence | Obvious in control flow and protected by early returns | Must be designed with salience, guard facts, halting, or control-state facts |
| Combinatorial rules | Manual loops and helper functions | Natural strength: patterns across many fact combinations |
| Optimization | Bounded exact/knapsack startup grouping plus greedy shedding | Not an optimizer; custom Python helpers would still be required |
| Database/framework coupling | Pure engine is dependency-free; adapter owns Django | Experta itself is independent, but integration code is still needed |
| Runtime dependencies | Standard-library dataclasses in the pure engines | Additional Experta package and its transitive dependencies |
| Unit testing | Directly fabricate dataclasses and assert one returned result | Reset engine, declare facts, run, and observe fired rules/facts/side effects |
| Best fit | Bounded safety/controller policy with strict precedence and auditable action intents | Large declarative knowledge bases with many independent interacting facts and chained conclusions |

## 5. What “a rule fires” means in each system

### 5.1 In SmartBreaker

The code uses rule terminology, but there is no activation object. A Tier-2
“firing” means:

1. `decide(facts)` reaches a branch function because its Python condition is
   the first applicable path under the explicit precedence.
2. That branch constructs a `RuleResult`.
3. `RuleResult.branch` records the selected primary decision, for example
   `day.deficit.power_saving`.
4. The branch and its helpers append zero or more action and alert intents.
5. The adapter persists the result.

Several breaker actions do **not** mean several rules fired. For example,
`protect_battery` is one selected branch that can emit an OFF countdown for
every currently running sheddable breaker and an ON intent for the grid.

Helpers such as `_shed_order`, `_keep_best_subset`, `_set_grid`, and
`_ensure_event_required_on` are policy operations used by a branch; they are
not independently matched production rules.

The stored `branch` is therefore the best answer to “which Tier-2 rule
fired?” Alerts and action reasons explain additional behavior around it.

### 5.2 In Experta

An Experta rule fires when:

1. Its LHS patterns match a particular valid combination of working-memory
   facts.
2. That match becomes an activation.
3. The activation reaches the top of the agenda.
4. The engine invokes that rule's RHS method with its bound values.

If three different breaker facts match one rule, that rule may have three
activations and fire three times. If an RHS declares or modifies a fact,
other rules can activate and fire before `run()` returns.

This is the central semantic difference: SmartBreaker chooses a path;
Experta resolves and fires an agenda of matches.

## 6. How the SmartBreaker Tier-2 engine works

### 6.1 Cycle scheduling and admission

`run_kbs_cycles()` scans active site settings. A site is queued if it has no
previous `KBSDecision` or the time since its last decision is at least
`cycle_seconds` (default 300 seconds). The simulator may instead call
`POST /api/kbs/sim/run-cycle/` directly.

`services.run_cycle()` then:

1. Loads or creates the site's `KBSSettings`.
2. Stops immediately if `mode != "active"`. Observing mode does not build
   facts or persist a decision.
3. Resolves the cycle time:
   - an explicitly supplied `now` wins;
   - simulator mode uses the newest telemetry timestamp;
   - real mode uses the server clock.
4. Skips the cycle if no usable cycle time exists.
5. Asks the adapter to build `SystemFacts`.
6. Skips the cycle if no telemetry/facts are available.
7. Calls `decide(facts)` exactly once.
8. Persists the returned result in one database transaction.

### 6.2 Fact construction

The adapter queries telemetry over the larger of the configured deficit and
baseline windows, obtains the latest breaker states, reads scheduled events,
and obtains weather context. It then derives the complete snapshot.

Important raw or near-raw facts include:

- current PV power, load power, battery voltage/percentage/current,
  heatsink temperature, and grid voltage;
- breaker relay state, health, fault, priority, schedule, load profile,
  lockout, current draw, and time since it was switched on;
- KBS settings such as power-saving mode, inverter rating, voltage floors,
  thresholds, and time windows.

Important derived facts include:

| Fact | Actual calculation/use |
|---|---|
| `is_daytime` | Configured/weather sunrise window **or** PV at/above `pv_day_min_W` |
| `pv_baseline_W` | Mean of recent samples excluding the latest |
| `sudden_pv_drop` | Current PV is at least the configured fraction below the baseline, with a 100 W baseline noise floor |
| `load_baseline_W` | Mean recent output load excluding the latest |
| `sudden_draw` | Current load exceeds baseline by at least `sudden_draw_W` |
| `sudden_draw_culprit_id` | Breaker with the largest newest-vs-earlier power jump |
| `joule_deficit_J` | Trapezoidal integral of `max(load - PV, 0)` over the deficit window |
| `overload` | `load_power_W >= max_inverter_power_W` |
| `battery_low` | Battery voltage at/below floor plus margin and charge current at/below 0.5 A |
| `battery_stable` | Battery percentage at/above the current stability threshold |
| `battery_remaining_Wh` | Battery percentage multiplied by configured usable capacity |
| `mandatory_need_Wh` | Expected draw of all mandatory/event-required breakers multiplied by hours to morning |
| `grid_failed` | Grid breaker is ON while measured grid voltage is below `grid_present_min_V` |
| `headroom_W` | `max(max_inverter_power_W - load_power_W, 0)` |
| `mean_load_on_W` | Sum of expected draw for ON, non-grid breakers |

For a motor, `expected_draw_W()` uses its peak draw while it is OFF or still
within `motor_peak_minutes` after starting. It uses mean draw after the peak
phase, then current draw as a fallback, then zero.

### 6.3 Exact primary-branch precedence

This is the effective control flow in `decide()`:

```text
(heat_high OR deficit_high)?
  |
  +-- yes AND overload
  |      -> protect_inverter.overload
  |      -> RETURN IMMEDIATELY
  |
  +-- yes, no overload, heat_high
  |      -> prepare inverter heat alert
  |      -> continue
  |
  +-- yes, no overload, only deficit_high
         -> no protection action/alert
         -> continue

battery_low?
  |
  +-- yes -> protect_battery
  |
  +-- no, is_daytime?
          |
          +-- yes, sudden_pv_drop?
          |       +-- yes -> daytime sudden-drop subtree
          |       +-- no  -> daytime normal subtree
          |
          +-- no -> night subtree

prepend any heat-only alert
ensure active-event-required breakers are ON within headroom
return RuleResult
```

The source-code order is the conflict-resolution policy. There is no agenda
and no implicit competition among all matching rules.

### 6.4 Every Tier-2 primary branch

| Stored branch | Trigger | Result |
|---|---|---|
| `protect_inverter.overload` | `(heat_high or deficit_high) and overload` | Shed running comfort/normal loads in shed order until estimated load fits the inverter rating; raise critical inverter alert; return before all lower logic |
| `protect_battery` | `battery_low`, if inverter overload did not return | Give every running sheddable load the same graceful OFF countdown; turn grid ON unless power saving is enabled; raise critical battery alert |
| `day.surplus.comfort_on` | Day, no sudden drop, `pv_power_W > mean_load_on_W` | Turn due healthy comfort loads ON within headroom; turn grid OFF |
| `day.battery_stable.comfort_on` | Day, no sudden drop, no PV surplus, stable battery | Same comfort scheduling and grid-OFF behavior |
| `day.deficit.power_saving` | Day, no sudden drop, no surplus/stable battery, power saving ON | Keep a greedy priority subset within the available PV budget; shed the rest; grid OFF |
| `day.deficit.buy_grid` | Same deficit, power saving OFF, grid not yet known failed | Turn AC-grid breaker ON if needed and healthy |
| `day.deficit.grid_out.shed` | Same deficit and grid breaker already ON but grid not energized | Leave grid breaker ON; keep only affordable loads against PV budget; raise critical outage alert |
| `day.sudden_drop.battery_ok` | Daytime sudden PV drop and stable battery | Raise seasonal diagnostic alert; let battery ride through; grid OFF |
| `day.sudden_drop.power_saving` | Sudden drop, unstable battery, power saving ON | Raise diagnostic alert; keep greedy priority subset within PV; grid OFF |
| `day.sudden_drop.buy_grid` | Sudden drop, unstable battery, power saving OFF, grid not failed | Raise diagnostic alert; turn grid ON |
| `day.sudden_drop.grid_out.shed` | Same path but grid is known failed | Raise diagnostic and outage alerts; leave grid breaker ON; shed against PV budget |
| `night.calm.battery` | Night with no sudden draw | If remaining battery covers mandatory need to morning, turn grid OFF; otherwise leave grid state unchanged |
| `night.sudden_draw.battery_ok` | Night sudden draw but reserve still covers mandatory need | Turn grid OFF; no trip |
| `night.sudden_draw.trip` | Reserve short, power saving ON, identifiable non-mandatory culprit, no recent user override | Turn culprit OFF immediately, lock it out, raise night-trip alert, and turn grid OFF |
| `night.sudden_draw.buy_grid` | Reserve short and targeted trip is not allowed/possible; grid not failed | Turn grid ON |
| `night.sudden_draw.grid_out.shed` | Same path but grid is known failed | Leave grid breaker ON, shed against PV budget, and raise outage alert |

### 6.5 Cross-cutting policies

#### Load shedding order

`_shed_order()` includes running `comfort` and `normal` loads that are not
required by an active event. It never includes mandatory loads or the grid
breaker. It sorts candidates by:

```text
outside usage window first
then comfort before normal
then lower priority_degree before higher priority_degree
```

#### Greedy keep policy

Despite its name, `select_best_subset()` is not an exhaustive optimizer or
knapsack solver. It is deterministic greedy selection:

1. Sort by category importance descending, then `priority_degree`
   descending.
2. Keep a breaker if its expected draw fits the remaining budget.
3. Continue through the candidates.

Mandatory and event-required consumption is subtracted before the available
budget is offered to normal/comfort candidates.

Experta would not replace this algorithm automatically. An Experta version
would still need to call an optimizer/grouping helper if true global subset
optimization were wanted.

#### Comfort and event turn-on

Due comfort loads and required event loads are routed through a bounded
three-level planner: exact subset DP for at most 15 candidates, priority-sum
knapsack when its state space fits the exact branch's work budget, and
importance-ordered greedy selection otherwise. The selected group is ordered
by importance and must fit the reported headroom. Offline or faulted loads are
not commanded ON; a `breaker_fault` alert is emitted instead. Locked-out loads
are also not turned ON.

Event-required loads are excluded from every normal shedding list. After
most primary branches, `_ensure_event_required_on()` tries to turn missing
required loads ON within headroom.

#### Grid state is a multi-cycle feedback loop

When grid power is wanted, the first cycle can only switch the grid breaker
ON. A later telemetry cycle reveals whether grid voltage appeared:

```text
cycle N:   need grid -> command grid breaker ON
cycle N+1: breaker ON + no grid voltage -> grid_failed = True
           -> leave breaker ON and shed loads until the grid returns
```

The breaker remains ON during an outage so power can resume automatically.

#### Night-trip memory

When a night culprit is tripped, the adapter sets a KBS lockout. If the user
later clears that lockout, `recently_tripped` remains true for 12 hours based
on `locked_at`. During that period the engine respects the override and buys
grid instead of immediately tripping the same breaker again.

### 6.6 Intent persistence and execution

`persist_result()` runs atomically and:

1. Creates a `KBSDecision` with the branch and JSON-safe fact snapshot.
2. Creates pending `BreakerAction` rows, except an identical unexecuted
   breaker/action created within the last 10 minutes is suppressed.
3. Applies a database lockout immediately when the intent requests one.
4. Creates alerts unless the same alert kind was created for the site within
   the last 5 minutes.

The rule engine has decided at this point, but the physical action has not
necessarily occurred. A consumer must apply each pending action and mark it
executed. In the simulator, immediate commands are applied directly;
countdown commands are armed and acknowledged after they fire.

The inspected repository persists the production-facing action queue, but
the complete real-device delivery worker is not part of the pure KBS. This
boundary is intentional: a selected branch is a decision, an `ActionIntent`
is a requested state, and device acknowledgement is evidence of execution.

### 6.7 Feedback and repeated firing

Tier-2 does not re-evaluate after adding an intent. Its input snapshot stays
unchanged for the entire call. Feedback occurs later:

```text
decision -> pending command -> device applies it -> new breaker/telemetry state
         -> next scheduled cycle builds a new snapshot -> new decision
```

Therefore, a condition can select the same branch on several scheduled
cycles if the physical state still satisfies it. Persistence dedupe may
suppress duplicate pending actions and alert cooldown may suppress repeated
alerts, but a new `KBSDecision` is still the audit record for each completed
active cycle.

## 7. How the Tier-1 engine works

Tier-1 is the local, dependency-free safety layer intended to run near the
inverter, without waiting for the server. `evaluate()` is pure and stateless.
It checks rules in this exact first-match order:

| Priority | Situation | Trigger | Commands |
|---:|---|---|---|
| 1 | `inverter_overheat` | Heatsink temperature at/above its limit | Immediately switch OFF all online running comfort/normal loads |
| 2 | `inverter_overload` | Load at/above `max_inverter_power_W * overload_fraction` | Shed least-important loads until estimated load fits the base inverter rating |
| 3 | `battery_critical` | Not charging and voltage at/below floor plus critical margin | Immediately shed all online running comfort/normal loads |
| 4 | `battery_low` | Not charging and voltage at/below floor plus normal margin | Arm graceful OFF countdowns for all online running comfort/normal loads |
| 5 | `grid_outage` | Grid breaker ON, grid voltage absent, and battery is thin | Leave grid breaker ON and shed until estimated load fits current PV power |
| — | no situation | None of the above returns actionable commands | Return an empty situation so Tier-2 remains in charge |

Every matching branch returns immediately. There is no agenda, no chaining,
and no second Tier-1 rule in the same evaluation. Like Tier-2, several
commands can be produced by one selected situation.

Tier-1 and Tier-2 intentionally overlap on safety, but their exact gates are
not identical:

- Tier-1 treats overheating as independently actionable and sheds all
  sheddable loads. Tier-2 heat without live overload raises an alert and
  continues into the battery/day/night logic.
- Tier-1 detects overload independently at the configured overload fraction.
  Tier-2 reaches its overload branch only when `overload` is true **and**
  either `heat_high` or `deficit_high` is also true.
- Tier-1 has a battery-critical immediate cutoff path. Tier-2 battery
  protection always uses the graceful countdown path.
- Tier-1 only handles grid outage locally when battery voltage is already
  thin. Tier-2 has the broader day/night and reserve context.

These are meaningful policy differences, not differences caused by Experta.

## 8. Important implementation details and edge cases

These points describe the code as it currently executes.

### 8.1 Inverter overload returns before event forcing

`protect_inverter.overload` uses an immediate `return` from `decide()`. As a
result, the final `_ensure_event_required_on()` call is **not** executed in
that branch. Event-required loads are still protected from being shed, but
an event-required load that is already OFF will not be turned ON during that
overload cycle.

This is safer than turning on more load during an overload, but it differs
from any description saying event forcing runs after every branch without
exception.

### 8.2 Tier-2 overload alone does not fire inverter protection

The top-level gate is:

```python
if facts.heat_high or facts.deficit_high:
    if facts.overload:
        return _protect_inverter_overload(facts)
```

Consequently, `overload=True`, `heat_high=False`, and `deficit_high=False`
falls into the normal battery/day/night tree. Tier-1 does not have this
additional gate.

### 8.3 Heat-only and deficit-only are deliberately asymmetric

- Heat high without overload creates a critical cooling/hardware alert and
  then continues into another primary branch.
- Deficit high without overload creates no inverter alert or action and
  continues, because it is treated as a trailing energy signal.

Thus, one stored primary branch can coexist with a prepended heat alert.

### 8.4 “One branch” does not mean “one action”

A branch can emit many OFF actions, a grid action, and several alerts. The
branch label represents the reason/path, not the count of side effects.

### 8.5 ON and OFF health guards differ

The Tier-2 ON paths explicitly require a breaker to be online and fault-free.
The Tier-2 `_shed_order()` filters by reported `switch`, priority, and event
requirement, but does not filter by `online` or `fault`; it can therefore
produce an OFF intent for a breaker whose last state says ON even if the
breaker is currently reported offline/faulted. Tier-1's `sheddable` property,
in contrast, requires `online=True`.

### 8.6 Calm-night short reserve does not turn the grid on

In `night.calm.battery`, enough reserve turns the grid OFF. Insufficient
reserve merely leaves the grid state unchanged. A proactive grid-ON action
on the night path requires a sudden draw with insufficient reserve and no
allowed targeted trip.

### 8.7 Headroom is a snapshot value

The headroom supplied to helper functions is calculated before the decision.
The engine does not mutate it as it accumulates multiple intents. Each helper
budgets internally, but separately invoked ON helpers use the original
snapshot headroom. This matters if a primary branch and event forcing both
add different ON actions in the same cycle.

### 8.8 Some configuration/audit facts are not direct rule inputs

- `night_reserve_percent` is a settings/model field but is not used in the
  inspected fact construction or decision tree.
- `event_upcoming`, `stability_threshold_percent`, and `grid_breaker_on` are
  retained in `SystemFacts` for context/audit, while the decision tree mainly
  consumes their derived consequences (`battery_stable`, `event_required`,
  and `grid_failed`).

## 9. How the same policy would behave if written in Experta

A rough mapping would be:

| SmartBreaker concept | Possible Experta concept |
|---|---|
| `SystemFacts` fields | One `System` Fact or several typed facts such as `Battery`, `Inverter`, and `Grid` |
| Each `BreakerFacts` item | One `Breaker` Fact |
| Branch condition | `@Rule(...)` LHS patterns and `TEST(...)` constraints |
| Branch priority | Rule `salience` |
| `RuleResult.branch` | A declared `Decision` Fact or an application trace object |
| Action intents | Declared `Action` Facts or appended domain intents |
| Alert intents | Declared `Alert` Facts or appended domain intents |
| Early return / first match | `DecisionMade` guard fact, phase/control facts, careful salience, or explicit halt |
| Next external cycle | Reset/redeclare from the next telemetry snapshot, if snapshot semantics are preserved |

However, a direct decorator-for-function rewrite would not preserve current
behavior by itself. Suppose inverter protection, battery protection, and a
daytime rule all match. Experta could place all of them on the agenda. High
salience would make the inverter activation fire first, but the lower rules
could still fire afterward unless the first RHS declares a control fact,
retracts/deactivates their inputs, or halts the engine.

To preserve SmartBreaker's exact one-primary-branch semantics, an Experta
design would need an explicit protocol such as:

```text
phase = classify
no Decision fact exists
highest-salience matching primary rule declares exactly one Decision
all primary rules require NOT(Decision())
action-building rules consume the chosen Decision
validation/deduplication rules run afterward
run stops when no activations remain
```

Per-breaker patterns also create multiple matching combinations. That can be
useful for declarative shedding, but it makes global constraints such as
“shed only until total remaining watts fit” harder. A central Python grouping
calculation or aggregate/control facts would still be required.

## 10. Strengths and trade-offs

### 10.1 Strengths of the current SmartBreaker design

- Safety precedence is visible and easy to audit.
- Identical snapshots give identical ordered results.
- The pure engine is isolated from database and device side effects.
- A branch can make a coordinated whole-site decision using total power,
  headroom, reserve energy, and a global greedy subset.
- The adapter provides application-specific persistence, cooldown, lockout,
  and action deduplication.
- Unit tests can call a plain function with fabricated immutable facts.
- Tier-1 works with only the standard library and has predictable local
  latency.

### 10.2 Trade-offs of the current design

- Rule priority is coupled to Python control flow.
- Adding a new rule requires choosing its exact place in the tree.
- There is no automatic explanation of all conditions that matched but lost.
- There is no native within-cycle chaining of derived conclusions.
- Large many-to-many fact relationships require manual loops/helpers.
- Some cross-cutting behavior can be bypassed by an early return.
- “Rule” is a project-level term, not an independent declarative rule object.

### 10.3 Strengths of Experta

- Rules are individually declared and can be added without rewriting one
  central `if` tree.
- Pattern matching is expressive for independent facts and relationships.
- The agenda provides a formal model of competing activations.
- Forward chaining naturally derives multi-step conclusions.
- A rule can bind and act on every matching fact combination.
- Salience makes priority explicit in rule metadata.

### 10.4 Trade-offs of Experta for this project

- Salience alone does not enforce “only one primary decision.”
- Multiple activations can cause duplicate or conflicting device intents if
  control and deduplication are not carefully designed.
- Fact modification can create firing cascades that are less obvious than
  the current decision tree.
- Global load budgeting and subset selection still need procedural helpers.
- Database, scheduling, action queues, alerts, acknowledgements, and device
  safety remain application responsibilities.
- Recreating the current snapshot semantics may require resetting and
  redeclaring facts every KBS cycle, reducing the benefit of persistent
  working memory.
- It adds a runtime dependency without automatically improving the quality
  of the energy policy.

## 11. Which model is the better fit here?

For the current SmartBreaker problem, the custom model is the stronger fit
for the **actuation controller**:

- the number of primary situations is bounded;
- precedence is safety-critical;
- the desired result is a coordinated set of actions, not every locally
  matching conclusion;
- the system already relies on external telemetry cycles for physical
  feedback;
- auditability and deterministic replay are more valuable than emergent
  chaining.

Experta becomes more attractive if the project grows into a large diagnostic
knowledge base where many independent observations should combine into
several simultaneous conclusions—for example, fault diagnosis across many
sensor types, maintenance recommendations, or explanation chains.

A sensible hybrid, if that need appears, would be:

```text
Experta-like diagnostic inference -> candidate diagnoses/advisories
                                  -> immutable facts
deterministic SmartBreaker policy -> one safe coordinated actuation result
```

This keeps agenda-driven inference away from direct device control while
still allowing a richer declarative diagnostic layer.

## 12. Final answer in one paragraph

SmartBreaker's engine does not ask “which of all matching production rules is
next on the agenda?” It asks “given this complete snapshot, which explicit
safety/strategy path has priority?” Tier-2 builds immutable facts, checks
inverter protection, then battery protection, then a day or night subtree,
adds cross-cutting alerts/event intents where reachable, persists one branch
and its action/alert intents, and waits for later telemetry to close the
loop. Tier-1 performs the same style of first-match evaluation locally for a
smaller set of urgent safety situations. Experta instead maintains working
memory, creates activations from pattern matches, orders them on an agenda,
fires potentially many RHS methods, and can chain immediately as facts
change. The current engine is therefore more procedural and deterministic;
Experta is more declarative and inference-oriented.
