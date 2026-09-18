from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from zira_dashboard.assignment_windows import WorkSegment
from zira_dashboard.quick_punch_smoothing import QUICK_PUNCH_LIMIT, smooth_quick_punches

CT = ZoneInfo("America/Chicago")


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
    assert smooth_quick_punches(()) == ()


def test_christians_detour_on_2026_09_18_becomes_one_dismantler_3_stint():
    now = ct(14, 25, 53)
    segments = (
        seg("Dismantler 3", ct(7), ct(7, 2, 10)),
        seg("Dismantler 2", ct(7, 2, 10), ct(7, 4, 27)),
        seg("Dismantler 3", ct(7, 4, 27), ct(11)),
        seg("Dismantler 3", ct(11, 30), now),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Dismantler 3", ct(7), ct(11)),
        ("Christian C.", "Dismantler 3", ct(11, 30), now),
    ]


def test_lunch_split_is_not_bridged():
    segments = (
        seg("Dismantler 2", ct(7), ct(11), person="Jose C.", odoo_id=24),
        seg("Dismantler 2", ct(11, 30), ct(14, 25), person="Jose C.", odoo_id=24),
    )

    assert smooth_quick_punches(segments) == segments


def test_same_station_gap_is_bridged_up_to_exactly_the_limit():
    for gap_seconds, expected_count in ((299, 1), (300, 1), (301, 2)):
        back_at = ct(9) + timedelta(seconds=gap_seconds)
        result = smooth_quick_punches(
            (seg("Repair 1", ct(8), ct(9)), seg("Repair 1", back_at, ct(10)))
        )
        assert len(result) == expected_count, gap_seconds

    (bridged,) = smooth_quick_punches(
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

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Repair 1", ct(8), ct(10)),
    ]


def test_detour_longer_than_the_limit_is_kept():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(9, 6)),
        seg("Repair 1", ct(9, 6), ct(10)),
    )

    assert smooth_quick_punches(segments) == segments


def test_wrong_first_pick_at_start_of_day_joins_the_next_station():
    segments = (
        seg("Dismantler 1", ct(7), ct(7, 3)),
        seg("Dismantler 4", ct(7, 3), ct(11)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Dismantler 4", ct(7), ct(11)),
    ]


def test_wrong_first_pick_after_lunch_joins_the_next_station_and_fills_the_gap():
    segments = (
        seg("Dismantler 4", ct(7), ct(11)),
        seg("Dismantler 1", ct(11, 30), ct(11, 32)),
        seg("Dismantler 4", ct(11, 34), ct(15, 30)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Dismantler 4", ct(7), ct(11)),
        ("Christian C.", "Dismantler 4", ct(11, 30), ct(15, 30)),
    ]


def test_chain_of_first_picks_settles_on_the_final_station_within_the_limit():
    segments = (
        seg("Repair 1", ct(7), ct(7, 1)),
        seg("Repair 2", ct(7, 1), ct(7, 3)),
        seg("Repair 3", ct(7, 3), ct(11)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Repair 3", ct(7), ct(11)),
    ]


def test_chain_of_first_picks_stops_once_the_combined_stint_passes_the_limit():
    segments = (
        seg("Repair 1", ct(7), ct(7, 3)),
        seg("Repair 2", ct(7, 3), ct(7, 6)),
        seg("Repair 3", ct(7, 6), ct(11)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Repair 2", ct(7), ct(7, 6)),
        ("Christian C.", "Repair 3", ct(7, 6), ct(11)),
    ]


def test_mid_day_short_stop_between_two_different_stations_is_kept():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(9, 2)),
        seg("Repair 3", ct(9, 2), ct(10)),
    )

    assert smooth_quick_punches(segments) == segments


def test_came_back_rule_runs_before_wrong_first_pick():
    # Run alone, wrong-first-pick would move 07:00-07:02 onto Dismantler 2.
    segments = (
        seg("Dismantler 3", ct(7), ct(7, 2)),
        seg("Dismantler 2", ct(7, 2), ct(7, 4)),
        seg("Dismantler 3", ct(7, 4), ct(9)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Dismantler 3", ct(7), ct(9)),
    ]


def test_live_detour_waits_until_the_person_comes_back():
    at_0703 = (
        seg("Dismantler 3", ct(7), ct(7, 2, 10)),
        seg("Dismantler 2", ct(7, 2, 10), ct(7, 3)),
    )
    assert shape(smooth_quick_punches(at_0703)) == [
        ("Christian C.", "Dismantler 2", ct(7), ct(7, 3)),
    ]

    mid_detour_after_real_work = (
        seg("Dismantler 3", ct(7), ct(9)),
        seg("Dismantler 2", ct(9), ct(9, 2)),
    )
    assert smooth_quick_punches(mid_detour_after_real_work) == mid_detour_after_real_work


def test_other_people_are_never_changed_and_order_is_preserved():
    bob = seg("Repair 3", ct(7), ct(15), person="Bob T.", odoo_id=6)
    christian_before = seg("Repair 1", ct(8), ct(9))
    ana = seg("Repair 2", ct(9), ct(9, 2), person="Ana M.", odoo_id=5)
    christian_after = seg("Repair 1", ct(9, 2), ct(10))

    result = smooth_quick_punches((bob, christian_before, ana, christian_after))

    assert len(result) == 3
    assert result[0] is bob
    assert shape(result[1:2]) == [("Christian C.", "Repair 1", ct(8), ct(10))]
    assert result[2] is ana


def test_same_name_with_different_odoo_ids_stays_separate():
    segments = (
        seg("Repair 1", ct(8), ct(9), person="Jose O.", odoo_id=31),
        seg("Repair 1", ct(9, 1), ct(10), person="Jose O.", odoo_id=32),
    )

    assert smooth_quick_punches(segments) == segments


def test_blocked_windows_stop_both_rules():
    came_back = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 1", ct(9, 3), ct(10)),
    )
    assert (
        smooth_quick_punches(came_back, blocked_windows={8: ((ct(9, 1), ct(9, 2)),)})
        == came_back
    )

    first_pick = (
        seg("Repair 1", ct(7), ct(7, 2)),
        seg("Repair 2", ct(7, 3), ct(11)),
    )
    assert (
        smooth_quick_punches(first_pick, blocked_windows={8: ((ct(7, 2), ct(7, 3)),)})
        == first_pick
    )


def test_blocked_window_that_only_touches_the_gap_does_not_block():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 1", ct(9, 3), ct(10)),
    )

    (bridged,) = smooth_quick_punches(
        segments, blocked_windows={8: ((ct(8, 50), ct(9)),)}
    )
    assert (bridged.start_utc, bridged.end_utc) == (ct(8), ct(10))


def test_detour_that_runs_past_the_return_is_left_alone():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(11)),
        seg("Repair 1", ct(9, 2), ct(10)),
    )

    assert smooth_quick_punches(segments) == segments
