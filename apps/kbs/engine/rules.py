"""The knowledge base of the main KBS: the project flowchart written as
Experta production rules.

There is no control flow here. Every cycle the gathered facts are declared
into working memory, and the rules whose conditions match compete on the
agenda; the KBS is whatever these rules say it is.

Three mechanisms replace what used to be an ``if``/``else`` tree:

* **salience** — conflict resolution. Inverter protection outranks battery
  protection, which outranks everyday branch selection, which outranks the
  follow-up rules that run once a branch is settled.
* **DecisionFact** — mutual exclusion. Every branch rule carries
  ``NOT(DecisionFact())`` and declares one when it fires, so exactly one
  branch is taken per cycle and all competitors leave the agenda.
* **TEST / joins** — the numeric and cross-fact conditions of the chart
  (PV vs. load, reserve vs. need, "the culprit breaker is sheddable").

Branch codes (stored on ``KBSDecision.branch``):
    protect_inverter                  heat/joule-deficit emergency shedding
    protect_battery                   battery near its voltage floor -> countdown shutdown
    day.surplus.comfort_on            PV covers the loads -> scheduled comfort ON
    day.battery_stable.comfort_on     battery above threshold -> scheduled comfort ON
    day.deficit.power_saving          PV short, saving mode -> keep best subset
    day.deficit.buy_grid              PV short, no saving -> AC-grid breaker ON
    day.deficit.grid_out.shed         grid tried but delivers nothing -> keep it ON, shed by priority
    day.sudden_drop.battery_ok        sudden PV drop, battery rides it through
    day.sudden_drop.power_saving      sudden PV drop, saving mode -> best subset
    day.sudden_drop.buy_grid          sudden PV drop -> AC-grid breaker ON
    day.sudden_drop.grid_out.shed     same grid fallback on the sudden-drop path
    night.calm.battery                quiet night -> run from battery
    night.sudden_draw.battery_ok      reserve still covers mandatory until morning
    night.sudden_draw.trip            saving mode -> trip the culprit breaker
    night.sudden_draw.buy_grid        reserve short -> AC-grid breaker ON
    night.sudden_draw.grid_out.shed   same grid fallback at night
"""

from experta import AS, MATCH, NOT, OR, TEST, KnowledgeEngine, L, Rule

from .facts import BreakerFact, DecisionFact, SystemFact
from .results import RuleResult
from .strategies import ActionStrategies

# Salience tiers — the engine's conflict-resolution order.
PROTECT_INVERTER = 100  # hardware at risk: outranks everything
PROTECT_BATTERY = 90    # the bank must never reach its voltage floor
DIAGNOSE = 60           # annotate the cycle (PV-drop cause) before a branch is chosen
PREFER_TRIP = 55        # night: trip the culprit rather than buy grid electricity
BRANCH = 50             # everyday branch selection
FOLLOW_UP = 10          # runs after the branch is settled

# The same message wherever the state grid is closed but dead.
GRID_OUTAGE_ALERT = (
    'grid_outage', 'critical',
    'AC-grid breaker is ON but the grid delivers no power. '
    'Shedding comfort/normal loads by priority until the grid returns.',
)


class SmartBreakerKBS(ActionStrategies, KnowledgeEngine):
    """The site's knowledge base: one instance decides one cycle."""

    # ==================================================================
    # 1. protection — hardware first, before any energy strategy
    # ==================================================================

    @Rule(
        OR(AS.system << SystemFact(heat_high=True),
           AS.system << SystemFact(deficit_high=True)),
        NOT(DecisionFact()),
        salience=PROTECT_INVERTER,
    )
    def protect_inverter(self, system):
        """Heatsink too hot or too much cumulative deficit: shed everything
        non-mandatory now and let the grid feed what is left."""
        self.take_branch('protect_inverter')
        self.shed_all('emergency shed: inverter protection')
        self.switch_grid(
            True, 'feed remaining loads from grid while the inverter recovers')
        self.alert(
            'inverter_protection', 'critical',
            f'Inverter protection engaged: heatsink {system["heatsink_temp_C"]} degC, '
            f'joule deficit {system["joule_deficit_J"]:.0f} J. Non-mandatory loads shed.',
        )

    @Rule(
        AS.system << SystemFact(battery_low=True),
        NOT(DecisionFact()),
        salience=PROTECT_BATTERY,
    )
    def protect_battery(self, system):
        """Bank near its voltage floor: schedule a graceful countdown shutdown.

        Nothing is cut instantly — every sheddable running load gets its device
        countdown armed so it flips OFF after the site has spent at most
        ``battery_buffer_Wh`` more energy, and the user is told what will go
        off and when.
        """
        self.take_branch('protect_battery')
        countdown_s = self.battery_countdown_s(system)  # delay before the scheduled switch-off (s)
        sheds = self.shed_all(
            'battery safety: scheduled shutdown to protect the battery',
            countdown_s=countdown_s,
        )
        if not system['power_saving']:
            self.switch_grid(True, 'battery near its voltage floor: grid takes over')
        if sheds:
            message = (
                f'Battery at {system["battery_voltage_V"]} V is close to its protection floor. '
                f'These breakers will switch off in ~{countdown_s // 60} min for battery safety: '
                f'{", ".join(b["device_id"] for b in sheds)}.'
            )
        else:
            message = (
                f'Battery at {system["battery_voltage_V"]} V is close to its protection floor and '
                f'only mandatory loads are still running — nothing left to shed.'
            )
        self.alert('battery_low', 'critical', message)

    # ==================================================================
    # 2. diagnosis — explain a sudden PV drop before acting on it
    # ==================================================================

    @Rule(
        SystemFact(is_daytime=True, sudden_pv_drop=True, season='summer',
                   pv_power_W=MATCH.pv_W, pv_baseline_W=MATCH.baseline_W),
        NOT(DecisionFact()),
        salience=DIAGNOSE,
    )
    def diagnose_panel_fault(self, pv_W, baseline_W):
        """A summer sky does not explain a PV collapse — suspect the hardware."""
        self.alert(
            'panel_fault', 'warning',
            f'Sudden PV drop in summer ({baseline_W:.0f} W -> {pv_W:.0f} W): '
            f'possible panel fault or shading on the panel.',
        )

    @Rule(
        SystemFact(is_daytime=True, sudden_pv_drop=True,
                   season=MATCH.season & ~L('summer'),
                   pv_power_W=MATCH.pv_W, pv_baseline_W=MATCH.baseline_W,
                   weather_condition=MATCH.condition),
        NOT(DecisionFact()),
        salience=DIAGNOSE,
    )
    def diagnose_weather_drop(self, pv_W, baseline_W, season, condition):
        """Outside summer, clouds and storms are the likely cause."""
        self.alert(
            'weather_drop', 'info',
            f'Sudden PV drop in {season} ({baseline_W:.0f} W -> {pv_W:.0f} W): '
            f'most likely weather ({condition or "cloud/storm"}).',
        )

    # ==================================================================
    # 3. daytime, production steady
    # ==================================================================

    @Rule(
        AS.system << SystemFact(is_daytime=True, sudden_pv_drop=False,
                                pv_power_W=MATCH.pv_W, mean_load_on_W=MATCH.load_W),
        TEST(lambda pv_W, load_W: pv_W > load_W),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def day_surplus_comfort_on(self, system, **_):
        """The panels out-produce the running loads: spend the surplus on comfort."""
        self.take_branch('day.surplus.comfort_on')
        self.turn_on_due_comfort(system['headroom_W'])
        self.switch_grid(False, 'PV/battery cover the loads')

    @Rule(
        AS.system << SystemFact(is_daytime=True, sudden_pv_drop=False, battery_stable=True,
                                pv_power_W=MATCH.pv_W, mean_load_on_W=MATCH.load_W),
        TEST(lambda pv_W, load_W: pv_W <= load_W),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def day_battery_stable_comfort_on(self, system, **_):
        """PV alone is short, but the battery is above its threshold: comfort still runs."""
        self.take_branch('day.battery_stable.comfort_on')
        self.turn_on_due_comfort(system['headroom_W'])
        self.switch_grid(False, 'PV/battery cover the loads')

    @Rule(
        AS.system << SystemFact(is_daytime=True, sudden_pv_drop=False, battery_stable=False,
                                power_saving=True,
                                pv_power_W=MATCH.pv_W, mean_load_on_W=MATCH.load_W),
        TEST(lambda pv_W, load_W: pv_W <= load_W),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def day_deficit_power_saving(self, system, **_):
        """PV short and battery low, but the user prefers saving over buying:
        keep the most important loads PV can carry."""
        self.take_branch('day.deficit.power_saving')
        self.keep_best_subset(system['pv_power_W'],
                              'power saving: outside the affordable subset')
        self.switch_grid(False, 'power saving: no grid purchase')

    @Rule(
        SystemFact(is_daytime=True, sudden_pv_drop=False, battery_stable=False,
                   power_saving=False, grid_failed=False,
                   pv_power_W=MATCH.pv_W, mean_load_on_W=MATCH.load_W),
        TEST(lambda pv_W, load_W: pv_W <= load_W),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def day_deficit_buy_grid(self, **_):
        """PV short and battery low: buy state-grid electricity."""
        self.take_branch('day.deficit.buy_grid')
        self.switch_grid(True, 'PV short and battery below threshold')

    @Rule(
        AS.system << SystemFact(is_daytime=True, sudden_pv_drop=False, battery_stable=False,
                                power_saving=False, grid_failed=True,
                                pv_power_W=MATCH.pv_W, mean_load_on_W=MATCH.load_W),
        TEST(lambda pv_W, load_W: pv_W <= load_W),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def day_deficit_grid_out_shed(self, system, **_):
        """The grid breaker is closed but the state grid delivers nothing:
        waiting for it would only drain the battery, so shed by priority."""
        self.take_branch('day.deficit.grid_out.shed')
        self.keep_best_subset(system['pv_power_W'], 'grid outage: outside the affordable subset')
        self.alert(*GRID_OUTAGE_ALERT)

    # ==================================================================
    # 4. daytime, production suddenly collapsed
    # ==================================================================

    @Rule(
        SystemFact(is_daytime=True, sudden_pv_drop=True, battery_stable=True),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def day_sudden_drop_battery_ok(self):
        """The battery is charged enough to ride the drop out."""
        self.take_branch('day.sudden_drop.battery_ok')
        self.switch_grid(False, 'battery rides through the PV drop')

    @Rule(
        AS.system << SystemFact(is_daytime=True, sudden_pv_drop=True,
                                battery_stable=False, power_saving=True),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def day_sudden_drop_power_saving(self, system):
        """Drop plus a thin battery, saving mode: shrink to what PV still carries."""
        self.take_branch('day.sudden_drop.power_saving')
        self.keep_best_subset(system['pv_power_W'],
                              'power saving: outside the affordable subset')
        self.switch_grid(False, 'power saving: no grid purchase')

    @Rule(
        SystemFact(is_daytime=True, sudden_pv_drop=True,
                   battery_stable=False, power_saving=False, grid_failed=False),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def day_sudden_drop_buy_grid(self):
        """Drop plus a thin battery: buy state-grid electricity."""
        self.take_branch('day.sudden_drop.buy_grid')
        self.switch_grid(True, 'PV dropped and battery below threshold')

    @Rule(
        AS.system << SystemFact(is_daytime=True, sudden_pv_drop=True,
                                battery_stable=False, power_saving=False, grid_failed=True),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def day_sudden_drop_grid_out_shed(self, system):
        """Same fallback as the deficit path when the state grid is out."""
        self.take_branch('day.sudden_drop.grid_out.shed')
        self.keep_best_subset(system['pv_power_W'], 'grid outage: outside the affordable subset')
        self.alert(*GRID_OUTAGE_ALERT)

    # ==================================================================
    # 5. night — the mandatory loads must reach the morning
    # ==================================================================

    @Rule(
        SystemFact(is_daytime=False, sudden_draw=False,
                   battery_remaining_Wh=MATCH.reserve_Wh, mandatory_need_Wh=MATCH.need_Wh),
        TEST(lambda reserve_Wh, need_Wh: reserve_Wh >= need_Wh),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def night_calm_reserve_safe(self, **_):
        """Quiet night with enough charge left: the grid is not needed."""
        self.take_branch('night.calm.battery')
        self.switch_grid(False, 'night: battery covers the reserve, grid not needed')

    @Rule(
        SystemFact(is_daytime=False, sudden_draw=False,
                   battery_remaining_Wh=MATCH.reserve_Wh, mandatory_need_Wh=MATCH.need_Wh),
        TEST(lambda reserve_Wh, need_Wh: reserve_Wh < need_Wh),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def night_calm_reserve_short(self, **_):
        """Quiet night but the reserve is thin: leave the grid breaker as it is —
        if it is ON and delivering it keeps relieving the battery."""
        self.take_branch('night.calm.battery')

    @Rule(
        SystemFact(is_daytime=False, sudden_draw=True,
                   battery_remaining_Wh=MATCH.reserve_Wh, mandatory_need_Wh=MATCH.need_Wh),
        TEST(lambda reserve_Wh, need_Wh: reserve_Wh >= need_Wh),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def night_sudden_draw_battery_ok(self, **_):
        """Something switched on hard, but the reserve still reaches morning."""
        self.take_branch('night.sudden_draw.battery_ok')
        self.switch_grid(False, 'reserve still covers mandatory loads until morning')

    @Rule(
        SystemFact(is_daytime=False, sudden_draw=True, power_saving=True,
                   battery_remaining_Wh=MATCH.reserve_Wh, mandatory_need_Wh=MATCH.need_Wh,
                   sudden_draw_culprit_id=MATCH.culprit_id),
        AS.culprit << BreakerFact(id=MATCH.culprit_id,
                                  priority_type=L('normal') | L('comfort'),
                                  recently_tripped=False),
        TEST(lambda reserve_Wh, need_Wh: reserve_Wh < need_Wh),
        NOT(DecisionFact()),
        salience=PREFER_TRIP,
    )
    def night_sudden_draw_trip(self, culprit, **_):
        """Saving mode, the reserve is short and the load behind the jump is
        sheddable and was not re-enabled tonight: trip it instead of buying power."""
        self.take_branch('night.sudden_draw.trip')
        self.command(culprit, 'off',
                     'night sudden draw endangers the morning reserve', lockout=True)
        self.alert(
            'night_trip', 'warning',
            f'Breaker {culprit["device_id"]} tripped: its sudden draw endangers the '
            f'mandatory night reserve. Re-enable it manually to override.',
        )
        self.switch_grid(False, 'power saving: culprit tripped instead of buying grid')

    @Rule(
        SystemFact(is_daytime=False, sudden_draw=True, grid_failed=False,
                   battery_remaining_Wh=MATCH.reserve_Wh, mandatory_need_Wh=MATCH.need_Wh),
        TEST(lambda reserve_Wh, need_Wh: reserve_Wh < need_Wh),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def night_sudden_draw_buy_grid(self, **_):
        """Reserve short and nothing worth tripping: buy state-grid electricity."""
        self.take_branch('night.sudden_draw.buy_grid')
        self.switch_grid(True, 'night reserve short for mandatory loads until morning')

    @Rule(
        AS.system << SystemFact(is_daytime=False, sudden_draw=True, grid_failed=True,
                                battery_remaining_Wh=MATCH.reserve_Wh,
                                mandatory_need_Wh=MATCH.need_Wh),
        TEST(lambda reserve_Wh, need_Wh: reserve_Wh < need_Wh),
        NOT(DecisionFact()),
        salience=BRANCH,
    )
    def night_sudden_draw_grid_out_shed(self, system, **_):
        """Same fallback as the daytime paths when the state grid is out."""
        self.take_branch('night.sudden_draw.grid_out.shed')
        self.keep_best_subset(system['pv_power_W'], 'grid outage: outside the affordable subset')
        self.alert(*GRID_OUTAGE_ALERT)

    # ==================================================================
    # 6. follow-up — applies to whatever branch was taken
    # ==================================================================

    @Rule(
        AS.system << SystemFact(),
        DecisionFact(branch=MATCH.branch),
        TEST(lambda branch: not branch.startswith('protect_')),
        salience=FOLLOW_UP,
    )
    def event_required_breakers_on(self, system, **_):
        """A running scheduled event gets its breakers ON, within head-room.

        Skipped during the protection branches: there the hardware, not the
        user's calendar, decides.
        """
        self.turn_on_event_required(system['headroom_W'])

    @Rule(
        DecisionFact(branch=MATCH.branch),
        AS.breaker << BreakerFact(priority_type='comfort', switch=False, locked_out=False,
                                  in_schedule_window=True, healthy=False),
        TEST(lambda branch: branch.endswith('comfort_on')),
        salience=FOLLOW_UP,
    )
    def comfort_breaker_unavailable(self, breaker, **_):
        """A comfort load is due by its schedule but the device cannot be commanded."""
        self.unavailable_alert(breaker, 'due ON')

    @Rule(
        DecisionFact(branch=MATCH.branch),
        AS.breaker << BreakerFact(event_required=True, switch=False,
                                  locked_out=False, healthy=False),
        TEST(lambda branch: not branch.startswith('protect_')),
        salience=FOLLOW_UP,
    )
    def event_breaker_unavailable(self, breaker, **_):
        """A running event needs this load but the device cannot be commanded."""
        self.unavailable_alert(breaker, 'required by a scheduled event')


def decide(facts):
    """Run one decision cycle over a gathered snapshot.

    facts: the fact list from ``gather_facts`` — one SystemFact plus one
           BreakerFact per breaker (list[Fact])

    returns: the branch taken, the switch commands and the alerts (RuleResult)
    """
    engine = SmartBreakerKBS()
    engine.reset()
    engine.declare(*facts)
    engine.run()
    return RuleResult.from_working_memory(engine.facts.values())
