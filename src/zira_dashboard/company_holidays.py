"""Transactional local mirror of whole-company Odoo holidays."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import logging
from threading import RLock
from typing import Any

from . import _http_cache, db, odoo_client, staffing
from .shift_config import SITE_TZ


_log = logging.getLogger(__name__)


class InvalidHolidayRow(ValueError):
    """An Odoo company-holiday row cannot be mirrored safely."""


@dataclass(frozen=True)
class CompanyHoliday:
    odoo_id: int
    name: str
    date_from: date
    date_to: date
    odoo_date_from: str
    odoo_date_to: str


@dataclass(frozen=True)
class HolidaySyncHealth:
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    last_error: str | None


_cache_lock = RLock()
_holidays_by_day: dict[date, CompanyHoliday] = {}


def _odoo_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise InvalidHolidayRow("holiday datetime must be text")
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise InvalidHolidayRow("holiday datetime is invalid") from exc


def _plant_date(value: object) -> date:
    return _odoo_utc(value).astimezone(SITE_TZ).date()


def normalize_odoo_row(row: Mapping[str, object]) -> CompanyHoliday:
    """Validate one Odoo row and convert its UTC bounds to plant dates."""
    if not isinstance(row, Mapping):
        raise InvalidHolidayRow("holiday row must be a mapping")

    odoo_id = row.get("id")
    if isinstance(odoo_id, bool) or not isinstance(odoo_id, int) or odoo_id <= 0:
        raise InvalidHolidayRow("holiday id must be a positive integer")

    raw_name = row.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise InvalidHolidayRow("holiday name must not be blank")

    raw_from = row.get("date_from")
    raw_to = row.get("date_to")
    utc_from = _odoo_utc(raw_from)
    utc_to = _odoo_utc(raw_to)
    if utc_to < utc_from:
        raise InvalidHolidayRow("holiday end must not be before its start")

    local_from = _plant_date(raw_from)
    local_to = _plant_date(raw_to)
    if local_to < local_from:
        raise InvalidHolidayRow("holiday end date must not be before its start")

    assert isinstance(raw_from, str)
    assert isinstance(raw_to, str)
    return CompanyHoliday(
        odoo_id=odoo_id,
        name=raw_name.strip(),
        date_from=local_from,
        date_to=local_to,
        odoo_date_from=raw_from,
        odoo_date_to=raw_to,
    )


def _date_value(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _from_mirror_row(row: Mapping[str, Any]) -> CompanyHoliday:
    return CompanyHoliday(
        odoo_id=int(row["odoo_id"]),
        name=str(row["name"]),
        date_from=_date_value(row["date_from"]),
        date_to=_date_value(row["date_to"]),
        odoo_date_from=str(row["odoo_date_from"]),
        odoo_date_to=str(row["odoo_date_to"]),
    )


def reload() -> dict[date, CompanyHoliday]:
    """Reload every persisted holiday and atomically replace the date cache."""
    rows = db.query(
        "SELECT odoo_id, name, date_from, date_to, "
        "odoo_date_from, odoo_date_to "
        "FROM company_holidays ORDER BY odoo_id"
    )
    holidays = [_from_mirror_row(row) for row in rows]

    with _cache_lock:
        replacement: dict[date, CompanyHoliday] = {}
        for holiday in holidays:
            day = holiday.date_from
            while day <= holiday.date_to:
                existing = replacement.get(day)
                if existing is None:
                    replacement[day] = holiday
                else:
                    winner = min(existing, holiday, key=lambda item: item.odoo_id)
                    replacement[day] = winner
                    _log.warning(
                        "company holiday overlap on %s between Odoo ids %s and %s; using %s",
                        day,
                        existing.odoo_id,
                        holiday.odoo_id,
                        winner.odoo_id,
                    )
                day += timedelta(days=1)
        global _holidays_by_day
        _holidays_by_day = replacement
        return dict(_holidays_by_day)


def for_day(day: date) -> CompanyHoliday | None:
    with _cache_lock:
        return _holidays_by_day.get(day)


def for_range(start: date, end: date) -> list[dict]:
    """Return unique mirrored holidays overlapping an inclusive date range."""
    if end < start:
        return []

    found: dict[int, CompanyHoliday] = {}
    with _cache_lock:
        day = start
        while day <= end:
            holiday = _holidays_by_day.get(day)
            if holiday is not None:
                found[holiday.odoo_id] = holiday
            day += timedelta(days=1)

    return [
        {
            "id": holiday.odoo_id,
            "name": holiday.name,
            "date_from": holiday.date_from.isoformat(),
            "date_to": holiday.date_to.isoformat(),
            "calendar_id": False,
        }
        for holiday in sorted(found.values(), key=lambda item: (item.date_from, item.odoo_id))
    ]


def sync_health() -> HolidaySyncHealth:
    rows = db.query(
        "SELECT last_success_at, last_attempt_at, last_error "
        "FROM company_holiday_sync_state WHERE singleton = TRUE"
    )
    if not rows:
        return HolidaySyncHealth(None, None, None)
    row = rows[0]
    return HolidaySyncHealth(
        last_success_at=row["last_success_at"],
        last_attempt_at=row["last_attempt_at"],
        last_error=row["last_error"],
    )


def has_synced() -> bool:
    return sync_health().last_success_at is not None


def _record_failure(attempted_at: datetime, exc: Exception) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO company_holiday_sync_state "
            "(singleton, last_attempt_at, last_error) "
            "VALUES (TRUE, %s, %s) "
            "ON CONFLICT (singleton) DO UPDATE SET "
            "last_attempt_at = EXCLUDED.last_attempt_at, "
            "last_error = EXCLUDED.last_error",
            (attempted_at, str(exc)[:500]),
        )


def refresh(
    *,
    fetcher: Callable[[], list[dict]] | None = None,
    now: datetime | None = None,
) -> int:
    """Replace the mirror from one complete, validated Odoo response."""
    attempted_at = now or datetime.now(UTC)
    fetch = fetcher or odoo_client.fetch_company_holidays

    try:
        raw_rows = fetch()
        if not isinstance(raw_rows, list):
            raise InvalidHolidayRow("holiday response must be a list")
        holidays = [normalize_odoo_row(row) for row in raw_rows]
        ids = [holiday.odoo_id for holiday in holidays]
        if len(ids) != len(set(ids)):
            raise InvalidHolidayRow("holiday response contains duplicate ids")

        with db.cursor() as cur:
            for holiday in holidays:
                cur.execute(
                    "INSERT INTO company_holidays "
                    "(odoo_id, name, date_from, date_to, odoo_date_from, "
                    "odoo_date_to, last_pulled_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (odoo_id) DO UPDATE SET "
                    "name = EXCLUDED.name, "
                    "date_from = EXCLUDED.date_from, "
                    "date_to = EXCLUDED.date_to, "
                    "odoo_date_from = EXCLUDED.odoo_date_from, "
                    "odoo_date_to = EXCLUDED.odoo_date_to, "
                    "last_pulled_at = EXCLUDED.last_pulled_at, "
                    "updated_at = now()",
                    (
                        holiday.odoo_id,
                        holiday.name,
                        holiday.date_from,
                        holiday.date_to,
                        holiday.odoo_date_from,
                        holiday.odoo_date_to,
                        attempted_at,
                    ),
                )
            if ids:
                cur.execute(
                    "DELETE FROM company_holidays WHERE NOT (odoo_id = ANY(%s))",
                    (ids,),
                )
            else:
                cur.execute("DELETE FROM company_holidays")
            cur.execute(
                "INSERT INTO company_holiday_sync_state "
                "(singleton, last_success_at, last_attempt_at, last_error) "
                "VALUES (TRUE, %s, %s, NULL) "
                "ON CONFLICT (singleton) DO UPDATE SET "
                "last_success_at = EXCLUDED.last_success_at, "
                "last_attempt_at = EXCLUDED.last_attempt_at, "
                "last_error = NULL",
                (attempted_at, attempted_at),
            )

        reload()
        staffing.invalidate_all_schedule_caches()
        _http_cache.invalidate_all_cache()
        return len(holidays)
    except Exception as exc:
        try:
            _record_failure(attempted_at, exc)
        except Exception:  # noqa: BLE001 - retain the original refresh error
            _log.exception("could not record company holiday refresh failure")
        _log.exception("company holiday refresh failed")
        raise


__all__ = [
    "CompanyHoliday",
    "HolidaySyncHealth",
    "InvalidHolidayRow",
    "for_day",
    "for_range",
    "has_synced",
    "normalize_odoo_row",
    "refresh",
    "reload",
    "sync_health",
]
