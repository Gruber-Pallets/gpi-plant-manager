from datetime import datetime, timezone

from zira_dashboard.assignment_windows import WorkSegment
from zira_dashboard.production_segments import (
    credit_work_segments,
    score_work_segments,
)


UTC = timezone.utc


def t(hour, minute=0):
    return datetime(2026, 8, 20, hour, minute, tzinfo=UTC)


def test_transfer_segments_keep_independent_actual_goal_and_runway():
    segments = [
        WorkSegment("Repair 4", "Humberto S.", t(12), t(19, 33), "punch"),
        WorkSegment("Repair 4", "Ana M.", t(19, 35), t(19, 50), "punch"),
    ]
    minutes = {"Humberto S.": 420.0, "Ana M.": 15.0}

    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 548.0},
        samples_by_wc={
            "Repair 4": [
                (t(18), 516),
                (t(19, 40), 32),
            ]
        },
        productive_minutes=lambda person, _wc, _start, _end: minutes[person],
        live_cap_utc=t(19, 50),
    )
    scored = score_work_segments(credits, target_per_hour={"Repair 4": 100.0})

    humberto, ana = scored["Repair 4"]
    assert (humberto.actual_units, humberto.goal_units) == (516.0, 700.0)
    assert (humberto.result, humberto.runway_units, humberto.is_active) == (
        "behind",
        700.0,
        False,
    )
    assert (ana.actual_units, ana.goal_units) == (32.0, 25.0)
    assert (ana.result, ana.runway_units, ana.is_active) == (
        "ahead",
        32.0,
        True,
    )


def test_transfer_boundary_and_overlap_credit_each_sample_once():
    segments = [
        WorkSegment("Hand Build #1", "A", t(12), t(13), "punch"),
        WorkSegment("Hand Build #1", "B", t(12), t(14), "punch"),
        WorkSegment("Hand Build #1", "C", t(13), t(14), "punch"),
    ]
    credits = credit_work_segments(
        segments,
        wc_totals={"Hand Build #1": 60},
        samples_by_wc={"Hand Build #1": [(t(12, 30), 20), (t(13), 40)]},
        productive_minutes=lambda _person, _wc, start, end: (
            end - start
        ).total_seconds()
        / 60,
    )["Hand Build #1"]
    assert [(row.person_name, row.actual_units) for row in credits] == [
        ("A", 10.0),
        ("B", 30.0),
        ("C", 20.0),
    ]


def test_same_worker_returning_keeps_two_segments():
    segments = [
        WorkSegment("Repair 4", "Humberto S.", t(12), t(13), "punch"),
        WorkSegment("Repair 4", "Humberto S.", t(14), t(15), "punch"),
    ]
    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 30},
        samples_by_wc={"Repair 4": [(t(12, 30), 10), (t(14, 30), 20)]},
        productive_minutes=lambda *_args: 60,
    )["Repair 4"]
    assert [(row.start_utc, row.actual_units) for row in credits] == [
        (t(12), 10.0),
        (t(14), 20.0),
    ]


def test_unassigned_and_total_without_samples_are_never_dropped():
    credits = credit_work_segments(
        [],
        wc_totals={"Repair 4": 50},
        samples_by_wc={"Repair 4": [(t(12, 30), 30)]},
        productive_minutes=lambda *_args: 0,
    )["Repair 4"]
    assert len(credits) == 1
    assert credits[0].person_name is None
    assert credits[0].source == "unassigned"
    assert credits[0].actual_units == 50.0


def test_remaining_total_uses_each_segment_productive_time():
    segments = [
        WorkSegment("Repair 4", "A", t(12), t(13), "punch"),
        WorkSegment("Repair 4", "B", t(13), t(15), "punch"),
    ]
    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 90},
        samples_by_wc={},
        productive_minutes=lambda _person, _wc, start, end: (
            end - start
        ).total_seconds()
        / 60,
    )["Repair 4"]
    assert [(row.person_name, row.actual_units) for row in credits] == [
        ("A", 30.0),
        ("B", 60.0),
    ]


def test_zero_target_stays_neutral_without_false_finish_goal():
    segment = WorkSegment("Repair 4", "A", t(12), t(13), "punch")
    credits = credit_work_segments(
        [segment],
        wc_totals={"Repair 4": 12},
        samples_by_wc={"Repair 4": [(t(12, 30), 12)]},
        productive_minutes=lambda *_args: 60,
    )
    (score,) = score_work_segments(
        credits, target_per_hour={"Repair 4": 0}
    )["Repair 4"]
    assert (score.goal_units, score.runway_units, score.result) == (
        0.0,
        12.0,
        "neutral",
    )


def test_sequential_transfers_cover_completed_and_live_result_states():
    segments = [
        WorkSegment("Repair 4", "A", t(12), t(13), "punch"),
        WorkSegment("Repair 4", "B", t(13), t(14), "punch"),
        WorkSegment("Repair 4", "C", t(14), t(15), "punch"),
        WorkSegment("Repair 4", "D", t(15), t(16), "punch"),
    ]
    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 220},
        samples_by_wc={
            "Repair 4": [
                (t(12, 30), 50),
                (t(13, 30), 60),
                (t(14, 30), 80),
                (t(15, 30), 30),
            ]
        },
        productive_minutes=lambda *_args: 60,
        live_cap_utc=t(16),
    )
    scored = score_work_segments(
        credits, target_per_hour={"Repair 4": 60}
    )["Repair 4"]
    assert [(row.person_name, row.result, row.is_active) for row in scored] == [
        ("A", "behind", False),
        ("B", "ahead", False),
        ("C", "ahead", False),
        ("D", "behind", True),
    ]
    assert [(row.actual_units, row.goal_units) for row in scored] == [
        (50.0, 60.0),
        (60.0, 60.0),
        (80.0, 60.0),
        (30.0, 60.0),
    ]
