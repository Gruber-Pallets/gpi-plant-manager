from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from zira_dashboard.attendance_timeline import LocationSpan
from zira_dashboard.quick_punch_fixes import (
    ITEM_KEY_PREFIX,
    MeterFreshness,
    after_summary,
    before_summary,
    find_fixes,
)

CT = ZoneInfo("America/Chicago")


def ct(hour, minute=0, second=0):
    return datetime(2026, 9, 18, hour, minute, second, tzinfo=CT).astimezone(UTC)


def span(att_id, wc, start, end, *, emp=8, name="Christian C.", status="valid", is_open=False):
    return LocationSpan(
        employee_odoo_id=emp, employee_name=name, start_utc=start, end_utc=end,
        status=status, app_work_center_name=wc if status == "valid" else None,
        odoo_work_center_id=None, odoo_work_center_name=wc,
        attendance_ids=(att_id,), department_repair=None, is_open=is_open,
    )


FRESH = {
    name: MeterFreshness(last_reading_at=ct(23), truncated=False)
    for name in ("Dismantler 1", "Dismantler 2", "Dismantler 3", "Dismantler 4")
}
IDLE = {name: () for name in FRESH}
BREAKS = ((ct(9), ct(9, 15)), (ct(11), ct(11, 30)))


def christian(now):
    return (
        span(6190, "Dismantler 3", ct(7), ct(7, 2, 10)),
        span(6207, "Dismantler 2", ct(7, 2, 10), ct(7, 4, 27)),
        span(6208, "Dismantler 3", ct(7, 4, 27), now, is_open=True),
    )


def scan(spans, now, **kw):
    kw.setdefault("production_times_by_wc", IDLE)
    kw.setdefault("meter_freshness_by_wc", FRESH)
    kw.setdefault("breaks", BREAKS)
    return find_fixes(spans, now_utc=now, **kw)


def test_christians_detour_becomes_one_open_fix_after_the_settle_delay():
    now = ct(7, 7)
    result = scan(christian(now), now)
    (fix,) = result.fixes
    assert (fix.employee_odoo_id, fix.wc_name, fix.start_utc, fix.end_utc) == (8, "Dismantler 3", ct(7), None)
    assert fix.source_attendance_ids == (6190, 6207, 6208)
    assert fix.item_key.startswith(f"{ITEM_KEY_PREFIX}8:")
    assert [(s.wc_name, s.start_utc) for s in fix.before] == [
        ("Dismantler 3", ct(7)), ("Dismantler 2", ct(7, 2, 10)), ("Dismantler 3", ct(7, 4, 27)),
    ]


def test_fix_waits_for_the_settle_delay():
    now = ct(7, 5)  # only 33 s after the return at 07:04:27
    result = scan(christian(now), now)
    assert result.fixes == ()
    assert [s.reason for s in result.skipped] == ["settling"]


def test_unsettled_first_pick_at_0703_is_not_fixed():
    now = ct(7, 3)
    spans = (
        span(6190, "Dismantler 3", ct(7), ct(7, 2, 10)),
        span(6207, "Dismantler 2", ct(7, 2, 10), now, is_open=True),
    )
    result = scan(spans, now)
    assert result.fixes == ()
    assert "first_pick_not_settled" in [s.reason for s in result.skipped] or "settling" in [
        s.reason for s in result.skipped
    ]


def test_first_pick_is_fixed_once_the_new_station_sticks():
    now = ct(7, 9)
    spans = (
        span(6190, "Dismantler 3", ct(7), ct(7, 2, 10)),
        span(6207, "Dismantler 2", ct(7, 2, 10), now, is_open=True),
    )
    (fix,) = scan(spans, now).fixes
    assert (fix.wc_name, fix.start_utc, fix.end_utc) == ("Dismantler 2", ct(7), None)


def test_stale_meter_for_the_blip_station_waits():
    now = ct(7, 7)
    fresh = dict(FRESH, **{"Dismantler 2": MeterFreshness(last_reading_at=ct(7, 3), truncated=False)})
    result = scan(christian(now), now, meter_freshness_by_wc=fresh)
    assert result.fixes == ()
    assert [s.reason for s in result.skipped] == ["meter_not_current"]


def test_truncated_or_missing_meter_waits():
    now = ct(7, 7)
    for fresh in (
        dict(FRESH, **{"Dismantler 2": MeterFreshness(last_reading_at=ct(23), truncated=True)}),
        {k: v for k, v in FRESH.items() if k != "Dismantler 2"},
    ):
        assert scan(christian(now), now, meter_freshness_by_wc=fresh).fixes == ()


def test_plain_same_station_sign_out_gap_needs_no_meter():
    now = ct(8, 30)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        span(2, "Dismantler 3", ct(8, 3), now, is_open=True),
    )
    (fix,) = scan(spans, now, meter_freshness_by_wc={}).fixes
    assert (fix.start_utc, fix.end_utc, fix.source_attendance_ids) == (ct(7), None, (1, 2))


def test_closed_merge_has_an_end():
    now = ct(10)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        span(2, "Dismantler 3", ct(8, 3), ct(8, 50)),
        span(3, "Dismantler 1", ct(8, 50), now, is_open=True),
    )
    (fix,) = scan(spans, now).fixes
    assert (fix.wc_name, fix.start_utc, fix.end_utc) == ("Dismantler 3", ct(7), ct(8, 50))


def test_blip_or_gap_touching_a_break_is_never_fixed():
    now = ct(10)
    spans = (
        span(1, "Dismantler 3", ct(8), ct(9, 1)),
        span(2, "Dismantler 3", ct(9, 3), now, is_open=True),
    )
    result = scan(spans, now)
    assert result.fixes == ()
    assert [s.reason for s in result.skipped] == ["break"]


def test_nothing_to_fix_when_one_row_was_split_into_two_spans():
    now = ct(10)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        span(1, "Dismantler 3", ct(8), now, is_open=True),
    )
    assert scan(spans, now).fixes == ()


def test_item_key_is_stable_and_changes_with_new_rows():
    now = ct(7, 7)
    first = scan(christian(now), now).fixes[0].item_key
    assert scan(christian(ct(7, 8)), ct(7, 8)).fixes[0].item_key == first
    spans = (*christian(now)[:2], span(6208, "Dismantler 3", ct(7, 4, 27), ct(8)),
             span(6300, "Dismantler 1", ct(8), ct(8, 2)), span(6301, "Dismantler 3", ct(8, 2), ct(9)))
    assert scan(spans, ct(9, 30)).fixes[0].item_key != first


def test_conflict_spans_block_as_in_the_dashboards():
    now = ct(10)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        span(9, None, ct(8), ct(8, 2), status="conflicting_location"),
        span(2, "Dismantler 3", ct(8, 2), now, is_open=True),
    )
    assert scan(spans, now).fixes == ()


# --- Beyond the plan: spec cases and boundaries -------------------------------


def ana(att_id, wc, start, end, **kw):
    return span(att_id, wc, start, end, emp=9, name="Ana R.", **kw)


def test_item_key_prefix_is_owned_by_the_correction_engine():
    from zira_dashboard import attendance_corrections

    assert ITEM_KEY_PREFIX is attendance_corrections.QUICK_PUNCH_ITEM_KEY_PREFIX


def test_two_people_on_the_same_day_get_independent_fixes():
    now = ct(8, 30)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        ana(21, "Dismantler 1", ct(7, 30), ct(8, 1)),
        span(2, "Dismantler 3", ct(8, 3), now, is_open=True),
        ana(22, "Dismantler 1", ct(8, 4), now, is_open=True),
    )
    by_employee = {fix.employee_odoo_id: fix for fix in scan(spans, now).fixes}
    assert set(by_employee) == {8, 9}
    christian_fix, ana_fix = by_employee[8], by_employee[9]
    assert (christian_fix.person_name, christian_fix.wc_name, christian_fix.start_utc) == (
        "Christian C.", "Dismantler 3", ct(7),
    )
    assert (ana_fix.person_name, ana_fix.wc_name, ana_fix.start_utc) == (
        "Ana R.", "Dismantler 1", ct(7, 30),
    )
    assert christian_fix.source_attendance_ids == (1, 2)
    assert ana_fix.source_attendance_ids == (21, 22)
    assert [s.attendance_ids for s in ana_fix.before] == [(21,), (22,)]
    assert christian_fix.item_key.startswith(f"{ITEM_KEY_PREFIX}8:")
    assert ana_fix.item_key.startswith(f"{ITEM_KEY_PREFIX}9:")


def mid_shift_christian(now):
    """Christian's detour, but after an hour at Dismantler 3 (no first pick)."""
    return (span(6100, "Dismantler 3", ct(6), ct(7, 2, 10)), *christian(now)[1:])


def test_a_relief_at_the_station_during_the_gap_means_no_fix():
    now = ct(7, 7)
    relief = ana(7001, "Dismantler 3", ct(7, 3), now, is_open=True)  # arrived during the gap
    result = scan((*mid_shift_christian(now), relief), now)
    assert result.fixes == ()
    assert result.skipped == ()  # smoothing itself keeps the stints apart


def test_a_relief_right_after_sign_in_is_not_rebuilt_by_a_chain_of_first_picks():
    # The relief stops the came-back merge, but D3 (2:10, idle) -> D2 (idle) is
    # a chain of wrong first picks that smoothing folds into D3 07:00-now all
    # the same. In Odoo that would split Ana's 07:03:30 pallet with Christian.
    now = ct(7, 7)
    relief = ana(7001, "Dismantler 3", ct(7, 3), now, is_open=True)
    production = dict(IDLE, **{"Dismantler 3": (ct(7, 3, 30),)})
    result = scan((*christian(now), relief), now, production_times_by_wc=production)
    assert result.fixes == ()
    (skip,) = result.skipped
    assert (skip.employee_odoo_id, skip.reason) == (8, "relief")
    assert [s.attendance_ids for s in skip.before] == [(6190,), (6207,), (6208,)]


def test_a_partner_there_across_the_whole_gap_is_not_a_relief():
    now = ct(7, 7)
    partner = ana(7001, "Dismantler 3", ct(6, 55), now, is_open=True)
    (fix,) = scan((*christian(now), partner), now).fixes
    assert (fix.employee_odoo_id, fix.source_attendance_ids) == (8, (6190, 6207, 6208))


def test_pallets_at_the_blip_station_that_nobody_else_covers_mean_no_fix():
    now = ct(7, 7)
    production = dict(IDLE, **{"Dismantler 2": (ct(7, 3),)})
    result = scan(mid_shift_christian(now), now, production_times_by_wc=production)
    assert result.fixes == ()
    assert result.skipped == ()
    # Right after sign-in the D3 first pick is folded into D2 instead (D2 then
    # holds his pallet); that stint never settles, so Odoo is left alone.
    result = scan(christian(now), now, production_times_by_wc=production)
    assert result.fixes == ()
    assert [s.reason for s in result.skipped] == ["first_pick_not_settled"]


def test_pallets_at_the_blip_station_covered_by_someone_else_still_fix():
    now = ct(7, 7)
    production = dict(IDLE, **{"Dismantler 2": (ct(7, 3),)})
    covering = ana(7001, "Dismantler 2", ct(6, 55), now, is_open=True)
    (fix,) = scan((*christian(now), covering), now, production_times_by_wc=production).fixes
    assert (fix.employee_odoo_id, fix.wc_name, fix.end_utc) == (8, "Dismantler 3", None)


def test_a_closed_merge_ends_at_its_last_absorbed_row_even_when_a_later_row_is_open():
    now = ct(10)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        span(2, "Dismantler 3", ct(8, 3), ct(8, 50)),
        span(3, "Dismantler 3", ct(9), now, is_open=True),  # back 10 min later: a real sign-out
    )
    (fix,) = scan(spans, now).fixes
    assert (fix.start_utc, fix.end_utc, fix.source_attendance_ids) == (ct(7), ct(8, 50), (1, 2))
    assert [s.is_open for s in fix.before] == [False, False]


def test_before_is_in_time_order_whatever_the_span_order():
    now = ct(7, 7)
    in_order = scan(christian(now), now).fixes[0]
    (fix,) = scan(tuple(reversed(christian(now))), now).fixes
    assert [(s.wc_name, s.start_utc, s.end_utc, s.is_open, s.attendance_ids) for s in fix.before] == [
        ("Dismantler 3", ct(7), ct(7, 2, 10), False, (6190,)),
        ("Dismantler 2", ct(7, 2, 10), ct(7, 4, 27), False, (6207,)),
        ("Dismantler 3", ct(7, 4, 27), now, True, (6208,)),
    ]
    assert fix == in_order
    settling = scan(tuple(reversed(christian(ct(7, 5)))), ct(7, 5)).skipped[0]
    assert [s.start_utc for s in settling.before] == [ct(7), ct(7, 2, 10), ct(7, 4, 27)]


def test_settle_delay_boundary_is_exactly_two_minutes():
    settled = ct(7, 6, 27)  # 2 min after the return at 07:04:27
    assert len(scan(christian(settled), settled).fixes) == 1
    early = ct(7, 6, 26)
    result = scan(christian(early), early)
    assert result.fixes == ()
    assert [s.reason for s in result.skipped] == ["settling"]


def test_first_pick_waits_until_the_new_station_has_stuck_for_more_than_the_limit():
    def spans_at(now):
        return (
            span(6190, "Dismantler 3", ct(7), ct(7, 1)),
            span(6207, "Dismantler 2", ct(7, 1), now, is_open=True),
        )

    # Past the settle delay, but at Dismantler 2 for only 3, then exactly 5, minutes.
    for now in (ct(7, 4), ct(7, 6)):
        result = scan(spans_at(now), now)
        assert result.fixes == ()
        assert [s.reason for s in result.skipped] == ["first_pick_not_settled"]
    now = ct(7, 6, 1)
    (fix,) = scan(spans_at(now), now).fixes
    assert (fix.wc_name, fix.start_utc, fix.end_utc) == ("Dismantler 2", ct(7), None)


def test_meter_read_exactly_at_the_blip_end_is_current():
    now = ct(7, 7)
    fresh = dict(
        FRESH, **{"Dismantler 2": MeterFreshness(last_reading_at=ct(7, 4, 27), truncated=False)}
    )
    assert len(scan(christian(now), now, meter_freshness_by_wc=fresh).fixes) == 1


def test_meter_with_no_reading_yet_waits():
    now = ct(7, 7)
    fresh = dict(FRESH, **{"Dismantler 2": MeterFreshness(last_reading_at=None, truncated=False)})
    result = scan(christian(now), now, meter_freshness_by_wc=fresh)
    assert result.fixes == ()
    assert [s.reason for s in result.skipped] == ["meter_not_current"]


def test_blip_station_without_pallet_data_waits_even_with_a_fresh_meter():
    # The orphaned-pallet guard could not look at Dismantler 2 (no pallet data,
    # e.g. a malformed sample), so the merge is not proven safe for Odoo.
    now = ct(7, 7)
    no_d2 = {k: v for k, v in IDLE.items() if k != "Dismantler 2"}
    for production in (no_d2, None):
        result = scan(christian(now), now, production_times_by_wc=production)
        assert result.fixes == ()
        assert [s.reason for s in result.skipped] == ["meter_not_current"]


def test_same_station_gap_needs_no_pallet_data_either():
    now = ct(8, 30)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        span(2, "Dismantler 3", ct(8, 3), now, is_open=True),
    )
    (fix,) = scan(spans, now, production_times_by_wc=None, meter_freshness_by_wc={}).fixes
    assert fix.source_attendance_ids == (1, 2)


def test_a_blip_during_a_break_is_never_fixed():
    now = ct(10)
    spans = (
        span(1, "Dismantler 3", ct(8), ct(9, 1)),
        span(2, "Dismantler 2", ct(9, 1), ct(9, 3)),
        span(3, "Dismantler 3", ct(9, 3), now, is_open=True),
    )
    result = scan(spans, now)
    assert result.fixes == ()
    assert [s.reason for s in result.skipped] == ["break"]


def test_before_and_after_summaries_use_short_names_and_central_time():
    now = ct(7, 7)
    (fix,) = scan(christian(now), now).fixes
    assert before_summary(fix) == "D3 7:00–7:02 · D2 7:02–7:04 · D3 7:04–now"
    assert after_summary(fix) == "D3 7:00–now"


def test_closed_and_unabbreviated_summaries():
    now = ct(10)
    spans = (
        span(1, "Repair 2", ct(7), ct(8)),
        span(2, "Repair 2", ct(8, 3), ct(8, 50)),
        span(3, "Trim Saw", ct(8, 50), now, is_open=True),
    )
    (fix,) = scan(spans, now).fixes
    assert before_summary(fix) == "R2 7:00–8:00 · R2 8:03–8:50"
    assert after_summary(fix) == "R2 7:00–8:50"


def test_maintenance_sign_out_gap_is_never_a_fix():
    now = ct(8, 30)
    spans = (
        span(1, "Work Orders", ct(7), ct(8), emp=40, name="Sam M."),
        span(2, "Work Orders", ct(8, 3), now, emp=40, name="Sam M.", is_open=True),
    )
    result = scan(spans, now)
    assert result.fixes == ()


def test_detour_through_maintenance_is_never_a_fix():
    now = ct(7, 10)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(7, 2)),
        span(2, "Work Orders", ct(7, 2), ct(7, 4)),
        span(3, "Dismantler 3", ct(7, 4), now, is_open=True),
    )
    result = scan(spans, now)
    assert result.fixes == ()


def test_unmetered_production_sign_out_gap_is_never_a_fix():
    now = ct(8, 30)
    spans = (
        span(1, "Chop/Notch", ct(7), ct(8)),
        span(2, "Chop/Notch", ct(8, 3), now, is_open=True),
    )
    result = scan(spans, now)
    assert result.fixes == ()
