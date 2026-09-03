from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from zira_dashboard import feedback_store
from zira_dashboard import feedback_review_reconciler as reconciler
from zira_dashboard.feedback_review_events import ReviewEvent
from zira_dashboard.odoo_improvements import ImprovementContract, MalformedMutationResponse


NOW = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)
EVENT_TIME = "2026-09-02T18:30:00Z"
CONTRACT = ImprovementContract(start_type="date", stop_type="date", version=3)


def event(action: str, *, detail: str | None = None, event_id: str = "event-1") -> ReviewEvent:
    return ReviewEvent(
        event_id=event_id,
        action=action,
        actor_odoo_user_id=7,
        actor_employee_id=41,
        occurred_at=EVENT_TIME,
        detail=detail,
        target_odoo_user_id=12 if action == "assign" else None,
    )


def task(state: str, *, description: str | None = None) -> dict[str, object]:
    return {
        "id": 55,
        "name": "Renamed by the reviewer",
        "project_id": 81,
        "project_name": "GPI OS Manager - TASKS",
        "stage_id": 91,
        "stage_name": "L10" if state != "01_in_progress" else "General",
        "user_ids": [12],
        "state": state,
        "active": True,
        "description": description
        or (
            "<p>Request</p><p><strong>Source:</strong> GPI Plant Manager<br>"
            "<strong>Source ID:</strong> GPI-PM-FB-42<br>"
            "<strong>Submitted by:</strong> operator@example.com</p>"
        ),
        "write_date": "2026-09-02 20:00:00",
    }


@pytest.mark.parametrize(
    ("state", "events", "expected"),
    [
        ("01_in_progress", (), "Requested"),
        ("03_approved", (event("accept"),), "In-Progress"),
        ("03_approved", (event("assign"),), "In-Progress"),
        ("03_approved", (event("move_l10"),), "In-Progress"),
        ("1_canceled", (event("decline", detail="Not useful"),), "Declined"),
        ("1_done", (event("complete", detail="Guard fixed"),), "Completed"),
    ],
)
def test_task_lifecycle_maps_only_matching_review_events(state, events, expected):
    assert reconciler.task_lifecycle(task(state), events) == expected


@pytest.mark.parametrize(
    ("state", "events"),
    [
        ("03_approved", ()),
        ("03_approved", (event("decline", detail="No"),)),
        ("1_canceled", (event("complete", detail="Done"),)),
        ("1_done", (event("decline", detail="No"),)),
        ("99_unknown", (event("complete", detail="Done"),)),
    ],
)
def test_task_lifecycle_rejects_missing_mismatched_or_unknown_state(state, events):
    assert reconciler.task_lifecycle(task(state), events) == "attention"


def test_task_lifecycle_uses_latest_matching_terminal_event():
    earlier = event(
        "complete",
        detail="Old result",
        event_id="event-earlier",
    )
    later = replace(
        earlier,
        event_id="event-later",
        occurred_at="2026-09-02T19:30:00Z",
        detail="Final result",
        actor_employee_id=52,
    )

    projection = reconciler.review_lifecycle_projection(
        task("1_done"),
        (later, earlier),
        stop_type="date",
    )

    assert projection.status == "Completed"
    assert projection.fields == {
        "x_studio_status": "Completed",
        "x_studio_date_stop": "2026-09-02",
        "x_studio_completed_by": 52,
        "x_studio_notes": "<p>Final result</p>",
    }


@pytest.mark.parametrize(
    "events",
    [
        (
            event("complete", detail="First result", event_id="event-a"),
            replace(
                event("complete", detail="Second result", event_id="event-b"),
                actor_employee_id=52,
            ),
        ),
        (
            event("accept", event_id="event-a"),
            event("move_l10", event_id="event-b"),
        ),
    ],
)
def test_equal_time_competing_latest_events_need_attention(events):
    state = "1_done" if events[0].action == "complete" else "03_approved"

    assert reconciler.task_lifecycle(task(state), events) == "attention"


def test_equal_time_competing_latest_assignments_remain_ambiguous_after_later_move():
    first = event("assign", event_id="event-a")
    competing = replace(first, event_id="event-b", target_odoo_user_id=99)
    later_move = replace(
        event("move_l10", event_id="event-c"),
        occurred_at="2026-09-02T19:30:00Z",
    )

    assert (
        reconciler.task_lifecycle(
            task("03_approved"),
            (first, competing, later_move),
        )
        == "attention"
    )


def test_terminal_state_without_valid_detail_needs_attention():
    malformed = replace(event("complete", detail="Done"), detail="")

    assert reconciler.task_lifecycle(task("1_done"), (malformed,)) == "attention"


def test_task_lifecycle_rejects_malformed_event_identity():
    malformed = replace(event("accept"), actor_employee_id=0)

    assert reconciler.task_lifecycle(task("03_approved"), (malformed,)) == "attention"


@pytest.mark.parametrize(
    "task_changes",
    [
        {"active": False},
        {"project_name": "Another project"},
        {"stage_name": "Done"},
        {"write_date": "not-a-time"},
    ],
)
def test_task_lifecycle_rejects_invalid_task_contract(task_changes):
    assert (
        reconciler.task_lifecycle(
            task("03_approved") | task_changes,
            (event("accept"),),
        )
        == "attention"
    )


def test_task_lifecycle_rejects_event_newer_than_task_write_date():
    future = replace(event("accept"), occurred_at="2026-09-02T20:00:01Z")

    assert reconciler.task_lifecycle(task("03_approved"), (future,)) == "attention"


def test_latest_assignment_must_match_current_assignee_after_later_move_event():
    assigned_elsewhere = replace(event("assign"), target_odoo_user_id=99)
    later_move = replace(
        event("move_l10"),
        event_id="event-2",
        occurred_at="2026-09-02T19:00:00Z",
    )

    assert (
        reconciler.task_lifecycle(
            task("03_approved"),
            (assigned_elsewhere, later_move),
        )
        == "attention"
    )


def candidate(**changes) -> feedback_store.ReviewCandidate:
    values = {
        "feedback_id": 42,
        "task_type": "floor_issue",
        "status": "requested",
        "projection_version": 3,
        "odoo_task_id": 55,
        "odoo_improvement_id": 71,
    }
    values.update(changes)
    return feedback_store.ReviewCandidate(**values)


def remote_reference(**changes) -> dict[str, object]:
    values: dict[str, object] = {
        "id": 71,
        "x_studio_source": "GPI Plant Manager",
        "x_studio_source_id": "GPI-PM-FB-42",
        "x_studio_type": "Physical - Issue",
        "x_studio_status": "Requested",
        "x_studio_date_stop": False,
        "x_studio_completed_by": False,
        "x_studio_notes": False,
        "x_studio_linked_task": [55, "Review task"],
        "x_studio_linked_wo": False,
    }
    values.update(changes)
    return values


class ReferenceClient:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = list(rows)
        self.writes: list[tuple[int, dict[str, object]]] = []
        self.link_task_once = MagicMock(side_effect=AssertionError("link writes are forbidden"))

    def read_contract(self):
        return CONTRACT

    def find_exact(self, source_id):
        assert source_id == "GPI-PM-FB-42"
        return [{"id": 71, "x_studio_source": "GPI Plant Manager", "x_studio_source_id": source_id}]

    def read_improvement(self, remote_id, fields, *, full_binary):
        assert remote_id == 71
        assert full_binary is False
        return self.rows.pop(0)

    def write_improvement(self, remote_id, fields, *, feedback_id, expected_contract):
        assert feedback_id == 42
        assert expected_contract == CONTRACT
        self.writes.append((remote_id, dict(fields)))


def install_task(monkeypatch, task_row, events):
    monkeypatch.setattr(
        reconciler.odoo_client,
        "read_feedback_review_task",
        MagicMock(return_value=task_row),
    )
    monkeypatch.setattr(reconciler, "parse_review_events", MagicMock(return_value=events))
    monkeypatch.setattr(
        reconciler.odoo_client,
        "ensure_review_project",
        MagicMock(return_value=81),
    )
    monkeypatch.setattr(
        reconciler.odoo_client,
        "ensure_review_stage",
        MagicMock(return_value=91),
    )


def test_process_candidate_writes_only_terminal_lifecycle_fields_then_adopts(monkeypatch):
    events = (event("complete", detail="Guard fixed < safely"),)
    install_task(monkeypatch, task("1_done"), events)
    before = remote_reference()
    after = remote_reference(
        x_studio_status="Completed",
        x_studio_date_stop="2026-09-02",
        x_studio_completed_by=[41, "Employee"],
        x_studio_notes="<p>Guard fixed &lt; safely</p>",
    )
    client = ReferenceClient([before, after])
    adopt = MagicMock(return_value=True)
    monkeypatch.setattr(reconciler.feedback_store, "adopt_review_lifecycle", adopt)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "adopted"

    assert client.writes == [
        (
            71,
            {
                "x_studio_status": "Completed",
                "x_studio_date_stop": "2026-09-02",
                "x_studio_completed_by": 41,
                "x_studio_notes": "<p>Guard fixed &lt; safely</p>",
            },
        )
    ]
    assert not (
        {
            "x_name",
            "x_studio_image",
            "x_studio_source",
            "x_studio_source_id",
            "x_studio_submitted_by",
            "x_studio_type",
            "x_studio_linked_task",
            "x_studio_linked_wo",
        }
        & client.writes[0][1].keys()
    )
    adopt.assert_called_once()
    client.link_task_once.assert_not_called()


@pytest.mark.parametrize(
    "linked_task",
    [False, [99, "Other task"]],
)
def test_empty_or_conflicting_legacy_link_fails_closed_without_link_write(monkeypatch, linked_task):
    install_task(monkeypatch, task("01_in_progress"), ())
    client = ReferenceClient([remote_reference(x_studio_linked_task=linked_task)])
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)
    monkeypatch.setattr(
        reconciler.feedback_store,
        "adopt_review_lifecycle",
        MagicMock(side_effect=AssertionError("mismatch cannot be adopted")),
    )

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "attention"

    assert client.writes == []
    client.link_task_once.assert_not_called()
    attention.assert_called_once_with(candidate(), "review_reference_link_mismatch", now=NOW)


def test_sourceFillers_in_request_text_cannot_replace_exact_structured_marker(monkeypatch):
    bad = task(
        "01_in_progress",
        description=(
            "<p>Source: GPI Plant Manager Source ID: GPI-PM-FB-42</p>"
            "<p><strong>Source:</strong> GPI Plant Manager<br>"
            "<strong>Source ID:</strong> GPI-PM-FB-99<br></p>"
        ),
    )
    install_task(monkeypatch, bad, ())
    client = ReferenceClient([remote_reference()])
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "attention"

    client.read_improvement = MagicMock(side_effect=AssertionError("bad task stops first"))
    assert client.writes == []
    attention.assert_called_once_with(candidate(), "review_task_identity_mismatch", now=NOW)


def test_expected_source_marker_plus_another_structured_identity_fails_closed(monkeypatch):
    bad = task(
        "01_in_progress",
        description=(
            "<p><strong>Source:</strong> GPI Plant Manager<br>"
            "<strong>Source ID:</strong> GPI-PM-FB-42<br></p>"
            "<p><strong>Source:</strong> GPI Plant Manager<br>"
            "<strong>Source ID:</strong> GPI-PM-FB-99<br></p>"
        ),
    )
    install_task(monkeypatch, bad, ())
    client = ReferenceClient([remote_reference()])
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "attention"

    assert client.rows == [remote_reference()]
    assert client.writes == []
    attention.assert_called_once_with(candidate(), "review_task_identity_mismatch", now=NOW)


def test_expected_and_competing_structured_identities_in_same_paragraph_fail_closed(
    monkeypatch,
):
    bad = task(
        "01_in_progress",
        description=(
            "<p><strong>Source:</strong> GPI Plant Manager<br>"
            "<strong>Source ID:</strong> GPI-PM-FB-42<br>"
            "<strong>Source:</strong> GPI Plant Manager<br>"
            "<strong>Source ID:</strong> GPI-PM-FB-99<br></p>"
        ),
    )
    install_task(monkeypatch, bad, ())
    client = ReferenceClient([remote_reference()])
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "attention"

    assert client.rows == [remote_reference()]
    assert client.writes == []
    attention.assert_called_once_with(candidate(), "review_task_identity_mismatch", now=NOW)


def test_escaped_source_marker_request_text_is_not_a_structured_identity():
    description = (
        "<p>&lt;strong&gt;Source:&lt;/strong&gt; GPI Plant Manager&lt;br&gt;"
        "&lt;strong&gt;Source ID:&lt;/strong&gt; GPI-PM-FB-99&lt;br&gt;</p>"
        "<p><strong>Source:</strong> GPI Plant Manager<br>"
        "<strong>Source ID:</strong> GPI-PM-FB-42<br></p>"
    )

    assert reconciler._has_exact_review_source_metadata(description, "GPI-PM-FB-42")


def test_malformed_initial_task_payload_records_fixed_identity_attention(monkeypatch):
    monkeypatch.setattr(
        reconciler.odoo_client,
        "read_feedback_review_task",
        MagicMock(side_effect=reconciler.odoo_client.OdooTaskPayloadError("malformed")),
    )
    client = ReferenceClient([remote_reference()])
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "attention"

    assert client.rows == [remote_reference()]
    assert client.writes == []
    attention.assert_called_once_with(candidate(), "review_task_identity_mismatch", now=NOW)


@pytest.mark.parametrize(
    "task_changes",
    [
        {"project_id": 82},
        {"stage_id": 92},
    ],
)
def test_task_must_match_exact_resolved_project_and_stage(monkeypatch, task_changes):
    task_row = task("01_in_progress") | task_changes
    install_task(monkeypatch, task_row, ())
    client = ReferenceClient([remote_reference()])
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "attention"

    assert client.writes == []


def test_unknown_write_reads_back_before_retry_and_never_blind_writes_twice(monkeypatch):
    install_task(monkeypatch, task("03_approved"), (event("accept"),))
    client = ReferenceClient([remote_reference(), remote_reference()])
    client.write_improvement = MagicMock(
        side_effect=MalformedMutationResponse("unknown acknowledgement")
    )
    adopt = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "adopt_review_lifecycle", adopt)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "retry"

    client.write_improvement.assert_called_once()
    assert client.rows == []
    adopt.assert_not_called()


def test_unknown_write_readback_that_matches_is_adopted_without_second_write(monkeypatch):
    install_task(monkeypatch, task("03_approved"), (event("accept"),))
    client = ReferenceClient([remote_reference(), remote_reference(x_studio_status="In-Progress")])
    client.write_improvement = MagicMock(
        side_effect=MalformedMutationResponse("unknown acknowledgement")
    )
    adopt = MagicMock(return_value=True)
    monkeypatch.setattr(reconciler.feedback_store, "adopt_review_lifecycle", adopt)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "adopted"

    client.write_improvement.assert_called_once()
    adopt.assert_called_once()


@pytest.mark.parametrize(
    ("local_status", "remote_status", "task_state", "events"),
    [
        ("completed", "Completed", "03_approved", (event("accept"),)),
        ("declined", "Declined", "1_done", (event("complete", detail="Done"),)),
    ],
)
def test_terminal_local_or_reference_rows_never_reopen(
    monkeypatch, local_status, remote_status, task_state, events
):
    install_task(monkeypatch, task(task_state), events)
    client = ReferenceClient([remote_reference(x_studio_status=remote_status)])
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)

    assert (
        reconciler.process_candidate(candidate(status=local_status), client=client, now=NOW)
        == "attention"
    )

    assert client.writes == []


def test_same_status_terminal_reference_detail_conflict_never_writes(monkeypatch):
    install_task(monkeypatch, task("1_done"), (event("complete", detail="Expected"),))
    client = ReferenceClient(
        [
            remote_reference(
                x_studio_status="Completed",
                x_studio_date_stop="2026-09-02",
                x_studio_completed_by=[41, "Employee"],
                x_studio_notes="<p>Different</p>",
            )
        ]
    )
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "attention"

    assert client.writes == []
    attention.assert_called_once_with(candidate(), "review_terminal_conflict", now=NOW)


def test_same_status_local_terminal_detail_conflict_stops_before_reference_write(monkeypatch):
    install_task(monkeypatch, task("1_done"), (event("complete", detail="Expected"),))
    terminal_candidate = candidate(
        status="completed",
        finished_at=datetime(2026, 9, 2, 18, 30, tzinfo=UTC),
        finished_by="odoo_employee:41",
        resolution_note="Different",
    )
    client = ReferenceClient([remote_reference()])
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)

    assert reconciler.process_candidate(terminal_candidate, client=client, now=NOW) == "attention"

    assert client.rows == [remote_reference()]
    assert client.writes == []
    attention.assert_called_once_with(terminal_candidate, "review_terminal_conflict", now=NOW)


@pytest.mark.parametrize(
    "stale_field",
    [
        {"x_studio_date_stop": "2026-09-02"},
        {"x_studio_completed_by": [41, "Employee"]},
        {"x_studio_notes": "<p>Old result</p>"},
    ],
)
def test_nonterminal_reference_with_stale_terminal_fields_fails_closed(monkeypatch, stale_field):
    install_task(monkeypatch, task("03_approved"), (event("accept"),))
    client = ReferenceClient([remote_reference(x_studio_status="In-Progress", **stale_field)])
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "attention"

    assert client.writes == []
    attention.assert_called_once_with(candidate(), "review_lifecycle_mismatch", now=NOW)


def test_malformed_terminal_employee_readback_records_fixed_attention(monkeypatch):
    install_task(monkeypatch, task("1_done"), (event("complete", detail="Done"),))
    client = ReferenceClient(
        [
            remote_reference(
                x_studio_status="Completed",
                x_studio_date_stop="2026-09-02",
                x_studio_completed_by={"id": 41},
                x_studio_notes="<p>Done</p>",
            )
        ]
    )
    attention = MagicMock()
    adoption = MagicMock(side_effect=AssertionError("malformed readback cannot adopt"))
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)
    monkeypatch.setattr(reconciler.feedback_store, "adopt_review_lifecycle", adoption)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "attention"

    assert client.writes == []
    adoption.assert_not_called()
    attention.assert_called_once_with(candidate(), "review_lifecycle_mismatch", now=NOW)


@pytest.mark.parametrize("unknown_outcome", [False, True])
def test_malformed_terminal_employee_post_write_readback_records_fixed_attention(
    monkeypatch, unknown_outcome
):
    install_task(monkeypatch, task("1_done"), (event("complete", detail="Done"),))
    client = ReferenceClient(
        [
            remote_reference(),
            remote_reference(
                x_studio_status="Completed",
                x_studio_date_stop="2026-09-02",
                x_studio_completed_by={"id": 41},
                x_studio_notes="<p>Done</p>",
            ),
        ]
    )
    if unknown_outcome:
        client.write_improvement = MagicMock(
            side_effect=MalformedMutationResponse("unknown acknowledgement")
        )
    attention = MagicMock()
    adoption = MagicMock(side_effect=AssertionError("malformed readback cannot adopt"))
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)
    monkeypatch.setattr(reconciler.feedback_store, "adopt_review_lifecycle", adoption)

    assert reconciler.process_candidate(candidate(), client=client, now=NOW) == "attention"

    adoption.assert_not_called()
    attention.assert_called_once_with(candidate(), "review_lifecycle_mismatch", now=NOW)


def test_local_terminal_detail_conflict_records_fixed_attention(monkeypatch):
    terminal_event = event("complete", detail="Done")
    install_task(monkeypatch, task("1_done"), (terminal_event,))
    client = ReferenceClient(
        [
            remote_reference(
                x_studio_status="Completed",
                x_studio_date_stop="2026-09-02",
                x_studio_completed_by=[41, "Employee"],
                x_studio_notes="<p>Done</p>",
            )
        ]
    )
    monkeypatch.setattr(
        reconciler.feedback_store,
        "adopt_review_lifecycle",
        MagicMock(side_effect=feedback_store.InvalidTransition("terminal differs")),
    )
    attention = MagicMock()
    monkeypatch.setattr(reconciler.feedback_store, "record_review_attention", attention)

    assert (
        reconciler.process_candidate(candidate(status="completed"), client=client, now=NOW)
        == "attention"
    )

    attention.assert_called_once_with(
        candidate(status="completed"), "review_terminal_conflict", now=NOW
    )


class RowsCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executions = []

    def execute(self, sql, params=None):
        self.executions.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


def test_local_adoption_aligns_both_outboxes_without_enqueueing(monkeypatch):
    locked = {
        "feedback_id": 42,
        "task_type": "floor_issue",
        "status": "requested",
        "lifecycle_origin": "local",
        "projection_version": 3,
        "odoo_task_id": 55,
        "odoo_improvement_id": 71,
        "sync_state": "idle",
        "active_attempt_id": None,
        "task_delivery_state": "delivered",
        "task_claim_owner": None,
        "task_claim_token": None,
        "task_claim_expires_at": None,
    }
    cursor = RowsCursor(
        [locked, {"id": 42, "projection_version": 4}, {"feedback_id": 42}, {"feedback_id": 42}]
    )

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    assert (
        feedback_store.adopt_review_lifecycle(
            candidate(),
            status="completed",
            finished_at=datetime(2026, 9, 2, 18, 30, tzinfo=UTC),
            finished_by_employee_id=41,
            resolution_note="Guard fixed",
            now=NOW,
        )
        is True
    )

    sql = " ".join(statement for statement, _params in cursor.executions)
    assert "UPDATE feedback SET" in sql
    assert "UPDATE feedback_odoo_sync" in sql
    assert "last_synced_version = %s" in sql
    assert "UPDATE feedback_task_delivery" in sql
    assert "state = 'delivered'" in sql
    assert "INSERT" not in sql
    assert "enqueue" not in sql.casefold()


def test_local_adoption_stops_before_updates_if_authority_changed(monkeypatch):
    locked = {
        "feedback_id": 42,
        "task_type": "floor_issue",
        "status": "requested",
        "lifecycle_origin": "legacy_project_task",
        "projection_version": 3,
        "odoo_task_id": 55,
        "odoo_improvement_id": 71,
        "sync_state": "idle",
        "active_attempt_id": None,
        "task_delivery_state": "delivered",
        "task_claim_owner": None,
        "task_claim_token": None,
        "task_claim_expires_at": None,
    }
    cursor = RowsCursor([locked])

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.adopt_review_lifecycle(
            candidate(),
            status="in_progress",
            finished_at=None,
            finished_by_employee_id=None,
            resolution_note=None,
            now=NOW,
        )

    assert len(cursor.executions) == 1


def test_local_adoption_does_not_revoke_in_flight_task_delivery_claim(monkeypatch):
    locked = {
        "feedback_id": 42,
        "task_type": "floor_issue",
        "status": "requested",
        "lifecycle_origin": "local",
        "projection_version": 3,
        "odoo_task_id": 55,
        "odoo_improvement_id": 71,
        "sync_state": "idle",
        "active_attempt_id": None,
        "task_delivery_state": "in_flight",
        "task_claim_owner": "worker-a",
        "task_claim_token": "4cf90fca-4350-4759-aa3b-31463f195be2",
        "task_claim_expires_at": NOW,
    }
    cursor = RowsCursor([locked])

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.adopt_review_lifecycle(
            candidate(),
            status="in_progress",
            finished_at=None,
            finished_by_employee_id=None,
            resolution_note=None,
            now=NOW,
        )

    assert len(cursor.executions) == 1


def test_review_candidates_require_both_exact_local_ids(monkeypatch):
    rows = [
        {
            "feedback_id": 42,
            "task_type": "floor_issue",
            "status": "requested",
            "projection_version": 3,
            "odoo_task_id": 55,
            "odoo_improvement_id": 71,
        }
    ]
    cursor = FairCandidateCursor(
        fetchones=[
            {"last_review_feedback_id": 0},
            {"last_review_feedback_id": 42},
        ],
        fetchalls=[rows],
    )

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    assert feedback_store.review_reconcile_candidates(9) == [candidate()]

    sql = " ".join(statement for statement, _params in cursor.executions)
    assert "td.odoo_task_id IS NOT NULL" in sql
    assert "s.odoo_improvement_id IS NOT NULL" in sql
    assert "f.lifecycle_origin = 'local'" in sql
    assert cursor.executions[1][1][-1] == 9


class FairCandidateCursor:
    def __init__(self, *, fetchones, fetchalls):
        self.fetchones = list(fetchones)
        self.fetchalls = list(fetchalls)
        self.executions = []

    def execute(self, sql, params=None):
        self.executions.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.fetchones.pop(0)

    def fetchall(self):
        return self.fetchalls.pop(0)


def _review_candidate_row(feedback_id):
    return {
        "feedback_id": feedback_id,
        "task_type": "floor_issue",
        "status": "requested",
        "projection_version": 3,
        "odoo_task_id": 1_000 + feedback_id,
        "odoo_improvement_id": 2_000 + feedback_id,
        "finished_at": None,
        "finished_by": None,
        "resolution_note": None,
    }


def test_review_candidates_rotate_durably_past_one_full_batch(monkeypatch):
    first = [_review_candidate_row(item) for item in range(1, 51)]
    after_cursor = [_review_candidate_row(item) for item in range(51, 61)]
    wrapped = [_review_candidate_row(item) for item in range(1, 41)]
    cursor = FairCandidateCursor(
        fetchones=[
            {"last_review_feedback_id": 0},
            {"last_review_feedback_id": 50},
            {"last_review_feedback_id": 50},
            {"last_review_feedback_id": 40},
        ],
        fetchalls=[first, after_cursor, wrapped],
    )

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)
    monkeypatch.setattr(
        feedback_store.db,
        "query",
        MagicMock(side_effect=AssertionError("rotation must use its locked durable cursor")),
    )

    first_batch = feedback_store.review_reconcile_candidates(50)
    second_batch = feedback_store.review_reconcile_candidates(50)

    assert [item.feedback_id for item in first_batch] == list(range(1, 51))
    assert [item.feedback_id for item in second_batch] == [
        *range(51, 61),
        *range(1, 41),
    ]
    sql = " ".join(statement for statement, _params in cursor.executions)
    assert "SELECT last_review_feedback_id" in sql
    assert "FOR UPDATE" in sql
    assert "UPDATE feedback_odoo_backfill_state SET last_review_feedback_id" in sql
    assert "td.state = 'delivered'" in sql
    assert "s.active_attempt_id IS NULL" in sql


def _review_batch_lease(
    *,
    owner="review-worker-a",
    token="4cf90fca-4350-4759-aa3b-31463f195be2",
):
    return feedback_store.ReviewReconcileLease(
        owner=owner,
        token=UUID(token),
        expires_at=NOW + timedelta(minutes=10),
    )


def test_first_review_batch_owner_acquires_unexpired_singleton_lease(monkeypatch):
    token = UUID("4cf90fca-4350-4759-aa3b-31463f195be2")
    expires = NOW + timedelta(minutes=10)
    cursor = RowsCursor(
        [
            {
                "review_lease_owner": "review-worker-a",
                "review_lease_token": token,
                "review_lease_expires_at": expires,
            }
        ]
    )

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)
    monkeypatch.setattr(feedback_store, "uuid4", MagicMock(return_value=token), raising=False)

    lease = feedback_store.acquire_review_reconcile_lease(
        owner="review-worker-a", now=NOW
    )

    assert lease == _review_batch_lease()
    sql, params = cursor.executions[0]
    assert "review_lease_token IS NULL" in sql
    assert "review_lease_expires_at <= %s" in sql
    assert params[-1] == NOW


def test_concurrent_review_batch_owner_cannot_acquire_active_lease(monkeypatch):
    cursor = RowsCursor([None])

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    assert (
        feedback_store.acquire_review_reconcile_lease(
            owner="review-worker-b", now=NOW
        )
        is None
    )


def test_review_batch_lease_has_ten_minute_crash_recovery_bound():
    assert feedback_store.REVIEW_RECONCILE_LEASE == timedelta(minutes=10)


def test_expired_review_batch_lease_can_be_reclaimed(monkeypatch):
    token = UUID("571edbd2-a505-4854-9ce3-a2eb833dc6f3")
    reclaimed_at = NOW + timedelta(minutes=10)
    expires = reclaimed_at + timedelta(minutes=10)
    cursor = RowsCursor(
        [
            {
                "review_lease_owner": "review-worker-b",
                "review_lease_token": token,
                "review_lease_expires_at": expires,
            }
        ]
    )

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)
    monkeypatch.setattr(feedback_store, "uuid4", MagicMock(return_value=token), raising=False)

    assert feedback_store.acquire_review_reconcile_lease(
        owner="review-worker-b", now=reclaimed_at
    ) == feedback_store.ReviewReconcileLease(
        owner="review-worker-b",
        token=token,
        expires_at=expires,
    )

    assert "review_lease_expires_at <= %s" in cursor.executions[0][0]
    assert cursor.executions[0][1][-1] == reclaimed_at


def test_review_batch_lease_release_is_token_owned(monkeypatch):
    cursor = RowsCursor([{"id": 1}, None])

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)
    owned = _review_batch_lease()
    wrong = _review_batch_lease(token="571edbd2-a505-4854-9ce3-a2eb833dc6f3")

    assert feedback_store.release_review_reconcile_lease(owned, now=NOW) is True
    assert feedback_store.release_review_reconcile_lease(wrong, now=NOW) is False

    assert all("review_lease_token = %s" in sql for sql, _params in cursor.executions)
    assert cursor.executions[0][1][-1] == owned.token
    assert cursor.executions[1][1][-1] == wrong.token


def test_review_batch_lease_renewal_requires_same_unexpired_token(monkeypatch):
    lease = _review_batch_lease()
    renewed_at = NOW + timedelta(minutes=1)
    renewed_expiry = renewed_at + timedelta(minutes=10)
    cursor = RowsCursor(
        [
            {
                "review_lease_owner": lease.owner,
                "review_lease_token": lease.token,
                "review_lease_expires_at": renewed_expiry,
            },
            None,
        ]
    )

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    assert feedback_store.renew_review_reconcile_lease(
        lease, now=renewed_at
    ) == feedback_store.ReviewReconcileLease(
        owner=lease.owner,
        token=lease.token,
        expires_at=renewed_expiry,
    )
    assert feedback_store.renew_review_reconcile_lease(
        _review_batch_lease(token="571edbd2-a505-4854-9ce3-a2eb833dc6f3"),
        now=renewed_at,
    ) is None

    sql, params = cursor.executions[0]
    assert "review_lease_token = %s" in sql
    assert "review_lease_expires_at > %s" in sql
    assert params[-2] == lease.token
    assert params[-1] == renewed_at


def test_review_candidate_claims_exact_idle_generic_sync_row(monkeypatch):
    lease = _review_batch_lease()
    cursor = RowsCursor(
        [
            {
                "feedback_id": 42,
                "odoo_improvement_id": 71,
                "state": "idle",
                "claim_owner": None,
                "claim_token": None,
                "claim_expires_at": None,
                "active_attempt_id": None,
            },
            {"feedback_id": 42},
        ]
    )

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    claimed = feedback_store.claim_review_candidate(candidate(), lease, now=NOW)

    assert claimed == candidate(
        sync_claim_owner=lease.owner,
        sync_claim_token=lease.token,
        sync_claim_expires_at=lease.expires_at,
        sync_prior_state="idle",
    )
    sql = " ".join(statement for statement, _params in cursor.executions)
    assert "FOR UPDATE" in sql
    assert "SET state = 'in_flight'" in sql
    assert "active_attempt_id IS NULL" in sql


def test_review_candidate_cannot_claim_active_generic_sync_attempt(monkeypatch):
    lease = _review_batch_lease()
    cursor = RowsCursor(
        [
            {
                "feedback_id": 42,
                "odoo_improvement_id": 71,
                "state": "in_flight",
                "claim_owner": "generic-worker",
                "claim_token": UUID("571edbd2-a505-4854-9ce3-a2eb833dc6f3"),
                "claim_expires_at": NOW + timedelta(minutes=5),
                "active_attempt_id": UUID("5d3cb65c-bc40-4cce-928e-d109cc9489f4"),
            }
        ]
    )

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    assert feedback_store.claim_review_candidate(candidate(), lease, now=NOW) is None
    assert len(cursor.executions) == 1


def test_claimed_review_adoption_clears_only_its_generic_sync_claim(monkeypatch):
    lease = _review_batch_lease()
    claimed = candidate(
        sync_claim_owner=lease.owner,
        sync_claim_token=lease.token,
        sync_claim_expires_at=lease.expires_at,
        sync_prior_state="idle",
    )
    locked = {
        "feedback_id": 42,
        "task_type": "floor_issue",
        "status": "requested",
        "lifecycle_origin": "local",
        "projection_version": 3,
        "odoo_task_id": 55,
        "odoo_improvement_id": 71,
        "sync_state": "in_flight",
        "sync_claim_owner": lease.owner,
        "sync_claim_token": lease.token,
        "sync_claim_expires_at": lease.expires_at,
        "active_attempt_id": None,
        "task_delivery_state": "delivered",
        "task_claim_owner": None,
        "task_claim_token": None,
        "task_claim_expires_at": None,
    }
    cursor = RowsCursor(
        [locked, {"id": 42, "projection_version": 4}, {"feedback_id": 42}, {"feedback_id": 42}]
    )

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    assert feedback_store.adopt_review_lifecycle(
        claimed,
        status="in_progress",
        finished_at=None,
        finished_by_employee_id=None,
        resolution_note=None,
        now=NOW,
    )

    sync_sql, sync_params = cursor.executions[2]
    assert "state = 'in_flight'" in sync_sql
    assert "claim_owner = %s" in sync_sql
    assert "claim_token = %s" in sync_sql
    assert sync_params[-2:] == (lease.owner, lease.token)


def test_run_batch_skips_concurrent_owner_before_candidates_or_rpc(monkeypatch):
    monkeypatch.setattr(reconciler, "_write_enabled", MagicMock(return_value=True))
    monkeypatch.setattr(
        reconciler.feedback_store,
        "acquire_review_reconcile_lease",
        MagicMock(return_value=None),
        raising=False,
    )
    poison = MagicMock(side_effect=AssertionError("lease loser cannot do work"))
    monkeypatch.setattr(reconciler.ImprovementsClient, "from_env", poison)
    monkeypatch.setattr(reconciler.feedback_store, "review_reconcile_candidates", poison)

    assert reconciler.run_batch() == reconciler.ReconcileResult(
        skipped="lease_unavailable"
    )

    poison.assert_not_called()


def test_run_batch_renews_and_releases_lease_after_success(monkeypatch):
    lease = _review_batch_lease()
    client = MagicMock(spec=reconciler.ImprovementsClient)
    acquire = MagicMock(return_value=lease)
    renew = MagicMock(return_value=lease)
    release = MagicMock(return_value=True)
    monkeypatch.setattr(reconciler, "_write_enabled", MagicMock(return_value=True))
    monkeypatch.setattr(
        reconciler.feedback_store, "acquire_review_reconcile_lease", acquire, raising=False
    )
    monkeypatch.setattr(
        reconciler.feedback_store, "renew_review_reconcile_lease", renew, raising=False
    )
    monkeypatch.setattr(
        reconciler.feedback_store, "release_review_reconcile_lease", release, raising=False
    )
    monkeypatch.setattr(
        reconciler.feedback_store,
        "claim_review_candidate",
        MagicMock(side_effect=lambda item, _lease, now: item),
        raising=False,
    )
    monkeypatch.setattr(reconciler.ImprovementsClient, "from_env", MagicMock(return_value=client))
    monkeypatch.setattr(
        reconciler.feedback_store, "review_reconcile_candidates", MagicMock(return_value=[candidate()])
    )
    monkeypatch.setattr(reconciler, "process_candidate", MagicMock(return_value="unchanged"))

    assert reconciler.run_batch() == reconciler.ReconcileResult(scanned=1, unchanged=1)

    renew.assert_called_once()
    release.assert_called_once()


def test_run_batch_releases_lease_when_candidate_selection_raises(monkeypatch):
    lease = _review_batch_lease()
    release = MagicMock(return_value=True)
    monkeypatch.setattr(reconciler, "_write_enabled", MagicMock(return_value=True))
    monkeypatch.setattr(
        reconciler.feedback_store,
        "acquire_review_reconcile_lease",
        MagicMock(return_value=lease),
        raising=False,
    )
    monkeypatch.setattr(
        reconciler.feedback_store, "release_review_reconcile_lease", release, raising=False
    )
    client = MagicMock(spec=reconciler.ImprovementsClient)
    monkeypatch.setattr(
        reconciler.ImprovementsClient, "from_env", MagicMock(return_value=client)
    )
    monkeypatch.setattr(
        reconciler.feedback_store,
        "review_reconcile_candidates",
        MagicMock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        reconciler.run_batch()

    release.assert_called_once()


def test_run_batch_does_not_rpc_without_exact_generic_sync_claim(monkeypatch):
    lease = _review_batch_lease()
    monkeypatch.setattr(reconciler, "_write_enabled", MagicMock(return_value=True))
    monkeypatch.setattr(
        reconciler.feedback_store,
        "acquire_review_reconcile_lease",
        MagicMock(return_value=lease),
        raising=False,
    )
    monkeypatch.setattr(
        reconciler.feedback_store,
        "renew_review_reconcile_lease",
        MagicMock(return_value=lease),
        raising=False,
    )
    monkeypatch.setattr(
        reconciler.feedback_store,
        "release_review_reconcile_lease",
        MagicMock(return_value=True),
        raising=False,
    )
    monkeypatch.setattr(
        reconciler.feedback_store,
        "claim_review_candidate",
        MagicMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr(
        reconciler.feedback_store,
        "release_review_candidate",
        MagicMock(return_value=True),
        raising=False,
    )
    client = MagicMock(spec=reconciler.ImprovementsClient)
    monkeypatch.setattr(
        reconciler.ImprovementsClient, "from_env", MagicMock(return_value=client)
    )
    monkeypatch.setattr(
        reconciler.feedback_store, "review_reconcile_candidates", MagicMock(return_value=[candidate()])
    )
    process = MagicMock(side_effect=AssertionError("unclaimed row cannot make an RPC"))
    monkeypatch.setattr(reconciler, "process_candidate", process)

    assert reconciler.run_batch() == reconciler.ReconcileResult(scanned=1, retried=1)

    process.assert_not_called()
