# Tuya Breaker Integration

Record of the design decisions, implementation and field problems behind cloud
control of Tuya smart breakers. Written in English to match [arch.md](../arch.md).

---

## 1. Scope

Cloud-only control of Tuya WiFi breakers: registration, live readings, and
on/off switching through the Tuya IoT Platform.

**Explicitly out of scope for now:** local LAN control (`tinytuya`). It was
discussed and deferred — see §8.

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| Control channel | Tuya Cloud API | Serves Tier-2. Tier-1 needs local control, deferred. |
| Credential scope | One Tuya project **per organization** | Credentials scope every API call, so Tuya itself enforces device ownership. |
| Credential storage | Encrypted at rest (Fernet), separate table | The secret is replayed into every signature, so it cannot be hashed. |
| Device IP | Not stored | Site-local and discoverable by the Pi; meaningless server-side. |
| Verification on create | Yes, live call to Tuya | A breaker row that does not correspond to a real device is worthless. |
| Offline device on create | Accepted with a warning | An unpowered breaker is still a legitimate registration. |
| Switching permission | Technician, admin, **and** organization owner | It is the owner's own electricity. |
| `protected` flag | Does **not** block manual switching | Reserved for KBS logic. |
| `device_id` / `organization` | Immutable after creation | Changing either describes a different physical device. |

### Why per-organization credentials close a security hole for free

`client_id` + `client_secret` do not just authenticate — they *scope* the
request. Asking Tuya for a device outside that project returns `1106 permission
deny`. So a user cannot register another organization's breaker even knowing
its `device_id`, with no ownership check written on our side.

**Cost:** every customer must create their own Tuya IoT project and extract
their keys. Accepted deliberately; heavy for a non-technical home user.

## 3. What was built

| File | Role |
|---|---|
| `apps/breakers/crypto.py` | Fernet encrypt/decrypt for credentials at rest |
| `apps/breakers/models.py` | `TuyaCredential` (OneToOne → Organization) |
| `apps/breakers/tuya.py` | Signing, token cache, error classification, HTTP calls |
| `apps/breakers/services.py` | Status reading with unit scaling, switch + confirmation |
| `apps/breakers/serializers.py` | Validation, live Tuya verification on create |
| `apps/breakers/views.py` | Mixin-based views |
| `apps/breakers/exceptions.py` | Tuya failure → HTTP status mapping |
| `apps/breakers/management/commands/` | `tuya_check`, `tuya_set_secret`, `tuya_verify_sign` |

Settings: `TUYA_FERNET_KEY` in `.env`. Dependencies added: `cryptography`,
`requests`. `apps/__init__.py` was added — its absence silently broke the test
loader while leaving Django itself working.

### Endpoints

```
GET    POST                     /api/breakers/tuya-credentials/
GET    PATCH  PUT  DELETE       /api/breakers/tuya-credentials/<pk>/
GET    POST                     /api/breakers/
GET    PATCH  PUT  DELETE       /api/breakers/<device_id>/
GET                             /api/breakers/<device_id>/status/     ?raw=1
POST                            /api/breakers/<device_id>/switch/     {"state":"on"|"off"}
POST                            /api/breakers/<device_id>/child-lock/ {"enabled":true|false}
```

Credentials are technician/admin only, even for reading. `client_secret` is
write-only and never appears in any response. Breaker reads are open to any
authenticated user but scoped: a stranger gets **404, not 403**, so the API
does not reveal that a device exists.

## 4. Non-obvious implementation details

**Signing.** `sign = HMAC-SHA256(client_id + access_token + t + str_to_sign, secret)`
where `str_to_sign = METHOD \n sha256(body) \n \n path`. The `access_token`
term is omitted when requesting the token itself. The secret is never
transmitted — it is only the HMAC key.

**Body bytes must match signed bytes.** The request body is serialised once and
sent verbatim. Passing the dict to `requests`' `json=` re-serialises it with
different spacing and invalidates the signature.

**Tuya returns HTTP 200 on failure.** Errors live in `success: false` with a
numeric `code`. `raise_for_status()` catches nothing. Classification decides
whether a failure is the caller's fault (400) or ours (5xx); collapsing them
makes production debugging impossible.

**Unit scaling is never guessed.** Scales come from
`/v1.0/devices/{id}/specifications` (cached 24h). If unavailable, raw integers
are returned with `units_resolved: false`. Current is additionally converted
from milliamps — applying scale alone would report `4500 A` instead of `4.5 A`.

> **Rule for the KBS: never base a shedding decision on a reading with
> `units_resolved: false`.** A factor-of-10 error in `power_W` would shed the
> wrong loads.

**Command acknowledgement is not confirmation.** Tuya returning `success` means
the command was accepted for delivery, not that the relay moved. `set_switch`
reads the device back (one retry after 0.6s) and reports `confirmed`. Treating
acceptance as success would make the system believe it had shed a load that is
still running.

## 5. Child lock is a full lockout

Verified on the hardware, and contrary to what the name suggests: enabling
`child_lock` **opens the relay and refuses every command** — at the panel and
over the API — until it is released. It de-energises the load.

Consequences baked into the code:

- Enabling it returns a `warning` saying the load is now off.
- A `/switch/` call that fails while the lock is engaged returns a `reason`
  explaining why, instead of an unexplained `confirmed: false`.
- No attempt is made to restore the relay after locking. An earlier version did
  exactly that; it fought the hardware, and reported `relay_restored: true`
  when nothing had been restored.
- `Breaker.child_lock` mirrors the device and is re-synced on every status read,
  so a change made in the Smart Life app is picked up. It is read-only over
  `PATCH` — the action endpoint is the only way to change it.

**This is not a permission mechanism.** Restricting who may switch a breaker is
a server-side concern, unrelated to this flag.

## 6. Field problems and their causes

| Symptom | Actual cause |
|---|---|
| `1004 sign invalid` on every request | A 64-char value was stored as the secret. Tuya's Access Secret is 32 chars; the 64-char hex value was a `sign` from an earlier curl. |
| `2008 command or value not support` | The write used the code a *read* reports (`switch_1`) instead of the code the instruction set accepts (`switch`). The endpoint was fine; it was misdiagnosed as an endpoint problem first. |
| `40000001 properties is empty` | Consequence of that misdiagnosis: switching to the v2.0 thing endpoint while still sending `switch_1`, which Tuya parsed, found unwritable, and dropped — leaving nothing. |

**Read and write codes are not the same.** This device reports `switch_1` in its
shadow but only accepts `switch` as an instruction. That asymmetry cannot be
inferred from a read, which is why `tuya_check` now prints the writable codes
from `/v1.0/iot-03/devices/{id}/functions`. Always check that list before
assuming a code.

All three were data mismatches, not logic errors. `1004` also legitimately
occurs from clock skew (Tuya rejects requests more than a few minutes off) and
from a region mismatch, which is why `tuya_check --debug` reports clock skew.

**Limit of mocked tests:** the `2008` failure passed every test, because mocks
do not know which endpoints Tuya actually accepts. Mocked integration tests
verify our logic, never the contract.

## 7. Diagnostics

```bash
python manage.py tuya_check --organization 1 --device <id> [--debug]
python manage.py tuya_set_secret --organization 1
python manage.py tuya_verify_sign --organization 1 --path "..." --t ... \
    --access-token ... --expected <sign from a known-good request>
```

`tuya_verify_sign` recomputes the signature of a request Tuya already accepted.
A match proves both the stored secret and the signing code are correct — it
separates "wrong data" from "wrong algorithm" without exposing the secret.

`tuya_check` prints the codes the device accepts for writing, which cannot be
inferred from a read.

## 8. Open items

1. **No technician ↔ organization link.** Any technician can configure and
   switch any organization's breakers. Acceptable for a graduation project, a
   real hole for a multi-customer product.
2. **Query parameters are not sorted when signing.** No effect today (zero or
   one parameter); will break the first multi-parameter endpoint.
3. **No pagination** on breaker lists.
4. **Local control (Tier-1) not started.** Requires `local_key` + device IP per
   breaker and a `BreakerDriver` abstraction so Tuya specifics do not leak into
   the rules engine.
5. **`arch.md` §7 trigger 2 is not achievable with this hardware.** Tuya
   breakers hold their last state indefinitely and do not revert to a safe
   state when the Pi's heartbeat stops. Needs rewording plus a Pi-side watchdog.
6. **Device discovery endpoint** (`/discover/`) not built — would let a
   technician pick from the organization's Tuya devices instead of typing a
   22-character `device_id`.
7. **`SECRET_KEY` is overwritten** in [config/settings/base.py](../config/settings/base.py)
   — read from env, then replaced by the insecure default on the next line.
   Unrelated to Tuya but found along the way.

## 9. Tests

`python manage.py test apps.breakers` — 32 tests covering role permissions,
organization scoping, secret non-exposure, create-time verification, error
classification, partial updates, unit scaling, spec caching, switch
confirmation, and child-lock behaviour.
