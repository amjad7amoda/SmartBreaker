"""Tier-1 safety KBS — runs locally on the Raspberry Pi, next to the inverter.

This is the fast, deterministic half of the two-tier design: it reacts to the
critical situations that cannot wait for the server (ms/seconds latency) and
keeps working with **no internet**. The server's Tier-2 engine
(``apps.kbs.engine``) still owns everything else: comfort schedules, events,
weather, learning, optimisation.

Deliberately dependency-free (no Django, no DB, stdlib only) so it can run on a
Pi as a plain systemd service and be unit-tested anywhere.

Situations handled here — and only these:
  1. inverter overheating / overload      -> immediate shed by priority
  2. battery at its voltage floor         -> countdown shutdown (graceful)
  3. AC-grid breaker ON but grid is dead  -> keep it ON, shed by priority
  4. battery empty                        -> immediate shed to the mandatory core

Everything Tier-1 does is also reachable by Tier-2; Tier-1 exists purely to act
sooner. Both tiers share the same priority semantics: mandatory loads are never
shed, comfort goes before normal, and lower priority degree goes first.
"""

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# configuration (synced from the server ruleset; safe defaults for offline boot)
# ---------------------------------------------------------------------------


@dataclass
class Tier1Config:
    """Thresholds the local engine acts on. Mirrors the matching KBSSettings fields."""

    heatsink_temp_limit_C: float = 70.0            # heatsink temperature above which loads are shed immediately (°C)
    max_inverter_power_W: float = 5000.0           # maximum continuous AC output the inverter tolerates (W)
    overload_fraction: float = 1.05                # load/rating ratio above which the inverter counts as overloaded (fraction)
    battery_low_voltage_V: float = 24.0            # bank voltage floor that must never be reached (V)
    battery_low_margin_V: float = 0.5              # act this far above the floor (V)
    battery_critical_margin_V: float = 0.1         # below floor + this, skip the countdown and shed at once (V)
    battery_capacity_Wh: float = 5000.0            # usable battery energy at 100% (Wh)
    battery_shutdown_buffer_percent: float = 2.0   # energy the site may still spend before the countdown fires (% of capacity)
    grid_present_min_V: float = 100.0              # grid voltage at/above which the state grid counts as delivering (V)
    charge_current_idle_A: float = 0.5             # charge current at/below which the bank counts as "not charging" (A)
    countdown_min_s: int = 60                      # shortest countdown, so the user gets warning time (s)
    countdown_max_s: int = 3600                    # longest useful countdown (s)


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------


@dataclass
class BreakerState:
    """One breaker as the Pi sees it on the local network."""

    device_id: str                 # hardware identifier (unitless)
    priority_type: str             # 'mandatory' | 'normal' | 'comfort' | 'ac_grid'
    priority_degree: int = 1       # importance inside the category; higher = more important (unitless)
    switch: bool = False           # relay position: True = ON (flag)
    online: bool = True            # reachable on the local network (flag)
    cur_power_W: float = 0.0       # instantaneous draw (W)

    CATEGORY_RANK = {'comfort': 1, 'normal': 2, 'mandatory': 3}

    @property
    def category_rank(self):
        """3=mandatory, 2=normal, 1=comfort, 0=ac_grid (unitless)."""
        return self.CATEGORY_RANK.get(self.priority_type, 0)

    @property
    def sheddable(self):
        """True when Tier-1 is allowed to switch this breaker off (flag)."""
        return self.switch and self.online and self.priority_type in ('comfort', 'normal')


@dataclass
class InverterState:
    """The latest inverter snapshot read over the serial console."""

    ac_output_active_power_W: float = 0.0      # total AC load (W)
    heatsink_temp_C: float = 25.0              # heatsink temperature (°C)
    battery_voltage_V: float = 26.0            # bank voltage (V)
    battery_capacity_percent: float = 100.0    # state of charge (% of capacity)
    battery_charge_current_A: float = 0.0      # current flowing into the bank (A)
    battery_discharge_current_A: float = 0.0   # current drawn from the bank (A)
    grid_voltage_V: float = 0.0                # voltage sensed on the grid input (V)
    pv_charging_power_W: float = 0.0           # PV power the inverter harvests (W)


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------


@dataclass
class Command:
    """One switch command Tier-1 wants applied to a breaker right now."""

    device_id: str        # target breaker (unitless)
    action: str           # 'on' | 'off'
    countdown_s: int = 0  # 0 = switch immediately; >0 = arm the device countdown (s)
    reason: str = ''      # why Tier-1 issued it (text)


@dataclass
class Tier1Result:
    """Outcome of one local evaluation."""

    situation: str                                # code of the situation that fired; '' = nothing to do (text)
    commands: list = field(default_factory=list)  # switch commands to apply (list[Command])
    notify: str = ''                              # message to buffer for the server/user (text)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def shed_order(breakers):
    """Sheddable running loads, least important first (list[BreakerState]).

    Comfort before normal; inside a category the lowest priority degree first.
    Mandatory loads and the AC-grid breaker are never included.
    """
    return sorted(
        [b for b in breakers if b.sheddable],
        key=lambda b: (b.category_rank, b.priority_degree),
    )


def grid_breaker(breakers):
    """The site's AC-grid breaker, or None if the site has none."""
    return next((b for b in breakers if b.priority_type == 'ac_grid'), None)


def graceful_countdown_s(buffer_Wh, draw_W, cfg):
    """Seconds a breaker may keep running before its countdown flips it OFF (s).

    Sized so the battery only spends ``buffer_Wh`` meanwhile. Mirrors the
    server-side ``graceful_countdown_s`` so both tiers behave identically.
    """
    if draw_W <= 0:
        return cfg.countdown_max_s
    return int(min(max(buffer_Wh / draw_W * 3600.0, cfg.countdown_min_s), cfg.countdown_max_s))


def _shed_all(breakers, reason):
    """Immediate OFF commands for every sheddable load, least important first."""
    return [
        Command(device_id=b.device_id, action='off', countdown_s=0, reason=reason)
        for b in shed_order(breakers)
    ]


def _shed_until_within(breakers, target_W, current_load_W, reason):
    """Shed the least important loads until the load fits ``target_W``.

    Stops as soon as the estimated remaining load is within the target, so a
    mild overload does not black out the whole site.
    """
    commands = []              # OFF commands issued (list[Command])
    remaining_W = current_load_W  # estimated load after the shedding so far (W)
    for b in shed_order(breakers):
        if remaining_W <= target_W:
            break
        commands.append(Command(
            device_id=b.device_id, action='off', countdown_s=0, reason=reason,
        ))
        remaining_W -= max(b.cur_power_W, 0.0)
    return commands


# ---------------------------------------------------------------------------
# the local decision function
# ---------------------------------------------------------------------------


def evaluate(inverter, breakers, cfg=None):
    """Evaluate the local safety rules once and return a ``Tier1Result``.

    Pure: no clock, no network, no state. Call it on every reading (~1 Hz).
    Situations are checked hardware-danger first; the first match wins.

    inverter: the latest InverterState read from the console
    breakers: current BreakerState of every breaker on the site
    cfg:      Tier1Config thresholds; defaults are used when omitted
    """
    cfg = cfg or Tier1Config()
    load_W = inverter.ac_output_active_power_W                      # total AC load (W)
    charging = inverter.battery_charge_current_A > cfg.charge_current_idle_A  # bank is being charged (flag)
    v_bat = inverter.battery_voltage_V                              # bank voltage (V)

    # ---- 1. inverter overheating --------------------------------------------
    if inverter.heatsink_temp_C >= cfg.heatsink_temp_limit_C:
        return Tier1Result(
            situation='inverter_overheat',
            commands=_shed_all(breakers, 'tier1: inverter overheating'),
            notify=(
                f'Tier-1: heatsink at {inverter.heatsink_temp_C} °C reached the limit '
                f'({cfg.heatsink_temp_limit_C} °C). Non-mandatory loads shed locally.'
            ),
        )

    # ---- 2. inverter overload ----------------------------------------------
    overload_limit_W = cfg.max_inverter_power_W * cfg.overload_fraction  # load above which the inverter is overloaded (W)
    if load_W >= overload_limit_W:
        return Tier1Result(
            situation='inverter_overload',
            commands=_shed_until_within(
                breakers, cfg.max_inverter_power_W, load_W, 'tier1: inverter overload',
            ),
            notify=(
                f'Tier-1: load {load_W:.0f} W exceeded the inverter limit '
                f'({cfg.max_inverter_power_W:.0f} W). Loads shed by priority.'
            ),
        )

    # ---- 3. battery at/below its floor -------------------------------------
    # A charging bank recovers on its own, so protection only applies while it
    # is not charging.
    if not charging and v_bat <= cfg.battery_low_voltage_V + cfg.battery_critical_margin_V:
        # Already at the floor: no time left for a countdown.
        return Tier1Result(
            situation='battery_critical',
            commands=_shed_all(breakers, 'tier1: battery at its protection floor'),
            notify=(
                f'Tier-1: battery at {v_bat:.2f} V reached its floor '
                f'({cfg.battery_low_voltage_V} V). Non-mandatory loads shed immediately.'
            ),
        )

    if not charging and v_bat <= cfg.battery_low_voltage_V + cfg.battery_low_margin_V:
        # Approaching the floor: schedule a graceful shutdown so the user is
        # warned and loads stop in an orderly way.
        buffer_Wh = cfg.battery_shutdown_buffer_percent / 100.0 * cfg.battery_capacity_Wh  # energy still allowed (Wh)
        draw_W = v_bat * inverter.battery_discharge_current_A or max(load_W - inverter.pv_charging_power_W, 0.0)  # discharge power (W)
        countdown_s = graceful_countdown_s(buffer_Wh, draw_W, cfg)  # delay before switch-off (s)
        sheds = shed_order(breakers)
        if not sheds:
            return Tier1Result(situation='')
        return Tier1Result(
            situation='battery_low',
            commands=[
                Command(device_id=b.device_id, action='off', countdown_s=countdown_s,
                        reason='tier1: battery safety countdown')
                for b in sheds
            ],
            notify=(
                f'Tier-1: battery at {v_bat:.2f} V is close to its floor. '
                f'{len(sheds)} breaker(s) will switch off in ~{countdown_s // 60} min: '
                f'{", ".join(b.device_id for b in sheds)}.'
            ),
        )

    # ---- 4. grid breaker closed but the state grid is dead ------------------
    # The breaker stays ON so supply resumes by itself the moment the grid
    # returns; meanwhile the site must live on PV/battery, so loads are shed
    # by priority. Only acted on while the battery cannot comfortably carry
    # the load — otherwise Tier-2 handles it at its own pace.
    grid = grid_breaker(breakers)
    if grid is not None and grid.switch and inverter.grid_voltage_V < cfg.grid_present_min_V:
        battery_thin = (not charging) and v_bat <= cfg.battery_low_voltage_V + 2 * cfg.battery_low_margin_V  # bank cannot carry this alone for long (flag)
        if battery_thin:
            sheds = _shed_until_within(
                breakers, max(inverter.pv_charging_power_W, 0.0), load_W,
                'tier1: grid outage while the battery is low',
            )
            if sheds:
                return Tier1Result(
                    situation='grid_outage',
                    commands=sheds,
                    notify=(
                        'Tier-1: the AC-grid breaker is closed but the grid delivers no power '
                        f'({inverter.grid_voltage_V:.0f} V) and the battery is low. '
                        'Loads shed by priority; the grid breaker stays ON to resume automatically.'
                    ),
                )

    return Tier1Result(situation='')  # nothing critical: Tier-2 is in charge
