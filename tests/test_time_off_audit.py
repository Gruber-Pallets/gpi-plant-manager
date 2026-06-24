from datetime import date

from zira_dashboard import time_off_audit


def test_record_decision_inserts_denormalized_row(monkeypatch):
    calls = []
    monkeypatch.setattr(time_off_audit.db, "execute",
                        lambda sql, params: calls.append((sql, params)))

    time_off_audit.record_decision(
        request_id=55, odoo_leave_id=99, person_odoo_id=7,
        person_name="Maria Delgado", leave_type="PTO",
        date_from=date(2026, 6, 30), date_to=date(2026, 7, 2),
        hour_from=8.5, hour_to=12.25,
        action="deny", result_state="refuse", reason="Coverage too thin",
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber",
        source="page",
    )

    assert len(calls) == 1
    sql, params = calls[0]
    assert "INSERT INTO time_off_decisions" in sql
    assert "hour_from, hour_to" in sql
    assert params[0] == 55 and "Maria Delgado" in params
    assert 8.5 in params and 12.25 in params
    assert "deny" in params and "Coverage too thin" in params


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
