"""Focused tests for app-owner feedback task delivery (all Odoo calls stubbed)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from zira_dashboard import feedback_task_delivery as delivery
from zira_dashboard import feedback_task_worker as worker
from zira_dashboard import app as app_module
from zira_dashboard.feedback_image import NormalizedImage


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
EXPIRES = NOW + timedelta(minutes=2)
CLAIM = delivery.TaskDeliveryClaim(
    feedback_id=42,
    claim_token=__import__("uuid").UUID("11111111-1111-1111-1111-111111111111"),
    task_id=None,
    before_attachment_id=None,
    expires_at=EXPIRES,
)


def _image() -> NormalizedImage:
    raw = b"jpeg"
    return NormalizedImage(
        jpeg_bytes=raw,
        sha256=sha256(raw).hexdigest(),
        byte_length=len(raw),
        width=2,
        height=2,
    )


def snapshot(*, task_type: str = "bug", before_image: NormalizedImage | None = None):
    return delivery.FeedbackTaskSnapshot(
        feedback_id=42,
        task_type=task_type,
        message="Save fails\nwhen the button is pressed.",
        submitter="operator@example.com",
        page_url="/feedback",
        before_image=before_image,
    )


def saved_claim(*, task_id: int = 55, attachment_id: int | None = None):
    return delivery.TaskDeliveryClaim(
        feedback_id=CLAIM.feedback_id,
        claim_token=CLAIM.claim_token,
        task_id=task_id,
        before_attachment_id=attachment_id,
        expires_at=EXPIRES,
    )


def stub_delivery(monkeypatch, item):
    monkeypatch.setattr(worker.task_delivery, "load_snapshot", MagicMock(return_value=item))
    monkeypatch.setattr(worker.task_delivery, "record_task_id", MagicMock(return_value=saved_claim()))
    monkeypatch.setattr(
        worker.task_delivery,
        "record_before_attachment",
        MagicMock(return_value=saved_claim(attachment_id=66)),
    )
    monkeypatch.setattr(worker.task_delivery, "mark_delivered", MagicMock())
    monkeypatch.setattr(worker.task_delivery, "schedule_retry", MagicMock())
    monkeypatch.setattr(worker.task_delivery, "block", MagicMock())
    monkeypatch.setattr(
        worker.task_delivery,
        "renew_claim",
        MagicMock(side_effect=lambda claim, **_kwargs: claim),
        raising=False,
    )
    monkeypatch.setattr(worker, "_local_today", lambda: date(2026, 8, 26))


def stub_odoo(monkeypatch, *, task_ids=None, attachment_ids=None):
    find_tasks = MagicMock(return_value=[] if task_ids is None else task_ids)
    find_attachments = MagicMock(return_value=[] if attachment_ids is None else attachment_ids)
    monkeypatch.setattr(worker.odoo_client, "ensure_feedback_project", MagicMock(return_value=3))
    monkeypatch.setattr(worker.odoo_client, "find_feedback_task_ids", find_tasks)
    monkeypatch.setattr(worker.odoo_client, "find_feedback_attachment_ids", find_attachments)
    monkeypatch.setattr(worker.odoo_client, "authenticate", MagicMock(return_value=9))
    monkeypatch.setattr(worker.odoo_client, "ensure_feedback_tag", MagicMock(return_value=4))
    monkeypatch.setattr(worker.odoo_client, "create_feedback_task", MagicMock(return_value=55))
    monkeypatch.setattr(worker.odoo_client, "add_task_attachment", MagicMock(return_value=66))
    return find_tasks, find_attachments


def test_process_claim_creates_one_bug_task_for_the_authenticated_owner(monkeypatch):
    stub_delivery(monkeypatch, snapshot())
    stub_odoo(monkeypatch)

    assert worker.process_claim(CLAIM, now=NOW) == "delivered"

    worker.odoo_client.create_feedback_task.assert_called_once_with(
        project_id=3,
        name="[GPI-PM-FB-42] [Bug] Save fails",
        description_html=ANY,
        assignee_uid=9,
        tag_id=4,
        deadline="2026-08-26",
    )
    worker.odoo_client.ensure_feedback_tag.assert_called_once_with("Bug")
    worker.task_delivery.record_task_id.assert_called_once_with(CLAIM, task_id=55, now=NOW)
    worker.task_delivery.mark_delivered.assert_called_once_with(saved_claim(), now=NOW)


def test_process_claim_creates_feature_task_for_the_same_authenticated_owner(monkeypatch):
    stub_delivery(monkeypatch, snapshot(task_type="feature"))
    stub_odoo(monkeypatch)

    assert worker.process_claim(CLAIM, now=NOW) == "delivered"

    worker.odoo_client.create_feedback_task.assert_called_once_with(
        project_id=3,
        name="[GPI-PM-FB-42] [Feature] Save fails",
        description_html=ANY,
        assignee_uid=9,
        tag_id=4,
        deadline="2026-08-26",
    )
    worker.odoo_client.ensure_feedback_tag.assert_called_once_with("Feature request")


def test_process_claim_adopts_one_matching_task_without_creating(monkeypatch):
    stub_delivery(monkeypatch, snapshot())
    find_tasks, _find_attachments = stub_odoo(monkeypatch, task_ids=[55])

    assert worker.process_claim(CLAIM, now=NOW) == "delivered"

    find_tasks.assert_called_once_with(3, "[GPI-PM-FB-42] [Bug] Save fails")
    worker.odoo_client.create_feedback_task.assert_not_called()
    worker.task_delivery.record_task_id.assert_called_once_with(CLAIM, task_id=55, now=NOW)


def test_process_claim_blocks_ambiguous_matching_tasks_without_remote_writes(monkeypatch):
    stub_delivery(monkeypatch, snapshot())
    stub_odoo(monkeypatch, task_ids=[54, 55])

    assert worker.process_claim(CLAIM, now=NOW) == "blocked"

    worker.odoo_client.create_feedback_task.assert_not_called()
    worker.task_delivery.record_task_id.assert_not_called()
    worker.task_delivery.block.assert_called_once_with(
        CLAIM, "More than one matching owner task exists.", now=NOW
    )


def test_process_claim_creates_and_persists_one_before_image_attachment(monkeypatch):
    existing_task = saved_claim()
    stub_delivery(monkeypatch, snapshot(before_image=_image()))
    _find_tasks, find_attachments = stub_odoo(monkeypatch)

    assert worker.process_claim(existing_task, now=NOW) == "delivered"

    find_attachments.assert_called_once_with(55, "GPI-PM-FB-42-before.jpg")
    worker.odoo_client.add_task_attachment.assert_called_once_with(
        55, "GPI-PM-FB-42-before.jpg", "image/jpeg", b"jpeg"
    )
    worker.task_delivery.record_before_attachment.assert_called_once_with(
        existing_task, attachment_id=66, now=NOW
    )
    worker.task_delivery.mark_delivered.assert_called_once_with(
        saved_claim(attachment_id=66), now=NOW
    )


def test_process_claim_adopts_one_matching_before_image_attachment(monkeypatch):
    existing_task = saved_claim()
    stub_delivery(monkeypatch, snapshot(before_image=_image()))
    _find_tasks, find_attachments = stub_odoo(monkeypatch, attachment_ids=[66])

    assert worker.process_claim(existing_task, now=NOW) == "delivered"

    find_attachments.assert_called_once_with(55, "GPI-PM-FB-42-before.jpg")
    worker.odoo_client.add_task_attachment.assert_not_called()
    worker.task_delivery.record_before_attachment.assert_called_once_with(
        existing_task, attachment_id=66, now=NOW
    )


def test_process_claim_blocks_ambiguous_before_image_attachments(monkeypatch):
    existing_task = saved_claim()
    stub_delivery(monkeypatch, snapshot(before_image=_image()))
    stub_odoo(monkeypatch, attachment_ids=[65, 66])

    assert worker.process_claim(existing_task, now=NOW) == "blocked"

    worker.odoo_client.add_task_attachment.assert_not_called()
    worker.task_delivery.record_before_attachment.assert_not_called()
    worker.task_delivery.block.assert_called_once_with(
        existing_task, "More than one matching owner screenshot exists.", now=NOW
    )


def test_transport_failure_rechecks_then_schedules_a_retry(monkeypatch):
    stub_delivery(monkeypatch, snapshot())
    find_tasks, _find_attachments = stub_odoo(monkeypatch)
    find_tasks.side_effect = [[], []]
    worker.odoo_client.create_feedback_task.side_effect = TimeoutError("Odoo timed out")

    assert worker.process_claim(CLAIM, now=NOW) == "retried"

    assert find_tasks.call_count == 2
    worker.task_delivery.schedule_retry.assert_called_once_with(CLAIM, now=NOW)
    worker.task_delivery.record_task_id.assert_not_called()


def test_identity_lookup_transport_failure_schedules_without_a_second_lookup(monkeypatch):
    stub_delivery(monkeypatch, snapshot())
    find_tasks, _find_attachments = stub_odoo(monkeypatch)
    find_tasks.side_effect = TimeoutError("Odoo timed out")

    assert worker.process_claim(CLAIM, now=NOW) == "retried"

    find_tasks.assert_called_once_with(3, "[GPI-PM-FB-42] [Bug] Save fails")
    worker.odoo_client.create_feedback_task.assert_not_called()
    worker.task_delivery.schedule_retry.assert_called_once_with(CLAIM, now=NOW)


def test_authentication_transport_failure_schedules_without_a_second_lookup(monkeypatch):
    stub_delivery(monkeypatch, snapshot())
    find_tasks, _find_attachments = stub_odoo(monkeypatch)
    worker.odoo_client.authenticate.side_effect = TimeoutError("Odoo timed out")

    assert worker.process_claim(CLAIM, now=NOW) == "retried"

    find_tasks.assert_called_once_with(3, "[GPI-PM-FB-42] [Bug] Save fails")
    worker.odoo_client.create_feedback_task.assert_not_called()
    worker.task_delivery.schedule_retry.assert_called_once_with(CLAIM, now=NOW)


def test_transport_failure_adopts_a_task_created_before_its_lost_response(monkeypatch):
    stub_delivery(monkeypatch, snapshot())
    find_tasks, _find_attachments = stub_odoo(monkeypatch)
    find_tasks.side_effect = [[], [55]]
    worker.odoo_client.create_feedback_task.side_effect = TimeoutError("Odoo timed out")

    assert worker.process_claim(CLAIM, now=NOW) == "delivered"

    assert find_tasks.call_count == 2
    worker.task_delivery.record_task_id.assert_called_once_with(CLAIM, task_id=55, now=NOW)
    worker.task_delivery.schedule_retry.assert_not_called()


def test_attachment_lookup_transport_failure_schedules_without_a_second_lookup(monkeypatch):
    existing_task = saved_claim()
    stub_delivery(monkeypatch, snapshot(before_image=_image()))
    _find_tasks, find_attachments = stub_odoo(monkeypatch)
    find_attachments.side_effect = TimeoutError("Odoo timed out")

    assert worker.process_claim(existing_task, now=NOW) == "retried"

    find_attachments.assert_called_once_with(55, "GPI-PM-FB-42-before.jpg")
    worker.odoo_client.add_task_attachment.assert_not_called()
    worker.task_delivery.schedule_retry.assert_called_once_with(existing_task, now=NOW)


def test_attachment_timeout_adopts_matching_attachment_after_renewing_lease(monkeypatch):
    existing_task = saved_claim()
    stub_delivery(monkeypatch, snapshot(before_image=_image()))
    _find_tasks, find_attachments = stub_odoo(monkeypatch)
    find_attachments.side_effect = [[], [66]]
    worker.odoo_client.add_task_attachment.side_effect = TimeoutError("Odoo timed out")

    assert worker.process_claim(existing_task, now=NOW) == "delivered"

    assert find_attachments.call_count == 2
    worker.task_delivery.renew_claim.assert_called_once_with(existing_task, now=NOW)
    worker.odoo_client.add_task_attachment.assert_called_once_with(
        55, "GPI-PM-FB-42-before.jpg", "image/jpeg", b"jpeg"
    )
    worker.task_delivery.record_before_attachment.assert_called_once_with(
        existing_task, attachment_id=66, now=NOW
    )
    worker.task_delivery.schedule_retry.assert_not_called()


def test_lost_lease_before_task_create_prevents_remote_write(monkeypatch):
    stub_delivery(monkeypatch, snapshot())
    stub_odoo(monkeypatch)
    worker.task_delivery.renew_claim.side_effect = delivery.StateTransitionError("lease lost")

    with pytest.raises(delivery.StateTransitionError, match="lease lost"):
        worker.process_claim(CLAIM, now=NOW)

    worker.odoo_client.create_feedback_task.assert_not_called()


def test_retry_uses_the_clock_time_when_the_create_failure_is_handled(monkeypatch):
    stub_delivery(monkeypatch, snapshot())
    find_tasks, _find_attachments = stub_odoo(monkeypatch)
    find_tasks.side_effect = [[], []]
    worker.odoo_client.create_feedback_task.side_effect = TimeoutError("Odoo timed out")
    failure_time = NOW + timedelta(minutes=5)

    assert worker.process_claim(CLAIM, clock=lambda: failure_time) == "retried"

    worker.task_delivery.schedule_retry.assert_called_once_with(CLAIM, now=failure_time)


def test_task_content_is_stable_and_html_escaped():
    item = delivery.FeedbackTaskSnapshot(
        feedback_id=42,
        task_type="feature",
        message='<b>Unsafe</b>\nnext & "quoted"',
        submitter='A < B & "C"',
        page_url='javascript:alert("no")',
        before_image=None,
    )

    assert worker.task_name(item) == "[GPI-PM-FB-42] [Feature] <b>Unsafe</b>"
    assert worker.before_attachment_name(42) == "GPI-PM-FB-42-before.jpg"
    description = worker.task_description(item)
    assert "&lt;b&gt;Unsafe&lt;/b&gt;<br>next &amp; &quot;quoted&quot;" in description
    assert "A &lt; B &amp; &quot;C&quot;" in description
    assert "javascript:" not in description
    assert "Source app: GPI Plant Manager (plant)" in description
    assert "Feedback ID: 42" in description


def test_task_name_uses_feedback_when_a_saved_message_is_blank():
    item = snapshot()
    item = delivery.FeedbackTaskSnapshot(
        feedback_id=item.feedback_id,
        task_type=item.task_type,
        message=" \n ",
        submitter=item.submitter,
        page_url=item.page_url,
        before_image=item.before_image,
    )

    assert worker.task_name(item) == "[GPI-PM-FB-42] [Bug] feedback"


def test_run_batch_isolates_claim_errors_and_counts_outcomes(monkeypatch):
    other = delivery.TaskDeliveryClaim(
        feedback_id=43,
        claim_token=CLAIM.claim_token,
        task_id=None,
        before_attachment_id=None,
        expires_at=EXPIRES,
    )
    monkeypatch.setattr(
        worker.task_delivery,
        "claim_due",
        MagicMock(side_effect=[[CLAIM], [other], []]),
    )
    process = MagicMock(side_effect=["delivered", RuntimeError("unexpected")])
    monkeypatch.setattr(worker, "process_claim", process)

    assert worker.run_batch(now=NOW, worker_id="task-worker", limit=99) == worker.BatchResult(
        attempted=2, delivered=1, isolated_errors=1
    )
    assert worker.task_delivery.claim_due.call_args_list == [
        ((), {"now": NOW, "worker_id": "task-worker", "limit": 1}),
        ((), {"now": NOW, "worker_id": "task-worker", "limit": 1}),
        ((), {"now": NOW, "worker_id": "task-worker", "limit": 1}),
    ]


def test_run_batch_claims_one_item_at_a_time_with_a_live_clock(monkeypatch):
    other = delivery.TaskDeliveryClaim(
        feedback_id=43,
        claim_token=CLAIM.claim_token,
        task_id=None,
        before_attachment_id=None,
        expires_at=EXPIRES,
    )
    claim_due = MagicMock(side_effect=[[CLAIM], [other], []])
    retries = MagicMock()
    times = iter(
        [
            NOW,
            NOW + timedelta(minutes=3),
            NOW + timedelta(minutes=4),
            NOW + timedelta(minutes=7),
            NOW + timedelta(minutes=8),
        ]
    )

    def clock():
        return next(times)

    def process(item, *, clock):
        retries(item, clock())
        return "retried"

    monkeypatch.setattr(worker.task_delivery, "claim_due", claim_due)
    monkeypatch.setattr(worker, "process_claim", process)

    assert worker.run_batch(worker_id="task-worker", limit=10, clock=clock) == worker.BatchResult(
        attempted=2, retried=2
    )

    assert claim_due.call_args_list == [
        ((), {"now": NOW, "worker_id": "task-worker", "limit": 1}),
        ((), {"now": NOW + timedelta(minutes=4), "worker_id": "task-worker", "limit": 1}),
        ((), {"now": NOW + timedelta(minutes=8), "worker_id": "task-worker", "limit": 1}),
    ]
    assert retries.call_args_list == [
        ((CLAIM, NOW + timedelta(minutes=3)), {}),
        ((other, NOW + timedelta(minutes=7)), {}),
    ]


def test_owner_task_delivery_warmer_is_lazy_and_runs_off_the_event_loop(monkeypatch):
    from zira_dashboard import feedback_task_worker

    run = MagicMock()
    to_thread = AsyncMock()
    monkeypatch.setattr(feedback_task_worker, "run_batch", run)
    monkeypatch.setattr(app_module.asyncio, "to_thread", to_thread)

    asyncio.run(app_module._tick_feedback_task_delivery())

    to_thread.assert_awaited_once_with(run)
    assert (
        "feedback owner task delivery",
        app_module._tick_feedback_task_delivery,
        60,
    ) in app_module._WARMERS


def test_app_import_does_not_eagerly_import_owner_task_worker():
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ | {
        "DATABASE_URL": "",
        "FEEDBACK_SYNC_TEST_DATABASE": "",
        "ODOO_URL": "",
        "ODOO_DB": "",
        "ODOO_LOGIN": "",
        "ODOO_API_KEY": "",
        "PYTHONPATH": str(project_root / "src"),
        "ZIRA_API_KEY": "test-dummy-zira-key",
        "ZIRA_BASE_URL": "http://127.0.0.1:9",
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import zira_dashboard.app; "
                "assert 'zira_dashboard.feedback_task_worker' not in sys.modules"
            ),
        ],
        cwd=project_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
