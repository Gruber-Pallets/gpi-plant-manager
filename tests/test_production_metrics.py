from datetime import date

from zira_dashboard import production_metrics as pm


STD_HOURS = 7.0


def rec(day, person, wc, units, hours):
    return {
        "day": day,
        "person": person,
        "wc": wc,
        "units": float(units),
        "hours": float(hours),
        "downtime": 0.0,
    }


def test_normalized_daily_scores_ignores_under_4_hours():
    rows = pm.normalized_daily_scores(
        [rec(date(2026, 7, 1), "Alice", "Repair 1", 60, 3.99)],
        wc_names={"Repair 1"},
        standard_full_day_hours=STD_HOURS,
    )
    assert rows == []


def test_normalized_daily_scores_exactly_4_hours_qualifies():
    rows = pm.normalized_daily_scores(
        [rec(date(2026, 7, 1), "Alice", "Repair 1", 80, 4.0)],
        wc_names={"Repair 1"},
        standard_full_day_hours=STD_HOURS,
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "Alice"
    assert rows[0]["day"] == date(2026, 7, 1)
    assert rows[0]["units"] == 80.0
    assert rows[0]["hours"] == 4.0
    assert rows[0]["normalized_units"] == 140.0


def test_normalized_daily_scores_sums_same_day_scope_before_cutoff():
    rows = pm.normalized_daily_scores(
        [
            rec(date(2026, 7, 1), "Alice", "Repair 1", 40, 2.0),
            rec(date(2026, 7, 1), "Alice", "Repair 2", 50, 3.0),
        ],
        wc_names={"Repair 1", "Repair 2"},
        standard_full_day_hours=STD_HOURS,
    )
    assert len(rows) == 1
    assert rows[0]["units"] == 90.0
    assert rows[0]["hours"] == 5.0
    assert rows[0]["normalized_units"] == 126.0


def test_normalized_average_by_person_averages_qualified_days():
    rows = pm.normalized_average_by_person(
        [
            rec(date(2026, 7, 1), "Alice", "Repair 1", 80, 4.0),   # 140
            rec(date(2026, 7, 2), "Alice", "Repair 1", 70, 7.0),   # 70
            rec(date(2026, 7, 3), "Alice", "Repair 1", 999, 3.0),  # ignored
        ],
        wc_names={"Repair 1"},
        standard_full_day_hours=STD_HOURS,
    )
    assert rows[0]["name"] == "Alice"
    assert rows[0]["days"] == 2
    assert rows[0]["avg_units"] == 105.0
    assert rows[0]["total_units"] == 150.0
    assert rows[0]["total_hours"] == 11.0


def test_normalized_average_by_person_sorts_by_avg_then_days_then_name():
    rows = pm.normalized_average_by_person(
        [
            rec(date(2026, 7, 1), "Bob", "Repair 1", 70, 7.0),
            rec(date(2026, 7, 1), "Anne", "Repair 1", 70, 7.0),
            rec(date(2026, 7, 2), "Anne", "Repair 1", 70, 7.0),
            rec(date(2026, 7, 1), "Cara", "Repair 1", 100, 7.0),
        ],
        wc_names={"Repair 1"},
        standard_full_day_hours=STD_HOURS,
    )
    assert [r["name"] for r in rows] == ["Cara", "Anne", "Bob"]


def test_build_recycling_leaderboard_l30_only_person_gets_ytd_not_enough_days():
    records = []
    # YTD leader has 20 repair days, so YTD threshold is 2.
    for i in range(20):
        records.append(rec(date(2026, 1, 1 + i), "YTD Leader", "Repair 1", 70, 7.0))
    # Recent person has only one YTD day, but it is inside L30. L30 leader has
    # one day too, so L30 threshold is 1 and the L30 cell qualifies.
    records.append(rec(date(2026, 7, 5), "Recent Star", "Repair 1", 140, 7.0))

    data = pm.build_recycling_leaderboard(
        records,
        today=date(2026, 7, 9),
        standard_full_day_hours=STD_HOURS,
        wc_role_by_name={"Repair 1": "Repair"},
    )

    repairs = data["roles"]["Repair"]["rows"]
    recent = next(r for r in repairs if r["name"] == "Recent Star")
    assert recent["ytd"]["eligible"] is False
    assert recent["ytd"]["label"] == "not enough days"
    assert recent["l30"]["eligible"] is True
    assert recent["l30"]["avg_units"] == 140.0
    assert recent["l30"]["days"] == 1


def test_build_recycling_leaderboard_thresholds_are_ceil_10_percent():
    records = [
        rec(date(2026, 1, day), "Leader", "Dismantler 1", 70, 7.0)
        for day in range(1, 13)
    ]
    data = pm.build_recycling_leaderboard(
        records,
        today=date(2026, 7, 9),
        standard_full_day_hours=STD_HOURS,
        wc_role_by_name={"Dismantler 1": "Dismantler"},
    )
    assert data["roles"]["Dismantler"]["thresholds"]["ytd"] == 2


def test_build_recycling_leaderboard_ribbons_use_normalized_amount():
    records = [
        rec(date(2026, 7, 2), "Short Day", "Repair 1", 80, 4.0),  # normalized 140
        rec(date(2026, 7, 3), "Full Day", "Repair 1", 100, 7.0),  # normalized 100
        rec(date(2026, 7, 4), "Tiny", "Repair 1", 200, 3.0),      # ignored
    ]
    data = pm.build_recycling_leaderboard(
        records,
        today=date(2026, 7, 9),
        standard_full_day_hours=STD_HOURS,
        wc_role_by_name={"Repair 1": "Repair"},
    )
    july = data["ribbons"][0]
    assert july["month"] == 7
    assert july["repair"]["name"] == "Short Day"
    assert july["repair"]["day"] == date(2026, 7, 2)
    assert july["repair"]["amount"] == 140.0


def test_build_family_leaderboard_keeps_families_independent_and_ordered():
    records = [
        rec(date(2026, 7, 1), "Junior Operator", "Junior #2", 600, 7.0),
        rec(date(2026, 7, 1), "Wood Operator", "Woodpecker #1", 300, 7.0),
        rec(date(2026, 7, 1), "Builder", "Hand Build #1", 100, 3.0),
        rec(date(2026, 7, 1), "Builder", "Big Build #1", 80, 4.0),
    ]
    data = pm.build_family_leaderboard(
        records,
        today=date(2026, 7, 10),
        standard_full_day_hours=STD_HOURS,
        family_wc_names={
            "Juniors": {"Junior #1", "Junior #2", "Junior #3"},
            "Woodpecker": {"Woodpecker #1"},
            "Hand Build": {"Hand Build #1", "Hand Build #2", "Big Build #1"},
        },
    )
    assert data["active_families"] == ["Juniors", "Woodpecker", "Hand Build"]
    assert data["families"]["Juniors"]["rows"][0]["name"] == "Junior Operator"
    assert data["families"]["Woodpecker"]["rows"][0]["name"] == "Wood Operator"
    hand_build = data["families"]["Hand Build"]["rows"][0]
    assert hand_build["name"] == "Builder"
    assert hand_build["ytd"]["avg_units"] == 180.0


def test_build_family_leaderboard_hides_family_without_qualifying_rows():
    data = pm.build_family_leaderboard(
        [rec(date(2026, 7, 1), "Short Shift", "Woodpecker #1", 200, 3.99)],
        today=date(2026, 7, 10),
        standard_full_day_hours=STD_HOURS,
        family_wc_names={
            "Juniors": {"Junior #2"},
            "Woodpecker": {"Woodpecker #1"},
        },
    )
    assert data["active_families"] == []
    assert data["families"]["Woodpecker"]["rows"] == []


def test_build_family_leaderboard_first_day_threshold_and_ribbon():
    data = pm.build_family_leaderboard(
        [rec(date(2026, 7, 2), "Launch Operator", "Junior #2", 80, 4.0)],
        today=date(2026, 7, 10),
        standard_full_day_hours=STD_HOURS,
        family_wc_names={"Juniors": {"Junior #2"}},
    )
    assert data["families"]["Juniors"]["thresholds"] == {"ytd": 1, "l30": 1}
    assert data["ribbons"][0]["winners"]["Juniors"] == {
        "name": "Launch Operator",
        "day": date(2026, 7, 2),
        "amount": 140.0,
        "days": 1,
    }
