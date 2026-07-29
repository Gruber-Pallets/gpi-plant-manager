from datetime import date
from types import SimpleNamespace

import pytest

from zira_dashboard import (
    company_holidays,
    optional_workday,
    saturday_recruiting_store,
    staffing,
)


SATURDAY = date(2026, 11, 28)
BLACK_FRIDAY = date(2026, 11, 27)
THANKSGIVING = date(2026, 11, 26)
WEDNESDAY = date(2026, 11, 25)


def _holiday(day: date, *, odoo_id: int = 42, name: str = "Black Friday"):
    return SimpleNamespace(
        odoo_id=odoo_id,
        name=name,
        date_from=day,
        date_to=day,
    )


def _bundle(
    *,
    status: str = "published",
    day_kind: str = "holiday",
    holiday_odoo_id: int | None = 42,
):
    return SimpleNamespace(
        recruitment=SimpleNamespace(
            status=status,
            day_kind=day_kind,
            holiday_odoo_id=holiday_odoo_id,
        )
    )


def test_ordinary_saturday_is_optional(monkeypatch):
    monkeypatch.setattr(company_holidays, "for_day", lambda _day: None)

    assert optional_workday.for_day(SATURDAY) == optional_workday.OptionalWorkday(
        SATURDAY, "saturday", "Saturday", None
    )


def test_weekday_holiday_is_optional(monkeypatch):
    monkeypatch.setattr(company_holidays, "for_day", lambda day: _holiday(day))

    assert optional_workday.for_day(BLACK_FRIDAY) == optional_workday.OptionalWorkday(
        BLACK_FRIDAY, "holiday", "Black Friday", 42
    )


def test_holiday_takes_precedence_when_it_falls_on_saturday(monkeypatch):
    monkeypatch.setattr(
        company_holidays,
        "for_day",
        lambda _day: _holiday(SATURDAY, name="Founders Day"),
    )

    assert optional_workday.for_day(SATURDAY) == optional_workday.OptionalWorkday(
        SATURDAY, "holiday", "Founders Day", 42
    )


def test_normal_weekday_is_not_optional(monkeypatch):
    monkeypatch.setattr(company_holidays, "for_day", lambda _day: None)

    assert optional_workday.for_day(WEDNESDAY) is None


@pytest.mark.parametrize(
    ("bundle", "schedule_published"),
    [
        (None, True),
        (_bundle(), False),
        (_bundle(holiday_odoo_id=99), True),
        (_bundle(status="recruiting"), True),
        (_bundle(day_kind="saturday"), True),
    ],
)
def test_holiday_publication_fails_closed_without_matching_dual_publication(
    monkeypatch, bundle, schedule_published
):
    monkeypatch.setattr(company_holidays, "for_day", lambda day: _holiday(day))
    monkeypatch.setattr(saturday_recruiting_store, "get", lambda _day: bundle)
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=schedule_published),
    )

    state = optional_workday.state_for_day(BLACK_FRIDAY)

    assert state is not None
    assert state.schedule_published is schedule_published
    assert state.operational is False
    assert optional_workday.holiday_is_explicitly_published(BLACK_FRIDAY) is False


def test_matching_holiday_recruiting_and_schedule_publication_is_operational(
    monkeypatch,
):
    monkeypatch.setattr(company_holidays, "for_day", lambda day: _holiday(day))
    monkeypatch.setattr(saturday_recruiting_store, "get", lambda _day: _bundle())
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=True),
    )

    state = optional_workday.state_for_day(BLACK_FRIDAY)

    assert state == optional_workday.OptionalWorkdayState(
        workday=optional_workday.OptionalWorkday(BLACK_FRIDAY, "holiday", "Black Friday", 42),
        recruiting_status="published",
        schedule_published=True,
        operational=True,
    )
    assert optional_workday.holiday_is_explicitly_published(BLACK_FRIDAY) is True


def test_removed_holiday_history_cannot_reopen_a_normal_weekday(monkeypatch):
    monkeypatch.setattr(company_holidays, "for_day", lambda _day: None)
    monkeypatch.setattr(saturday_recruiting_store, "get", lambda _day: _bundle())
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=True),
    )

    assert optional_workday.for_day(BLACK_FRIDAY) is None
    assert optional_workday.state_for_day(BLACK_FRIDAY) is None
    assert optional_workday.holiday_is_explicitly_published(BLACK_FRIDAY) is False


def test_adjacent_normal_workdays_skip_consecutive_holidays(monkeypatch):
    holidays = {THANKSGIVING, BLACK_FRIDAY}
    monkeypatch.setattr(
        company_holidays,
        "for_day",
        lambda day: _holiday(day) if day in holidays else None,
    )
    weekdays = frozenset(range(5))

    assert optional_workday.previous_normal_workday(date(2026, 11, 28), weekdays) == WEDNESDAY
    assert optional_workday.next_normal_workday(WEDNESDAY, weekdays) == date(2026, 11, 30)


def test_adjacent_normal_workday_does_not_count_optional_saturday(monkeypatch):
    monkeypatch.setattr(company_holidays, "for_day", lambda _day: None)
    monday = date(2026, 11, 30)

    assert optional_workday.previous_normal_workday(monday, frozenset(range(6))) == date(
        2026, 11, 27
    )


def test_adjacent_normal_workday_search_is_bounded(monkeypatch):
    monkeypatch.setattr(company_holidays, "for_day", lambda _day: None)

    with pytest.raises(optional_workday.NoNormalWorkday):
        optional_workday.previous_normal_workday(BLACK_FRIDAY, frozenset())
    with pytest.raises(optional_workday.NoNormalWorkday):
        optional_workday.next_normal_workday(BLACK_FRIDAY, frozenset())
