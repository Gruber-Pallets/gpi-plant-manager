"""Unit tests for the Odoo feedback-task helpers (execute is stubbed)."""

import xmlrpc.client

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


def test_feedback_operations_live_in_private_module():
    from zira_dashboard import _odoo_feedback

    assert odoo_client.FEEDBACK_PROJECT_NAME == _odoo_feedback.FEEDBACK_PROJECT_NAME
    assert odoo_client.FEEDBACK_STAGES is _odoo_feedback.FEEDBACK_STAGES
    assert odoo_client.FEEDBACK_DONE_STAGE == _odoo_feedback.FEEDBACK_DONE_STAGE
    assert odoo_client.FEEDBACK_REJECTED_STAGE == _odoo_feedback.FEEDBACK_REJECTED_STAGE
    assert callable(_odoo_feedback.find_or_create_feedback_project)
    assert callable(_odoo_feedback.ensure_feedback_stages)


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


def test_create_feedback_task_uses_user_ids_and_tag_and_deadline(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append(900)  # create → task id

    task_id = odoo_client.create_feedback_task(
        project_id=7, name="[Bug] x", description_html="<p>x</p>",
        assignee_uid=3, tag_id=55, deadline="2026-06-24",
    )

    assert task_id == 900
    model, method, args, kwargs = calls[0]
    assert (model, method) == ("project.task", "create")
    vals = args[0]
    assert vals["name"] == "[Bug] x"
    assert vals["project_id"] == 7
    assert vals["date_deadline"] == "2026-06-24"
    assert vals["user_ids"] == [(6, 0, [3])]
    assert vals["tag_ids"] == [(6, 0, [55])]


def test_create_feedback_task_falls_back_to_user_id(monkeypatch):
    calls = []
    state = {"first": True}

    def fake(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if state["first"]:
            state["first"] = False
            raise xmlrpc.client.Fault(2, "Invalid field 'user_ids'")
        return 901

    monkeypatch.setattr(odoo_client, "execute", fake)
    odoo_client._reset_cache_for_tests()

    task_id = odoo_client.create_feedback_task(
        project_id=7, name="x", description_html="x",
        assignee_uid=3, tag_id=None, deadline="2026-06-24",
    )

    assert task_id == 901
    assert "user_ids" in calls[0][2][0]
    assert calls[1][2][0]["user_id"] == 3
    assert "tag_ids" not in calls[1][2][0]


def test_add_task_attachment_creates_ir_attachment(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append(500)

    att_id = odoo_client.add_task_attachment(
        task_id=900, filename="shot.png", mimetype="image/png", raw_bytes=b"abc",
    )

    assert att_id == 500
    model, method, args, kwargs = calls[0]
    assert (model, method) == ("ir.attachment", "create")
    vals = args[0]
    assert vals["name"] == "shot.png"
    assert vals["res_model"] == "project.task"
    assert vals["res_id"] == 900
    assert vals["mimetype"] == "image/png"
    import base64
    assert base64.b64decode(vals["datas"]) == b"abc"


def test_fetch_task_stage_names_maps_id_to_name(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append([
        {"id": 900, "stage_id": [3, "In Progress"]},
        {"id": 901, "stage_id": [4, "Done"]},
        {"id": 902, "stage_id": False},
    ])

    out = odoo_client.fetch_task_stage_names([900, 901, 902])

    assert out == {900: "In Progress", 901: "Done", 902: None}
    assert calls[0][0:2] == ("project.task", "read")


def test_fetch_task_stage_names_empty_input_skips_call(monkeypatch):
    calls, _ = _stub(monkeypatch)
    assert odoo_client.fetch_task_stage_names([]) == {}
    assert calls == []


def test_feedback_status_bucket():
    assert odoo_client.feedback_status_bucket("Done") == "done"
    assert odoo_client.feedback_status_bucket("Rejected") == "rejected"
    assert odoo_client.feedback_status_bucket("New") == "open"
    assert odoo_client.feedback_status_bucket("In Progress") == "open"
    assert odoo_client.feedback_status_bucket(None) == "open"


def test_create_feedback_task_reraises_unrelated_fault(monkeypatch):
    calls = []

    def fake(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        raise xmlrpc.client.Fault(1, "AccessError: not allowed")

    monkeypatch.setattr(odoo_client, "execute", fake)
    odoo_client._reset_cache_for_tests()

    import pytest
    with pytest.raises(xmlrpc.client.Fault):
        odoo_client.create_feedback_task(
            project_id=7, name="x", description_html="x",
            assignee_uid=3, tag_id=None, deadline="2026-06-24",
        )
    # Only the first create was attempted; no blind retry on an unrelated fault.
    assert len(calls) == 1
