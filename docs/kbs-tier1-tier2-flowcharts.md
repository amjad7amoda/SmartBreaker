# SmartBreaker KBS: Tier 1 and Tier 2 Flowcharts

These flowcharts document the behavior implemented by the current code. Tier 1 is
the local safety layer and Tier 2 is the server-side policy layer.

## Tier 1: local safety flow

Tier 1 evaluates the current inverter and breaker state in a fixed order. The first
matching safety situation returns immediately, so a rule lower in the chart is not
evaluated after a higher-priority rule fires.

```mermaid
flowchart TD
    T1_START([Receive inverter state,<br/>breaker states, and config])
    T1_INIT[Calculate load, battery voltage,<br/>and whether the battery is charging]

    T1_OVERHEAT{"Heatsink temperature >=<br/>temperature limit?"}
    T1_OVERHEAT_ACT[Select every eligible load<br/>and emit immediate OFF commands]
    T1_OVERHEAT_OUT([Return inverter_overheat<br/>with notification and trace])

    T1_OVERLOAD{"Load >= inverter rating x<br/>overload fraction?"}
    T1_OVERLOAD_ACT[Rank eligible loads and shed<br/>until estimated load <= inverter rating]
    T1_OVERLOAD_OUT([Return inverter_overload<br/>with notification and trace])

    T1_CRITICAL{"Not charging and battery voltage <=<br/>floor + critical margin?"}
    T1_CRITICAL_ACT[Select every eligible load<br/>and emit immediate OFF commands]
    T1_CRITICAL_OUT([Return battery_critical<br/>with notification and trace])

    T1_LOW{"Not charging and battery voltage <=<br/>floor + low margin?"}
    T1_COUNTDOWN[Calculate shutdown countdown from<br/>battery buffer energy and current draw]
    T1_LOW_LOADS{"Any eligible loads?"}
    T1_LOW_ACT[Emit countdown OFF command<br/>for every eligible load]
    T1_LOW_OUT([Return battery_low<br/>with notification and trace])
    T1_LOW_NOOP([Return no situation<br/>with trace])

    T1_GRID{"Grid breaker exists and is ON,<br/>but grid voltage < present threshold?"}
    T1_THIN{"Not charging and battery voltage <=<br/>floor + 2 x low margin?"}
    T1_GRID_SHED[Rank eligible loads and shed until<br/>estimated load <= current PV power]
    T1_GRID_ACTIONS{"Were any loads selected?"}
    T1_GRID_OUT([Return grid_outage<br/>with notification and trace;<br/>leave grid breaker ON])

    T1_NONE([Return no critical situation;<br/>Tier 2 remains in charge])

    T1_START --> T1_INIT --> T1_OVERHEAT
    T1_OVERHEAT -- Yes --> T1_OVERHEAT_ACT --> T1_OVERHEAT_OUT
    T1_OVERHEAT -- No --> T1_OVERLOAD
    T1_OVERLOAD -- Yes --> T1_OVERLOAD_ACT --> T1_OVERLOAD_OUT
    T1_OVERLOAD -- No --> T1_CRITICAL
    T1_CRITICAL -- Yes --> T1_CRITICAL_ACT --> T1_CRITICAL_OUT
    T1_CRITICAL -- No --> T1_LOW
    T1_LOW -- Yes --> T1_COUNTDOWN --> T1_LOW_LOADS
    T1_LOW_LOADS -- Yes --> T1_LOW_ACT --> T1_LOW_OUT
    T1_LOW_LOADS -- No --> T1_LOW_NOOP
    T1_LOW -- No --> T1_GRID
    T1_GRID -- No --> T1_NONE
    T1_GRID -- Yes --> T1_THIN
    T1_THIN -- No --> T1_NONE
    T1_THIN -- Yes --> T1_GRID_SHED --> T1_GRID_ACTIONS
    T1_GRID_ACTIONS -- Yes --> T1_GRID_OUT
    T1_GRID_ACTIONS -- No --> T1_NONE
```

### Tier 1 breaker selection

- A load is eligible for Tier 1 shedding only when it is ON, online, and its
  priority type is `comfort` or `normal`.
- Eligible loads are shed least-important first: category rank ascending
  (`comfort` before `normal`), then priority degree ascending.
- `mandatory` loads and the `ac_grid` breaker are never selected by the Tier 1
  shedding helper.
- Battery-low countdown is clamped to the configured minimum and maximum. It uses
  measured battery draw when available, otherwise `max(load - PV, 0)`.
- The audited edge service stores a new decision only when the active situation or
  command signature changes. It also records clear transitions and evaluator errors
  in local SQLite, then uploads events and action results asynchronously when the
  server is reachable. Decision-making itself does not wait for this upload.

Implementation: [`edge/tier1_kbs.py`](../edge/tier1_kbs.py) and
[`edge/audit.py`](../edge/audit.py).

## Tier 2: server cycle flow

Tier 2 is run for active organizations by Celery or through the run-cycle API. A
cycle can be skipped before the decision engine when there is no usable cycle time
or telemetry.

```mermaid
flowchart TD
    T2_TICK([Celery dispatch])
    T2_API([Run-cycle API])
    T2_DUE{"Organization active and<br/>cycle is due?"}
    T2_QUEUE[Queue one organization cycle]
    T2_SETTINGS[Load KBS settings]
    T2_ACTIVE{"Mode is active?"}
    T2_TIME[Resolve cycle time:<br/>requested time, latest simulator reading,<br/>or current server time]
    T2_TIME_OK{"Cycle time available?"}
    T2_FACTS[Load telemetry window, weather,<br/>events, breaker state, and thresholds;<br/>derive canonical SystemFacts]
    T2_FACTS_OK{"Facts available?"}
    T2_DECIDE[Run pure Tier 2 decision tree]
    T2_PERSIST[Persist decision, facts, and trace]
    T2_ACTIONS[Create action rows;<br/>suppress recent pending duplicates;<br/>apply lockout when requested]
    T2_ALERTS[Create alert rows;<br/>suppress same-kind alerts in cooldown]
    T2_DONE([Return persisted decision])
    T2_SKIP([Skip cycle without a decision])

    T2_TICK --> T2_DUE
    T2_DUE -- No --> T2_SKIP
    T2_DUE -- Yes --> T2_QUEUE --> T2_SETTINGS --> T2_ACTIVE
    T2_API --> T2_SETTINGS
    T2_ACTIVE -- No --> T2_SKIP
    T2_ACTIVE -- Yes --> T2_TIME --> T2_TIME_OK
    T2_TIME_OK -- No --> T2_SKIP
    T2_TIME_OK -- Yes --> T2_FACTS --> T2_FACTS_OK
    T2_FACTS_OK -- No --> T2_SKIP
    T2_FACTS_OK -- Yes --> T2_DECIDE --> T2_PERSIST
    T2_PERSIST --> T2_ACTIONS --> T2_ALERTS --> T2_DONE
```

## Tier 2: decision flow

```mermaid
flowchart TD
    D_START([Canonical SystemFacts])
    D_STRESS{"Heat high or cumulative<br/>energy deficit high?"}
    D_OVERLOAD{"Live load >=<br/>inverter rating?"}
    D_SHED_OVERLOAD[Priority-shed running comfort/normal loads<br/>until estimated load fits the inverter;<br/>emit critical inverter alert]
    D_OVERLOAD_OUT([Branch: protect_inverter.overload<br/>finish immediately])
    D_HEAT{"Heat high?"}
    D_HEAT_ALERT[Carry a critical cooling/hardware alert<br/>into the selected policy result]

    D_BATTERY_LOW{"Battery low and<br/>not charging?"}
    D_BATTERY[Schedule eligible loads OFF using a<br/>battery-buffer countdown; request grid ON<br/>unless power-saving; emit critical alert]
    D_BATTERY_BRANCH[Branch: protect_battery]

    D_DAY{"Daytime by clock<br/>or PV signal?"}
    D_DROP{"Sudden PV drop?"}
    D_DROP_ALERT[Add panel-fault warning in summer,<br/>otherwise weather-drop information]
    D_DROP_STABLE{"Battery stable?"}
    D_DROP_OK[Branch: day.sudden_drop.battery_ok<br/>request grid OFF]
    D_DROP_SAVE{"Power-saving?"}
    D_DROP_SUBSET[Branch: day.sudden_drop.power_saving<br/>keep best subset within PV; request grid OFF]
    D_DROP_GRID[Buy grid or shed<br/>with prefix day.sudden_drop]

    D_SURPLUS{"PV > expected running load?"}
    D_DAY_STABLE{"Battery stable?"}
    D_COMFORT[Turn on due, healthy, unlocked comfort loads<br/>that fit headroom; request grid OFF]
    D_SURPLUS_BRANCH[Branch: day.surplus.comfort_on]
    D_STABLE_BRANCH[Branch: day.battery_stable.comfort_on]
    D_DAY_SAVE{"Power-saving?"}
    D_DAY_SUBSET[Branch: day.deficit.power_saving<br/>keep best subset within PV; request grid OFF]
    D_DAY_GRID[Buy grid or shed<br/>with prefix day.deficit]

    D_SUDDEN_DRAW{"Sudden load increase?"}
    D_RESERVE_CALM{"Battery energy >= mandatory<br/>need until morning?"}
    D_CALM_OFF[Request grid OFF]
    D_CALM_KEEP[Preserve current grid state]
    D_CALM_BRANCH[Branch: night.calm.battery]
    D_RESERVE_JUMP{"Battery energy >= mandatory<br/>need until morning?"}
    D_JUMP_OK[Branch: night.sudden_draw.battery_ok<br/>request grid OFF]
    D_CAN_TRIP{"Power-saving and culprit is<br/>comfort/normal and not recently tripped?"}
    D_TRIP[Branch: night.sudden_draw.trip<br/>switch culprit OFF, lock it out,<br/>emit warning, request grid OFF]
    D_NIGHT_GRID[Buy grid or shed<br/>with prefix night.sudden_draw]

    D_GRID_FAILED{"Grid breaker is ON but<br/>grid has no voltage?"}
    D_GRID_SHED[Branch: prefix.grid_out.shed<br/>keep best subset within PV;<br/>emit critical grid-outage alert]
    D_BUY[Branch: prefix.buy_grid<br/>request grid ON]

    D_POST[Prepend carried heat alert, if any;<br/>try to turn on healthy event-required loads<br/>that fit headroom and lack another command]
    D_FINISH([Emit branch, actions, alerts, and trace])

    D_START --> D_STRESS
    D_STRESS -- Yes --> D_OVERLOAD
    D_OVERLOAD -- Yes --> D_SHED_OVERLOAD --> D_OVERLOAD_OUT
    D_OVERLOAD -- No --> D_HEAT
    D_HEAT -- Yes --> D_HEAT_ALERT --> D_BATTERY_LOW
    D_HEAT -- No --> D_BATTERY_LOW
    D_STRESS -- No --> D_BATTERY_LOW

    D_BATTERY_LOW -- Yes --> D_BATTERY --> D_BATTERY_BRANCH --> D_POST
    D_BATTERY_LOW -- No --> D_DAY

    D_DAY -- Yes --> D_DROP
    D_DROP -- Yes --> D_DROP_ALERT --> D_DROP_STABLE
    D_DROP_STABLE -- Yes --> D_DROP_OK --> D_POST
    D_DROP_STABLE -- No --> D_DROP_SAVE
    D_DROP_SAVE -- Yes --> D_DROP_SUBSET --> D_POST
    D_DROP_SAVE -- No --> D_DROP_GRID --> D_GRID_FAILED

    D_DROP -- No --> D_SURPLUS
    D_SURPLUS -- Yes --> D_SURPLUS_BRANCH --> D_COMFORT --> D_POST
    D_SURPLUS -- No --> D_DAY_STABLE
    D_DAY_STABLE -- Yes --> D_STABLE_BRANCH --> D_COMFORT
    D_DAY_STABLE -- No --> D_DAY_SAVE
    D_DAY_SAVE -- Yes --> D_DAY_SUBSET --> D_POST
    D_DAY_SAVE -- No --> D_DAY_GRID --> D_GRID_FAILED

    D_DAY -- No --> D_SUDDEN_DRAW
    D_SUDDEN_DRAW -- No --> D_RESERVE_CALM
    D_RESERVE_CALM -- Yes --> D_CALM_OFF --> D_CALM_BRANCH --> D_POST
    D_RESERVE_CALM -- No --> D_CALM_KEEP --> D_CALM_BRANCH
    D_SUDDEN_DRAW -- Yes --> D_RESERVE_JUMP
    D_RESERVE_JUMP -- Yes --> D_JUMP_OK --> D_POST
    D_RESERVE_JUMP -- No --> D_CAN_TRIP
    D_CAN_TRIP -- Yes --> D_TRIP --> D_POST
    D_CAN_TRIP -- No --> D_NIGHT_GRID --> D_GRID_FAILED

    D_GRID_FAILED -- Yes --> D_GRID_SHED --> D_POST
    D_GRID_FAILED -- No --> D_BUY --> D_POST
    D_POST --> D_FINISH
```

### Tier 2 selection and output rules

- Normal shedding protects `mandatory` and event-required loads. It ranks eligible
  running `comfort`/`normal` loads outside their usage window first, then by category
  and priority degree from least to most important.
- Best-subset branches first reserve supply for mandatory and event-required loads,
  then greedily keep the most important sheddable loads that fit the remaining power
  budget.
- Comfort and event-required switch-on selection is greedy, most-important first,
  and accounts for motor peak draw during the configured peak period.
- A grid state request becomes a no-op if the grid breaker is absent or already in
  that state. An unhealthy grid breaker blocks an ON command and emits a critical
  breaker-fault alert.
- The inverter-overload branch finishes immediately. All other completed policy
  branches pass through event-required post-processing before the trace is finalized.
- Persistence keeps every decision for audit. Same-breaker, same-action commands that
  are already pending or scheduled within 10 minutes are stored as
  `suppressed_duplicate`; same-kind alerts within 5 minutes are stored as suppressed.

Implementation: [`apps/kbs/tasks.py`](../apps/kbs/tasks.py),
[`apps/kbs/services.py`](../apps/kbs/services.py),
[`apps/kbs/adapters/django.py`](../apps/kbs/adapters/django.py), and
[`apps/kbs/engine/rules.py`](../apps/kbs/engine/rules.py).

