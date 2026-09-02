import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .feedback_types import REVIEW_ACTIONS, REVIEW_EVENT_MARKER


ReviewAction = Literal["accept", "decline", "assign", "complete", "move_l10"]


@dataclass(frozen=True)
class ReviewEvent:
    event_id: str
    action: ReviewAction
    actor_odoo_user_id: int
    actor_employee_id: int
    occurred_at: str
    detail: str | None
    target_odoo_user_id: int | None = None


_BLOCK_PATTERN = re.compile(
    rf"<p><strong>{re.escape(REVIEW_EVENT_MARKER)}</strong></p>\s*<ul>(.*?)</ul>",
    re.DOTALL,
)
_ITEM_PATTERN = re.compile(r"\s*<li>([^<>]*)</li>", re.DOTALL)
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
)


def _is_positive_id(value: object) -> bool:
    return type(value) is int and value > 0


def _is_utc_timestamp(value: object) -> bool:
    if type(value) is not str or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        return False
    return True


def _validate_event(event: ReviewEvent) -> None:
    if type(event.event_id) is not str or not event.event_id.strip():
        raise ValueError("event ID must be non-empty text")
    if event.action not in REVIEW_ACTIONS:
        raise ValueError("unsupported review action")
    if not _is_positive_id(event.actor_odoo_user_id):
        raise ValueError("actor Odoo user ID must be a positive integer")
    if not _is_positive_id(event.actor_employee_id):
        raise ValueError("actor employee ID must be a positive integer")
    if not _is_utc_timestamp(event.occurred_at):
        raise ValueError("event time must be an RFC3339 UTC timestamp")
    if event.detail is not None and type(event.detail) is not str:
        raise ValueError("detail must be text or None")
    if event.action in {"decline", "complete"} and not (event.detail or "").strip():
        raise ValueError("detail is required for decline and complete")
    if event.action == "assign":
        if not _is_positive_id(event.target_odoo_user_id):
            raise ValueError("assign requires a positive target Odoo user ID")
    elif event.target_odoo_user_id is not None:
        raise ValueError("target Odoo user ID is only valid for assign")


def encode_review_event(event: ReviewEvent) -> str:
    _validate_event(event)
    items = [
        ("Event ID", event.event_id),
        ("Action", event.action),
        ("Actor Odoo user ID", str(event.actor_odoo_user_id)),
        ("Actor employee ID", str(event.actor_employee_id)),
        ("Time UTC", event.occurred_at),
        ("Detail", event.detail or ""),
    ]
    if event.action == "assign":
        items.append(("Target Odoo user ID", str(event.target_odoo_user_id)))
    encoded_items = "\n".join(
        f"  <li>{label}: {html.escape(value, quote=True)}</li>"
        for label, value in items
    )
    return (
        f"<p><strong>{REVIEW_EVENT_MARKER}</strong></p>\n"
        f"<ul>\n{encoded_items}\n</ul>"
    )


def _parse_positive_id(value: str) -> int | None:
    if re.fullmatch(r"[0-9]+", value) is None:
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _parse_block(block: str) -> ReviewEvent | None:
    values: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(block):
        match = _ITEM_PATTERN.match(block, cursor)
        if match is None:
            if block[cursor:].strip():
                return None
            break
        raw_item = html.unescape(match.group(1))
        separator = raw_item.find(": ")
        if separator < 0:
            return None
        values.append((raw_item[:separator], raw_item[separator + 2 :]))
        cursor = match.end()

    action = next((value for label, value in values if label == "Action"), None)
    labels = [label for label, _value in values]
    expected_labels = [
        "Event ID",
        "Action",
        "Actor Odoo user ID",
        "Actor employee ID",
        "Time UTC",
        "Detail",
    ]
    if action == "assign":
        expected_labels.append("Target Odoo user ID")
    if labels != expected_labels:
        return None

    fields = dict(values)
    actor_odoo_user_id = _parse_positive_id(fields["Actor Odoo user ID"])
    actor_employee_id = _parse_positive_id(fields["Actor employee ID"])
    target_odoo_user_id = (
        _parse_positive_id(fields["Target Odoo user ID"])
        if action == "assign"
        else None
    )
    if actor_odoo_user_id is None or actor_employee_id is None:
        return None
    if action == "assign" and target_odoo_user_id is None:
        return None

    event = ReviewEvent(
        event_id=fields["Event ID"],
        action=action,  # type: ignore[arg-type]
        actor_odoo_user_id=actor_odoo_user_id,
        actor_employee_id=actor_employee_id,
        occurred_at=fields["Time UTC"],
        detail=fields["Detail"] or None,
        target_odoo_user_id=target_odoo_user_id,
    )
    try:
        _validate_event(event)
    except ValueError:
        return None
    return event


def parse_review_events(description_html: object) -> tuple[ReviewEvent, ...]:
    if type(description_html) is not str:
        return ()
    events: list[ReviewEvent] = []
    for match in _BLOCK_PATTERN.finditer(description_html):
        event = _parse_block(match.group(1))
        if event is not None:
            events.append(event)
    return tuple(events)
