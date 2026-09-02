import json
from pathlib import Path

import pytest

from zira_dashboard import feedback_types
from zira_dashboard.feedback_types import FEEDBACK_TYPES, feedback_type


CONTRACT_PATH = Path(__file__).parents[1] / "docs/odoo/contracts/2s-review-workflow-v2.json"


def test_feedback_types_match_odoo_reference_order_and_values():
    assert [item.value for item in FEEDBACK_TYPES] == [
        "bug",
        "feature",
        "floor_issue",
        "floor_suggestion",
        "repair",
        "two_s_improvement",
    ]
    assert [item.label for item in FEEDBACK_TYPES] == [
        "Bug",
        "New Feature",
        "Floor Issue",
        "Floor Suggestion",
        "Repair",
        "2s Improvement",
    ]
    assert [item.odoo_value for item in FEEDBACK_TYPES] == [
        "Digital",
        "Digital - New Feature",
        "Physical - Issue",
        "Physical - Suggestion",
        None,
        "2s Improvement",
    ]


def test_feedback_catalog_has_exact_six_routes():
    assert [(item.label, item.group, item.behavior) for item in FEEDBACK_TYPES] == [
        ("Bug", "reporting", "coding"),
        ("New Feature", "reporting", "coding"),
        ("Floor Issue", "reporting", "review"),
        ("Floor Suggestion", "reporting", "review"),
        ("Repair", "ready", "external"),
        ("2s Improvement", "ready", "review"),
    ]


def test_dark_v3_contract_constants_match_the_shared_contract():
    assert feedback_types.IMPROVEMENT_CONTRACT_VERSION == 3
    assert feedback_types.IMPROVEMENT_TYPE_VALUES == (
        "Digital",
        "Digital - New Feature",
        "Physical - Issue",
        "Physical - Suggestion",
        "2s Improvement",
    )
    assert feedback_types.REVIEW_IMPROVEMENT_TYPES == frozenset(
        {"Physical - Issue", "Physical - Suggestion", "2s Improvement"}
    )
    assert feedback_types.IMPROVEMENT_STATUS_VALUES == (
        "Requested",
        "In-Progress",
        "Completed",
        "Declined",
    )
    assert feedback_types.REPAIR_URL == "https://www.gpimaintenance.com/request"
    assert [
        item.odoo_value for item in FEEDBACK_TYPES if item.odoo_value is not None
    ] == list(feedback_types.IMPROVEMENT_TYPE_VALUES)


def test_canonical_v2_contract_fixture_has_exact_shared_values():
    assert json.loads(CONTRACT_PATH.read_text()) == {
        "version": 2,
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
    }
@pytest.mark.parametrize("value", [None, "", "other", True])
def test_feedback_type_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="unsupported feedback type"):
        feedback_type(value)
