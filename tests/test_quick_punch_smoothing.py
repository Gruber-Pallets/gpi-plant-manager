from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from zira_dashboard.assignment_windows import WorkSegment
from zira_dashboard.quick_punch_smoothing import (
    QUICK_PUNCH_LIMIT,
    production_times_from_samples,
    smooth_quick_punches,
)

CT = ZoneInfo("America/Chicago")

# Meter data saying no station made a pallet, so the wrong-first-pick rule is
# live for the existing cases.
NO_PALLETS = {f"Dismantler {n}": () for n in range(1, 5)} | {
    f"Repair {n}": () for n in range(1, 6)
}


def smooth(segments, **kwargs):
    kwargs.setdefault("production_times_by_wc", NO_PALLETS)
    return smooth_quick_punches(segments, **kwargs)


def ct(hour, minute=0, second=0):
    return datetime(2026, 9, 18, hour, minute, second, tzinfo=CT).astimezone(UTC)


def seg(wc, start, end, *, person="Christian C.", odoo_id=8):
    return WorkSegment(wc, person, start, end, "odoo", odoo_id)


def shape(segments):
    return [
        (segment.person_name, segment.wc_name, segment.start_utc, segment.end_utc)
        for segment in segments
    ]


def test_limit_is_five_minutes():
    assert QUICK_PUNCH_LIMIT == timedelta(minutes=5)


def test_empty_input_stays_empty():
    assert smooth(()) == ()


def test_christians_detour_on_2026_09_18_becomes_one_dismantler_3_stint():
    now = ct(14, 25, 53)
    segments = (
        seg("Dismantler 3", ct(7), ct(7, 2, 10)),
        seg("Dismantler 2", ct(7, 2, 10), ct(7, 4, 27)),
        seg("Dismantler 3", ct(7, 4, 27), ct(11)),
        seg("Dismantler 3", ct(11, 30), now),
    )

    assert shape(smooth(segments)) == [
        ("Christian C.", "Dismantler 3", ct(7), ct(11)),
        ("Christian C.", "Dismantler 3", ct(11, 30), now),
    ]


def test_lunch_split_is_not_bridged():
    segments = (
        seg("Dismantler 2", ct(7), ct(11), person="Jose C.", odoo_id=24),
        seg("Dismantler 2", ct(11, 30), ct(14, 25), person="Jose C.", odoo_id=24),
    )

    assert smooth(segments) == segments


def test_same_station_gap_is_bridged_up_to_exactly_the_limit():
    for gap_seconds, expected_count in ((299, 1), (300, 1), (301, 2)):
        back_at = ct(9) + timedelta(seconds=gap_seconds)
        result = smooth(
            (seg("Repair 1", ct(8), ct(9)), seg("Repair 1", back_at, ct(10)))
        )
        assert len(result) == expected_count, gap_seconds

    (bridged,) = smooth(
        (seg("Repair 1", ct(8), ct(9)), seg("Repair 1", ct(9, 5), ct(10)))
    )
    assert (bridged.start_utc, bridged.end_utc) == (ct(8), ct(10))


def test_detour_through_several_stations_inside_the_limit_is_absorbed():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(9, 1)),
        seg("Repair 3", ct(9, 1), ct(9, 3)),
        seg("Repair 1", ct(9, 4), ct(10)),
    )

    assert shape(smooth(segments)) == [
        ("Christian C.", "Repair 1", ct(8), ct(10)),
    ]


def test_detour_longer_than_the_limit_is_kept():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(9, 6)),
        seg("Repair 1", ct(9, 6), ct(10)),
    )

    assert smooth(segments) == segments


def test_wrong_first_pick_at_start_of_day_joins_the_next_station():
    segments = (
        seg("Dismantler 1", ct(7), ct(7, 3)),
        seg("Dismantler 4", ct(7, 3), ct(11)),
    )

    assert shape(smooth(segments)) == [
        ("Christian C.", "Dismantler 4", ct(7), ct(11)),
    ]


def test_wrong_first_pick_after_lunch_joins_the_next_station_and_fills_the_gap():
    segments = (
        seg("Dismantler 4", ct(7), ct(11)),
        seg("Dismantler 1", ct(11, 30), ct(11, 32)),
        seg("Dismantler 4", ct(11, 34), ct(15, 30)),
    )

    assert shape(smooth(segments)) == [
        ("Christian C.", "Dismantler 4", ct(7), ct(11)),
        ("Christian C.", "Dismantler 4", ct(11, 30), ct(15, 30)),
    ]


def test_chain_of_first_picks_settles_on_the_final_station_within_the_limit():
    segments = (
        seg("Repair 1", ct(7), ct(7, 1)),
        seg("Repair 2", ct(7, 1), ct(7, 3)),
        seg("Repair 3", ct(7, 3), ct(11)),
    )

    assert shape(smooth(segments)) == [
        ("Christian C.", "Repair 3", ct(7), ct(11)),
    ]


def test_chain_of_first_picks_stops_once_the_combined_stint_passes_the_limit():
    segments = (
        seg("Repair 1", ct(7), ct(7, 3)),
        seg("Repair 2", ct(7, 3), ct(7, 6)),
        seg("Repair 3", ct(7, 6), ct(11)),
    )

    assert shape(smooth(segments)) == [
        ("Christian C.", "Repair 2", ct(7), ct(7, 6)),
        ("Christian C.", "Repair 3", ct(7, 6), ct(11)),
    ]


def test_mid_day_short_stop_between_two_different_stations_is_kept():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(9, 2)),
        seg("Repair 3", ct(9, 2), ct(10)),
    )

    assert smooth(segments) == segments


def test_came_back_rule_runs_before_wrong_first_pick():
    # Wrong-first-pick first would give D2 07:00-07:06, D3 07:06-09:00.
    segments = (
        seg("Dismantler 3", ct(7), ct(7, 2)),
        seg("Dismantler 2", ct(7, 2), ct(7, 6)),
        seg("Dismantler 3", ct(7, 6), ct(9)),
    )

    assert shape(smooth(segments)) == [
        ("Christian C.", "Dismantler 3", ct(7), ct(9)),
    ]


def test_live_detour_waits_until_the_person_comes_back():
    at_0703 = (
        seg("Dismantler 3", ct(7), ct(7, 2, 10)),
        seg("Dismantler 2", ct(7, 2, 10), ct(7, 3)),
    )
    assert shape(smooth(at_0703)) == [
        ("Christian C.", "Dismantler 2", ct(7), ct(7, 3)),
    ]

    mid_detour_after_real_work = (
        seg("Dismantler 3", ct(7), ct(9)),
        seg("Dismantler 2", ct(9), ct(9, 2)),
    )
    assert smooth(mid_detour_after_real_work) == mid_detour_after_real_work


def test_other_people_are_never_changed_and_order_is_preserved():
    bob = seg("Repair 3", ct(7), ct(15), person="Bob T.", odoo_id=6)
    christian_before = seg("Repair 1", ct(8), ct(9))
    ana = seg("Repair 2", ct(9), ct(9, 2), person="Ana M.", odoo_id=5)
    christian_after = seg("Repair 1", ct(9, 2), ct(10))

    result = smooth((bob, christian_before, ana, christian_after))

    assert len(result) == 3
    assert result[0] is bob
    assert shape(result[1:2]) == [("Christian C.", "Repair 1", ct(8), ct(10))]
    assert result[2] is ana


def test_same_name_with_different_odoo_ids_stays_separate():
    segments = (
        seg("Repair 1", ct(8), ct(9), person="Jose O.", odoo_id=31),
        seg("Repair 1", ct(9, 1), ct(10), person="Jose O.", odoo_id=32),
    )

    assert smooth(segments) == segments


def test_blocked_windows_stop_both_rules():
    came_back = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 1", ct(9, 3), ct(10)),
    )
    assert (
        smooth(came_back, blocked_windows={8: ((ct(9, 1), ct(9, 2)),)})
        == came_back
    )

    first_pick = (
        seg("Repair 1", ct(7), ct(7, 2)),
        seg("Repair 2", ct(7, 3), ct(11)),
    )
    assert (
        smooth(first_pick, blocked_windows={8: ((ct(7, 2), ct(7, 3)),)})
        == first_pick
    )


def test_blocked_window_that_only_touches_the_gap_does_not_block():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 1", ct(9, 3), ct(10)),
    )

    (bridged,) = smooth(
        segments, blocked_windows={8: ((ct(8, 50), ct(9)),)}
    )
    assert (bridged.start_utc, bridged.end_utc) == (ct(8), ct(10))


def test_detour_that_runs_past_the_return_is_left_alone():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(11)),
        seg("Repair 1", ct(9, 2), ct(10)),
    )

    assert smooth(segments) == segments


@pytest.mark.parametrize(("stint_seconds", "merged"), ((300, True), (301, False)))
def test_wrong_first_pick_counts_a_stint_of_exactly_the_limit_as_quick(
    stint_seconds, merged
):
    picked_until = ct(7) + timedelta(seconds=stint_seconds)
    segments = (
        seg("Repair 1", ct(7), picked_until),
        seg("Repair 2", picked_until, ct(11)),
    )

    result = smooth(segments)

    if merged:
        assert shape(result) == [("Christian C.", "Repair 2", ct(7), ct(11))]
    else:
        assert result == segments


@pytest.mark.parametrize(("gap_seconds", "merged"), ((300, True), (301, False)))
def test_wrong_first_pick_bridges_a_gap_of_exactly_the_limit(gap_seconds, merged):
    next_start = ct(7, 2) + timedelta(seconds=gap_seconds)
    segments = (
        seg("Repair 1", ct(7), ct(7, 2)),
        seg("Repair 2", next_start, ct(11)),
    )

    result = smooth(segments)

    if merged:
        assert shape(result) == [("Christian C.", "Repair 2", ct(7), ct(11))]
    else:
        assert result == segments


@pytest.mark.parametrize(("away_seconds", "merged"), ((300, False), (301, True)))
def test_wrong_first_pick_needs_away_time_longer_than_the_limit(away_seconds, merged):
    back_at = ct(11) + timedelta(seconds=away_seconds)
    picked_until = back_at + timedelta(minutes=2)
    segments = (
        seg("Repair 5", ct(7), ct(11)),
        seg("Repair 1", back_at, picked_until),
        seg("Repair 2", picked_until, ct(15)),
    )

    result = smooth(segments)

    if merged:
        assert shape(result) == [
            ("Christian C.", "Repair 5", ct(7), ct(11)),
            ("Christian C.", "Repair 2", back_at, ct(15)),
        ]
    else:
        assert result == segments


def test_came_back_rechecks_from_the_merged_stint():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(9, 2)),
        seg("Repair 1", ct(9, 3), ct(9, 4)),
        seg("Repair 3", ct(9, 4), ct(9, 5)),
        seg("Repair 1", ct(9, 6), ct(10)),
    )

    assert shape(smooth(segments)) == [
        ("Christian C.", "Repair 1", ct(8), ct(10)),
    ]


def test_blocked_windows_only_apply_to_their_own_person():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 1", ct(9, 3), ct(10)),
    )

    (bridged,) = smooth(
        segments, blocked_windows={6: ((ct(9, 1), ct(9, 2)),)}
    )
    assert (bridged.start_utc, bridged.end_utc) == (ct(8), ct(10))


def test_person_without_odoo_id_is_grouped_and_blocked_by_name():
    segments = (
        seg("Repair 1", ct(8), ct(9), person="Temp W.", odoo_id=None),
        seg("Repair 1", ct(9, 2), ct(10), person="Temp W.", odoo_id=None),
    )

    assert shape(smooth(segments)) == [
        ("Temp W.", "Repair 1", ct(8), ct(10)),
    ]
    assert (
        smooth(
            segments, blocked_windows={"Temp W.": ((ct(9, 0, 30), ct(9, 1)),)}
        )
        == segments
    )


def test_in_between_stint_that_overlaps_the_current_stint_stops_came_back():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(8, 10), ct(9, 2)),
        seg("Repair 1", ct(9, 3), ct(10)),
    )

    assert smooth(segments) == segments


FIRST_PICK = (
    seg("Dismantler 1", ct(7), ct(7, 3)),
    seg("Dismantler 4", ct(7, 3), ct(11)),
)


def test_short_first_stint_that_made_pallets_is_real_work_and_is_kept():
    result = smooth_quick_punches(
        FIRST_PICK,
        production_times_by_wc={"Dismantler 1": (ct(7, 2),), "Dismantler 4": ()},
    )

    assert result == FIRST_PICK


@pytest.mark.parametrize(
    ("pallet_at", "kept"),
    (
        (ct(7), True),  # the stint's first instant is inside it
        (ct(7, 3), False),  # the stint's last instant belongs to the next stint
    ),
)
def test_pallet_timing_matches_how_credit_picks_a_stint(pallet_at, kept):
    result = smooth_quick_punches(
        FIRST_PICK,
        production_times_by_wc={"Dismantler 1": (pallet_at,), "Dismantler 4": ()},
    )

    if kept:
        assert result == FIRST_PICK
    else:
        assert shape(result) == [("Christian C.", "Dismantler 4", ct(7), ct(11))]


def test_pallets_at_a_different_station_do_not_keep_the_first_pick():
    result = smooth_quick_punches(
        FIRST_PICK,
        production_times_by_wc={
            "Dismantler 1": (ct(6, 59), ct(7, 3)),
            "Dismantler 4": (ct(7), ct(7, 1), ct(7, 2)),
        },
    )

    assert shape(result) == [("Christian C.", "Dismantler 4", ct(7), ct(11))]


def test_unknown_meter_data_keeps_the_first_pick_but_still_bridges_came_back():
    assert smooth_quick_punches(FIRST_PICK) == FIRST_PICK
    assert smooth_quick_punches(FIRST_PICK, production_times_by_wc=None) == FIRST_PICK
    assert (
        smooth_quick_punches(FIRST_PICK, production_times_by_wc={"Dismantler 4": ()})
        == FIRST_PICK
    )

    christians_detour = (
        seg("Dismantler 3", ct(7), ct(7, 2, 10)),
        seg("Dismantler 2", ct(7, 2, 10), ct(7, 4, 27)),
        seg("Dismantler 3", ct(7, 4, 27), ct(11)),
    )
    assert shape(smooth_quick_punches(christians_detour, production_times_by_wc=None)) == [
        ("Christian C.", "Dismantler 3", ct(7), ct(11)),
    ]


def test_production_times_from_samples_keeps_only_pallet_timestamps():
    assert production_times_from_samples(
        {
            "Repair 1": [(ct(7), 3), (ct(7, 1), 0), (ct(7, 2), -1.0), (ct(7, 3), 0.5)],
            "Repair 2": [],
            "Repair 3": iter([(ct(8), 0)]),
        }
    ) == {
        "Repair 1": (ct(7), ct(7, 3)),
        "Repair 2": (),
        "Repair 3": (),
    }


DETOUR = (
    seg("Repair 1", ct(8), ct(9)),
    seg("Repair 2", ct(9), ct(9, 2)),
    seg("Repair 1", ct(9, 3), ct(10)),
)
DETOUR_MERGED = [("Christian C.", "Repair 1", ct(8), ct(10))]


def with_repair_2_pallets(*times):
    return NO_PALLETS | {"Repair 2": tuple(times)}


def test_detour_that_made_pallets_nobody_else_covers_is_kept():
    assert smooth(DETOUR, production_times_by_wc=with_repair_2_pallets(ct(9, 1))) == DETOUR


def test_detour_pallets_another_person_covers_still_merge():
    bob = seg("Repair 2", ct(8, 30), ct(9, 30), person="Bob T.", odoo_id=6)

    result = smooth((*DETOUR, bob), production_times_by_wc=with_repair_2_pallets(ct(9, 1)))

    assert shape(result) == [*DETOUR_MERGED, ("Bob T.", "Repair 2", ct(8, 30), ct(9, 30))]


@pytest.mark.parametrize(
    ("pallet_at", "kept"),
    (
        (ct(8, 59), False),  # before the detour
        (ct(9), True),  # the detour's first instant
        (ct(9, 1, 59), True),
        (ct(9, 2), False),  # the detour's end belongs to what comes next
        (ct(9, 2, 30), False),  # in the sign-out gap, not the detour
    ),
)
def test_orphaned_pallet_must_fall_inside_the_detour(pallet_at, kept):
    result = smooth(DETOUR, production_times_by_wc=with_repair_2_pallets(pallet_at))

    if kept:
        assert result == DETOUR
    else:
        assert shape(result) == DETOUR_MERGED


@pytest.mark.parametrize(
    ("bob_start", "bob_end", "kept"),
    (
        (ct(9, 1), ct(9, 30), False),  # covers the pallet from its first instant
        (ct(8, 30), ct(9, 1), True),  # left the station as the pallet landed
    ),
)
def test_another_persons_cover_is_start_inclusive_and_end_exclusive(
    bob_start, bob_end, kept
):
    bob = seg("Repair 2", bob_start, bob_end, person="Bob T.", odoo_id=6)

    result = smooth((*DETOUR, bob), production_times_by_wc=with_repair_2_pallets(ct(9, 1)))

    if kept:
        assert result == (*DETOUR, bob)
    else:
        assert shape(result) == [*DETOUR_MERGED, shape((bob,))[0]]


def test_one_uncovered_pallet_keeps_a_blip_even_when_another_is_covered():
    # Bob covers the 9:00:30 pallet but has left before the 9:01:30 one.
    bob = seg("Repair 2", ct(8, 30), ct(9, 1), person="Bob T.", odoo_id=6)

    result = smooth(
        (*DETOUR, bob),
        production_times_by_wc=with_repair_2_pallets(ct(9, 0, 30), ct(9, 1, 30)),
    )

    assert result == (*DETOUR, bob)


def test_orphaned_pallets_at_any_blip_of_a_multi_station_detour_keep_it():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(9, 1)),
        seg("Repair 3", ct(9, 1), ct(9, 3)),
        seg("Repair 1", ct(9, 4), ct(10)),
    )

    result = smooth(segments, production_times_by_wc=NO_PALLETS | {"Repair 3": (ct(9, 2),)})

    assert result == segments


def test_unknown_meter_data_never_stops_came_back():
    assert shape(smooth(DETOUR, production_times_by_wc=None)) == DETOUR_MERGED
    assert shape(smooth(DETOUR, production_times_by_wc={"Repair 1": ()})) == DETOUR_MERGED


def test_christians_detour_still_merges_because_jose_covers_dismantler_2():
    jose = seg("Dismantler 2", ct(7), ct(11), person="Jose C.", odoo_id=24)
    segments = (
        seg("Dismantler 3", ct(7), ct(7, 2, 10)),
        seg("Dismantler 2", ct(7, 2, 10), ct(7, 4, 27)),
        seg("Dismantler 3", ct(7, 4, 27), ct(11)),
        jose,
    )

    result = smooth(
        segments,
        production_times_by_wc=NO_PALLETS | {"Dismantler 2": (ct(7, 3), ct(7, 4))},
    )

    assert shape(result) == [
        ("Christian C.", "Dismantler 3", ct(7), ct(11)),
        ("Jose C.", "Dismantler 2", ct(7), ct(11)),
    ]
    assert result[1] is jose
