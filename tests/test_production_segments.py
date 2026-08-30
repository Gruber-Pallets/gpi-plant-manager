from datetime import datetime, timezone

import pytest

from zira_dashboard.assignment_windows import WorkSegment
from zira_dashboard import production_segments as production_segments_module
from zira_dashboard.production_segments import (
    SegmentScore,
    coalesce_display_scores,
    credit_work_segments,
    distinct_named_producers,
    score_work_segments,
    worker_coverage_is_split,
)


def unassigned_runs_for_samples(*args, **kwargs):
    function = getattr(production_segments_module, "unassigned_runs_for_samples", None)
    if function is None:
        pytest.fail("unassigned_runs_for_samples is not implemented")
    return function(*args, **kwargs)


UTC = timezone.utc


def t(hour, minute=0):
    return datetime(2026, 8, 20, hour, minute, tzinfo=UTC)


def _score(
    person,
    start,
    end,
    *,
    actual,
    goal,
    active=False,
    minutes=60,
    wc="Dismantler 1",
    segment_id=0,
):
    return SegmentScore(
        segment_id=segment_id,
        wc_name=wc,
        person_name=person,
        start_utc=start,
        end_utc=end,
        source="punch" if person else "unassigned",
        productive_minutes=minutes,
        actual_units=actual,
        goal_units=goal,
        runway_units=max(actual, goal),
        is_active=active,
        result=(
            "neutral" if person is None or goal <= 0 else "ahead" if actual >= goal else "behind"
        ),
    )


def test_distinct_named_producers_counts_people_not_segments_or_unassigned():
    humberto_morning = _score("Humberto S.", t(12), t(15), actual=200, goal=240, segment_id=0)
    unassigned = _score(None, t(15), t(15), actual=3, goal=0, segment_id=1)
    empty_name = _score("", t(15), t(15), actual=0, goal=0, segment_id=4)
    humberto_afternoon = _score("Humberto S.", t(16), t(18), actual=316, goal=460, segment_id=2)
    ana = _score("Ana M.", t(18), t(19), actual=40, goal=50, segment_id=3)

    assert distinct_named_producers(
        (humberto_morning, unassigned, empty_name, humberto_afternoon)
    ) == ("Humberto S.",)
    assert distinct_named_producers((ana, humberto_afternoon, unassigned, humberto_morning)) == (
        "Humberto S.",
        "Ana M.",
    )
    assert distinct_named_producers((unassigned, empty_name)) == ()


def test_display_scores_join_same_worker_across_scheduled_lunch():
    morning = _score("Jesus G.", t(12), t(16), actual=311, goal=260, minutes=210)
    afternoon = _score(
        "Jesus G.",
        t(16, 30),
        t(19, 30),
        actual=256,
        goal=260,
        active=True,
        minutes=210,
        segment_id=1,
    )

    (joined,) = coalesce_display_scores((morning, afternoon), ignored_gaps=((t(16), t(16, 30)),))

    assert (joined.start_utc, joined.end_utc) == (t(12), t(19, 30))
    assert (joined.actual_units, joined.goal_units) == (567, 520)
    assert joined.productive_minutes == 420
    assert (joined.result, joined.runway_units, joined.is_active) == (
        "ahead",
        567,
        True,
    )


def test_display_scores_keep_productive_gap_and_lunch_transfer_split():
    productive_gap = coalesce_display_scores(
        (
            _score("Jesus G.", t(12), t(15), actual=100, goal=100),
            _score("Jesus G.", t(16), t(17), actual=40, goal=50, segment_id=1),
        ),
        ignored_gaps=((t(15, 15), t(15, 30)),),
    )
    lunch_transfer = coalesce_display_scores(
        (
            _score("Jesus G.", t(12), t(16), actual=100, goal=100),
            _score("Ana M.", t(16, 30), t(17), actual=40, goal=50, segment_id=1),
        ),
        ignored_gaps=((t(16), t(16, 30)),),
    )

    assert len(productive_gap) == 2
    assert (
        worker_coverage_is_split(productive_gap, window_start_utc=t(12), window_end_utc=t(17))
        is True
    )
    assert [row.person_name for row in lunch_transfer] == ["Jesus G.", "Ana M."]


def test_worker_coverage_split_policy_ignores_scheduled_break_boundaries():
    full = _score("Jesus G.", t(12), t(19), actual=500, goal=480, active=True)
    lunch_now = _score("Jesus G.", t(12), t(16), actual=300, goal=260)
    left_early = _score("Humberto S.", t(12), t(18), actual=516, goal=700)
    late_start = _score("Ana M.", t(13), t(19), actual=400, goal=360, active=True)
    second_worker = _score(
        "Ana M.",
        t(16, 30),
        t(19),
        actual=200,
        goal=180,
        active=True,
        segment_id=1,
    )
    overlapping_worker = _score("Ana M.", t(14), t(15), actual=50, goal=60, segment_id=3)
    unassigned = _score(None, t(16, 10), t(16, 10), actual=3, goal=0, segment_id=2)

    assert worker_coverage_is_split((full,), window_start_utc=t(12), window_end_utc=t(19)) is False
    assert (
        worker_coverage_is_split(
            (lunch_now,),
            window_start_utc=t(12),
            window_end_utc=t(16, 15),
            ignored_gaps=((t(16), t(16, 30)),),
        )
        is False
    )
    assert (
        worker_coverage_is_split((left_early,), window_start_utc=t(12), window_end_utc=t(19))
        is True
    )
    assert (
        worker_coverage_is_split((late_start,), window_start_utc=t(12), window_end_utc=t(19))
        is True
    )
    assert (
        worker_coverage_is_split(
            (lunch_now, second_worker),
            window_start_utc=t(12),
            window_end_utc=t(19),
            ignored_gaps=((t(16), t(16, 30)),),
        )
        is True
    )
    assert (
        worker_coverage_is_split(
            (full, overlapping_worker),
            window_start_utc=t(12),
            window_end_utc=t(19),
        )
        is True
    )
    assert (
        worker_coverage_is_split((full, unassigned), window_start_utc=t(12), window_end_utc=t(19))
        is False
    )


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
        productive_minutes=lambda _person, _wc, start, end: (end - start).total_seconds() / 60,
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
        productive_minutes=lambda _person, _wc, start, end: (end - start).total_seconds() / 60,
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
    (score,) = score_work_segments(credits, target_per_hour={"Repair 4": 0})["Repair 4"]
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
    scored = score_work_segments(credits, target_per_hour={"Repair 4": 60})["Repair 4"]
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


def test_odoo_identity_keeps_duplicate_display_names_distinct_and_conserves_units():
    segments = [
        WorkSegment("Repair 4", "Alex", t(12), t(13), "odoo", 101),
        WorkSegment("Repair 4", "Alex", t(12), t(13), "odoo", 202),
    ]

    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 30},
        samples_by_wc={"Repair 4": [(t(12, 15), 12), (t(12, 45), 18)]},
        productive_minutes=lambda *_args: 60,
        allow_total_fallback=False,
    )["Repair 4"]

    assert [row.person_odoo_id for row in credits] == [101, 202]
    assert [row.actual_units for row in credits] == [15.0, 15.0]
    assert sum(row.actual_units for row in credits) == pytest.approx(30.0)


def test_strict_credit_leaves_gap_and_conflict_samples_unassigned_without_fabricating_units():
    segments = [
        WorkSegment("Repair 4", "Alex", t(12), t(12, 30), "odoo", 101),
        WorkSegment("Repair 4", "Alex", t(12, 45), t(13), "odoo", 101),
    ]

    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 30},
        samples_by_wc={"Repair 4": [(t(12, 10), 10), (t(12, 35), 12), (t(12, 50), 8)]},
        productive_minutes=lambda *_args: 30,
        allow_total_fallback=False,
    )["Repair 4"]

    named = sum(row.actual_units for row in credits if row.person_name is not None)
    unassigned = sum(row.actual_units for row in credits if row.person_name is None)
    assert (named, unassigned, named + unassigned) == (18.0, 12.0, 30.0)


def test_disabling_total_fallback_never_allocates_total_only_output():
    credits = credit_work_segments(
        [WorkSegment("Repair 4", "Alex", t(12), t(13), "odoo", 101)],
        wc_totals={"Repair 4": 20},
        samples_by_wc={},
        productive_minutes=lambda *_args: 60,
        allow_total_fallback=False,
    )["Repair 4"]

    assert [(row.person_name, row.actual_units) for row in credits] == [("Alex", 0.0)]


def test_unassigned_runs_use_original_sample_adjacency_and_active_interval_identity():
    samples = [
        (t(12, 5), 2),
        (t(12, 10), 3),
        (t(12, 15), 5),  # assigned: splits the uncovered stream
        (t(12, 20), 7),
        (t(12, 35), 11),  # second active interval: splits again
        (t(12, 40), 13),
    ]

    runs = unassigned_runs_for_samples(
        samples,
        {t(12, 15)},
        ((t(12), t(12, 30)), (t(12, 30), t(13))),
        wc_name="Repair 4",
    )

    assert [
        (run.wc_name, run.start_utc, run.end_utc, run.units, run.sample_count) for run in runs
    ] == [
        ("Repair 4", t(12, 5), t(12, 10), 5.0, 2),
        ("Repair 4", t(12, 20), t(12, 20), 7.0, 1),
        ("Repair 4", t(12, 35), t(12, 40), 24.0, 2),
    ]


@pytest.mark.parametrize(
    ("samples", "intervals", "message"),
    [
        ([(datetime(2026, 8, 20, 12), 1)], ((t(12), t(13)),), "aware UTC"),
        ([(t(12), 0)], ((t(12), t(13)),), "positive"),
        ([(t(12), -1)], ((t(12), t(13)),), "positive"),
        ([(t(12), 1)], ((t(13), t(12)),), "positive"),
        (
            [(t(12), 1)],
            ((t(12), t(13)), (t(12, 30), t(13, 30))),
            "overlap",
        ),
    ],
)
def test_unassigned_runs_reject_malformed_samples_and_intervals(samples, intervals, message):
    with pytest.raises((TypeError, ValueError), match=message):
        unassigned_runs_for_samples(samples, set(), intervals)


def test_unassigned_runs_require_assigned_times_to_be_aware_utc():
    with pytest.raises((TypeError, ValueError), match="aware UTC"):
        unassigned_runs_for_samples(
            [(t(12), 1)],
            {datetime(2026, 8, 20, 12)},
            ((t(12), t(13)),),
        )
