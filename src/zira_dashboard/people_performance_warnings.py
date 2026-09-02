from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import blake2s
from typing import Literal, TypeAlias

from . import shift_config
from .wc_dashboard_data import dashboard_url_for_wc_day


WarningKind: TypeAlias = Literal[
    "production_metric_unavailable",
    "production_data_unavailable",
    "forklift_data_unavailable",
    "forklift_identity_conflict",
    "unmatched_forklift_calls",
    "forklift_timeline_incomplete",
    "attendance_source_stale",
    "attendance_data_unavailable",
]
WarningActionId: TypeAlias = Literal[
    "check_again",
    "open_work_center",
    "review_settings",
    "review_identities",
    "open_diagnostics",
]


@dataclass(frozen=True)
class WarningAction:
    action_id: WarningActionId
    label: str
    href: str | None = None


@dataclass(frozen=True)
class DashboardWarning:
    key: str
    kind: WarningKind
    label: str
    title: str
    summary: str
    source: str
    subject: str
    reason_code: str
    impact: str
    checked_at_utc: datetime
    last_success_at_utc: datetime | None = None
    facts: tuple[tuple[str, str], ...] = ()
    actions: tuple[WarningAction, ...] = ()


def warning_key(kind: WarningKind, subject: str) -> str:
    clean_subject = str(subject).strip()
    if not clean_subject:
        raise ValueError("warning subject is required")
    return blake2s(
        f"{kind}\0{clean_subject}".encode("utf-8"), digest_size=12
    ).hexdigest()


def _checked(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError("warning timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _action(action_id: WarningActionId, label: str, href: str | None = None):
    return WarningAction(action_id, label, href)


def _local_time(value: datetime) -> str:
    return _checked(value).astimezone(shift_config.SITE_TZ).strftime("%-I:%M %p")


def _make_warning(
    *,
    kind: WarningKind,
    label: str,
    title: str,
    summary: str,
    source: str,
    subject: str,
    reason_code: str,
    impact: str,
    checked_at_utc: datetime,
    last_success_at_utc: datetime | None = None,
    facts: tuple[tuple[str, str], ...] = (),
    actions: tuple[WarningAction, ...] = (),
) -> DashboardWarning:
    return DashboardWarning(
        key=warning_key(kind, subject),
        kind=kind,
        label=label,
        title=title,
        summary=summary,
        source=source,
        subject=subject,
        reason_code=reason_code,
        impact=impact,
        checked_at_utc=_checked(checked_at_utc),
        last_success_at_utc=(
            _checked(last_success_at_utc)
            if last_success_at_utc is not None
            else None
        ),
        facts=facts,
        actions=actions,
    )


_PRODUCTION_REASON_COPY = {
    "missing_totals": "No production total was available for this work center.",
    "incomplete_data": "The latest production total is incomplete.",
    "duplicate_data": "More than one production total claimed this work center.",
    "missing_goal": "This work center does not have an active production goal.",
    "metric_mismatch": "The production total did not match this work center's meter.",
    "calculation_failure": "Plant Manager could not safely calculate this production result.",
}


def production_metric_warning(
    *, station_name: str, reason_code: str, checked_at_utc: datetime, day: date
) -> DashboardWarning:
    explanation = _PRODUCTION_REASON_COPY.get(reason_code)
    if explanation is None:
        raise ValueError("unknown production warning reason")
    check = _action("check_again", "Check again")
    center = _action(
        "open_work_center",
        "Open work center dashboard",
        dashboard_url_for_wc_day(station_name, day),
    )
    actions = (check, center)
    if reason_code == "missing_goal":
        actions += (
            _action(
                "review_settings",
                "Review settings",
                "/settings?section=work_centers",
            ),
        )
    return _make_warning(
        kind="production_metric_unavailable",
        label=f"Production metric unavailable: {station_name}",
        title=f"{station_name} production is unavailable",
        summary=explanation,
        source="production",
        subject=station_name,
        reason_code=reason_code,
        impact="Production, goal progress, uptime, and downtime are hidden for this work center.",
        checked_at_utc=checked_at_utc,
        facts=(("Work center", station_name),),
        actions=actions,
    )


def production_source_warning(*, checked_at_utc: datetime) -> DashboardWarning:
    return _make_warning(
        kind="production_data_unavailable",
        label="Production data unavailable",
        title="Production data is unavailable",
        summary="Plant Manager could not read the production source.",
        source="production",
        subject="production-source",
        reason_code="source_unavailable",
        impact="Production values are hidden while attendance and forklift information stay visible.",
        checked_at_utc=checked_at_utc,
        actions=(
            _action("check_again", "Check again"),
            _action(
                "open_diagnostics",
                "Open diagnostics",
                "/settings?section=diagnostics",
            ),
        ),
    )


def forklift_source_warning(
    *, checked_at_utc: datetime, last_success_at_utc: datetime | None
) -> DashboardWarning:
    return _make_warning(
        kind="forklift_data_unavailable",
        label="Forklift data unavailable",
        title="Forklift data is unavailable",
        summary="Plant Manager does not have a complete forklift call snapshot.",
        source="forklift",
        subject="forklift-source",
        reason_code="source_unavailable",
        impact="Forklift calls, on-time results, handling time, and scores are hidden.",
        checked_at_utc=checked_at_utc,
        last_success_at_utc=last_success_at_utc,
        actions=(
            _action("check_again", "Check again"),
            _action(
                "open_diagnostics",
                "Open diagnostics",
                "/settings?section=diagnostics",
            ),
        ),
    )


def forklift_identity_conflict_warning(
    *,
    identity_count: int,
    checked_at_utc: datetime,
    last_success_at_utc: datetime | None,
    day: date,
) -> DashboardWarning:
    return _make_warning(
        kind="forklift_identity_conflict",
        label="Forklift driver identity conflict",
        title="Forklift driver identities conflict",
        summary="One or more outside driver identities cannot be assigned safely.",
        source="forklift",
        subject="forklift-identity-conflict",
        reason_code="identity_conflict",
        impact="Conflicting drivers do not receive forklift calls or scores on the People page.",
        checked_at_utc=checked_at_utc,
        last_success_at_utc=last_success_at_utc,
        facts=(("Conflicting identities", str(identity_count)),),
        actions=(
            _action("check_again", "Check again"),
            _action(
                "review_identities",
                "Review identities",
                f"/settings?section=forklift&identity_day={day.isoformat()}#forklift-identities",
            ),
        ),
    )


def unmatched_forklift_warning(
    *,
    call_count: int,
    identities: tuple[tuple[str, tuple[str, ...], int], ...],
    first_call_utc: datetime,
    last_call_utc: datetime,
    checked_at_utc: datetime,
    last_success_at_utc: datetime | None,
    day: date,
) -> DashboardWarning:
    shown_identities = identities[:20]
    identity_summary = "; ".join(
        f"{driver_id} ({', '.join(names) or 'name unavailable'}) — {count} calls"
        for driver_id, names, count in shown_identities
    )
    if len(identities) > len(shown_identities):
        identity_summary += f"; +{len(identities) - len(shown_identities)} more"
    return _make_warning(
        kind="unmatched_forklift_calls",
        label=f"Unmatched forklift calls: {call_count}",
        title="Forklift calls need an employee match",
        summary="Forklift calls could not be matched to active employees.",
        source="forklift",
        subject="unmatched-forklift-calls",
        reason_code="identity_unmatched",
        impact="These calls and their results are not credited to a person.",
        checked_at_utc=checked_at_utc,
        last_success_at_utc=last_success_at_utc,
        facts=(
            ("Unmatched calls", str(call_count)),
            ("Distinct identities", str(len(identities))),
            ("External identities", identity_summary),
            ("First call", _local_time(first_call_utc)),
            ("Last call", _local_time(last_call_utc)),
        ),
        actions=(
            _action("check_again", "Check again"),
            _action(
                "review_identities",
                "Review identities",
                f"/settings?section=forklift&identity_day={day.isoformat()}#forklift-identities",
            ),
        ),
    )


def forklift_timeline_warning(
    *, checked_at_utc: datetime, last_success_at_utc: datetime | None
) -> DashboardWarning:
    return _make_warning(
        kind="forklift_timeline_incomplete",
        label="Forklift timeline incomplete",
        title="Forklift timeline is incomplete",
        summary="Stored driver totals do not match the available call details.",
        source="forklift",
        subject="forklift-timeline",
        reason_code="incomplete_data",
        impact="Affected forklift totals may show, but timeline and score details stay unavailable.",
        checked_at_utc=checked_at_utc,
        last_success_at_utc=last_success_at_utc,
        actions=(
            _action("check_again", "Check again"),
            _action(
                "open_diagnostics",
                "Open diagnostics",
                "/settings?section=diagnostics",
            ),
        ),
    )


def attendance_stale_warning(
    *, blocker_count: int, checked_at_utc: datetime
) -> DashboardWarning:
    return _make_warning(
        kind="attendance_source_stale",
        label="Attendance source stale",
        title="Attendance has not updated on time",
        summary="Plant Manager is keeping the last safe attendance snapshot.",
        source="attendance",
        subject="attendance-source",
        reason_code="stale_source",
        impact="People locations may be older than the check time until attendance catches up.",
        checked_at_utc=checked_at_utc,
        facts=(("Freshness checks blocked", str(blocker_count)),),
        actions=(
            _action("check_again", "Check again"),
            _action(
                "open_diagnostics",
                "Open diagnostics",
                "/settings?section=diagnostics",
            ),
        ),
    )


def attendance_source_warning(*, checked_at_utc: datetime) -> DashboardWarning:
    return _make_warning(
        kind="attendance_data_unavailable",
        label="Attendance data unavailable",
        title="Attendance data is unavailable",
        summary="Plant Manager could not load a safe attendance snapshot.",
        source="attendance",
        subject="attendance-source",
        reason_code="source_unavailable",
        impact="The People list is empty because attendance owns page membership and location.",
        checked_at_utc=checked_at_utc,
        actions=(
            _action("check_again", "Check again"),
            _action(
                "open_diagnostics",
                "Open diagnostics",
                "/settings?section=diagnostics",
            ),
        ),
    )
