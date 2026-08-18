from datetime import UTC, date, datetime, time, timedelta

import pytest

from zira_dashboard import goat_categories, goat_notifications


def _delivery(**changes):
    delivery = {
        "id": 7,
        "category_key": "repairs",
        "group_name": "Repairs",
        "person": "Jose O.",
        "wc_name": "Repair 3",
        "units": 898,
        "achieved_day": date(2026, 7, 29),
        "prior_record_holder": "Jose Ochoa",
        "prior_record_units": 891,
        "prior_record_day": date(2026, 6, 10),
        "client_msg_id": "1f7194a2-79de-4e95-a5f4-087743431fe9",
        "claim_token": "a02b5f81-2c89-4f2d-bcdf-c9f0f431838d",
    }
    delivery.update(changes)
    return delivery


def test_winner_uses_person_day_total_and_largest_contributing_center(monkeypatch):
    category = goat_categories.GoatCategory("repairs", "Repairs", "Repair GOAT", group_name="Repairs")
    monkeypatch.setattr(goat_categories, "work_center_names", lambda _: {"Repair 1", "Repair 3"})
    records = [
        {"person": "Jose O.", "day": date(2026, 7, 28), "wc": "Repair 1", "units": 250, "hours": 7},
        {"person": "Jose O.", "day": date(2026, 7, 28), "wc": "Repair 3", "units": 648, "hours": 7},
        {"person": "Ana", "day": date(2026, 7, 28), "wc": "Repair 3", "units": 897, "hours": 7},
    ]

    assert goat_notifications.winner_for_day(category, date(2026, 7, 28), records) == {
        "person": "Jose O.", "wc_name": "Repair 3", "units": 898,
    }


def test_finalize_requires_a_strict_new_record(monkeypatch):
    category = goat_categories.GoatCategory("repairs", "Repairs", "Repair GOAT", group_name="Repairs")
    monkeypatch.setattr(goat_notifications, "_eligible_categories", lambda: (category,))
    monkeypatch.setattr(goat_categories, "work_center_names", lambda _: {"Repair 3"})
    monkeypatch.setattr(goat_notifications, "_records_through", lambda _: [
        {"person": "Old Holder", "day": date(2026, 6, 10), "wc": "Repair 3", "units": 891, "hours": 7},
        {"person": "Jose O.", "day": date(2026, 7, 28), "wc": "Repair 3", "units": 891, "hours": 7},
    ])
    inserted = []
    monkeypatch.setattr(goat_notifications.store, "insert_alert_and_delivery", inserted.append)
    monkeypatch.setattr(goat_notifications.precompute, "precompute_day", lambda *_: None)

    assert goat_notifications.finalize_day(date(2026, 7, 28), client=object()) == []
    assert inserted == []


def test_hand_build_notifications_wait_for_30_positive_days(monkeypatch):
    category = goat_categories.category_for_key("hand_build")
    records = [
        {
            "day": date(2026, 7, 1) + timedelta(days=offset),
            "person": "Builder",
            "wc": "Hand Build #1",
            "units": 100 + offset,
            "hours": 7,
        }
        for offset in range(29)
    ]
    winner_calls = []
    inserted = []
    monkeypatch.setattr(
        goat_categories,
        "work_center_names",
        lambda _: {"Hand Build #1"},
    )
    monkeypatch.setattr(
        goat_notifications,
        "_eligible_categories",
        lambda: (category,),
    )
    monkeypatch.setattr(
        goat_notifications.precompute,
        "precompute_day",
        lambda *_: None,
    )
    monkeypatch.setattr(goat_notifications, "_records_through", lambda _: records)
    monkeypatch.setattr(
        goat_notifications,
        "winner_for_day",
        lambda *_: winner_calls.append(True),
    )
    monkeypatch.setattr(
        goat_notifications.store,
        "insert_alert_and_delivery",
        inserted.append,
    )

    assert goat_notifications.finalize_day(
        records[-1]["day"],
        client=object(),
    ) == []
    assert winner_calls == []
    assert inserted == []


def test_message_payload_keeps_previous_record_secondary():
    text, blocks = goat_notifications.message_payload({
        "group_name": "Repairs", "person": "Jose O.", "wc_name": "Repair 3", "units": 898,
        "achieved_day": date(2026, 7, 28), "prior_record_holder": "Jose Ochoa",
        "prior_record_units": 891, "prior_record_day": date(2026, 6, 10),
    })

    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "🏆 NEW REPAIRS GOAT!"
    assert blocks[1]["text"]["text"] == "*Jose O.* — *898 pallets* at Repair 3 on Jul 28, 2026"
    assert blocks[2] == {"type": "context", "elements": [{"type": "mrkdwn", "text": "_(previous = Jose Ochoa · 891 · Jun 10, 2026)_"}]}
    assert blocks[3]["text"]["text"] == "Congratulate Jose when you see them! 🎉"
    assert "previous = Jose Ochoa · 891 · Jun 10, 2026" in text


def test_drain_requeues_a_slack_error(monkeypatch):
    delivery = _delivery(achieved_day=date(2026, 7, 28))
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-MGMT")
    monkeypatch.setattr(goat_notifications.store, "claim_delivery", lambda: delivery)
    monkeypatch.setattr(goat_notifications.slack_client, "post_message", lambda **kwargs: (_ for _ in ()).throw(goat_notifications.slack_client.SlackError("not_in_channel")))
    seen = []
    monkeypatch.setattr(goat_notifications.store, "return_delivery_to_pending", lambda delivery_id, claim_token, error: seen.append((delivery_id, claim_token, error)))

    assert goat_notifications.drain_deliveries(date(2026, 7, 29)) == 0
    assert seen == [(7, delivery["claim_token"], "not_in_channel")]


def test_drain_suppresses_a_noncanonical_delivery_without_posting(monkeypatch):
    delivery = _delivery(category_key="pytest-goat-4e10e3564cd543bfac6924d796bbc864")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-MGMT")
    monkeypatch.setattr(
        goat_notifications.store, "claim_delivery", iter([delivery, None]).__next__
    )
    suppressed = []
    monkeypatch.setattr(
        goat_notifications.store,
        "suppress_delivery",
        lambda *args: suppressed.append(args),
    )
    monkeypatch.setattr(
        goat_notifications.slack_client,
        "post_message",
        lambda **_: pytest.fail("unsafe delivery reached Slack"),
    )

    assert goat_notifications.drain_deliveries(date(2026, 7, 29)) == 0
    assert suppressed == [
        (delivery["id"], delivery["claim_token"], "unknown GOAT category")
    ]


def test_delivery_suppresses_saved_hand_build_alert_before_day_30(monkeypatch):
    delivery = _delivery(
        category_key="hand_build",
        group_name="Hand Build",
        achieved_day=date(2026, 8, 18),
    )
    records = [
        {
            "day": date(2026, 7, 1) + timedelta(days=offset),
            "person": "Builder",
            "wc": "Hand Build #1",
            "units": 100,
            "hours": 7,
        }
        for offset in range(29)
    ]
    monkeypatch.setattr(
        goat_categories,
        "work_center_names",
        lambda _: {"Hand Build #1"},
    )
    monkeypatch.setattr(goat_notifications, "_records_through", lambda _: records)

    assert goat_notifications.delivery_suppression_reason(
        delivery,
        date(2026, 8, 18),
    ) == "Hand Build GOAT requires 30 production days"


def test_delivery_does_not_replay_day_29_alert_after_day_30(monkeypatch):
    achieved_day = date(2026, 8, 18)
    today = date(2026, 8, 19)
    delivery = _delivery(
        category_key="hand_build",
        group_name="Hand Build",
        achieved_day=achieved_day,
    )
    records = [
        {
            "day": date(2026, 7, 1) + timedelta(days=offset),
            "person": "Builder",
            "wc": "Hand Build #1",
            "units": 100,
            "hours": 7,
        }
        for offset in range(30)
    ]
    requested_through = []
    monkeypatch.setattr(
        goat_categories,
        "work_center_names",
        lambda _: {"Hand Build #1"},
    )

    def records_through(day):
        requested_through.append(day)
        return records[:29] if day == achieved_day else records

    monkeypatch.setattr(goat_notifications, "_records_through", records_through)

    assert goat_notifications.delivery_suppression_reason(
        delivery,
        today,
    ) == "Hand Build GOAT requires 30 production days"
    assert requested_through == [achieved_day]


def test_drain_suppresses_a_future_delivery_without_posting(monkeypatch):
    delivery = _delivery(achieved_day=date(2099, 1, 2))
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-MGMT")
    monkeypatch.setattr(
        goat_notifications.store, "claim_delivery", iter([delivery, None]).__next__
    )
    suppressed = []
    monkeypatch.setattr(
        goat_notifications.store,
        "suppress_delivery",
        lambda *args: suppressed.append(args),
    )
    monkeypatch.setattr(
        goat_notifications.slack_client,
        "post_message",
        lambda **_: pytest.fail("future delivery reached Slack"),
    )

    assert goat_notifications.drain_deliveries(date(2026, 8, 12)) == 0
    assert suppressed == [
        (delivery["id"], delivery["claim_token"], "achieved day is in the future")
    ]


def test_drain_suppresses_an_expired_delivery_without_posting(monkeypatch):
    delivery = _delivery(achieved_day=date(2026, 7, 28))
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-MGMT")
    monkeypatch.setattr(
        goat_notifications.store, "claim_delivery", iter([delivery, None]).__next__
    )
    suppressed = []
    monkeypatch.setattr(
        goat_notifications.store,
        "suppress_delivery",
        lambda *args: suppressed.append(args),
    )
    monkeypatch.setattr(
        goat_notifications.slack_client,
        "post_message",
        lambda **_: pytest.fail("expired delivery reached Slack"),
    )

    assert goat_notifications.drain_deliveries(date(2026, 7, 31)) == 0
    assert suppressed == [
        (delivery["id"], delivery["claim_token"], "delivery window expired")
    ]


def test_drain_posts_a_current_canonical_delivery(monkeypatch):
    delivery = _delivery()
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-MGMT")
    monkeypatch.setattr(
        goat_notifications.store, "claim_delivery", iter([delivery, None]).__next__
    )
    posted = []
    monkeypatch.setattr(
        goat_notifications.slack_client,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"message_ts": "1722280000.000100"},
    )
    marked = []
    monkeypatch.setattr(
        goat_notifications.store,
        "mark_delivery_sent",
        lambda *args: marked.append(args),
    )

    assert goat_notifications.drain_deliveries(date(2026, 7, 29)) == 1
    assert [post["client_msg_id"] for post in posted] == [delivery["client_msg_id"]]
    assert marked == [
        (delivery["id"], delivery["claim_token"], "1722280000.000100")
    ]


def test_finalize_precomputes_then_creates_one_transactional_alert(monkeypatch):
    category = goat_categories.GoatCategory("repairs", "Repairs", "Repair GOAT", group_name="Repairs")
    completed_day = date(2026, 7, 28)
    calls = []
    records = [
        {"person": "Old Holder", "day": date(2026, 6, 10), "wc": "Repair 3", "units": 891.4, "hours": 7},
        {"person": "Jose O.", "day": completed_day, "wc": "Repair 1", "units": 250, "hours": 7},
        {"person": "Jose O.", "day": completed_day, "wc": "Repair 3", "units": 648.2, "hours": 7},
    ]
    monkeypatch.setattr(goat_notifications, "_eligible_categories", lambda: (category,))
    monkeypatch.setattr(goat_categories, "work_center_names", lambda _: {"Repair 1", "Repair 3"})
    monkeypatch.setattr(goat_notifications.precompute, "precompute_day", lambda day, client: calls.append(("precompute", day, client)))
    monkeypatch.setattr(goat_notifications, "_records_through", lambda day: calls.append(("records", day)) or records)
    inserted = []
    monkeypatch.setattr(goat_notifications.store, "insert_alert_and_delivery", lambda alert: inserted.append(alert) or 42)

    result = goat_notifications.finalize_day(completed_day, client="client")

    expected = {
        "achieved_day": completed_day,
        "category_key": "repairs",
        "group_name": "Repairs",
        "person": "Jose O.",
        "wc_name": "Repair 3",
        "units": 898,
        "prior_record_units": 891,
        "prior_record_holder": "Old Holder",
        "prior_record_day": date(2026, 6, 10),
    }
    assert calls == [("precompute", completed_day, "client"), ("records", completed_day)]
    assert inserted == [expected]
    assert result == [expected]


def test_finalize_ignores_new_record_when_the_outbox_insert_already_exists(monkeypatch):
    category = goat_categories.GoatCategory("repairs", "Repairs", "Repair GOAT", group_name="Repairs")
    completed_day = date(2026, 7, 28)
    monkeypatch.setattr(goat_notifications, "_eligible_categories", lambda: (category,))
    monkeypatch.setattr(goat_categories, "work_center_names", lambda _: {"Repair 3"})
    monkeypatch.setattr(goat_notifications.precompute, "precompute_day", lambda *_: None)
    monkeypatch.setattr(goat_notifications, "_records_through", lambda _: [
        {"person": "Old Holder", "day": date(2026, 6, 10), "wc": "Repair 3", "units": 891, "hours": 7},
        {"person": "Jose O.", "day": completed_day, "wc": "Repair 3", "units": 898, "hours": 7},
    ])
    monkeypatch.setattr(goat_notifications.store, "insert_alert_and_delivery", lambda _: None)

    assert goat_notifications.finalize_day(completed_day, client=object()) == []


def test_finalize_does_not_announce_an_initial_record_without_a_prior_holder(monkeypatch):
    category = goat_categories.GoatCategory("repairs", "Repairs", "Repair GOAT", group_name="Repairs")
    monkeypatch.setattr(goat_notifications, "_eligible_categories", lambda: (category,))
    monkeypatch.setattr(goat_categories, "work_center_names", lambda _: {"Repair 3"})
    monkeypatch.setattr(goat_notifications.precompute, "precompute_day", lambda *_: None)
    monkeypatch.setattr(goat_notifications, "_records_through", lambda _: [
        {"person": "Jose O.", "day": date(2026, 7, 28), "wc": "Repair 3", "units": 898, "hours": 7},
    ])
    inserted = []
    monkeypatch.setattr(goat_notifications.store, "insert_alert_and_delivery", inserted.append)

    assert goat_notifications.finalize_day(date(2026, 7, 28), client=object()) == []
    assert inserted == []


def test_drain_marks_each_delivery_sent(monkeypatch):
    first = _delivery(achieved_day=date(2026, 7, 28))
    second = {**first, "id": 8, "person": "Ana", "client_msg_id": "80b7b976-b02b-4a31-a211-4c3c649e1bcf", "claim_token": "f3d5e049-37bd-4881-bcfb-43d41c28e5a9"}
    deliveries = iter([first, second, None])
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-MGMT")
    monkeypatch.setattr(goat_notifications.store, "claim_delivery", lambda: next(deliveries))
    posted = []
    monkeypatch.setattr(goat_notifications.slack_client, "post_message", lambda **kwargs: posted.append(kwargs) or {"message_ts": f"ts-{len(posted)}"})
    marked = []
    monkeypatch.setattr(goat_notifications.store, "mark_delivery_sent", lambda delivery_id, claim_token, message_ts: marked.append((delivery_id, claim_token, message_ts)))

    assert goat_notifications.drain_deliveries(date(2026, 7, 28)) == 2
    assert [post["channel_id"] for post in posted] == ["C-MGMT", "C-MGMT"]
    assert [post["client_msg_id"] for post in posted] == [first["client_msg_id"], second["client_msg_id"]]
    assert marked == [(7, first["claim_token"], "ts-1"), (8, second["claim_token"], "ts-2")]


def test_drain_retries_with_the_same_persisted_client_message_id(monkeypatch):
    delivery = _delivery(achieved_day=date(2026, 7, 28))
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-MGMT")
    claims = iter([delivery, {**delivery, "claim_token": "f3d5e049-37bd-4881-bcfb-43d41c28e5a9"}, None])
    monkeypatch.setattr(goat_notifications.store, "claim_delivery", lambda: next(claims))
    sent = []

    def post_message(**kwargs):
        sent.append(kwargs["client_msg_id"])
        if len(sent) == 1:
            raise goat_notifications.slack_client.SlackError("timeout")
        return {"message_ts": "1722280000.000100"}

    monkeypatch.setattr(goat_notifications.slack_client, "post_message", post_message)
    monkeypatch.setattr(goat_notifications.store, "return_delivery_to_pending", lambda *_: None)
    monkeypatch.setattr(goat_notifications.store, "mark_delivery_sent", lambda *_: None)

    assert goat_notifications.drain_deliveries(date(2026, 7, 28)) == 0
    assert goat_notifications.drain_deliveries(date(2026, 7, 28)) == 1
    assert sent == [delivery["client_msg_id"], delivery["client_msg_id"]]


def test_drain_warns_and_leaves_outbox_untouched_without_the_schedule_channel(monkeypatch, caplog):
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    claimed = []
    monkeypatch.setattr(goat_notifications.store, "claim_delivery", lambda: claimed.append(True))

    assert goat_notifications.drain_deliveries(date(2026, 7, 29)) == 0
    assert claimed == []
    assert "SLACK_CHANNEL_ID" in caplog.text


def test_run_due_recovers_unfinalized_workdays_before_the_current_shift_ends(monkeypatch):
    now_utc = datetime(2026, 7, 29, 19, 0, tzinfo=UTC)
    finalized = []
    events = []
    expected_days = [date(2026, 7, 27), date(2026, 7, 28)]
    monkeypatch.setattr(goat_notifications.shift_config, "shift_end_for", lambda _: time(15, 30))
    monkeypatch.setattr(goat_notifications.shift_config, "work_weekdays", lambda: frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(goat_notifications.store, "ensure_enabled_on", lambda day: events.append(("enabled", day)))
    monkeypatch.setattr(goat_notifications.store, "unfinalized_workdays", lambda day: events.append(("due", day)) or expected_days)
    monkeypatch.setattr(goat_notifications, "finalize_day", lambda day, client: events.append(("finalize", day, client)))
    monkeypatch.setattr(goat_notifications.store, "record_finalized_day", lambda day: finalized.append(day))
    monkeypatch.setattr(
        goat_notifications,
        "drain_deliveries",
        lambda today: events.append(("drain", today)),
    )

    goat_notifications.run_due(now_utc, client="client")

    assert events == [
        ("enabled", date(2026, 7, 29)),
        ("due", date(2026, 7, 28)),
        ("finalize", date(2026, 7, 27), "client"),
        ("finalize", date(2026, 7, 28), "client"),
        ("drain", date(2026, 7, 29)),
    ]
    assert finalized == expected_days


def test_run_due_finalizes_a_published_saturday(monkeypatch):
    now_utc = datetime(2026, 8, 2, 19, 0, tzinfo=UTC)
    saturday = date(2026, 8, 1)
    finalized = []
    monkeypatch.setattr(goat_notifications.shift_config, "shift_end_for", lambda _: time(15, 30))
    monkeypatch.setattr(goat_notifications.shift_config, "is_workday", lambda day: day == saturday)
    monkeypatch.setattr(goat_notifications.store, "ensure_enabled_on", lambda _: None)
    monkeypatch.setattr(goat_notifications.store, "unfinalized_workdays", lambda day: [day])
    monkeypatch.setattr(goat_notifications, "finalize_day", lambda day, _: finalized.append(day))
    monkeypatch.setattr(goat_notifications.store, "record_finalized_day", lambda _: None)
    monkeypatch.setattr(goat_notifications, "drain_deliveries", lambda _today: None)

    goat_notifications.run_due(now_utc, client=object())

    assert finalized == [saturday]


def test_run_due_finalizes_the_current_day_once_its_shift_has_ended(monkeypatch):
    now_utc = datetime(2026, 7, 29, 21, 0, tzinfo=UTC)
    selected = []
    monkeypatch.setattr(goat_notifications.shift_config, "shift_end_for", lambda _: time(15, 30))
    monkeypatch.setattr(goat_notifications.store, "ensure_enabled_on", lambda _: None)
    monkeypatch.setattr(goat_notifications.store, "unfinalized_workdays", lambda day: selected.append(day) or [])
    monkeypatch.setattr(goat_notifications, "drain_deliveries", lambda _today: None)

    goat_notifications.run_due(now_utc, client="client")

    assert selected == [date(2026, 7, 29)]


def test_run_due_does_not_record_a_day_when_finalization_raises(monkeypatch):
    now_utc = datetime(2026, 7, 29, 21, 0, tzinfo=UTC)
    monkeypatch.setattr(goat_notifications.shift_config, "shift_end_for", lambda _: time(15, 30))
    monkeypatch.setattr(goat_notifications.store, "ensure_enabled_on", lambda _: None)
    monkeypatch.setattr(goat_notifications.store, "unfinalized_workdays", lambda _: [date(2026, 7, 29)])
    monkeypatch.setattr(goat_notifications, "finalize_day", lambda *_: (_ for _ in ()).throw(RuntimeError("source unavailable")))
    recorded = []
    monkeypatch.setattr(goat_notifications.store, "record_finalized_day", recorded.append)
    drained = []
    monkeypatch.setattr(goat_notifications, "drain_deliveries", lambda: drained.append(True))

    with pytest.raises(RuntimeError, match="source unavailable"):
        goat_notifications.run_due(now_utc, client="client")

    assert recorded == []
    assert drained == []
