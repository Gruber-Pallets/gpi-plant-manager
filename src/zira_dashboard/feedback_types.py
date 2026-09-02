from dataclasses import dataclass
from typing import Literal


IMPROVEMENT_CONTRACT_VERSION = 3
IMPROVEMENT_TYPE_VALUES = (
    "Digital",
    "Digital - New Feature",
    "Physical - Issue",
    "Physical - Suggestion",
    "2s Improvement",
)
REVIEW_IMPROVEMENT_TYPES = frozenset(
    {"Physical - Issue", "Physical - Suggestion", "2s Improvement"}
)
IMPROVEMENT_STATUS_VALUES = ("Requested", "In-Progress", "Completed", "Declined")
REPAIR_URL = "https://www.gpimaintenance.com/request"
REVIEW_WORKFLOW_ENABLED = False
REVIEW_EVENT_MARKER = "GPI-REVIEW-EVENT-V1"
REVIEW_ACTIONS = ("accept", "decline", "assign", "complete", "move_l10")
REVIEW_TASK_STATES = {
    "accepted": "03_approved",
    "declined": "1_canceled",
    "completed": "1_done",
}
REVIEW_TASK_PROJECT = "GPI OS Manager - TASKS"
REVIEW_TASK_STAGES = {"initial": "General", "meeting": "L10"}
REFERENCE_SYNC_SECONDS = 60
TASK_OWNER = "plant-manager"
PLANT_WRITABLE_REFERENCE_FIELDS = (
    "x_studio_linked_task",
    "x_studio_status",
    "x_studio_date_stop",
    "x_studio_completed_by",
    "x_studio_notes",
)


@dataclass(frozen=True)
class FeedbackType:
    value: str
    label: str
    description: str
    odoo_value: str | None
    group: Literal["reporting", "ready"]
    behavior: Literal["coding", "review", "external"]


FEEDBACK_TYPES = (
    FeedbackType(
        "bug",
        "Bug",
        "Something in this app is broken",
        "Digital",
        "reporting",
        "coding",
    ),
    FeedbackType(
        "feature",
        "New Feature",
        "An idea to make this app better",
        "Digital - New Feature",
        "reporting",
        "coding",
    ),
    FeedbackType(
        "floor_issue",
        "Floor Issue",
        "Something wrong out on the floor",
        "Physical - Issue",
        "reporting",
        "review",
    ),
    FeedbackType(
        "floor_suggestion",
        "Floor Suggestion",
        "An idea for the team to consider",
        "Physical - Suggestion",
        "reporting",
        "review",
    ),
    FeedbackType(
        "repair",
        "Repair",
        "Create a maintenance request",
        None,
        "ready",
        "external",
    ),
    FeedbackType(
        "two_s_improvement",
        "2s Improvement",
        "Work ready for the floor team",
        "2s Improvement",
        "ready",
        "review",
    ),
)
_BY_VALUE = {item.value: item for item in FEEDBACK_TYPES}


def feedback_type(value: object) -> FeedbackType:
    if type(value) is not str or value not in _BY_VALUE:
        raise ValueError("unsupported feedback type")
    return _BY_VALUE[value]


def feedback_type_or_legacy_bug(value: object) -> FeedbackType:
    if value is None:
        return _BY_VALUE["bug"]
    return feedback_type(value)
