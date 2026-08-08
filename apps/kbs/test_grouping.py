"""Focused tests for the breaker startup grouping policies."""

from math import nan
from unittest import TestCase
from unittest.mock import patch

from .engine import grouping
from .engine.facts import BreakerFacts


MOTOR_PEAK_MINUTES = 20


def make_breaker(
    breaker_id,
    priority,
    normal_W,
    peak_W=None,
    *,
    priority_type='comfort',
):
    return BreakerFacts(
        id=breaker_id,
        device_id=f'b{breaker_id}',
        priority_type=priority_type,
        priority_degree=priority,
        load_type='motor' if peak_W is not None else 'normal',
        peak_load_W=peak_W,
        mean_load_W=normal_W,
        cycle_start=None,
        cycle_end=None,
        switch=False,
        online=True,
        fault='',
        locked_out=False,
        recently_tripped=False,
        event_required=False,
        cur_power_W=None,
        minutes_since_on=None,
    )


def selected_ids(breakers):
    return [breaker.id for breaker in breakers]


class ExactStartupGroupingTests(TestCase):

    def test_exact_dp_reconstructs_minimum_groups_and_priority_orders_them(self):
        breakers = [
            make_breaker(1, priority=1, normal_W=1.0, peak_W=6.0),
            make_breaker(2, priority=9, normal_W=1.0, peak_W=6.0),
            make_breaker(3, priority=5, normal_W=1.0, peak_W=6.0),
        ]
        profiles = grouping._load_profiles(breakers, MOTOR_PEAK_MINUTES)

        groups = grouping._exact_startup_groups(profiles, 10.0)

        # No two 6 W startup loads fit together, while all three fit when
        # started separately and allowed to settle to 1 W.
        self.assertEqual(len(groups), 3)
        self.assertEqual(
            [group[0].breaker.priority_degree for group in groups],
            [9, 5, 1],
        )
        self.assertCountEqual(
            [profile.breaker.id for group in groups for profile in group],
            [1, 2, 3],
        )
        self.assertEqual(
            selected_ids(grouping.first_group_within_headroom(
                breakers, 10.0, MOTOR_PEAK_MINUTES,
            )),
            [2],
        )

    def test_exact_dp_packs_breakers_into_two_groups(self):
        breakers = [
            make_breaker(1, priority=9, normal_W=1.0, peak_W=6.0),
            make_breaker(2, priority=5, normal_W=1.0, peak_W=4.0),
            make_breaker(3, priority=1, normal_W=1.0, peak_W=4.0),
        ]
        profiles = grouping._load_profiles(breakers, MOTOR_PEAK_MINUTES)

        groups = grouping._exact_startup_groups(profiles, 10.0)

        self.assertEqual(len(groups), 2)
        self.assertCountEqual(
            [profile.breaker.id for group in groups for profile in group],
            [1, 2, 3],
        )
        self.assertEqual(max(item.peak_W for item in profiles), 6.0)
        self.assertEqual(groups[0][0].breaker.id, 1)

    def test_infeasible_complete_plan_still_returns_one_safe_group(self):
        breakers = [
            make_breaker(1, priority=9, normal_W=4.0, peak_W=10.0),
            make_breaker(2, priority=1, normal_W=4.0, peak_W=10.0),
        ]
        profiles = grouping._load_profiles(breakers, MOTOR_PEAK_MINUTES)
        self.assertIsNone(grouping._exact_startup_groups(profiles, 10.0))

        selected = grouping.first_group_within_headroom(
            breakers, 10.0, MOTOR_PEAK_MINUTES,
        )

        self.assertEqual(selected_ids(selected), [1])
        self.assertLessEqual(
            sum(breaker.expected_draw_W(MOTOR_PEAK_MINUTES) for breaker in selected),
            10.0,
        )

    def test_peak_is_never_allowed_below_normal_draw(self):
        breaker = make_breaker(1, priority=1, normal_W=100.0, peak_W=50.0)

        selected = grouping.first_group_within_headroom(
            [breaker], 75.0, MOTOR_PEAK_MINUTES,
        )

        self.assertEqual(selected, [])

    def test_empty_invalid_and_individually_oversized_inputs(self):
        oversized = make_breaker(1, priority=1, normal_W=1.0, peak_W=101.0)

        self.assertEqual(
            grouping.first_group_within_headroom([], 100.0, MOTOR_PEAK_MINUTES),
            [],
        )
        self.assertEqual(
            grouping.first_group_within_headroom(
                [oversized], nan, MOTOR_PEAK_MINUTES,
            ),
            [],
        )
        self.assertEqual(
            grouping.first_group_within_headroom(
                [oversized], 100.0, MOTOR_PEAK_MINUTES,
            ),
            [],
        )


class HeuristicStartupGroupingTests(TestCase):

    def test_medium_state_space_uses_priority_sum_knapsack(self):
        breakers = [
            make_breaker(1, priority=10, normal_W=4.0),
            make_breaker(2, priority=9, normal_W=6.0),
            make_breaker(3, priority=8, normal_W=3.0),
            make_breaker(4, priority=7, normal_W=3.0),
        ]
        breakers.extend(
            make_breaker(index, priority=1, normal_W=10.0)
            for index in range(5, 17)
        )

        with (
            patch.object(
                grouping,
                '_priority_knapsack_group',
                wraps=grouping._priority_knapsack_group,
            ) as knapsack,
            patch.object(
                grouping,
                '_greedy_group',
                wraps=grouping._greedy_group,
            ) as greedy,
        ):
            selected = grouping.first_group_within_headroom(
                breakers, 10.0, MOTOR_PEAK_MINUTES,
            )

        # Greedy would take breakers 1 + 2 (priority sum 19). The knapsack
        # anchors breaker 1, then finds breakers 3 + 4 (priority sum 25).
        self.assertEqual(selected_ids(selected), [1, 3, 4])
        knapsack.assert_called_once()
        greedy.assert_not_called()

    def test_large_priority_state_space_falls_back_to_greedy(self):
        breakers = [
            make_breaker(
                index,
                priority=grouping.GROUPING_WORK_LIMIT,
                normal_W=1.0,
            )
            for index in range(1, 17)
        ]

        with (
            patch.object(
                grouping,
                '_priority_knapsack_group',
                wraps=grouping._priority_knapsack_group,
            ) as knapsack,
            patch.object(
                grouping,
                '_greedy_group',
                wraps=grouping._greedy_group,
            ) as greedy,
        ):
            selected = grouping.first_group_within_headroom(
                breakers, 3.0, MOTOR_PEAK_MINUTES,
            )

        self.assertEqual(len(selected), 3)
        knapsack.assert_not_called()
        greedy.assert_called_once()

    def test_exact_algorithm_is_used_at_fifteen_breakers(self):
        breakers = [
            make_breaker(index, priority=1, normal_W=1.0)
            for index in range(1, 16)
        ]

        with patch.object(
            grouping,
            '_exact_startup_groups',
            wraps=grouping._exact_startup_groups,
        ) as exact:
            selected = grouping.first_group_within_headroom(
                breakers, 15.0, MOTOR_PEAK_MINUTES,
            )

        self.assertEqual(len(selected), 15)
        exact.assert_called_once()
