import pytest

from zira_dashboard.feedback_types import FEEDBACK_TYPES, feedback_type


def test_feedback_types_match_odoo_reference_order_and_values():
    assert [item.value for item in FEEDBACK_TYPES] == [
        "bug", "feature", "floor_issue", "floor_suggestion"
    ]
    assert [item.label for item in FEEDBACK_TYPES] == [
        "Bug", "New Feature", "Floor Issue", "Floor Suggestion"
    ]
    assert [item.odoo_value for item in FEEDBACK_TYPES] == [
        "Digital",
        "Digital - New Feature",
        "Physical - Issue",
        "Physical - Suggestion",
    ]


@pytest.mark.parametrize("value", [None, "", "other", True])
def test_feedback_type_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="unsupported feedback type"):
        feedback_type(value)
