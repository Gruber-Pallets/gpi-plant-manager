"""DB-free unit tests for feedback_store helpers."""

from zira_dashboard import feedback_store


def test_feedback_store_has_no_unvalidated_insert_bypass():
    assert not hasattr(feedback_store, "insert")


def test_for_submitter_clamps_limit_and_filters(monkeypatch):
    seen = []

    def fake_query(sql, params):
        seen.append((sql, params))
        return []

    monkeypatch.setattr(feedback_store.db, "query", fake_query)

    feedback_store.for_submitter("dale@x.com", limit=0)
    feedback_store.for_submitter("dale@x.com", limit=9999)

    assert "WHERE submitter = %s" in seen[0][0]
    assert seen[0][1] == ("dale@x.com", 1)
    assert seen[1][1] == ("dale@x.com", 500)


def test_for_submitter_uses_default_limit_for_invalid_values(monkeypatch):
    seen = []
    monkeypatch.setattr(
        feedback_store.db, "query",
        lambda sql, params: seen.append((sql, params)) or [],
    )
    feedback_store.for_submitter("dale@x.com", limit="nope")
    assert seen[0][1] == ("dale@x.com", 100)
