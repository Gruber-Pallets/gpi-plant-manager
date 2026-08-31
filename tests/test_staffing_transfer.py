"""Department-transfer decision logic. staffing.load_roster and odoo_client are
stubbed; no DB / Odoo needed."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from zira_dashboard import staffing, staffing_transfer, odoo_client


WIN_START = datetime(2026, 6, 2, 13, 0, tzinfo=timezone.utc)


@pytest.fixture
def roster(monkeypatch):
    people = [
        staffing.Person(name="Lauro", employee_id=5),
        staffing.Person(name="Legacy Lou", employee_id=None),
    ]
    monkeypatch.setattr(staffing, "load_roster", lambda: people)
    return people


def test_skips_when_no_employee_id(roster, monkeypatch):
    monkeypatch.setattr(odoo_client, "get_current_attendance",
                        lambda eid: (_ for _ in ()).throw(AssertionError("must not call Odoo")))
    out = staffing_transfer.decide_and_apply("Legacy Lou", "Junior #2", WIN_START)
    assert out["transfer"] == "skipped_no_employee"


def test_no_op_when_already_in_dept(roster, monkeypatch):
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda wc: 9)
    monkeypatch.setattr(odoo_client, "get_current_attendance", lambda eid: {
        "id": 1, "check_in": "2026-06-02 12:00:00",
        "department_id": 9, "department_name": "07 New",
    })
    transfer_called = {"n": 0}
    monkeypatch.setattr(odoo_client, "transfer",
                        lambda *a, **k: transfer_called.__setitem__("n", 1) or (1, 2))
    out = staffing_transfer.decide_and_apply("Lauro", "Junior #2", WIN_START)
    assert out["transfer"] == "already_in_dept"
    assert transfer_called["n"] == 0


def test_transfers_when_dept_differs(roster, monkeypatch):
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda wc: 9)
    current = {
        "id": 1, "check_in": "2026-06-02 12:00:00",
        "department_id": 3, "department_name": "01 Recycled",
    }
    monkeypatch.setattr(odoo_client, "get_current_attendance", lambda eid: current)
    captured = {}
    def fake_transfer(eid, wc, ts, current=None):
        captured.update(eid=eid, wc=wc, ts=ts, current=current)
        return (100, 200)
    monkeypatch.setattr(odoo_client, "transfer", fake_transfer)

    out = staffing_transfer.decide_and_apply("Lauro", "Junior #2", WIN_START)

    assert out["transfer"] == "moved"
    assert out["closed_id"] == 100 and out["new_id"] == 200
    assert out["from_dept"] == "01 Recycled"
    assert out["to_dept"] == "New"  # Junior #2's Location.department
    assert captured["ts"] == WIN_START
    assert captured["current"] is current


def test_authoritative_employee_id_selects_the_right_same_name_worker(monkeypatch):
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [
            staffing.Person(name="Alex", employee_id=101),
            staffing.Person(name="Alex", employee_id=202),
        ],
    )
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda _wc: 9)
    seen = []
    monkeypatch.setattr(
        odoo_client,
        "get_current_attendance",
        lambda employee_id: seen.append(employee_id) or {
            "id": 1,
            "check_in": "2026-06-02 12:00:00",
            "department_id": 9,
            "department_name": "07 New",
        },
    )

    result = staffing_transfer.decide_and_apply(
        "Alex", "Junior #2", WIN_START, employee_odoo_id=202
    )

    assert result["transfer"] == "already_in_dept"
    assert seen == [202]


def test_authoritative_employee_id_requires_matching_display_name(monkeypatch):
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [staffing.Person(name="Alex", employee_id=202)],
    )
    monkeypatch.setattr(
        odoo_client,
        "get_current_attendance",
        lambda _employee_id: pytest.fail("name guard must run before Odoo"),
    )

    with pytest.raises(ValueError, match="does not match"):
        staffing_transfer.decide_and_apply(
            "Another Alex", "Junior #2", WIN_START, employee_odoo_id=202
        )


def test_transfer_ts_clamps_to_checkin(roster, monkeypatch):
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda wc: 9)
    monkeypatch.setattr(odoo_client, "get_current_attendance", lambda eid: {
        "id": 1, "check_in": "2026-06-02 14:00:00",  # AFTER window start
        "department_id": 3, "department_name": "01 Recycled",
    })
    captured = {}
    monkeypatch.setattr(
        odoo_client,
        "transfer",
        lambda eid, wc, ts, current=None: captured.update(ts=ts) or (1, 2),
    )
    staffing_transfer.decide_and_apply("Lauro", "Junior #2", WIN_START)
    assert captured["ts"] == datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)


def test_opens_new_punch_when_none(roster, monkeypatch):
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda wc: 9)
    monkeypatch.setattr(odoo_client, "get_current_attendance", lambda eid: None)
    captured = {}
    monkeypatch.setattr(odoo_client, "clock_in",
                        lambda eid, wc, ts: captured.update(eid=eid, wc=wc, ts=ts) or 300)
    out = staffing_transfer.decide_and_apply("Lauro", "Junior #2", WIN_START)
    assert out["transfer"] == "opened"
    assert out["new_id"] == 300
    assert captured["ts"] == WIN_START


def test_transfers_when_current_dept_unknown(roster, monkeypatch):
    """Dept field unconfigured -> current department_id is None -> treat as
    differs and transfer (reversible via undo)."""
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda wc: 9)
    monkeypatch.setattr(odoo_client, "get_current_attendance", lambda eid: {
        "id": 1, "check_in": "2026-06-02 12:00:00",
        "department_id": None, "department_name": None,
    })
    captured = {}
    monkeypatch.setattr(
        odoo_client,
        "transfer",
        lambda eid, wc, ts, current=None: captured.update(called=True) or (1, 2),
    )
    out = staffing_transfer.decide_and_apply("Lauro", "Junior #2", WIN_START)
    assert out["transfer"] == "moved"
    assert captured.get("called") is True


def test_transfers_when_wc_dept_unknown(roster, monkeypatch):
    """WC has no resolvable Odoo dept id -> still transfer (punch opens without
    a department tag; reversible)."""
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda wc: None)
    monkeypatch.setattr(odoo_client, "get_current_attendance", lambda eid: {
        "id": 1, "check_in": "2026-06-02 12:00:00",
        "department_id": 3, "department_name": "01 Recycled",
    })
    monkeypatch.setattr(odoo_client, "transfer", lambda eid, wc, ts, current=None: (1, 2))
    out = staffing_transfer.decide_and_apply("Lauro", "Junior #2", WIN_START)
    assert out["transfer"] == "moved"
