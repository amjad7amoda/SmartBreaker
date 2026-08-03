# SmartBreaker KBS — Facts and Rules Reference

This document catalogs every **fact** (piece of knowledge the engine derives
or reads) and every **rule** (decision-tree branch) built on this branch, for
both KBS tiers. For each item: its purpose, how it's computed/triggered, and
how it connects to the others. Read alongside [KBS_ENGINE.md](KBS_ENGINE.md),
which covers the data model and pipeline in more depth — this file focuses on
the *reasoning*, not the plumbing.

The system is two engines sharing one vocabulary of priority (`mandatory` >
`normal` > `comfort`, plus the `ac_grid` actuator) so that whichever tier acts
first, the site behaves the same way.

---

## 1. Tier-2 (server) — Facts

Facts are gathered once per cycle into an immutable `SystemFacts` snapshot
(`apps/kbs/engine/facts.py`). `rules.py` never touches the database or the
clock directly — it only reasons over these fields. This separation is what
makes every rule unit-testable with fabricated facts.

### 1.1 Raw / near-raw facts (read from telemetry, one hop from the DB)

| Fact | Purpose | Source |
|---|---|---|
| `pv_power_W` | current solar production — the supply side of every energy decision | inverter's `pv_charging_power_W`, else `V×A` |
| `load_power_W` | current total AC load — the demand side | inverter `ac_output_active_power_W` |
| `battery_voltage_V`, `battery_capacity_percent` | the two views of battery health: voltage protects hardware, percent (SoC) drives strategy | inverter reading |
| `heatsink_temp_C` | inverter hardware stress signal | inverter reading |
| `grid_breaker_on`, `grid_energized` | whether the AC-grid breaker is closed, and whether the state grid is actually live | breaker status + inverter grid-voltage register |
| `breakers` (`tuple[BreakerFacts, ...]`) | per-breaker state this cycle: priority, switch position, health, schedule window, lockout | `Breaker` + `BreakerStatus` joined by the backend adapter |

Every `BreakerFacts` carries `priority_type`/`priority_degree` (the shedding
order key), `load_type` + `peak_load_W`/`mean_load_W` (for headroom math),
`cycle_start`/`cycle_end` (the usage window), and `locked_out`/
`recently_tripped` (night-trip memory). These feed almost every rule below.

### 1.2 Derived facts (computed in `derived.py`, pure math)

| Fact | Purpose | How it's computed | Feeds into |
|---|---|---|---|
| `heat_high` | inverter overheating flag | `heatsink_temp_C ≥ heatsink_temp_limit_C` | inverter protection gate |
| `joule_deficit_J` / `deficit_high` | cumulative energy the loads owe beyond PV, over `deficit_window_minutes` | trapezoid integral of `max(load−pv, 0)`; only deficit accumulates, surplus never cancels it | inverter protection gate |
| `overload` | is the *current* load actually more than the inverter can carry right now? | `load_power_W ≥ max_inverter_power_W` | decides whether `heat_high`/`deficit_high` triggers emergency shedding or just an alert/no-op |
| `battery_low` | bank approaching its voltage floor | `voltage ≤ floor + margin` **and** charge current ≤ 0.5 A | `protect_battery` |
| `battery_draw_W` | power currently leaving the battery | `V × discharge_A`, else `max(load−pv, 0)` | sizes the battery countdown |
| `battery_buffer_Wh` | energy still allowed to spend after `battery_low` fires | `battery_shutdown_buffer_percent × capacity` | sizes the battery countdown |
| `pv_baseline_W` / `sudden_pv_drop` | is production collapsing right now vs. its own recent trend? | mean of the last `baseline_minutes` (latest excluded); drop flags if current ≤ baseline×(1−`sudden_drop_fraction`), ignored under a 100 W noise floor | day/sudden-drop branch |
| `load_baseline_W` / `sudden_draw` | is total load spiking right now vs. its own recent trend? | same baseline technique, flags if current − baseline ≥ `sudden_draw_W` | night/sudden-draw branch |
| `sudden_draw_culprit_id` | *which* breaker caused the spike | breaker whose newest `BreakerReading` rose most over its own earlier average in the window | night trip rule |
| `battery_stable` | is SoC healthy enough to run the comfort/discretionary logic? | `capacity_percent ≥ stability_threshold_percent` | day-normal, sudden-drop, night branches |
| `stability_threshold_percent` | the threshold itself — not fixed, it ramps | `ramped_threshold()`: linear ramp from the everyday threshold to the (higher) event threshold across `event_prep_hours` before a scheduled event | `battery_stable`, indirectly everything gated on it |
| `is_daytime` | which half of the decision tree applies | clock window (weather API sunrise/sunset, else `day_start`/`day_end`) **OR** `pv_power_W ≥ pv_day_min_W` | top-level branch split |
| `season` | explains *why* PV dropped | month + hemisphere (`Organization.latitude`) | sudden-drop alert wording |
| `weather_condition` | explains *why* PV dropped, outside summer | weather plug-in (season works now, condition is a TODO seam) | sudden-drop alert wording |
| `headroom_W` | how much more the inverter can carry this instant | `max_inverter_power_W − load_power_W` | comfort turn-on, event turn-on group sizing |
| `mean_load_on_W` | steady draw of everything currently ON | Σ `expected_draw_W()` of ON breakers | day-normal surplus test |
| `hours_to_morning` | night reserve horizon | hours until `day_start`/sunrise from local now | `mandatory_need_Wh` |
| `mandatory_need_Wh` | energy the untouchable loads need to survive until morning | Σ mandatory/event-required `expected_draw_W() × hours_to_morning` | night reserve check |
| `grid_failed` | the AC-grid breaker is closed but delivering nothing | `grid_breaker_on and not grid_energized` | every "buy grid" branch's fallback |
| `event_upcoming`, `event_required` (per breaker) | a scheduled event is active or being prepared for | `ScheduledEvent` window ± `event_prep_hours` | ramped threshold, shed exclusion, forced-ON list |

`BreakerFacts.expected_draw_W()` is a small embedded rule of its own: a motor
load counts at `peak_load_W` for its first `motor_peak_minutes` after
switch-on (or if it isn't on yet — inrush is always ahead of it), and at
`mean_load_W` once settled. Every headroom/budget calculation routes through
this so motor inrush is never underestimated.

---

## 2. Tier-2 (server) — Rules (decision-tree branches)

`decide(facts)` (`apps/kbs/engine/rules.py`) walks **exactly one path** per
cycle, first match wins, in this priority order:

```
heat_high or deficit_high
  overload                      → protect_inverter.overload   (short-circuits — outranks everything)
  else heat_high                → alert only, falls through
  else                          → nothing, falls through
battery_low                    → protect_battery
is_daytime
  sudden_pv_drop                → _daytime_sudden_drop
  else                          → _daytime_normal
else (night)                    → _night
--- always, regardless of branch above ---
running event's required breakers forced ON within headroom
```

> **Correction on this branch's physical model**: the AC-grid breaker is the
> inverter's *own AC input*, not a separate supply line to the loads — every
> watt bought from the grid still passes through the same inverter. Turning
> it on while the inverter is overloaded or overheating would add current
> through an already-stressed unit, not relieve it. Only shedding load can
> fix a real overload; grid purchases are decided later, once the day/night
> branch runs, and never as part of inverter protection.

### 2.1 `protect_inverter.overload`
- **Purpose**: keep the inverter's own hardware alive when the *current* load
  genuinely exceeds what it can carry — this is the one situation shedding
  can actually fix.
- **Fires when**: (`heat_high` OR `deficit_high`) AND `overload`
  (`load_power_W ≥ max_inverter_power_W` — see §1.2). Heat/deficit alone,
  without a live overload, do **not** reach this branch (see below).
- **Action**: sheds running comfort/normal loads via `_shed_order` (comfort
  before normal, lowest `priority_degree` first, mandatory/ac_grid/
  event-required never touched), **stopping as soon as the estimated
  remaining load fits `max_inverter_power_W`** — a mild overload does not
  black out the whole site. The AC-grid breaker is never touched. Raises a
  `critical` `inverter_protection` alert.
- **Interactions**: this is the only branch that short-circuits the cycle —
  `decide()` returns immediately, so the day/night branch never also tries to
  buy grid power or turn something back on the same cycle. It's the one rule
  Tier-1 also implements independently (see §4), because a second's delay
  here can damage hardware.

### 2.1b Heat-only alert and deficit-alone no-op (fall-through, not branches)
- **Purpose**: don't force an emergency response when shedding wouldn't fix
  anything — a hot heatsink with normal current draw points at a cooling or
  hardware fault (not something breaker actions can address), and a joule
  deficit with no *live* overload is a trailing signal already covered by the
  battery/day-night rules.
- **Fires when**: (`heat_high` OR `deficit_high`) AND NOT `overload`. If
  `heat_high` is the (or a) cause, a `critical` `inverter_protection` alert is
  raised naming it as a likely cooling/hardware issue; if only `deficit_high`
  is true, nothing happens at all.
- **Action**: no shedding, no grid action. The cycle **falls through** to
  `battery_low` → day/night logic as if this check hadn't fired; any alert
  raised here is prepended to whatever `RuleResult` that branch produces.
- **Interactions**: this is what makes the heat/deficit check non-blocking
  when there's nothing productive to do about it — the rest of the tree
  (battery protection, comfort scheduling, grid purchases) still runs that
  same cycle.

### 2.2 `protect_battery`
- **Purpose**: never let the bank reach its voltage floor — a hard cutoff
  would be abrupt and destructive; a countdown is graceful.
- **Fires when**: `battery_low` (voltage within margin of the floor, and not
  charging — a charging bank is recovering, so it's left alone).
- **Action**: arms a **device countdown** (not an instant switch) on every
  sheddable running load, sized so the site spends at most
  `battery_shutdown_buffer_percent` of capacity before it flips (`buffer_Wh /
  draw_W`, clamped 60 s–1 h). Raises a `critical` `battery_low` alert naming
  the breakers and the ETA. Unless `power_saving` is on, also forces grid ON
  immediately (not on a countdown) so supply doesn't gap.
- **Interactions**: second in priority — checked after inverter protection,
  before the day/night split, because the battery question matters regardless
  of what the sun or clock is doing.

### 2.3 `day.surplus.comfort_on` / `day.battery_stable.comfort_on`
- **Purpose**: spend surplus energy on comfort loads instead of wasting it.
- **Fires when**: daytime, no sudden drop, and (`pv_power_W > mean_load_on_W`
  → *surplus*) OR (`battery_stable` → *battery already healthy*). Two branch
  codes record which condition actually triggered it.
- **Action**: `_turn_on_due_comfort` — every comfort breaker whose
  `cycle_start`/`cycle_end` window contains now, restricted to the **first
  group that fits `headroom_W`** (motor loads counted at `peak_load_W`).
  Grid is turned OFF (PV/battery already cover it).
- **Interactions**: this is where `headroom_W` and `expected_draw_W()`'s
  motor-peak accounting produce the *staggered start* — the remainder of the
  due comfort loads simply reappear as `due` again next cycle, and by then
  earlier motors have settled to `mean_load_W`, freeing headroom for the next
  group. No explicit "next group" state is kept; it falls out of recomputing
  headroom every cycle. Delegates the actual set-choice to the
  `grouping.py` plug-in (`first_group_within_headroom`).

### 2.4 `day.deficit.power_saving`
- **Purpose**: when PV can't cover the load and the user prefers self-
  sufficiency over grid purchases, keep the *most valuable* subset of loads
  running instead of buying power.
- **Fires when**: daytime, no surplus, battery not stable, `power_saving=True`.
- **Action**: `_keep_best_subset` — mandatory (+event-required) draw is paid
  first and excluded from the auction; the remaining `pv_power_W` budget is
  handed to `select_best_subset` (grouping plug-in) which decides what stays
  ON among comfort/normal; everything else is shed. Grid forced OFF.
- **Interactions**: mirrors `day.sudden_drop.power_saving` (§2.6) — both call
  the same helper, only the budget's source (`pv_power_W` here) differs. Both
  are the power-saving user's answer to "PV is short."

### 2.5 `day.deficit.buy_grid` / `day.deficit.grid_out.shed`
- **Purpose**: when PV is short, no comfort/no saving mode applies, the
  default is to buy grid power — with a fallback for when the grid can't
  actually deliver.
- **Fires when**: daytime, no surplus, battery not stable, `power_saving=False`.
- **Action**: `_buy_grid_or_shed` — if `grid_failed` is *not* yet true, switch
  the AC-grid breaker ON (this cycle it doesn't yet know if the grid is
  live). If `grid_failed` *is* true (a **previous** cycle already turned the
  breaker on and the inverter now reports no grid voltage), the breaker is
  **left ON** — so supply resumes automatically the instant the real grid
  comes back — and loads are shed by priority via `_keep_best_subset` in the
  meantime, plus a `critical` `grid_outage` alert. This is a genuinely
  cycle-based sensing loop: "try grid → find out next cycle → adapt."
- **Interactions**: this exact `buy_grid`/`grid_out.shed` pair is reused
  verbatim by the sudden-drop path (§2.7) and the night sudden-draw path
  (§2.9) — one helper, three call sites with different branch-code prefixes.

### 2.6 `day.sudden_drop.*` (diagnosis + response)
- **Purpose**: explain an unexpected PV collapse to the user, then react.
- **Fires when**: daytime AND `sudden_pv_drop`.
- **Action**: always raises a diagnostic alert first — `panel_fault`
  (warning) if it's summer (a fault/shading is more likely than weather in
  peak sun season), otherwise `weather_drop` (info) naming the season and
  `weather_condition`. Then behaves exactly like `_daytime_normal`'s deficit
  side: `battery_stable` → ride it out on battery (`day.sudden_drop.battery_ok`,
  grid OFF); else `power_saving` → best subset
  (`day.sudden_drop.power_saving`); else → `_buy_grid_or_shed` under the
  `day.sudden_drop` prefix.
- **Interactions**: shares its post-diagnosis logic with `_daytime_normal`
  almost line for line — the only difference is the alert and that "surplus"
  isn't tested here (a *sudden drop* by definition isn't a surplus moment).

### 2.7 `night.calm.battery`
- **Purpose**: default night behavior — just run off the battery.
- **Fires when**: nighttime, no `sudden_draw`.
- **Action**: if `battery_remaining_Wh ≥ mandatory_need_Wh`, turn grid OFF
  (battery alone can carry the reserve). If the reserve is *not* covered, the
  grid breaker is deliberately **left as-is** — if a previous cycle already
  turned it on, it keeps relieving the battery until the reserve recovers.
- **Interactions**: this is the calm counterpart to the whole `night.sudden_draw.*`
  family below — same reserve math, no trigger to react to.

### 2.8 `night.sudden_draw.battery_ok`
- **Purpose**: confirm an unexpected load jump at night isn't actually a
  threat to the mandatory reserve.
- **Fires when**: nighttime, `sudden_draw=True`, but
  `battery_remaining_Wh ≥ mandatory_need_Wh` still holds even with the jump.
- **Action**: grid OFF, nothing else — the reserve absorbs it.

### 2.9 `night.sudden_draw.trip`
- **Purpose**: when the reserve *is* threatened and the user prefers self-
  sufficiency, remove the actual cause rather than blindly shedding by
  priority or buying grid power.
- **Fires when**: nighttime, `sudden_draw=True`, reserve insufficient,
  `power_saving=True`, a `sudden_draw_culprit_id` was identified, its
  category is `normal`/`comfort` (never mandatory), and it is **not**
  `recently_tripped` (see night-trip memory, §2.11).
- **Action**: switches the culprit OFF immediately **and locks it out**
  (`locked_out=True` — only the user can re-enable it), raises a `warning`
  `night_trip` alert naming it, grid stays/goes OFF.
- **Interactions**: this is the one branch that skips generic priority-based
  shedding in favor of *causal* diagnosis (`sudden_draw_culprit_id` from
  §1.2) — it targets the actual offender, not just "the least important load."

### 2.10 `night.sudden_draw.buy_grid` / `night.sudden_draw.grid_out.shed`
- **Purpose**: the fallback when the reserve is threatened but tripping isn't
  applicable (not power-saving, no identifiable culprit, culprit is
  mandatory, or it was already tripped once tonight).
- **Fires when**: nighttime, `sudden_draw=True`, reserve insufficient, and the
  `can_trip` conditions of §2.9 are not all met.
- **Action**: same `_buy_grid_or_shed` helper as the day branches — buy grid,
  or if it's already known dead, shed by priority while leaving the breaker
  ON to self-heal.

### 2.11 Cross-cutting rule: night-trip memory
- **Purpose**: stop the engine from re-tripping a breaker the user has
  explicitly overridden, without permanently exempting it forever.
- **How it works**: `recently_tripped` is true when a breaker was
  `locked_at` within `TRIP_MEMORY_HOURS` (12h) **and** the user has since
  cleared `locked_out` (re-enabled it). While true, §2.9's `can_trip` check
  fails, so the engine falls through to `night.sudden_draw.buy_grid` instead
  — mirrored in the design doc's phrase "trip → user re-enables → buy grid."
- **Interactions**: purely a guard inside §2.9; it doesn't fire actions of its
  own.

### 2.12 Cross-cutting rule: event-required forcing (`_ensure_event_required_on`)
- **Purpose**: guarantee a scheduled event's breakers are ON for its entire
  window, independent of whatever branch the rest of the tree took.
- **How it works**: runs **after every branch above**, unconditionally. Any
  `event_required` breaker that is OFF, healthy, and not already commanded
  this cycle is added to the first group that fits `headroom_W`. Breakers
  already targeted by the branch (e.g. shed by `protect_inverter`) are
  skipped because event-required breakers are excluded from every shed list
  in the first place (§1.1 notes `event_required` guards `_shed_order`).
- **Interactions**: this is what makes `event_upcoming`/`ramped_threshold`
  (§1.2) actually matter — the ramp hoards battery charge *before* the event,
  and this rule guarantees the event's loads are ON *during* it, even if
  `protect_inverter` or `protect_battery` fired that cycle (those still won't
  touch event-required breakers).

### 2.13 Cross-cutting rule: usage-window-first shedding
- **Purpose**: when several loads are equally rankable, shed the ones the
  user isn't actually using right now before ones they are.
- **How it works**: `_shed_order`'s sort key is
  `(in_usage_window, category_rank, priority_degree)` — `False` (outside the
  window) sorts before `True`, so an out-of-window comfort/normal load is
  always the very first candidate, ahead of category and degree.
- **Interactions**: applies inside every shedding call —
  `protect_inverter`, `protect_battery`, `_keep_best_subset`'s exclusion
  list, and the grid-out fallback shedding.

### 2.14 Cross-cutting rule: unhealthy-breaker guard
- **Purpose**: never command a breaker that can't actually respond, and tell
  the user instead of failing silently.
- **How it works**: `_turn_on_due_comfort`, `_ensure_event_required_on`, and
  `_set_grid` all check `.healthy` (`online and not fault`) before adding an
  ON action; if unhealthy, a `breaker_fault` alert is raised instead
  (`critical` if it's the AC-grid breaker being needed, `warning` otherwise).
- **Interactions**: a safety net around every "turn something ON" rule; it
  never touches OFF/shed actions (an unreachable breaker can't be commanded
  off either, but there's nothing to warn about since it isn't consuming
  head-room the engine is trying to reclaim).

---

## 3. How the Tier-2 rules compose into one engine

- **Priority ordering is the spine, but only a real overload blocks it.**
  `overload` (inside `heat_high/deficit_high`) > `battery_low` > day/night
  strategy is the precedence chain, but the top guard only short-circuits
  when shedding can actually help (a live overload). Heat without overload
  (alert only) and deficit without overload (no-op) both fall through, so
  hardware protection blocks the rest of the tree only when blocking is
  actually productive — otherwise battery protection and day/night strategy
  still get to run that same cycle.
- **One shared vocabulary drives every shed/keep decision.**
  `priority_type` → `priority_degree` → usage-window is the single sort key
  (`_shed_order`) reused by inverter protection, battery protection, power-
  saving subset selection, and grid-outage fallback shedding. Change the
  ordering once, and every rule that sheds loads changes consistently.
- **Two independent “budgets” recur everywhere**: `headroom_W` (how much more
  the *inverter* can carry this instant — governs turning things ON) and
  `mandatory_need_Wh` / `battery_remaining_Wh` (whether the *battery* can
  survive to morning — governs whether the grid is needed or a trip is
  warranted). Nearly every branch is really just "which of these two budgets
  is the binding constraint right now, and what do we do about it."
- **The day/night split governs risk tolerance, not the mechanism.** Daytime
  branches lean on `battery_stable`/`pv_power_W` because more energy is
  incoming; night branches lean on `mandatory_need_Wh` because none is. But
  the underlying `_buy_grid_or_shed`, `_keep_best_subset`, and `_shed_order`
  machinery is identical on both sides — only which facts gate them differs.
  This is why the day and night `power_saving`/`buy_grid`/`grid_out.shed`
  branches read almost line-for-line the same.
- **Grid state is a two-cycle observation loop, not a one-shot command.**
  `_set_grid` only emits an action when the desired state differs from the
  current one, and `grid_failed` can only become true a cycle *after* the
  breaker was switched on — the engine literally tries the grid, waits one
  cycle to see if `grid_energized` came true, and reacts. This is deliberate:
  there is no other way to know the state grid is dead except by trying it.
- **Cross-cutting guards (§2.11–2.14) are applied uniformly** rather than
  duplicated per branch: night-trip memory only matters inside §2.9;
  event-forcing and the health/window guards are called from whichever
  branch needs a "turn ON" or "shed" decision, so adding a new branch
  automatically inherits correct event/health/window behavior for free.
- **`decide()` is pure** — it takes a frozen `SystemFacts` and returns
  `RuleResult` (actions + alerts), so the tree above is the *entire*
  reasoning of the server engine. `apps/kbs/adapters/django.py` gathers facts
  and persists results, `apps/kbs/services.py` orchestrates the boundary, and
  Celery schedules it; none of that backend plumbing adds decision logic.

---

## 4. Tier-1 (Raspberry Pi) — Facts and Rules

Tier-1 (`edge/tier1_kbs.py`) is a smaller, dependency-free ruleset that
duplicates only the situations that cannot wait for a server round-trip. It
shares Tier-2's priority vocabulary (mandatory never shed, comfort before
normal, lower `priority_degree` first) but works from a much smaller fact set
— one live `InverterState` + `BreakerState` list, no history, no database.

### 4.1 Facts (all instantaneous, no windows/baselines)
| Fact | Purpose |
|---|---|
| `load_W`, `heatsink_temp_C`, `v_bat` | the three hardware-danger signals |
| `charging` (`charge_current_A > 0.5`) | same "don't protect a recovering battery" rule as Tier-2 |
| `sheddable` per breaker | `switch AND online AND priority_type in (comfort, normal)` — no fault/lockout/window nuance, since the Pi doesn't track those |
| `overload_limit_W` | `max_inverter_power_W × overload_fraction (1.05)` — the 5% margin before Tier-1 calls it an overload |

### 4.2 Rules — first match wins, hardware-danger first

1. **`inverter_overheat`** — `heatsink_temp_C ≥ limit` → shed *all* sheddable
   loads immediately (`_shed_all`). Purpose: identical emergency to Tier-2's
   `protect_inverter`, but Tier-1 gets there in ~1 s instead of minutes.
2. **`inverter_overload`** — `load_W ≥ rating × 1.05` → `_shed_until_within`:
   sheds least-important-first **only until the estimated remaining load fits
   the rating**, then stops. Purpose: a *mild* overload shouldn't black out
   the whole site — Tier-2's `protect_inverter.overload` (§2.1) now uses the
   exact same "shed until it fits" logic, after a correction: both tiers
   originally either shed everything or ignored instantaneous overload
   entirely, until it was pointed out that the AC-grid breaker feeds the
   inverter's own input rather than the loads directly, and only a real
   overload — not heat or joule-deficit alone — can be fixed by shedding.
3. **`battery_critical`** — not charging, `v_bat ≤ floor + 0.1 V` → shed *all*
   sheddable loads immediately, no countdown. Purpose: this far past the
   floor there's no time for a graceful countdown — a Tier-1-only
   escalation that Tier-2's `protect_battery` doesn't have (Tier-2 always
   uses a countdown).
4. **`battery_low`** — not charging, `v_bat ≤ floor + margin (0.5 V)` →
   the **same** `buffer_Wh / draw_W` countdown formula as Tier-2's
   `protect_battery`, so whichever tier reacts first produces the same ETA.
5. **`grid_outage`** — grid breaker ON, `grid_voltage_V < grid_present_min_V`,
   **and** `battery_thin` (not charging, voltage within `2×margin` of the
   floor) → `_shed_until_within(target=pv_power_W)`: sheds by priority until
   load fits current PV production; grid breaker is **left ON** (mirrors
   Tier-2's grid-outage philosophy — resume automatically). Purpose: only
   fires when the battery genuinely can't carry the load through the outage;
   a healthy battery during an outage is explicitly left to Tier-2, "which
   has the full picture" — this is the one rule with a built-in deference
   clause back to the server tier.

### 4.3 How Tier-1 and Tier-2 fit together
- **Overlap is intentional, not redundant.** Both tiers implement inverter-
  overheat and battery-floor protection because Tier-1's job is *speed*
  (LAN-local, ~1 Hz, no internet dependency) while Tier-2's job is
  *completeness* (weather, schedules, learning, optimal subsets). Whichever
  fires first wins on the wire; the other tier's next cycle will simply see
  the loads already off and take no further shedding action.
- **Tier-1 rules are strictly narrower.** No comfort scheduling, no
  power-saving subset optimization, no event awareness, no night-trip
  memory — anything requiring history, a clock, or user preference stays on
  the server. Tier-1's `grid_outage` rule explicitly hands off to Tier-2 when
  the battery can comfortably absorb the outage, which is the clearest
  statement of the division of labor in the whole system.
- **Shared constants, independently configured.** `Tier1Config` mirrors the
  matching `KBSSettings` fields (heatsink limit, battery floor/margin, grid
  presence voltage, countdown bounds) so both tiers agree on thresholds; in
  production these are meant to be synced from the server (`TODO`: ruleset
  sync), not maintained by hand in two places.

---

## 5. Summary map: fact → rule dependency

```
heat_high, deficit_high + overload ───► protect_inverter.overload (T2) / inverter_overheat, inverter_overload (T1)
heat_high without overload ───────────► alert only, falls through (T2)
load_W vs rating ─────────────────────► overload flag (T2) / inverter_overload (T1)
battery_low, battery_critical ────────► protect_battery (T2) / battery_low, battery_critical (T1)
grid_failed / grid_outage + thin batt ─► grid_out.shed (T2) / grid_outage (T1)
is_daytime ────────────────────────────► day.* vs night.* split (T2)
sudden_pv_drop ────────────────────────► day.sudden_drop.* diagnosis + response (T2)
sudden_draw, sudden_draw_culprit_id ───► night.sudden_draw.* incl. targeted trip (T2)
battery_stable, stability threshold ───► comfort_on / ride-through gating (T2)
power_saving ───────────────────────────► best-subset vs buy-grid choice (T2, both day+night)
headroom_W, expected_draw_W ────────────► staggered comfort/event turn-on group sizing (T2)
event_upcoming, event_required ─────────► ramped threshold + forced-ON + shed exclusion (T2)
usage window, health, lockout ──────────► shedding order + turn-on guards (T2, cross-cutting)
```

Every fact above is either a direct sensor reading, a small pure
transformation of one (derived.py / Tier-1 inline math), or a lookup on the
breaker's configured metadata — none of it depends on ordering within a
cycle, which is exactly what lets `decide()`/`evaluate()` be pure functions
and every branch be tested in isolation.
