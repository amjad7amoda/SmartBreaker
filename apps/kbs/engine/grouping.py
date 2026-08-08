"""Pure grouping plug-ins used by the dataclass decision engine."""

from dataclasses import dataclass
from math import inf, isfinite, isnan


EXACT_GROUPING_MAX_BREAKERS = 15
# Keep the medium-size heuristic within the same approximate work budget as
# the exact algorithm at its largest supported input.
GROUPING_WORK_LIMIT = EXACT_GROUPING_MAX_BREAKERS * (1 << EXACT_GROUPING_MAX_BREAKERS)


@dataclass(frozen=True)
class _LoadProfile:
    breaker: object
    normal_W: float
    peak_W: float
    priority_points: int
    input_index: int


def importance_key(breaker):
    return (-breaker.category_rank, -breaker.priority_degree)


def _breaker_order_key(breaker):
    """Importance order with a stable breaker identity tie-break."""
    return (*importance_key(breaker), str(breaker.device_id), breaker.id)


def _watts(value, *, missing=None):
    if value is None:
        return missing
    try:
        value = float(value)
    except (TypeError, ValueError):
        return inf
    if not isfinite(value):
        return inf
    return max(value, 0.0)


def _capacity_W(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if isnan(value):
        return None
    return max(value, 0.0)


def _priority_points(breaker):
    try:
        return max(int(breaker.priority_degree), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _load_profiles(candidates, motor_peak_minutes):
    profiles = []
    for input_index, breaker in enumerate(candidates):
        # Candidates passed here are OFF in the normal engine flow, so
        # expected_draw_W is their startup/peak draw. When a learned mean is
        # unavailable, conservatively assume that draw never settles lower.
        peak_W = _watts(breaker.expected_draw_W(motor_peak_minutes), missing=0.0)
        normal_W = _watts(breaker.mean_load_W, missing=peak_W)
        peak_W = max(peak_W, normal_W)
        profiles.append(_LoadProfile(
            breaker=breaker,
            normal_W=normal_W,
            peak_W=peak_W,
            priority_points=_priority_points(breaker),
            input_index=input_index,
        ))
    return profiles


def _fits(load_W, capacity_W):
    if not isfinite(load_W):
        return False
    if capacity_W == inf:
        return True
    tolerance = 1e-9 * max(1.0, abs(capacity_W))
    return load_W <= capacity_W + tolerance


def _profile_order_key(profile):
    return (*_breaker_order_key(profile.breaker), profile.input_index)


def _order_groups_by_priority(groups):
    """Put the group containing the most important breaker first."""
    for group in groups:
        group.sort(key=_profile_order_key)
    groups.sort(key=lambda group: tuple(_profile_order_key(item) for item in group))
    return groups


def _exact_startup_groups(profiles, capacity_W):
    """Return a minimum-group plan, or None if all loads cannot run.

    A DP state stores the minimum group count for a breaker mask and, among
    states with that count, the minimum peak-minus-normal draw of its current
    group. Parent pointers retain both membership and group boundaries.

    The resulting groups are priority ordered before being returned. The
    public helper applies only the first group and replans on the next engine
    cycle against measured headroom; it does not execute this list as a
    static schedule.
    """
    count = len(profiles)
    if count == 0:
        return []

    state_count = 1 << count
    normal_sum_W = [0.0] * state_count
    for mask in range(1, state_count):
        bit = mask & -mask
        index = bit.bit_length() - 1
        normal_sum_W[mask] = normal_sum_W[mask ^ bit] + profiles[index].normal_W

    unreachable_groups = count + 1
    group_count = [unreachable_groups] * state_count
    last_extra_W = [inf] * state_count
    parent = [None] * state_count

    # The empty state uses a fictional empty group. The first appended
    # breaker therefore creates group 1 without a special-case transition.
    group_count[0] = 1
    last_extra_W[0] = 0.0

    def relax(next_mask, groups, extra_W, previous_mask, index, starts_group):
        current_groups = group_count[next_mask]
        current_extra_W = last_extra_W[next_mask]
        tolerance = 1e-9 * max(1.0, abs(capacity_W)) if capacity_W != inf else 0.0
        if (
            groups < current_groups
            or (groups == current_groups and extra_W < current_extra_W - tolerance)
        ):
            group_count[next_mask] = groups
            last_extra_W[next_mask] = extra_W
            parent[next_mask] = (previous_mask, index, starts_group)

    for mask in range(state_count):
        if group_count[mask] == unreachable_groups:
            continue
        for index, profile in enumerate(profiles):
            bit = 1 << index
            if mask & bit:
                continue

            next_mask = mask | bit
            extra_W = profile.peak_W - profile.normal_W

            current_group_load_W = (
                normal_sum_W[mask] + last_extra_W[mask] + profile.peak_W
            )
            if _fits(current_group_load_W, capacity_W):
                relax(
                    next_mask,
                    group_count[mask],
                    last_extra_W[mask] + extra_W,
                    mask,
                    index,
                    False,
                )

            new_group_load_W = normal_sum_W[mask] + profile.peak_W
            if _fits(new_group_load_W, capacity_W):
                relax(
                    next_mask,
                    group_count[mask] + 1,
                    extra_W,
                    mask,
                    index,
                    True,
                )

    full_mask = state_count - 1
    if group_count[full_mask] == unreachable_groups:
        return None

    reversed_steps = []
    mask = full_mask
    while mask:
        previous_mask, index, starts_group = parent[mask]
        reversed_steps.append((index, starts_group))
        mask = previous_mask

    groups = [[]]
    for index, starts_group in reversed(reversed_steps):
        if starts_group:
            groups.append([])
        groups[-1].append(profiles[index])

    return _order_groups_by_priority(groups)


def _knapsack_work(profiles):
    priority_sum = sum(profile.priority_points for profile in profiles)
    return len(profiles) * (priority_sum + 1)


def _priority_knapsack_group(profiles, capacity_W):
    """Choose one priority-first group using a bounded 0/1 knapsack.

    The most important feasible breaker is always included. For the
    remaining room, least_peak_W[priority_sum] stores the minimum peak
    draw that obtains each exact priority-degree sum. A Python-int bitset
    reconstructs the chosen breakers without an O(n * priority_sum) table.
    """
    ordered = sorted(profiles, key=_profile_order_key)
    if not ordered:
        return []

    anchor = ordered[0]
    remaining_capacity_W = capacity_W - anchor.peak_W
    items = ordered[1:]
    priority_sum = sum(item.priority_points for item in items)
    least_peak_W = [inf] * (priority_sum + 1)
    chosen_bits = [0] * (priority_sum + 1)
    least_peak_W[0] = 0.0

    reachable_priority = 0
    for index, item in enumerate(items):
        points = item.priority_points
        if points <= 0:
            continue
        next_reachable = reachable_priority + points
        for score in range(next_reachable, points - 1, -1):
            previous_score = score - points
            previous_peak_W = least_peak_W[previous_score]
            if previous_peak_W == inf:
                continue
            candidate_peak_W = previous_peak_W + item.peak_W
            if (
                _fits(candidate_peak_W, remaining_capacity_W)
                and candidate_peak_W < least_peak_W[score]
            ):
                least_peak_W[score] = candidate_peak_W
                chosen_bits[score] = chosen_bits[previous_score] | (1 << index)
        reachable_priority = next_reachable

    best_score = max(
        score
        for score, peak_W in enumerate(least_peak_W)
        if _fits(peak_W, remaining_capacity_W)
    )
    selected = [anchor]
    selected.extend(
        item
        for index, item in enumerate(items)
        if (chosen_bits[best_score] >> index) & 1
    )

    # Zero-priority breakers do not appear in the DP dimension. Preserve the
    # old greedy helper's behavior by filling any that still fit.
    used_peak_W = sum(item.peak_W for item in selected)
    selected_indexes = {item.input_index for item in selected}
    for item in items:
        if item.input_index in selected_indexes or item.priority_points > 0:
            continue
        if _fits(used_peak_W + item.peak_W, capacity_W):
            selected.append(item)
            selected_indexes.add(item.input_index)
            used_peak_W += item.peak_W

    return sorted(selected, key=_profile_order_key)


def _greedy_group(profiles, capacity_W):
    """The original importance-ordered first-fit policy."""
    group = []
    used_peak_W = 0.0
    for profile in sorted(profiles, key=_profile_order_key):
        if _fits(used_peak_W + profile.peak_W, capacity_W):
            group.append(profile)
            used_peak_W += profile.peak_W
    return group


def first_group_within_headroom(candidates, headroom_W, motor_peak_minutes):
    """Return the next breaker group using one of three bounded policies.

    * Up to 15 candidates: exact minimum-group subset DP with reconstruction.
    * Larger inputs within GROUPING_WORK_LIMIT: priority-sum knapsack.
    * Larger state spaces: the original importance-ordered greedy policy.

    The return value intentionally remains the flat list consumed by the
    decision engine; full exact groups are reconstructed only to select the
    highest-priority group for this cycle.
    """
    candidates = list(candidates)
    capacity_W = _capacity_W(headroom_W)
    if not candidates or capacity_W is None:
        return []

    profiles = _load_profiles(candidates, motor_peak_minutes)
    feasible = [profile for profile in profiles if _fits(profile.peak_W, capacity_W)]
    if not feasible:
        return []

    if len(profiles) <= EXACT_GROUPING_MAX_BREAKERS:
        groups = _exact_startup_groups(feasible, capacity_W)
        if groups:
            selected = groups[0]
        else:
            # All breakers may fit individually while no complete startup
            # order exists. One safe priority-first group is still useful.
            selected = _greedy_group(feasible, capacity_W)
    elif _knapsack_work(feasible) <= GROUPING_WORK_LIMIT:
        selected = _priority_knapsack_group(feasible, capacity_W)
    else:
        selected = _greedy_group(feasible, capacity_W)

    if not _fits(sum(profile.peak_W for profile in selected), capacity_W):
        return []

    return [profile.breaker for profile in selected]


def select_best_subset(candidates, budget_W, motor_peak_minutes):
    keep = []
    remaining_W = budget_W
    for breaker in sorted(candidates, key=importance_key):
        draw_W = breaker.expected_draw_W(motor_peak_minutes)
        if draw_W <= remaining_W:
            keep.append(breaker)
            remaining_W -= draw_W
    return keep
