import json
from dataclasses import replace
from pathlib import Path

import pytest

from zira_dashboard import feedback_types
from zira_dashboard.feedback_review_events import (
    ReviewEvent,
    encode_review_event,
    parse_review_events,
)


CONTRACT_PATH = (
    Path(__file__).parents[1] / "docs/odoo/contracts/2s-review-workflow-v3.json"
)
CANONICAL_EVENT = ReviewEvent(
    event_id="018f2f2e-1234-7abc-8def-1234567890ab",
    action="complete",
    actor_odoo_user_id=7,
    actor_employee_id=41,
    occurred_at="2026-09-02T18:30:00Z",
    detail="Guard fixed < safely",
    target_odoo_user_id=None,
)


def test_canonical_v3_contract_and_runtime_constants_match():
    assert json.loads(CONTRACT_PATH.read_text()) == {
        "version": 3,
        "model": "x_2s_improvements",
        "types": [
            "Digital",
            "Digital - New Feature",
            "Physical - Issue",
            "Physical - Suggestion",
            "2s Improvement",
        ],
        "reviewTypes": ["Physical - Issue", "Physical - Suggestion", "2s Improvement"],
        "statuses": ["Requested", "In-Progress", "Completed", "Declined"],
        "taskStates": {
            "accepted": "03_approved",
            "declined": "1_canceled",
            "completed": "1_done",
        },
        "project": "GPI OS Manager - TASKS",
        "stages": {"initial": "General", "meeting": "L10"},
        "repairUrl": "https://www.gpimaintenance.com/request",
        "taskOwner": "plant-manager",
        "referenceSyncSeconds": 60,
        "reviewEventMarker": "GPI-REVIEW-EVENT-V1",
        "actions": ["accept", "decline", "assign", "complete", "move_l10"],
        "plantWritableReferenceFields": [
            "x_studio_linked_task",
            "x_studio_status",
            "x_studio_date_stop",
            "x_studio_completed_by",
            "x_studio_notes",
        ],
    }
    assert feedback_types.IMPROVEMENT_CONTRACT_VERSION == 3
    assert feedback_types.REVIEW_WORKFLOW_ENABLED is False
    assert feedback_types.REVIEW_EVENT_MARKER == "GPI-REVIEW-EVENT-V1"
    assert feedback_types.REVIEW_ACTIONS == (
        "accept",
        "decline",
        "assign",
        "complete",
        "move_l10",
    )
    assert feedback_types.REVIEW_TASK_STATES == {
        "accepted": "03_approved",
        "declined": "1_canceled",
        "completed": "1_done",
    }
    assert feedback_types.REVIEW_TASK_PROJECT == "GPI OS Manager - TASKS"
    assert feedback_types.REVIEW_TASK_STAGES == {"initial": "General", "meeting": "L10"}
    assert feedback_types.REPAIR_URL == "https://www.gpimaintenance.com/request"
    assert feedback_types.REFERENCE_SYNC_SECONDS == 60


def test_review_event_round_trip_preserves_escaped_detail():
    encoded = encode_review_event(CANONICAL_EVENT)

    assert encoded == """<p><strong>GPI-REVIEW-EVENT-V1</strong></p>
<ul>
  <li>Event ID: 018f2f2e-1234-7abc-8def-1234567890ab</li>
  <li>Action: complete</li>
  <li>Actor Odoo user ID: 7</li>
  <li>Actor employee ID: 41</li>
  <li>Time UTC: 2026-09-02T18:30:00Z</li>
  <li>Detail: Guard fixed &lt; safely</li>
</ul>"""
    assert parse_review_events(encoded) == (CANONICAL_EVENT,)


def test_review_event_assign_requires_and_round_trips_target():
    event = replace(CANONICAL_EVENT, action="assign", detail=None, target_odoo_user_id=12)

    encoded = encode_review_event(event)

    assert "<li>Target Odoo user ID: 12</li>" in encoded
    assert parse_review_events(encoded) == (event,)


@pytest.mark.parametrize(
    "event",
    [
        replace(CANONICAL_EVENT, actor_odoo_user_id=True),
        replace(CANONICAL_EVENT, actor_employee_id=0),
        replace(CANONICAL_EVENT, occurred_at="2026-09-02T13:30:00-05:00"),
        replace(CANONICAL_EVENT, detail=""),
        replace(CANONICAL_EVENT, action="erase"),
        replace(CANONICAL_EVENT, target_odoo_user_id=12),
        replace(CANONICAL_EVENT, action="assign", detail=None),
    ],
)
def test_review_event_encoder_rejects_invalid_events(event):
    with pytest.raises(ValueError):
        encode_review_event(event)


def test_review_event_parser_ignores_incomplete_or_unknown_blocks():
    malformed = """<p>GPI-REVIEW-EVENT-V1</p><p>Action: erase</p>
<p><strong>GPI-REVIEW-EVENT-V1</strong></p>
<ul>
  <li>Event ID: unsafe</li>
  <li>Action: erase</li>
  <li>Actor Odoo user ID: true</li>
  <li>Actor employee ID: 41</li>
  <li>Time UTC: 2026-09-02T18:30:00Z</li>
  <li>Detail: nope</li>
</ul>"""

    assert parse_review_events(malformed) == ()
    assert parse_review_events(f"{malformed}\n{encode_review_event(CANONICAL_EVENT)}") == (
        CANONICAL_EVENT,
    )
