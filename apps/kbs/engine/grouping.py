"""Breaker grouping / subset selection.

PLUG-IN POINT: the project owners designed their own algorithm for turning a
requested set of breakers on **gradually in groups** (steps of ~15-20 min) so
the inverter always tolerates the combined peak — especially motor loads that
draw ``peak_load_W`` for the first ``motor_peak_minutes``. When that algorithm
is delivered, it replaces the naive fallbacks in this module; the function
signatures below are the contract the rest of the engine relies on.
"""


def importance_key(breaker):
    """Sort key: most important breaker first (unitless tuple)."""
    return (-breaker.category_rank, -breaker.priority_degree)


def first_group_within_headroom(candidates, headroom_W, motor_peak_minutes):
    """The subset of ``candidates`` safe to switch ON right now (list of BreakerFacts).

    Each KBS cycle turns on only this first group; later groups follow on the
    next cycles once earlier motor loads settle from peak to mean draw, which
    naturally produces the staggered start.

    candidates:         breakers requested ON, most important considered first (BreakerFacts)
    headroom_W:         AC power the inverter can still supply on top of the current load (W)
    motor_peak_minutes: how long a motor load draws its peak after switch-on (min)
    """
    # TODO(user-algorithm): replace this greedy fill with the owners' grouping algorithm.
    group = []              # breakers accepted into the first group
    remaining_W = headroom_W  # head-room left while filling the group (W)
    for breaker in sorted(candidates, key=importance_key):
        draw_W = breaker.expected_draw_W(motor_peak_minutes)  # power this breaker will pull right after switch-on (W)
        if draw_W <= remaining_W:
            group.append(breaker)
            remaining_W -= draw_W
    return group


def select_best_subset(candidates, budget_W, motor_peak_minutes):
    """The best subset of ``candidates`` to KEEP running within a power budget (list of BreakerFacts).

    Used in power-saving situations: instead of buying grid electricity, keep
    the most important possible set of loads and shed the rest.

    candidates:         currently-ON sheddable breakers (BreakerFacts)
    budget_W:           power the system can sustainably supply to these loads (W)
    motor_peak_minutes: how long a motor load draws its peak after switch-on (min)
    """
    # TODO(user-algorithm): replace this greedy fill with the owners' algorithm.
    keep = []               # breakers that stay ON
    remaining_W = budget_W  # budget left while filling (W)
    for breaker in sorted(candidates, key=importance_key):
        draw_W = breaker.expected_draw_W(motor_peak_minutes)  # sustained draw of this breaker (W)
        if draw_W <= remaining_W:
            keep.append(breaker)
            remaining_W -= draw_W
    return keep
