from datetime import date, datetime, time

import pytest

from zira_dashboard import optional_workday, saturday_recruiting as sr
from zira_dashboard.shift_config import SITE_TZ


def test_deadline_is_previous_configured_workday_start():
    starts = {date(2026, 7, 24): time(7, 0)}
    assert sr.response_deadline(
        date(2026, 7, 25), frozenset({0, 1, 2, 3, 4}), starts.__getitem__
    ) == datetime(2026, 7, 24, 7, 0, tzinfo=SITE_TZ)


def test_deadline_label_is_consistent_and_explicit():
    value = datetime(2026, 7, 24, 7, 0, tzinfo=SITE_TZ)
    assert sr.format_deadline(value) == "Friday, July 24 at 7:00 AM"


def test_partial_hours_label_uses_half_hour_range():
    assert sr.format_time_range(time(7, 0), time(11, 30)) == "7:00 AM–11:30 AM"


def test_deadline_skips_nonworking_friday():
    starts = {date(2026, 7, 23): time(6, 30)}
    assert sr.response_deadline(
        date(2026, 7, 25), frozenset({0, 1, 2, 3}), starts.__getitem__
    ) == datetime(2026, 7, 23, 6, 30, tzinfo=SITE_TZ)


def test_black_friday_deadline_skips_thanksgiving():
    black_friday = date(2026, 11, 27)
    thanksgiving = date(2026, 11, 26)
    starts = {date(2026, 11, 25): time(7, 0)}

    assert sr.response_deadline(
        black_friday,
        frozenset(range(5)),
        starts.__getitem__,
        is_holiday=lambda day: day in {thanksgiving, black_friday},
    ) == datetime(2026, 11, 25, 7, 0, tzinfo=SITE_TZ)


def test_preclassified_deadline_does_not_recheck_displayed_day_holiday():
    black_friday = date(2026, 11, 27)
    thanksgiving = date(2026, 11, 26)
    prior_workday = date(2026, 11, 25)
    holiday_calls = []

    def is_holiday(candidate):
        holiday_calls.append(candidate)
        return candidate == thanksgiving

    assert sr.response_deadline(
        black_friday,
        frozenset(range(5)),
        lambda day: time(7) if day == prior_workday else time(6),
        is_holiday=is_holiday,
        classified_optional_day=optional_workday.OptionalWorkday(
            black_friday,
            "holiday",
            "Black Friday",
            42,
        ),
    ) == datetime(2026, 11, 25, 7, 0, tzinfo=SITE_TZ)
    assert holiday_calls == [thanksgiving, prior_workday]


def test_deadline_searches_past_consecutive_holidays():
    friday = date(2026, 12, 25)
    holidays = {
        date(2026, 12, 23),
        date(2026, 12, 24),
        friday,
    }
    starts = {date(2026, 12, 22): time(6, 30)}

    assert sr.response_deadline(
        friday,
        frozenset(range(5)),
        starts.__getitem__,
        is_holiday=holidays.__contains__,
    ) == datetime(2026, 12, 22, 6, 30, tzinfo=SITE_TZ)


def test_deadline_search_retains_fourteen_day_bound():
    holiday = date(2026, 12, 25)

    with pytest.raises(sr.SaturdayRecruitingError):
        sr.response_deadline(
            holiday,
            frozenset(),
            lambda _day: time(7),
            is_holiday=lambda day: day == holiday,
        )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (time(5, 30), time(10, 0)),
        (time(6, 0), time(12, 30)),
        (time(8, 15), time(10, 0)),
        (time(10, 0), time(10, 0)),
    ],
)
def test_partial_rejects_invalid_boundaries(start, end):
    with pytest.raises(sr.InvalidAvailability):
        sr.validate_availability(start, end, time(6, 0), time(12, 0))


def test_partial_accepts_half_hour_boundaries():
    sr.validate_availability(time(7, 0), time(11, 30), time(6, 0), time(12, 0))


def _opening(wc_id, count, *skills):
    return sr.Opening(wc_id, f"WC {wc_id}", count, tuple(skills))


def test_eligibility_requires_level_two_in_every_skill():
    openings = [_opening(10, 1, "Repair", "Forklift")]
    assert sr.eligible_work_centers({"Repair": 3, "Forklift": 2}, openings) == {10}
    assert sr.eligible_work_centers({"Repair": 3, "Forklift": 1}, openings) == set()
    assert sr.eligible_work_centers({"Repair": 3, "Forklift": 4}, openings) == set()


def test_matcher_rematches_multiskilled_person():
    openings = [_opening(10, 1, "Repair"), _opening(20, 1, "Dismantle")]
    result = sr.match_commitments(
        openings,
        [
            sr.Commitment(1, frozenset({10, 20})),
            sr.Commitment(2, frozenset({10})),
        ],
    )
    assert result.wc_by_person == {1: 20, 2: 10}


def test_matcher_rejects_impossible_skill_mix():
    openings = [_opening(10, 1, "Repair"), _opening(20, 1, "Dismantle")]
    assert sr.match_commitments(
        openings,
        [
            sr.Commitment(1, frozenset({10})),
            sr.Commitment(2, frozenset({10})),
        ],
    ) is None
