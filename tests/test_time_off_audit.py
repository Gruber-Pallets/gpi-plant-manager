from datetime import date

from zira_dashboard._schema import SCHEMA_DDL
from zira_dashboard import time_off_audit


def test_decision_schema_supports_namespaced_structured_requests():
    decision_ddl = SCHEMA_DDL.split("CREATE TABLE IF NOT EXISTS time_off_decisions", 1)[1]
    assert "request_kind    TEXT NOT NULL DEFAULT 'time_off'" in decision_ddl
    assert "request_key     TEXT" in decision_ddl
    assert "detail          JSONB" in decision_ddl
    assert "ADD COLUMN IF NOT EXISTS request_kind TEXT NOT NULL DEFAULT 'time_off'" in decision_ddl


def test_record_decision_inserts_denormalized_row(monkeypatch):
    calls = []
    monkeypatch.setattr(
        time_off_audit.db, "execute", lambda sql, params: calls.append((sql, params))
    )

    time_off_audit.record_decision(
        request_id=55,
        odoo_leave_id=99,
        person_odoo_id=7,
        person_name="Maria Delgado",
        leave_type="PTO",
        date_from=date(2026, 6, 30),
        date_to=date(2026, 7, 2),
        hour_from=8.5,
        hour_to=12.25,
        action="deny",
        result_state="refuse",
        reason="Coverage too thin",
        actor_upn="dale@gruberpallets.com",
        actor_name="Dale Gruber",
        source="page",
    )

    assert len(calls) == 1
    sql, params = calls[0]
    assert "INSERT INTO time_off_decisions" in sql
    assert "hour_from, hour_to" in sql
    assert params[0] == 55 and "Maria Delgado" in params
    assert 8.5 in params and 12.25 in params
    assert "deny" in params and "Coverage too thin" in params
    assert params[-3:] == ("time_off", None, None)


def test_record_decision_accepts_namespaced_request_and_json_detail(monkeypatch):
    calls = []
    monkeypatch.setattr(
        time_off_audit.db, "execute", lambda sql, params: calls.append((sql, params))
    )

    detail = {"original_absence_leave_id": 70, "pto_leave_id": 71}
    time_off_audit.record_decision(
        request_id=41,
        odoo_leave_id=71,
        person_odoo_id=44,
        person_name="Maria Example",
        leave_type="Paid Time Off",
        date_from=date(2026, 8, 20),
        date_to=date(2026, 8, 20),
        hour_from=None,
        hour_to=None,
        action="approve",
        result_state="validate",
        reason=None,
        actor_upn="dale@example.com",
        actor_name="Dale",
        source="page",
        request_kind="absence_pto",
        request_key="absence_pto:41",
        detail=detail,
    )

    sql, params = calls[0]
    assert "request_kind, request_key, detail" in sql
    assert params[-3:-1] == ("absence_pto", "absence_pto:41")
    assert params[-1].adapted == detail


def test_recent_decisions_queries_window(monkeypatch):
    captured = {}

    def fake_query(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{"action": "approve", "person_name": "Ana Flores"}]

    monkeypatch.setattr(time_off_audit.db, "query", fake_query)

    rows = time_off_audit.recent_decisions(days=30)

    assert rows and rows[0]["person_name"] == "Ana Flores"
    assert "FROM time_off_decisions" in captured["sql"]
    assert "hour_from, hour_to" in captured["sql"]
    assert "ORDER BY decided_at DESC" in captured["sql"]
    assert captured["params"] == (30,)
