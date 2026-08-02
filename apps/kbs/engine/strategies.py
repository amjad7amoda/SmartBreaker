"""Right-hand-side vocabulary of the rules.

An Experta rule matches facts and then *acts*. Everything a KBS rule can do —
emit a command, raise an alert, shed by priority, keep the affordable subset,
flip the AC-grid breaker — is one method here, so ``rules.py`` stays a
readable list of ``@Rule`` conditions and one-line consequences.

Why these live outside the rules: several KBS decisions are about the breaker
set *as a whole* (what fits in the inverter head-room, which subset survives a
power budget). Pattern matching works fact by fact and cannot express that, so
those decisions read the ``BreakerFact``s back out of working memory and do
plain set math on them. Nothing here touches the database or the clock.
"""

from .derived import graceful_countdown_s
from .facts import (
    AlertFact,
    CommandFact,
    DecisionFact,
    breaker_facts,
    grid_fact,
)
from .grouping import first_group_within_headroom, select_best_subset


class ActionStrategies:
    """Mixin of the consequences the KBS rules may apply."""

    # ------------------------------------------------------------------
    # working-memory access
    # ------------------------------------------------------------------

    def breakers(self):
        """Every BreakerFact currently in working memory (list[BreakerFact])."""
        return breaker_facts(self.facts.values())

    def commanded_ids(self):
        """Breaker pks this cycle already emitted a command for (set)."""
        return {
            f['breaker_id'] for f in self.facts.values()
            if isinstance(f, CommandFact)
        }

    def shed_order(self):
        """Currently-ON sheddable loads, least important first (list[BreakerFact]).

        Loads outside their user-configured usage window come first (the user
        is not using them right now anyway), then comfort before normal, and
        inside a category the lowest priority degree first. Mandatory, the
        AC-grid breaker, and event-required loads are never listed.
        """
        return sorted(
            [b for b in self.breakers() if b['switch'] and b['sheddable']],
            key=lambda b: (b['in_usage_window'], b['category_rank'], b['priority_degree']),
        )

    # ------------------------------------------------------------------
    # emitting conclusions
    # ------------------------------------------------------------------

    def take_branch(self, branch):
        """Commit this cycle to one decision-tree branch.

        Declaring the DecisionFact is what closes the tree: every competing
        branch rule carries ``NOT(DecisionFact())`` and leaves the agenda the
        moment this fires.
        """
        self.declare(DecisionFact(branch=branch))

    def command(self, breaker, action, reason, lockout=False, countdown_s=0):
        """Emit one switch command for a breaker.

        breaker:     the breaker to switch (BreakerFact)
        action:      target relay state: 'on' | 'off'
        reason:      why the KBS wants this switch (text)
        lockout:     True = lock the breaker until the user re-enables it (flag)
        countdown_s: 0 = switch now; >0 = arm the device countdown (s)
        """
        self.declare(CommandFact(
            breaker_id=breaker['id'], device_id=breaker['device_id'],
            action=action, reason=reason,
            lockout=lockout, countdown_s=countdown_s,
        ))

    def alert(self, kind, severity, message):
        """Raise one notification for the user.

        kind:     Alert.KIND_CHOICES code (text)
        severity: 'info' | 'warning' | 'critical'
        message:  human-readable description (text)
        """
        self.declare(AlertFact(kind=kind, severity=severity, message=message))

    def unavailable_alert(self, breaker, situation, severity='warning'):
        """Tell the user a breaker the KBS needs is offline or faulted.

        breaker:   the breaker that cannot be commanded (BreakerFact)
        situation: what the KBS wanted from it, e.g. 'due ON' (text)
        """
        state = f'faulted: {breaker["fault"]}' if breaker['fault'] else 'offline'  # why it cannot be commanded (text)
        self.alert(
            'breaker_fault', severity,
            f'Breaker {breaker["device_id"]} is {situation} but {state}.',
        )

    # ------------------------------------------------------------------
    # shedding / restoring strategies
    # ------------------------------------------------------------------

    def shed_all(self, reason, countdown_s=0):
        """Switch off every sheddable running load, least important first.

        countdown_s > 0 arms the device countdown instead of switching now, so
        the loads keep running for that grace period (used by battery safety).

        returns: the breakers that were commanded off (list[BreakerFact])
        """
        sheds = self.shed_order()
        for breaker in sheds:
            self.command(breaker, 'off', reason, countdown_s=countdown_s)
        return sheds

    def keep_best_subset(self, budget_W, reason):
        """Keep the most important loads that fit ``budget_W``, shed the rest.

        The draw of the untouchable loads (mandatory and event-required) is
        subtracted from the budget *before* the remaining loads compete for
        it, so protecting them is never up for auction.

        budget_W: power the system can sustainably supply this cycle (W)
        reason:   why the shed loads are being switched off (text)
        """
        protected_draw_W = sum(
            b['expected_draw_W'] for b in self.breakers()
            if b['switch'] and (b['priority_type'] == 'mandatory' or b['event_required'])
        )  # power the loads that may not be shed already consume (W)
        sheddable = self.shed_order()
        keep_ids = {
            b['id'] for b in
            select_best_subset(sheddable, max(budget_W - protected_draw_W, 0.0))
        }  # pks of the loads that stay ON (set)
        for breaker in sheddable:
            if breaker['id'] not in keep_ids:
                self.command(breaker, 'off', reason)

    def turn_on_within_headroom(self, headroom_W, candidates, reason):
        """Switch ON as many of ``candidates`` as the inverter head-room allows.

        Motor loads enter through their peak draw, so only the first group
        fits now; the rest follow on later cycles once earlier loads settle —
        this is what produces the staggered start.

        headroom_W: AC power the inverter can still supply on top of the current load (W)
        """
        for breaker in first_group_within_headroom(candidates, headroom_W):
            self.command(breaker, 'on', reason)

    def turn_on_due_comfort(self, headroom_W):
        """Switch ON the comfort breakers whose schedule window contains now.

        Unhealthy ones are skipped here; the ``comfort_breaker_unavailable``
        rule reports them to the user instead.
        """
        due = [
            b for b in self.breakers()
            if b['priority_type'] == 'comfort' and not b['switch']
            and not b['locked_out'] and b['in_schedule_window'] and b['healthy']
        ]  # comfort breakers that should be ON now and can be commanded (list[BreakerFact])
        self.turn_on_within_headroom(
            headroom_W, due, 'comfort schedule window and the system affords it',
        )

    def turn_on_event_required(self, headroom_W):
        """Switch ON the breakers a currently running event needs, within head-room.

        Event-required breakers are treated like mandatory loads for the whole
        event window: they are excluded from every shedding list, and here
        they are brought back if anything switched them off before the event.
        """
        already_commanded = self.commanded_ids()  # breakers this cycle already targets (set of pks)
        candidates = [
            b for b in self.breakers()
            if b['event_required'] and not b['switch'] and not b['locked_out']
            and b['healthy'] and b['id'] not in already_commanded
        ]  # event-required breakers that are OFF and can be commanded (list[BreakerFact])
        self.turn_on_within_headroom(
            headroom_W, candidates, 'required by the running scheduled event',
        )

    def switch_grid(self, on, reason):
        """Command the AC-grid breaker to the wanted state, if it exists and differs.

        on:     True = buy grid electricity, False = stop buying (flag)
        reason: why the grid state was chosen (text)
        """
        grid = grid_fact(self.facts.values())  # the site's AC-grid breaker (BreakerFact | None)
        if grid is None or grid['switch'] == on:
            return
        if on and not grid['healthy']:
            self.unavailable_alert(grid, 'needed ON', severity='critical')
        self.command(grid, 'on' if on else 'off', reason)

    def battery_countdown_s(self, system):
        """Seconds the sheddable loads may keep running before the battery
        protection countdown flips them OFF (s)."""
        return graceful_countdown_s(system['battery_buffer_Wh'], system['battery_draw_W'])
