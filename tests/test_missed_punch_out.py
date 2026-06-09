"""Pure logic for the missed-punch-out alert (no DB/Odoo)."""

import asyncio
from datetime import date, datetime, timezone

from zira_dashboard import app as app_module, missed_punch_out as mpo, odoo_client
from zira_dashboard.shift_config import SITE_TZ


def _iso(y, m, d, hh, mm):
    """A UTC ISO string the way odoo_client.fetch_open_attendances emits."""
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).isoformat()


def test_overdue_closures_flags_only_prior_day():
    today = date(2026, 6, 9)
    # 18:00 UTC on 6/8 == 13:00 site-local on 6/8 (prior day) -> overdue.
    rows = [
        {"att_id": 1, "employee_odoo_id": 10, "check_in": _iso(2026, 6, 8, 18, 0)},
        # 15:00 UTC on 6/9 == 10:00 site-local on 6/9 (today) -> NOT overdue.
        {"att_id": 2, "employee_odoo_id": 20, "check_in": _iso(2026, 6, 9, 15, 0)},
    ]
    out = mpo.overdue_closures(rows, today)
    assert [c["att_id"] for c in out] == [1]
    c = out[0]
    assert c["employee_odoo_id"] == 10
    # midnight ending the check-in day (6/8) == 6/9 00:00 site-local.
    assert c["midnight"] == datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    assert c["check_in"] == rows[0]["check_in"]


def test_overdue_closures_uses_site_local_date():
    # 04:00 UTC on 6/9 == 23:00 site-local on 6/8 (prior day) -> overdue,
    # even though the UTC date is already 6/9.
    today = date(2026, 6, 9)
    rows = [{"att_id": 5, "employee_odoo_id": 30, "check_in": _iso(2026, 6, 9, 4, 0)}]
    out = mpo.overdue_closures(rows, today)
    assert len(out) == 1
    assert out[0]["midnight"] == datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)


def test_overdue_closures_skips_bad_or_missing_check_in():
    today = date(2026, 6, 9)
    rows = [
        {"att_id": 7, "employee_odoo_id": 40, "check_in": None},
        {"att_id": 8, "employee_odoo_id": 41, "check_in": "not-a-date"},
    ]
    assert mpo.overdue_closures(rows, today) == []


def test_check_in_label_includes_date_in_site_local():
    label = mpo._check_in_label(_iso(2026, 6, 8, 18, 0))  # 13:00 local, Monday
    assert label == "1:00 PM Mon Jun 8"


def test_run_close_closes_only_prior_day_and_records(monkeypatch):
    today = date(2026, 6, 9)
    monkeypatch.setattr(odoo_client, "fetch_open_attendances", lambda: [
        {"att_id": 1, "employee_odoo_id": 10, "check_in": _iso(2026, 6, 8, 18, 0)},
        {"att_id": 2, "employee_odoo_id": 20, "check_in": _iso(2026, 6, 9, 15, 0)},
    ])
    closed, recorded = [], []
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda att, ts: closed.append((att, ts)))
    monkeypatch.setattr(mpo, "record_close",
                        lambda att, emp, ci, mid: recorded.append((att, emp, mid)))

    n = mpo.run_close(today)

    assert n == 1
    assert closed == [(1, datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ))]
    assert recorded == [(1, 10, datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ))]


def test_run_close_noop_when_all_today(monkeypatch):
    today = date(2026, 6, 9)
    monkeypatch.setattr(odoo_client, "fetch_open_attendances", lambda: [
        {"att_id": 2, "employee_odoo_id": 20, "check_in": _iso(2026, 6, 9, 15, 0)},
    ])
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda att, ts: (_ for _ in ()).throw(AssertionError("should not close")))
    monkeypatch.setattr(mpo, "record_close", lambda *a: None)
    assert mpo.run_close(today) == 0


def test_run_close_isolates_per_record_failure(monkeypatch):
    # Two overdue records; closing the first raises. The sweep must not abort:
    # the second still closes, the failed one is neither flagged nor counted.
    today = date(2026, 6, 9)
    monkeypatch.setattr(odoo_client, "fetch_open_attendances", lambda: [
        {"att_id": 1, "employee_odoo_id": 10, "check_in": _iso(2026, 6, 7, 18, 0)},
        {"att_id": 2, "employee_odoo_id": 20, "check_in": _iso(2026, 6, 8, 18, 0)},
    ])

    def _clock_out(att, ts):
        if att == 1:
            raise RuntimeError("odoo boom")

    flagged = []
    monkeypatch.setattr(odoo_client, "clock_out", _clock_out)
    monkeypatch.setattr(mpo, "record_close",
                        lambda att, emp, ci, mid: flagged.append(att))

    n = mpo.run_close(today)

    assert n == 1          # only the record that closed cleanly is counted
    assert flagged == [2]  # the failed record was never flagged; the sweep continued


def test_tick_calls_run_close_with_site_local_today(monkeypatch):
    seen = {}
    monkeypatch.setattr(mpo, "run_close", lambda today: seen.update({"today": today}))
    asyncio.run(app_module._tick_missed_punch_out())
    assert seen["today"] == datetime.now(SITE_TZ).date()
