"""Unit tests for the Odoo feedback-task helpers (execute is stubbed)."""

from zira_dashboard import odoo_client


def _stub(monkeypatch):
    calls = []
    responses = []

    def fake(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        return responses.pop(0) if responses else None

    monkeypatch.setattr(odoo_client, "execute", fake)
    odoo_client._reset_cache_for_tests()
    return calls, responses


def test_ensure_feedback_project_reuses_existing(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.extend([
        [{"id": 7}],                       # project search_read → found
        [{"name": "New"}, {"name": "In Progress"},
         {"name": "Done"}, {"name": "Rejected"}],  # stages search_read → all present
    ])

    pid = odoo_client.ensure_feedback_project()

    assert pid == 7
    assert calls[0][0:2] == ("project.project", "search_read")
    assert all(c[1] != "create" or c[0] != "project.project" for c in calls)


def test_ensure_feedback_project_creates_when_absent(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.extend([
        [],        # project search_read → none
        11,        # project create → id
        [],        # stages search_read → none present
        101, 102, 103, 104,  # create the 4 stages
    ])

    pid = odoo_client.ensure_feedback_project()

    assert pid == 11
    creates = [c for c in calls if c[0] == "project.task.type" and c[1] == "create"]
    assert len(creates) == 4
    names = [c[2][0]["name"] for c in creates]
    assert names == ["New", "In Progress", "Done", "Rejected"]
    rejected = next(c[2][0] for c in creates if c[2][0]["name"] == "Rejected")
    assert rejected["fold"] is True


def test_ensure_feedback_tag_finds_then_creates(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.extend([[], 55])  # search_read → none, create → 55

    tag_id = odoo_client.ensure_feedback_tag("Bug")

    assert tag_id == 55
    assert calls[0][0:2] == ("project.tags", "search_read")
    assert calls[1][0:2] == ("project.tags", "create")
    assert calls[1][2][0]["name"] == "Bug"
