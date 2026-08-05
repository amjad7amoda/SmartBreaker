# Tier 1 and Tier 2 Safety Interlock

## Purpose

This document describes the KBS and backend changes that make Tier 1 the
authoritative owner of danger handling while allowing Tier 2 to retain its
existing behavior whenever no Tier 1 danger is active.

The central rule is:

> Tier 1 detects and owns danger. Tier 2 mirrors the active Tier 1 safety
> decision and does not run normal grouping, comfort, event, grid-purchase, or
> restoration rules until Tier 1 explicitly clears the danger.

This design does not add a third KBS. The new interlock code does not evaluate
sensor thresholds or choose a danger response. It only transports and mirrors a
decision already made by the existing Tier 1 KBS.

## Scope

The change covers:

- Tier 1 danger lifetime and clear behavior.
- Durable backend storage of the latest Tier 1 safety state.
- Ordered and idempotent Tier 1 event ingestion.
- Tier 2 preemption while Tier 1 safety is active.
- Mirroring of Tier 1 commands in Tier 2 decision audits.
- Suppression of duplicate physical commands.
- Supersession of unresolved Tier 2 work when danger starts.
- Monotonic action-result handling.
- Simulator-state API exposure of the safety interlock.
- Database migration and backfill.
- Unit, API, adapter, concurrency, and regression coverage.

The change intentionally does not alter the Tier 2 grouping algorithm or any
normal Tier 2 rule.

## Architecture

### Before

Tier 1 and Tier 2 evaluated independently:

~~~text
Tier 1 sensors -> Tier 1 danger decision -> local action

Backend telemetry -> Tier 2 rules/grouping -> backend action
~~~

The backend stored Tier 1 decisions as audit history, but a Tier 1 danger was
not an authoritative live state used to gate Tier 2. This allowed Tier 2 to
generate an ON action while Tier 1 required the same breaker to remain OFF.

### After

~~~text
Tier 1 sensors
    |
    v
Tier 1 danger-only KBS
    |
    | decision / clear event
    v
Backend Tier1SafetyState
    |
    +--> supersede unresolved Tier 2 actions
    |
    v
Tier 2 cycle gate
    |
    +-- active --> mirror Tier 1 desired state; skip normal Tier 2 rules
    |
    +-- clear ---> run the existing Tier 2 decision tree unchanged
~~~

Tier1SafetyState is coordination state. It is not a KBS because it has no
sensor evaluation, thresholds, priorities, grouping, or autonomous cycle.

## Safety ownership contract

Tier 1 remains the single authority for danger:

- Tier 1 selects the danger situation.
- Tier 1 selects the target breakers, action direction, countdown, and reason.
- Tier 1 emits the clear transition after the physical danger condition is no
  longer true.
- An evaluator error does not invent a new danger and does not clear the last
  confirmed safety hold.

Tier 2 behavior is:

- If Tier 1 safety is active, normal Tier 2 rules are bypassed.
- Tier 2 creates an audit decision with branch
  tier1_interlock.<tier1-situation>.
- Tier 2 mirrors Tier 1 commands that are not already satisfied.
- Existing action deduplication prevents a matching Tier 1 and Tier 2 command
  from being executed twice.
- If Tier 1 safety is clear, the original Tier 2 decide function runs without
  any changes to its grouping or normal decision rules.

## Tier 1 KBS changes

The Tier 1 KBS was already danger-only. Its rule ordering and thresholds remain
unchanged.

Two false-clear paths were corrected in edge/tier1_kbs.py.

### Low battery

Previously, battery_low returned an empty situation when no eligible running
loads remained. The audit layer interpreted the empty situation as danger
cleared, even though the battery voltage was still inside the danger margin.

Now battery_low remains active with an empty command list. Its notification
states that the safety hold remains active and no additional eligible load can
be shed.

### Grid outage

Previously, grid_outage returned an empty situation after all eligible loads
had been shed. This could clear the Tier 1 hold while grid voltage remained
zero.

Now grid_outage remains active while its original raw condition remains true,
even if there is no new action to emit.

### Meaning of an empty command list

An empty command list on a new Tier 1 event no longer means safe. It means:

- the danger situation is still active; and
- the current breaker state requires no additional Tier 1 command.

The backend retains the cumulative desired breaker targets from the active
episode, so a breaker that is later turned back on can be driven to the safe
state again. Only a Tier 1 clear event releases those targets and the backend
interlock.

## Backend safety-state model

Migration apps/kbs/migrations/0008_tier1_safety_interlock.py creates one
Tier1SafetyState row per organization.

| Field | Meaning |
| --- | --- |
| organization | Organization protected by the hold |
| edge_device | Edge device that supplied the current state |
| source_decision | Tier 1 decision or clear event that last advanced the state |
| active | Whether Tier 2 must be preempted |
| situation | Current Tier 1 danger branch |
| episode_id | Stable identifier for one active safety episode |
| commands | Cumulative desired breaker targets for the active episode |
| source_occurred_at | Edge event time used for ordering |
| activated_at | Time the current episode started |
| cleared_at | Time the latest episode cleared |
| updated_at | Backend row update time |

The migration backfills each organization from its latest confirmed Tier 1
decision or clear event. Error events are skipped during backfill because they
must not replace the last confirmed safety state.

## Tier 1 event ingestion

The Tier 1 decision-events endpoint still stores immutable KBSDecision and
BreakerAction audit rows. It now also advances Tier1SafetyState.

### Transaction and ordering rules

- Ingestion locks the organization row.
- Tier 2 persistence locks the same organization row.
- This serializes Tier 1 safety transitions with Tier 2 action persistence.
- Events are ordered by edge occurrence time, backend receipt time, and
  database identifier.
- A retried or late older event cannot reactivate a hold after a newer clear.
- Duplicate event uploads remain idempotent.

### Activation

For a new Tier 1 danger decision, ingestion:

1. Stores the immutable Tier 1 decision and actions.
2. Creates or updates Tier1SafetyState.
3. Starts a new episode when the prior state was clear or the danger situation
   changed.
4. Merges new commands into the episode's retained desired breaker targets.
5. Marks unresolved Tier 2 pending or scheduled actions as superseded.

### Clear

For a Tier 1 clear event, ingestion:

1. Marks the safety state inactive.
2. Clears the current situation and command list.
3. Retains the episode metadata for audit context.
4. Allows the next Tier 2 cycle to perform a fresh normal evaluation.

Previously queued Tier 2 actions are not restored. Restoration must come from a
new Tier 2 decision made from current telemetry.

### Evaluator errors

Tier 1 evaluator errors are still stored as audit events. They do not activate
or clear Tier1SafetyState. This is fail-safe: an existing confirmed danger hold
remains active until a confirmed clear arrives.

## Tier 2 decision changes

The pure normal Tier 2 engine in apps/kbs/engine/rules.py was not modified.

The Django adapter now asks the interlock for a safety snapshot before invoking
the normal engine:

- active snapshot: call mirror_tier1_decision;
- inactive or missing snapshot: call the existing decide function.

The adapter rechecks the safety state inside the persistence transaction. This
closes two races:

- Tier 1 activates after Tier 2 starts computing a normal decision.
- Tier 1 clears after Tier 2 starts computing an interlock decision.

The state seen under the organization lock is authoritative. The adapter
rebuilds the correct result before writing any Tier 2 actions.

## Mirroring behavior

apps/kbs/interlock.py contains a pure coordination mapper.

It accepts:

- immutable Tier 2 breaker facts; and
- a snapshot of an already-made Tier 1 safety decision.

It does not accept raw inverter sensors and cannot independently classify a
danger.

For every Tier 1 command:

- If the breaker is missing from current Tier 2 facts, the trace records
  breaker_missing and emits no command.
- If the breaker already has the desired state, the trace records
  already_satisfied and emits no command.
- Otherwise Tier 2 emits the same device, action, countdown, and reason.

The trace explicitly records that normal Tier 2 guards were bypassed.

## Action lifecycle

### New superseded status

BreakerAction now supports the terminal status superseded.

When a Tier 1 danger is activated or updated, unresolved Tier 2 pending and
scheduled actions are changed to superseded. The failure_reason field contains
the Tier 1 episode and situation that invalidated the action.

Superseded is a resolved status, so it is excluded from pending-action API
responses.

### Duplicate suppression

If Tier 2 mirrors a Tier 1 command while an equivalent unresolved command
already exists, the Tier 2 audit action is stored as suppressed_duplicate. The
agreement remains visible in the audit without a second physical operation.

### Tier 1 result reconciliation

When a later Tier 1 event contains a breaker snapshot matching an unresolved
Tier 1 action target, the backend marks that action applied. This prevents
successfully executed edge actions from remaining pending forever when a
separate explicit action acknowledgement was missed.

### Monotonic terminal results

Resolved actions cannot be changed to a contradictory terminal state later.
For example:

- applied cannot later become failed;
- superseded cannot later become applied;
- suppressed_duplicate cannot be revived as pending.

The edge result endpoint rejects contradictory transitions. The simulator ACK
endpoint ignores already resolved actions and returns ignored_resolved in its
response.

## API changes

GET /api/kbs/sim/state/ now includes tier1_safety:

~~~json
{
  "tier1_safety": {
    "active": true,
    "situation": "inverter_overheat",
    "episode_id": "...",
    "source_event_id": "...",
    "commands": [
      {
        "device_id": "ac",
        "action": "off",
        "countdown_s": 0,
        "reason": "tier1: inverter overheating"
      }
    ],
    "source_occurred_at": "...",
    "activated_at": "...",
    "cleared_at": null,
    "updated_at": "..."
  }
}
~~~

When no state exists, the API returns an explicit inactive object with empty
commands instead of omitting the field.

POST /api/kbs/sim/ack/ retains acknowledged and adds ignored_resolved.

Simulator reset deletes Tier1SafetyState for the selected simulator
organization along with its run history.

## Behavior by scenario

| Scenario | Result with the interlock |
| --- | --- |
| Normal | No active hold; original Tier 2 rules and grouping run |
| Inverter overheat | Tier 2 bypasses comfort restoration and mirrors Tier 1 OFF targets |
| Inverter overload | Tier 2 cannot create the ON step that caused OFF/ON/OFF oscillation |
| Battery critical | Tier 2 mirrors the immediate Tier 1 safety target |
| Low-battery countdown | Tier 2 mirrors countdown metadata; duplicate execution is suppressed |
| Grid outage with thin battery | Hold remains active at zero grid voltage even after all eligible loads are off |

## Concurrency and failure behavior

### A danger starts during a Tier 2 cycle

The Tier 1 ingestion transaction obtains the organization lock, advances the
hold, and supersedes unresolved Tier 2 actions. Tier 2 persistence rechecks the
hold under the same lock and persists an interlock decision instead of its
previously calculated normal result.

### A danger clears during a Tier 2 cycle

Tier 2 persistence sees the clear state under the organization lock and
recalculates the original normal Tier 2 result from the already-built current
facts.

### The edge is temporarily offline

The last confirmed safety state remains active. There is deliberately no
timeout-based automatic clear because losing contact with Tier 1 is not proof
that the physical danger ended.

### Events arrive late or are retried

Immutable events remain in history, but only a newer confirmed Tier 1 event may
advance the live safety state.

## Files changed

### Tier 1

- edge/tier1_kbs.py: keep low-battery and grid-outage situations active when no
  more commands are needed.
- edge/audit.py: align supported action statuses.
- edge/test_tier1_kbs.py: danger-persistence tests.
- edge/test_audit.py: transition test proving an empty command list does not
  cause a false clear.

### Backend and Tier 2 coordination

- apps/kbs/models.py: Tier1SafetyState and superseded action status.
- apps/kbs/migrations/0008_tier1_safety_interlock.py: schema and live-state
  backfill.
- apps/kbs/audit_views.py: ordered state advancement, stale-action
  supersession, result reconciliation, and terminal-state protection.
- apps/kbs/interlock.py: pure Tier 1 decision mirroring contract.
- apps/kbs/adapters/django.py: pre-decision gate and transactional recheck.
- apps/kbs/services.py: adapter decision hook.
- apps/kbs/views.py: API exposure, ACK protection, and simulator reset.
- apps/kbs/admin.py: read-only safety-state visibility.
- apps/kbs/test_interlock.py: Tier 2 bypass, parity, duplicate, and race tests.
- apps/kbs/test_audit_api.py: ingestion, ordering, clear, reconciliation, and
  action-lifecycle tests.
- apps/kbs/test_sim_api.py: state API, reset, and superseded ACK tests.

The existing apps/kbs/engine/rules.py and apps/kbs/engine/grouping.py are
unchanged.

## Deployment

Apply the migration before serving requests with the new code:

~~~bash
python manage.py migrate
~~~

Then restart the Django workers and any Celery workers that run Tier 2 cycles.
The edge bridge can be restarted independently.

The migration backfills safety state from existing Tier 1 audit history. After
deployment, confirm:

1. Tier1SafetyState exists for organizations with Tier 1 history.
2. Active rows match the latest confirmed Tier 1 situation.
3. Tier 2 interlock decisions use the tier1_interlock branch prefix.
4. Superseded actions no longer appear in pending_actions.
5. A Tier 1 clear is followed by a fresh normal Tier 2 cycle.

## Verification

The implemented test coverage includes:

- 24 dependency-free Tier 1 tests.
- 72 Django KBS tests.
- Django model/migration consistency checks.
- Django system checks.

Useful commands:

~~~bash
python3 -m unittest edge.test_tier1_kbs edge.test_audit edge.test_simulator_bridge
python manage.py test apps.kbs
python manage.py check
python manage.py makemigrations --check --dry-run
~~~

### Live development smoke test

Migration 0008 was applied to the populated development database. The
backfilled active inverter_overheat episode retained the historical OFF targets
for the AC, fridge, and event load. The AC had subsequently returned to ON.

A manual Tier 2 cycle then produced:

- branch tier1_interlock.inverter_overheat;
- no normal tier2.guard steps;
- a mirrored AC OFF action;
- already_satisfied no-ops for the fridge and event load.

This verifies that retained episode targets can correct a later unsafe state
instead of merely remembering that the original Tier 1 command once ran.

### Broader repository suite

The complete backend suite discovered 142 tests. The KBS changes did not fail
outside their own suite, but the repository-wide run was not fully green in the
current environment:

- seven breaker test groups could not start because TUYA_FERNET_KEY was not
  configured; and
- two organization email tests expected Arabic subject text while the current
  implementation returned English subjects.

These failures are outside the KBS/interlock files and were present in the
current repository configuration. The complete Tier 1 and Django KBS suites
listed above pass.

## Frontend simulator boundary

The backend now exposes the authoritative tier1_safety state and prevents a
superseded action from being revived in backend audit state. A frontend that
already copied an action into its own private deferred queue must also discard
that local action when its server action becomes superseded, or when a new Tier
1 episode starts.

No React source was changed in this backend/KBS branch. Therefore the backend
contract is complete, but updating the React deferred queue remains a separate
frontend integration task if the simulator must immediately consume this new
contract.

## Non-goals

- No third KBS was introduced.
- No Tier 2 grouping change was made.
- No normal Tier 2 threshold, branch, priority, or comfort rule was changed.
- No automatic time-based safety clear was added.
- No React simulator source was changed.
