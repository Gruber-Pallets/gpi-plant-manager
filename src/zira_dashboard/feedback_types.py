from dataclasses import dataclass


IMPROVEMENT_CONTRACT_VERSION = 2
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


@dataclass(frozen=True)
class FeedbackType:
    value: str
    label: str
    description: str
    odoo_value: str


FEEDBACK_TYPES = (
    FeedbackType("bug", "Bug", "Something in this app is broken", "Digital"),
    FeedbackType(
        "feature",
        "New Feature",
        "An idea to make this app better",
        "Digital - New Feature",
    ),
    FeedbackType(
        "floor_issue",
        "Floor Issue",
        "Something wrong out on the floor",
        "Physical - Issue",
    ),
    FeedbackType(
        "floor_suggestion",
        "Floor Suggestion",
        "An idea for the team to consider",
        "Physical - Suggestion",
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
