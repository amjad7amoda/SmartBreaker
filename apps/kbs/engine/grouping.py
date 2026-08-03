"""Pure grouping plug-ins used by the dataclass decision engine."""


def importance_key(breaker):
    return (-breaker.category_rank, -breaker.priority_degree)


def first_group_within_headroom(candidates, headroom_W, motor_peak_minutes):
    group = []
    remaining_W = headroom_W
    for breaker in sorted(candidates, key=importance_key):
        draw_W = breaker.expected_draw_W(motor_peak_minutes)
        if draw_W <= remaining_W:
            group.append(breaker)
            remaining_W -= draw_W
    return group


def select_best_subset(candidates, budget_W, motor_peak_minutes):
    keep = []
    remaining_W = budget_W
    for breaker in sorted(candidates, key=importance_key):
        draw_W = breaker.expected_draw_W(motor_peak_minutes)
        if draw_W <= remaining_W:
            keep.append(breaker)
            remaining_W -= draw_W
    return keep
