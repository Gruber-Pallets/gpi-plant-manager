"""Unit tests for the Odoo feedback-task helpers (execute is stubbed)."""

import xmlrpc.client

import pytest

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
    assert callable(_odoo_feedback.find_feedback_task)
    assert callable(_odoo_feedback.ensure_feedback_stages)


def test_find_active_users_by_login_is_exact_and_bounded(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append([{"id": 17, "login": "wendy@gruberpallets.com"}])

    assert odoo_client.find_active_users_by_login(
        "wendy@gruberpallets.com"
    ) == [{"id": 17, "login": "wendy@gruberpallets.com"}]
    assert calls == [
        (
            "res.users",
            "search_read",
            (
                [
                    ("active", "=", True),
                    ("login", "=ilike", "wendy@gruberpallets.com"),
                ],
            ),
            {"fields": ["id", "login"], "limit": 2},
        )
    ]


def test_find_active_users_by_login_keeps_multiple_exact_matches(monkeypatch):
    _calls, responses = _stub(monkeypatch)
    responses.append(
        [
            {"id": 17, "login": "wendy@gruberpallets.com"},
            {"id": 18, "login": "WENDY@GRUBERPALLETS.COM"},
        ]
    )

    assert odoo_client.find_active_users_by_login(
        "wendy@gruberpallets.com"
    ) == [
        {"id": 17, "login": "wendy@gruberpallets.com"},
        {"id": 18, "login": "WENDY@GRUBERPALLETS.COM"},
    ]


def test_find_active_users_by_login_filters_wrong_echo(monkeypatch):
    _calls, responses = _stub(monkeypatch)
    responses.append([{"id": 17, "login": "other@gruberpallets.com"}])

    assert odoo_client.find_active_users_by_login(
        "wendy@gruberpallets.com"
    ) == []


def test_find_active_users_by_login_returns_empty_for_real_empty_list(
    monkeypatch,
):
    _calls, responses = _stub(monkeypatch)
    responses.append([])

    assert odoo_client.find_active_users_by_login(
        "wendy@gruberpallets.com"
    ) == []


@pytest.mark.parametrize("payload", [None, {"id": 17}])
def test_find_active_users_by_login_rejects_non_list_payloads(
    monkeypatch, payload
):
    _calls, responses = _stub(monkeypatch)
    responses.append(payload)

    with pytest.raises(RuntimeError, match="user payload"):
        odoo_client.find_active_users_by_login("wendy@gruberpallets.com")


def test_find_active_users_by_login_rejects_payload_over_fixed_bound(
    monkeypatch,
):
    _calls, responses = _stub(monkeypatch)
    responses.append(
        [
            {"id": 17, "login": "wendy@gruberpallets.com"},
            {"id": 18, "login": "WENDY@GRUBERPALLETS.COM"},
            {"id": 19, "login": "wendy@gruberpallets.com"},
        ]
    )

    with pytest.raises(RuntimeError, match="user payload"):
        odoo_client.find_active_users_by_login("wendy@gruberpallets.com")


@pytest.mark.parametrize(
    ("login", "limit"),
    [
        (" Wendy@gruberpallets.com ", 2),
        ("wendy", 2),
        ("wendy@gruberpallets.com", 1),
    ],
)
def test_find_active_users_by_login_requires_normalized_email_and_limit(
    monkeypatch, login, limit
):
    calls, _responses = _stub(monkeypatch)

    with pytest.raises(ValueError, match="normalized email"):
        odoo_client.find_active_users_by_login(login, limit=limit)
    assert calls == []


@pytest.mark.parametrize(
    "rows",
    [
        [{"id": True, "login": "wendy@gruberpallets.com"}],
        [{"id": [17, "Wendy"], "login": "wendy@gruberpallets.com"}],
        [{"id": 0, "login": "wendy@gruberpallets.com"}],
        [{"id": -1, "login": "wendy@gruberpallets.com"}],
        ["not-a-row"],
    ],
)
def test_find_active_users_by_login_rejects_malformed_rows(
    monkeypatch, rows
):
    _calls, responses = _stub(monkeypatch)
    responses.append(rows)

    with pytest.raises(RuntimeError, match="user payload"):
        odoo_client.find_active_users_by_login("wendy@gruberpallets.com")


def test_ensure_feedback_project_uses_facade_stage_helper(monkeypatch):
    _calls, responses = _stub(monkeypatch)
    responses.append([{"id": 7}])
    seeded_project_ids = []
    monkeypatch.setattr(
        odoo_client, "_ensure_feedback_stages", seeded_project_ids.append
    )

    assert odoo_client.ensure_feedback_project() == 7
    assert seeded_project_ids == [7]


def test_ensure_review_project_requires_one_exact_active_project(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append([{"id": 81}])

    assert odoo_client.ensure_review_project() == 81
    assert calls == [
        (
            "project.project",
            "search_read",
            ([
                ("name", "=", "GPI OS Manager - TASKS"),
                ("active", "=", True),
            ],),
            {"fields": ["id"], "order": "id asc", "limit": 3},
        )
    ]


@pytest.mark.parametrize("rows", [[], [{"id": 81}, {"id": 82}]])
def test_ensure_review_project_fails_closed_when_missing_or_duplicated(monkeypatch, rows):
    _calls, responses = _stub(monkeypatch)
    responses.append(rows)

    with pytest.raises(odoo_client.OdooTaskPayloadError, match="review project"):
        odoo_client.ensure_review_project()


def test_ensure_review_stage_requires_one_exact_active_associated_stage(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append([{"id": 91, "name": "General"}])

    assert odoo_client.ensure_review_stage(81, "General") == 91
    assert calls == [
        (
            "project.task.type",
            "search_read",
            ([
                ("project_ids", "in", [81]),
                ("name", "=", "General"),
                ("active", "=", True),
            ],),
            {"fields": ["id", "name"], "order": "id asc", "limit": 3},
        )
    ]


def test_find_review_task_ids_is_exact_project_scoped_and_archived_inclusive(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append([{"id": 901}])
    name = "[GPI-PM-FB-42] [Floor Issue] Save fails"

    assert odoo_client.find_review_task_ids(81, name) == [901]
    assert calls == [
        (
            "project.task",
            "search_read",
            ([('project_id', '=', 81), ('name', '=', name)],),
            {
                "fields": ["id"],
                "order": "id asc",
                "limit": 3,
                "context": {"active_test": False},
            },
        )
    ]


def test_create_feedback_review_task_sets_general_stage_dale_and_open_state(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append(901)

    assert odoo_client.create_feedback_review_task(
        project_id=81,
        stage_id=91,
        name="[GPI-PM-FB-42] [Floor Issue] Save fails",
        description_html="<p>Safe</p>",
        assignee_uid=17,
    ) == 901

    model, method, args, kwargs = calls[0]
    assert (model, method, kwargs) == ("project.task", "create", {})
    assert args[0] == {
        "project_id": 81,
        "stage_id": 91,
        "name": "[GPI-PM-FB-42] [Floor Issue] Save fails",
        "description": "<p>Safe</p>",
        "user_ids": [(6, 0, [17])],
        "state": "01_in_progress",
    }


def test_read_feedback_review_task_returns_exact_contract_fields(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append([
        {
            "id": 901,
            "name": "[GPI-PM-FB-42] [Floor Issue] Save fails",
            "project_id": [81, "GPI OS Manager - TASKS"],
            "stage_id": [91, "General"],
            "user_ids": [17],
            "state": "01_in_progress",
            "active": True,
            "description": "<p>Source: GPI Plant Manager</p>",
            "write_date": "2026-09-02 18:31:00",
        }
    ])

    assert odoo_client.read_feedback_review_task(901) == {
        "id": 901,
        "name": "[GPI-PM-FB-42] [Floor Issue] Save fails",
        "project_id": 81,
        "project_name": "GPI OS Manager - TASKS",
        "stage_id": 91,
        "stage_name": "General",
        "user_ids": [17],
        "state": "01_in_progress",
        "active": True,
        "description": "<p>Source: GPI Plant Manager</p>",
        "write_date": "2026-09-02 18:31:00",
    }
    assert calls[0][0:2] == ("project.task", "read")
    assert calls[0][3]["fields"] == [
        "id", "name", "project_id", "stage_id", "user_ids", "state", "active",
        "description", "write_date",
    ]


def test_stage_failure_leaves_project_uncached_and_retries(monkeypatch):
    import pytest

    calls, responses = _stub(monkeypatch)
    responses.extend([[{"id": 7}], [{"id": 7}]])
    seeded_project_ids = []

    def seed_stages(project_id):
        seeded_project_ids.append(project_id)
        if len(seeded_project_ids) == 1:
            raise RuntimeError("stage seeding failed")

    monkeypatch.setattr(odoo_client, "_ensure_feedback_stages", seed_stages)

    with pytest.raises(RuntimeError, match="stage seeding failed"):
        odoo_client.ensure_feedback_project()

    assert odoo_client._feedback_project_id is None
    assert odoo_client.ensure_feedback_project() == 7
    assert seeded_project_ids == [7, 7]
    project_searches = [
        call
        for call in calls
        if call[0:2] == ("project.project", "search_read")
    ]
    assert len(project_searches) == 2


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


def test_find_feedback_task_uses_exact_active_domain_and_newest_id(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append([{"id": 902}])

    task_id = odoo_client.find_feedback_task(7, "Payroll work entries need review")

    assert task_id == 902
    model, method, args, kwargs = calls[0]
    assert (model, method) == ("project.task", "search_read")
    assert args == (
        [
            ("project_id", "=", 7),
            ("name", "=", "Payroll work entries need review"),
            ("active", "=", True),
        ],
    )
    assert kwargs == {"fields": ["id"], "order": "id desc", "limit": 1}


def test_find_feedback_task_returns_none_when_exact_active_task_is_absent(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append([])

    assert odoo_client.find_feedback_task(7, "Payroll work entries need review") is None
    assert len(calls) == 1


def test_find_feedback_task_ids_uses_bounded_archived_inclusive_identity_lookup(
    monkeypatch,
):
    calls, responses = _stub(monkeypatch)
    responses.append([{"id": 901}])

    assert odoo_client.find_feedback_task_ids(
        7, "[GPI-PM-FB-42] [Bug] Save fails"
    ) == [901]

    model, method, args, kwargs = calls[0]
    assert (model, method) == ("project.task", "search_read")
    assert args == (
        [
            ("project_id", "=", 7),
            ("name", "=", "[GPI-PM-FB-42] [Bug] Save fails"),
        ],
    )
    assert kwargs == {
        "fields": ["id"],
        "order": "id asc",
        "limit": 2,
        "context": {"active_test": False},
    }


def test_find_feedback_attachment_ids_uses_bounded_archived_inclusive_identity_lookup(
    monkeypatch,
):
    calls, responses = _stub(monkeypatch)
    responses.append([{"id": 18}, {"id": 19}])

    assert odoo_client.find_feedback_attachment_ids(
        901, "GPI-PM-FB-42-before.jpg"
    ) == [18, 19]

    model, method, args, kwargs = calls[0]
    assert (model, method) == ("ir.attachment", "search_read")
    assert args == (
        [
            ("res_model", "=", "project.task"),
            ("res_id", "=", 901),
            ("name", "=", "GPI-PM-FB-42-before.jpg"),
        ],
    )
    assert kwargs == {
        "fields": ["id"],
        "order": "id asc",
        "limit": 2,
        "context": {"active_test": False},
    }


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


def test_read_feedback_attachment_returns_exact_identity_contract(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append(
        [
            {
                "id": 500,
                "name": "shot.png",
                "res_model": "project.task",
                "res_id": 900,
                "mimetype": "image/png",
            }
        ]
    )

    assert odoo_client.read_feedback_attachment(500) == {
        "id": 500,
        "name": "shot.png",
        "res_model": "project.task",
        "res_id": 900,
        "mimetype": "image/png",
    }
    assert calls == [
        (
            "ir.attachment",
            "read",
            ([500],),
            {"fields": ["id", "name", "res_model", "res_id", "mimetype"]},
        )
    ]


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


def test_find_feedback_stage_ids_is_exact_project_scoped_and_bounded(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append([{"id": 8, "name": "Done"}])

    assert odoo_client.find_feedback_stage_ids(3, "Done") == [8]
    assert calls == [
        (
            "project.task.type",
            "search_read",
            ([('project_ids', 'in', [3]), ('name', '=', 'Done')],),
            {"fields": ["id", "name"], "order": "id asc", "limit": 2},
        )
    ]


def test_read_feedback_task_returns_verified_identity_and_stage(monkeypatch):
    calls, responses = _stub(monkeypatch)
    responses.append([
        {
            "id": 55,
            "name": "[GPI-PM-FB-42] [Bug] Save fails",
            "project_id": [3, "Plant Manager"],
            "active": True,
            "stage_id": [8, "Done"],
            "state": "1_done",
        }
    ])

    assert odoo_client.read_feedback_task(55) == {
        "id": 55,
        "name": "[GPI-PM-FB-42] [Bug] Save fails",
        "project_id": 3,
        "active": True,
        "stage_id": 8,
        "stage_name": "Done",
        "state": "1_done",
    }
    assert calls[0][0:2] == ("project.task", "search_read")
    assert calls[0][3]["fields"] == ["id", "name", "project_id", "active", "stage_id", "state"]
    assert calls[0][3]["limit"] == 2


@pytest.mark.parametrize(
    "row",
    [
        {"id": 55, "name": "x", "project_id": [3, "Plant Manager"], "active": True, "stage_id": False},
        {"id": 55, "name": "x", "project_id": [3, "Plant Manager"], "active": True, "stage_id": [True, "Done"]},
        {"id": 56, "name": "x", "project_id": [3, "Plant Manager"], "active": True, "stage_id": [8, "Done"]},
        {"id": 55, "name": "x", "project_id": [3, "Plant Manager"], "active": True, "stage_id": [8, "Done"]},
        {
            "id": 55, "name": "x", "project_id": [3, "Plant Manager"], "active": True,
            "stage_id": [8, "Done"], "state": False,
        },
    ],
)
def test_read_feedback_task_rejects_malformed_identity_or_stage(monkeypatch, row):
    _calls, responses = _stub(monkeypatch)
    responses.append([row])

    with pytest.raises(odoo_client.OdooTaskPayloadError):
        odoo_client.read_feedback_task(55)


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
