# GOAT Slack Celebrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically celebrate each finalized production GOAT record in `#MGMT-Sups` with the winner, work center, final pallet count, record date, and previous GOAT record.

**Architecture:** A shared category registry defines the five production GOAT areas and their work-center membership. A durable day-close worker evaluates completed, post-feature workdays from the canonical production attribution data, writes at most one `goat_alerts` row per category/day, and creates a transactional Slack outbox row beside it. The existing in-process warmer runs the close-and-delivery worker; the outbox safely claims, retries, and records Slack posts across restarts and multiple app processes.

**Tech Stack:** Python 3.11, FastAPI lifespan warmers, PostgreSQL/psycopg2, existing Zira production attribution, Slack Web API `chat.postMessage`, Slack Block Kit, pytest, ruff.

## Global Constraints

- Use only `GOAT_SLACK_CHANNEL_ID` for the `#MGMT-Sups` destination. Keep `SLACK_CHANNEL_ID` assigned to staffing-PDF posts.
- Support Repairs, Dismantlers, Juniors, Woodpecker, and Hand Build. Categories without a metered Zira center are skipped and become eligible automatically once a `meter_id` exists.
- Run only after the configured shift end and only for workdays on or after the feature's first enabled plant day. Never back-post old alerts.
- A record must strictly exceed the prior record. Create one final celebration per category/day; use the winner's largest contributing work center, alphabetically breaking an equal contribution tie.
- Use Block Kit header, record section, compact context line, and short closing. Include equivalent top-level fallback text. Do not @mention people or send live contender posts.
- Slack errors and missing configuration never block GOAT finalization. Retain a pending delivery with its error for retry.
- Add a short, child-friendly `CHANGELOG.md` entry before the feature is pushed to `main`.

---

## File structure

| File | Responsibility |
| --- | --- |
| `src/zira_dashboard/goat_categories.py` | The five authoritative category definitions and their dynamic work-center/meter resolution. |
| `src/zira_dashboard/awards.py` | Public, override-free helper that ranks person-day rows with the existing GOAT tie-break. |
| `src/zira_dashboard/routes/recycling_leaderboard.py` | Use the registry for Recycling GOAT cards. |
| `src/zira_dashboard/routes/new_leaderboard.py` | Use the registry for New GOAT cards. |
| `src/zira_dashboard/_schema.py` | Idempotent notification-state, finalization, and outbox tables. |
| `src/zira_dashboard/goat_notification_store.py` | SQL-only persistence for day markers and claimable deliveries. |
| `src/zira_dashboard/goat_notifications.py` | Record selection, alert/outbox creation, Block Kit rendering, and delivery draining. |
| `src/zira_dashboard/goat_watch.py` | Preserve dashboard APIs and delegate post-shift finalization. |
| `src/zira_dashboard/slack_client.py` | Block Kit message sender beside the existing PDF uploader. |
| `src/zira_dashboard/app.py` | One non-blocking GOAT notification warmer tick. |
| `tests/test_goat_categories.py` | Pure category and automatic eligibility tests. |
| `tests/test_goat_notifications.py` | Pure winner, payload, and decision tests. |
| `tests/test_goat_notification_store.py` | DB schema, transaction, claim, failure, and retry tests. |
| `tests/test_goat_notification_warmer.py` | Tick registration and worker-adapter tests. |
| `tests/test_slack_client.py` | `chat.postMessage` request and error tests. |

## Task 1: Centralize production GOAT categories

**Files:**

- Create: `src/zira_dashboard/goat_categories.py`
- Create: `tests/test_goat_categories.py`
- Modify: `src/zira_dashboard/awards.py`
- Modify: `src/zira_dashboard/routes/recycling_leaderboard.py`
- Modify: `src/zira_dashboard/routes/new_leaderboard.py`

**Interfaces:**

- Consumes: `staffing.LOCATIONS` and `work_centers_store.members("group", name)`.
- Produces: `GoatCategory`, `all_categories()`, `category_for_key()`, `recycling_categories()`, `new_categories()`, `work_center_names()`, `has_metered_source()`, and `awards.best_person_day()`.
- Used by later tasks: category evaluation and both current GOAT leaderboards.

- [ ] **Step 1: Write the failing registry and tie-break tests**

Create `tests/test_goat_categories.py`:

```python
from datetime import date
from types import SimpleNamespace

from zira_dashboard import awards, goat_categories


def test_categories_match_the_approved_groups_and_auto_activation(monkeypatch):
    locations = (
        SimpleNamespace(name="Repair 1", skill="Repair", meter_id="r1"),
        SimpleNamespace(name="Dismantler 1", skill="Dismantler", meter_id="d1"),
        SimpleNamespace(name="Junior #2", skill="Junior", meter_id="j2"),
        SimpleNamespace(name="Woodpecker #1", skill="Woodpecker", meter_id=None),
        SimpleNamespace(name="Hand Build #1", skill="Hand Build", meter_id=None),
        SimpleNamespace(name="Big Build #1", skill="Hand Build", meter_id=None),
    )
    monkeypatch.setattr(goat_categories.staffing, "LOCATIONS", locations)
    monkeypatch.setattr(
        goat_categories.work_centers_store,
        "members",
        lambda kind, name: {("group", "Repairs"): [locations[0]], ("group", "Dismantlers"): [locations[1]]}[(kind, name)],
    )

    categories = goat_categories.all_categories()

    assert [category.key for category in categories] == ["repairs", "dismantlers", "juniors", "woodpecker", "hand_build"]
    assert [category.label for category in categories] == ["Repairs", "Dismantlers", "Juniors", "Woodpecker", "Hand Build"]
    assert [goat_categories.has_metered_source(category) for category in categories] == [True, True, True, False, False]
    assert goat_categories.work_center_names(categories[-1]) == {"Hand Build #1", "Big Build #1"}


def test_best_person_day_uses_record_tie_breaks():
    rows = [
        {"name": "Zoe", "day": date(2026, 7, 28), "units": 100, "hours": 7},
        {"name": "Amy", "day": date(2026, 7, 29), "units": 100, "hours": 7},
        {"name": "Bob", "day": date(2026, 7, 28), "units": 100, "hours": 7},
    ]

    assert awards.best_person_day(rows) == {
        "name": "Bob", "day": date(2026, 7, 28), "units": 100, "pph": 14.3,
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_goat_categories.py -q`

Expected: collection fails because the registry and public ranking helper do not exist.

- [ ] **Step 3: Implement the single category registry**

Create `src/zira_dashboard/goat_categories.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from . import staffing, work_centers_store


@dataclass(frozen=True)
class GoatCategory:
    key: str
    label: str
    leaderboard_label: str
    group_name: str | None = None
    skill: str | None = None


_CATEGORIES = (
    GoatCategory("repairs", "Repairs", "Repair GOAT", group_name="Repairs"),
    GoatCategory("dismantlers", "Dismantlers", "Dismantler GOAT", group_name="Dismantlers"),
    GoatCategory("juniors", "Juniors", "Junior GOAT", skill="Junior"),
    GoatCategory("woodpecker", "Woodpecker", "Woodpecker GOAT", skill="Woodpecker"),
    GoatCategory("hand_build", "Hand Build", "Hand Build GOAT", skill="Hand Build"),
)


def all_categories() -> tuple[GoatCategory, ...]:
    return _CATEGORIES


def category_for_key(key: str) -> GoatCategory:
    return next(category for category in _CATEGORIES if category.key == key)


def recycling_categories() -> tuple[GoatCategory, ...]:
    return tuple(category for category in _CATEGORIES if category.group_name is not None)


def new_categories() -> tuple[GoatCategory, ...]:
    return tuple(category for category in _CATEGORIES if category.skill is not None)


def members(category: GoatCategory):
    if category.group_name is not None:
        return tuple(work_centers_store.members("group", category.group_name))
    return tuple(location for location in staffing.LOCATIONS if location.skill == category.skill)


def work_center_names(category: GoatCategory) -> set[str]:
    return {location.name for location in members(category)}


def has_metered_source(category: GoatCategory) -> bool:
    return any(location.meter_id for location in members(category))
```

Rename private `awards._goat_from_rows` to public `awards.best_person_day`, preserving its exact ranking body, then update `goat()` and `goat_for_wc_names()` to call it. This exposes the existing factual-record ranking without applying Trophy Case overrides.

- [ ] **Step 4: Make both leaderboards consume the registry**

Replace local category constants with registry values while preserving existing route data shapes:

```python
# routes/recycling_leaderboard.py
from .. import goat_categories

_CURRENT_GOAT_GROUPS = tuple(
    (category.leaderboard_label, category.label)
    for category in goat_categories.recycling_categories()
)

# routes/new_leaderboard.py
from .. import goat_categories

_FAMILY_SKILLS = tuple(
    (category.label, category.skill, category.leaderboard_label)
    for category in goat_categories.new_categories()
)
```

- [ ] **Step 5: Run focused verification**

Run: `uv run pytest tests/test_goat_categories.py tests/test_awards.py tests/test_recycling_leaderboard_static.py tests/test_new_leaderboard_routes.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the registry slice**

```bash
git add src/zira_dashboard/goat_categories.py src/zira_dashboard/awards.py src/zira_dashboard/routes/recycling_leaderboard.py src/zira_dashboard/routes/new_leaderboard.py tests/test_goat_categories.py
git commit -m "feat: centralize production GOAT categories"
```

## Task 2: Add Slack Block Kit posting

**Files:**

- Modify: `src/zira_dashboard/slack_client.py`
- Modify: `tests/test_slack_client.py`

**Interfaces:**

- Consumes: `SLACK_BOT_TOKEN`, channel ID, fallback text, and blocks.
- Produces: `post_message(*, channel_id: str, text: str, blocks: list[dict]) -> dict` returning `{"message_ts": str}`.
- Used by later tasks: the outbox worker.

- [ ] **Step 1: Write failing sender tests**

Add to `tests/test_slack_client.py`:

```python
def test_post_message_posts_fallback_and_blocks(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    response = _ok_response({"ok": True, "channel": "C-MGMT", "ts": "1722280000.000100"})
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return response

    monkeypatch.setattr(slack_client.requests, "post", fake_post)
    result = slack_client.post_message(
        channel_id="C-MGMT",
        text="NEW REPAIRS GOAT: Jose O., 898 pallets.",
        blocks=[{"type": "header", "text": {"type": "plain_text", "text": "🏆 NEW REPAIRS GOAT!"}}],
    )

    assert seen["url"] == "https://slack.com/api/chat.postMessage"
    assert seen["headers"]["Authorization"] == "Bearer xoxb-test"
    assert seen["json"]["channel"] == "C-MGMT"
    assert seen["json"]["text"].startswith("NEW REPAIRS GOAT")
    assert seen["json"]["blocks"][0]["type"] == "header"
    assert seen["json"]["unfurl_links"] is False
    assert result == {"message_ts": "1722280000.000100"}


def test_post_message_wraps_api_error(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(slack_client.requests, "post", lambda *args, **kwargs: _ok_response({"ok": False, "error": "not_in_channel"}))

    with pytest.raises(slack_client.SlackError, match="not_in_channel"):
        slack_client.post_message(channel_id="C-MGMT", text="x", blocks=[])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_slack_client.py -q`

Expected: FAIL because `post_message` does not exist.

- [ ] **Step 3: Implement the Slack API wrapper**

Add after `SlackError` in `src/zira_dashboard/slack_client.py`:

```python
def post_message(*, channel_id: str, text: str, blocks: list[dict]) -> dict:
    """Post Block Kit content and return Slack's stable message timestamp."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise SlackError("Slack not configured (SLACK_BOT_TOKEN missing)")
    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json={"channel": channel_id, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise SlackError(f"chat.postMessage failed: {payload.get('error')}")
        message_ts = payload.get("ts") or payload.get("message", {}).get("ts")
        if not message_ts:
            raise SlackError("chat.postMessage failed: response missing message timestamp")
        return {"message_ts": str(message_ts)}
    except requests.exceptions.RequestException as exc:
        raise SlackError(f"Slack request failed: {exc}") from exc
```

- [ ] **Step 4: Verify and commit the Slack client**

Run: `uv run pytest tests/test_slack_client.py -q`

Expected: PASS.

```bash
git add src/zira_dashboard/slack_client.py tests/test_slack_client.py
git commit -m "feat: add Slack Block Kit message posting"
```

## Task 3: Add durable finalization and delivery storage

**Files:**

- Modify: `src/zira_dashboard/_schema.py`
- Create: `src/zira_dashboard/goat_notification_store.py`
- Create: `tests/test_goat_notification_store.py`

**Interfaces:**

- Consumes: `db.cursor()`, a category-day alert, and `goat_alerts.id`.
- Produces: `ensure_enabled_on()`, `unfinalized_workdays()`, `record_finalized_day()`, `insert_alert_and_delivery()`, `claim_delivery()`, `mark_delivery_sent()`, and `return_delivery_to_pending()`.
- Used by later tasks: finalization persists an alert/outbox pair; delivery draining claims one row at a time.

- [ ] **Step 1: Write DB-backed persistence tests**

Create `tests/test_goat_notification_store.py`. Skip the module without `DATABASE_URL`; call `db.bootstrap_schema()` in an autouse fixture and delete only test rows from the three new tables and `goat_alerts`.

```python
import os
from datetime import date

import pytest

from zira_dashboard import db, goat_notification_store as store

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


def _alert(day=date(2026, 7, 29)):
    return {
        "achieved_day": day, "group_name": "Repairs", "person": "Jose O.",
        "wc_name": "Repair 3", "units": 898, "prior_record_units": 891,
        "prior_record_holder": "Jose Ochoa", "prior_record_day": date(2026, 6, 10),
    }


def test_activation_day_is_written_once():
    assert store.ensure_enabled_on(date(2026, 7, 29)) == date(2026, 7, 29)
    assert store.ensure_enabled_on(date(2026, 8, 1)) == date(2026, 7, 29)


def test_alert_and_delivery_are_single_transactional_unit():
    alert_id = store.insert_alert_and_delivery(_alert())
    assert isinstance(alert_id, int)
    assert store.insert_alert_and_delivery(_alert()) is None
    rows = db.query("SELECT goat_alert_id, status, attempts FROM goat_slack_deliveries WHERE goat_alert_id = %s", (alert_id,))
    assert rows == [{"goat_alert_id": alert_id, "status": "pending", "attempts": 0}]


def test_failure_retries_the_same_delivery_without_duplication():
    store.insert_alert_and_delivery(_alert())
    first = store.claim_delivery()
    store.return_delivery_to_pending(first["id"], "not_in_channel")
    second = store.claim_delivery()
    store.mark_delivery_sent(second["id"], "1722280000.000100")
    rows = db.query("SELECT status, attempts, slack_message_ts, last_error FROM goat_slack_deliveries WHERE id = %s", (second["id"],))
    assert rows == [{"status": "sent", "attempts": 2, "slack_message_ts": "1722280000.000100", "last_error": None}]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_goat_notification_store.py -q`

Expected: collection fails because the store and schema do not exist. Without `DATABASE_URL`, pytest reports a skip; run against the normal CI database to execute the assertions.

- [ ] **Step 3: Add idempotent tables and indexes**

Append this SQL immediately after the existing `goat_alerts` schema in `src/zira_dashboard/_schema.py`. The application supplies the site-local enabled date, preventing a database-time-zone mismatch.

```sql
CREATE TABLE IF NOT EXISTS goat_notification_state (
  id          SMALLINT PRIMARY KEY CHECK (id = 1),
  enabled_on  DATE NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS goat_notification_days (
  day           DATE PRIMARY KEY,
  finalized_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS goat_slack_deliveries (
  id                BIGSERIAL PRIMARY KEY,
  goat_alert_id     INTEGER NOT NULL UNIQUE REFERENCES goat_alerts(id) ON DELETE CASCADE,
  status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent')),
  attempts          INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  attempted_at      TIMESTAMPTZ,
  sent_at           TIMESTAMPTZ,
  slack_message_ts  TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_goat_slack_deliveries_claim
  ON goat_slack_deliveries (status, attempted_at, id);
```

- [ ] **Step 4: Implement the SQL-only store**

Create `src/zira_dashboard/goat_notification_store.py`. Do not import FastAPI, Slack, or Zira here. `ensure_enabled_on` and the atomic alert/outbox insert must be:

```python
from __future__ import annotations

from datetime import date, timedelta


def ensure_enabled_on(day: date) -> date:
    from . import db
    with db.cursor() as cur:
        cur.execute("INSERT INTO goat_notification_state (id, enabled_on) VALUES (1, %s) ON CONFLICT (id) DO NOTHING", (day,))
        cur.execute("SELECT enabled_on FROM goat_notification_state WHERE id = 1")
        return cur.fetchone()["enabled_on"]


def insert_alert_and_delivery(alert: dict) -> int | None:
    from . import db
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO goat_alerts (achieved_day, group_name, person, wc_name, units, prior_record_units, prior_record_holder, prior_record_day) "
            "VALUES (%(achieved_day)s, %(group_name)s, %(person)s, %(wc_name)s, %(units)s, %(prior_record_units)s, %(prior_record_holder)s, %(prior_record_day)s) "
            "ON CONFLICT (achieved_day, group_name, wc_name) DO NOTHING RETURNING id",
            alert,
        )
        row = cur.fetchone()
        if row is None:
            return None
        alert_id = int(row["id"])
        cur.execute("INSERT INTO goat_slack_deliveries (goat_alert_id) VALUES (%s)", (alert_id,))
        return alert_id
```

Implement `unfinalized_workdays(through_day)` by selecting `enabled_on` and finalized dates, then returning every workday through `through_day` that has no `goat_notification_days` row. Use `shift_config.work_weekdays()` and increment by `timedelta(days=1)`. Implement `record_finalized_day(day)` as an idempotent insert with `ON CONFLICT DO NOTHING`.

Use this claim query so concurrent app processes cannot post the same record. A `sending` row older than five minutes represents a worker that crashed after claiming it and is safely reclaimable.

```python
def claim_delivery() -> dict | None:
    from . import db
    with db.cursor() as cur:
        cur.execute(
            "WITH candidate AS (SELECT id FROM goat_slack_deliveries "
            "WHERE status = 'pending' OR (status = 'sending' AND attempted_at < now() - interval '5 minutes') "
            "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) "
            "UPDATE goat_slack_deliveries delivery SET status = 'sending', attempts = delivery.attempts + 1, attempted_at = now() "
            "FROM candidate JOIN goat_alerts alert ON alert.id = delivery.goat_alert_id "
            "WHERE delivery.id = candidate.id "
            "RETURNING delivery.id, delivery.goat_alert_id, alert.achieved_day, alert.group_name, alert.person, alert.wc_name, alert.units, alert.prior_record_units, alert.prior_record_holder, alert.prior_record_day"
        )
        return cur.fetchone()
```

Implement `mark_delivery_sent(id, message_ts)` with `status = 'sent'`, `sent_at = now()`, `slack_message_ts = message_ts`, and `last_error = NULL`. Implement `return_delivery_to_pending(id, error)` with `status = 'pending'` and `last_error = error` for the claimed ID only.

- [ ] **Step 5: Verify and commit the persistence slice**

Run: `uv run pytest tests/test_goat_notification_store.py tests/test_db.py -q`

Expected: PASS with a configured database; DB-gated tests skip otherwise.

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/goat_notification_store.py tests/test_goat_notification_store.py
git commit -m "feat: persist GOAT Slack deliveries"
```

## Task 4: Finalize category GOATs and render the compact celebration

**Files:**

- Create: `src/zira_dashboard/goat_notifications.py`
- Create: `tests/test_goat_notifications.py`

**Interfaces:**

- Consumes: `goat_categories`, `awards.best_person_day`, `production_history.daily_records`, `precompute.precompute_day`, the notification store, `slack_client.post_message`, and `GOAT_SLACK_CHANNEL_ID`.
- Produces: `winner_for_day()`, `message_payload()`, `finalize_day()`, `run_due()`, and `drain_deliveries()`.
- Used by later tasks: the app warmer and the existing dashboard alert path.

- [ ] **Step 1: Write failing winner, strict-record, payload, and retry tests**

Create `tests/test_goat_notifications.py`:

```python
from datetime import date

from zira_dashboard import goat_categories, goat_notifications


def test_winner_uses_person_day_total_and_largest_contributing_center(monkeypatch):
    category = goat_categories.GoatCategory("repairs", "Repairs", "Repair GOAT", group_name="Repairs")
    monkeypatch.setattr(goat_categories, "work_center_names", lambda _: {"Repair 1", "Repair 3"})
    records = [
        {"person": "Jose O.", "day": date(2026, 7, 28), "wc": "Repair 1", "units": 250, "hours": 7},
        {"person": "Jose O.", "day": date(2026, 7, 28), "wc": "Repair 3", "units": 648, "hours": 7},
        {"person": "Ana", "day": date(2026, 7, 28), "wc": "Repair 3", "units": 897, "hours": 7},
    ]

    assert goat_notifications.winner_for_day(category, date(2026, 7, 28), records) == {"person": "Jose O.", "wc_name": "Repair 3", "units": 898}


def test_finalize_requires_a_strict_new_record(monkeypatch):
    category = goat_categories.GoatCategory("repairs", "Repairs", "Repair GOAT", group_name="Repairs")
    monkeypatch.setattr(goat_notifications, "_eligible_categories", lambda: (category,))
    monkeypatch.setattr(goat_notifications, "_records_through", lambda _: [
        {"person": "Old Holder", "day": date(2026, 6, 10), "wc": "Repair 3", "units": 891, "hours": 7},
        {"person": "Jose O.", "day": date(2026, 7, 28), "wc": "Repair 3", "units": 891, "hours": 7},
    ])
    inserted = []
    monkeypatch.setattr(goat_notifications.store, "insert_alert_and_delivery", inserted.append)

    assert goat_notifications.finalize_day(date(2026, 7, 28), client=object()) == []
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
    delivery = {"id": 7, "group_name": "Repairs", "person": "Jose O.", "wc_name": "Repair 3", "units": 898, "achieved_day": date(2026, 7, 28), "prior_record_holder": "Jose Ochoa", "prior_record_units": 891, "prior_record_day": date(2026, 6, 10)}
    monkeypatch.setenv("GOAT_SLACK_CHANNEL_ID", "C-MGMT")
    monkeypatch.setattr(goat_notifications.store, "claim_delivery", lambda: delivery)
    monkeypatch.setattr(goat_notifications.slack_client, "post_message", lambda **kwargs: (_ for _ in ()).throw(goat_notifications.slack_client.SlackError("not_in_channel")))
    seen = []
    monkeypatch.setattr(goat_notifications.store, "return_delivery_to_pending", lambda delivery_id, error: seen.append((delivery_id, error)))

    assert goat_notifications.drain_deliveries() == 0
    assert seen == [(7, "not_in_channel")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_goat_notifications.py -q`

Expected: collection fails because `goat_notifications` does not exist.

- [ ] **Step 3: Implement final record selection and transactional alert creation**

Create `src/zira_dashboard/goat_notifications.py`. Do not load `award_overrides`; a Trophy Case edit must not create or suppress an actual production record.

```python
def winner_for_day(category, day: date, records: list[dict]) -> dict | None:
    wc_names = goat_categories.work_center_names(category)
    rows = awards.person_days_in_wc_names(wc_names, day, day, records=records)
    winner = awards.best_person_day(rows)
    if winner is None:
        return None
    contributions = [
        record for record in records
        if record["day"] == day and record["person"] == winner["name"] and record["wc"] in wc_names
    ]
    if not contributions:
        return None
    location = sorted(contributions, key=lambda record: (-float(record["units"]), record["wc"]))[0]
    return {"person": winner["name"], "wc_name": location["wc"], "units": int(round(float(winner["units"]))) }


def _eligible_categories():
    return tuple(category for category in goat_categories.all_categories() if goat_categories.has_metered_source(category))


def _records_through(day: date) -> list[dict]:
    return production_history.daily_records(awards.AWARDS_DATA_FLOOR, day)
```

`finalize_day(day, client)` must call `precompute.precompute_day(day, client)` before `_records_through(day)`. For every eligible category, call `winner_for_day` for the completed day; calculate the prior record with `awards.best_person_day` over the category's person-day rows excluding that day. Insert only if the current integer total strictly exceeds the rounded prior total. Pass this exact dictionary to `store.insert_alert_and_delivery`:

```python
{
    "achieved_day": day,
    "group_name": category.label,
    "person": winner["person"],
    "wc_name": winner["wc_name"],
    "units": winner["units"],
    "prior_record_units": int(round(float(prior["units"]))),
    "prior_record_holder": prior["name"],
    "prior_record_day": prior["day"],
}
```

Only append the alert to the return value when the store returns an ID. That outcome means both `goat_alerts` and the unique outbox row committed together.

- [ ] **Step 4: Implement the approved Slack payload and delivery loop**

Add these exact rendering and delivery functions. The `context` block is Slack's supported secondary treatment; do not attempt custom fonts or colors.

```python
def _short_date(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def _first_name(person: str) -> str:
    return person.strip().split(maxsplit=1)[0] if person.strip() else "them"


def message_payload(alert: dict) -> tuple[str, list[dict]]:
    headline = f"🏆 NEW {str(alert['group_name']).upper()} GOAT!"
    record_line = f"*{alert['person']}* — *{int(alert['units']):,} pallets* at {alert['wc_name']} on {_short_date(alert['achieved_day'])}"
    previous = f"previous = {alert['prior_record_holder']} · {int(alert['prior_record_units'])} · {_short_date(alert['prior_record_day'])}"
    closing = f"Congratulate {_first_name(str(alert['person']))} when you see them! 🎉"
    text = f"{headline}\n{str(alert['person'])} — {int(alert['units']):,} pallets at {alert['wc_name']} on {_short_date(alert['achieved_day'])}\n({previous})\n{closing}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": headline}},
        {"type": "section", "text": {"type": "mrkdwn", "text": record_line}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_({previous})_"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": closing}},
    ]
    return text, blocks


def drain_deliveries() -> int:
    channel_id = os.environ.get("GOAT_SLACK_CHANNEL_ID")
    if not channel_id:
        return 0
    sent = 0
    while delivery := store.claim_delivery():
        text, blocks = message_payload(delivery)
        try:
            result = slack_client.post_message(channel_id=channel_id, text=text, blocks=blocks)
        except slack_client.SlackError as exc:
            store.return_delivery_to_pending(delivery["id"], str(exc))
            break
        store.mark_delivery_sent(delivery["id"], result["message_ts"])
        sent += 1
    return sent
```

Add a warning when `GOAT_SLACK_CHANNEL_ID` is absent, then return zero pending posts without changing any GOAT alert. Stop draining after a Slack failure so the same row is retried on the next worker tick rather than hot-looping. The missing setting is a deployment problem, not a failure to finalize production.

- [ ] **Step 5: Implement restart-safe day-close orchestration**

Implement `run_due(now_utc, client)` as follows:

1. Convert `now_utc` to `shift_config.SITE_TZ`.
2. Call `store.ensure_enabled_on(local_now.date())`.
3. Select today if the local time is at or after `shift_config.shift_end_for(today)`; otherwise select the prior configured workday.
4. For every `store.unfinalized_workdays(latest_completed_day)`, call `finalize_day(day, client)` and then `store.record_finalized_day(day)`. Let source errors raise before recording the day so the normal 60-second worker retry can recover.
5. Call `drain_deliveries()` after finalization.

This catches up a short outage without a historical backfill: the persisted enabled-on date prevents pre-feature production from being scanned, and each completed day is marked only after all category evaluations finish.

- [ ] **Step 6: Verify and commit the finalization slice**

Run: `uv run pytest tests/test_goat_notifications.py tests/test_goat_categories.py tests/test_awards.py -q`

Expected: PASS.

```bash
git add src/zira_dashboard/goat_notifications.py tests/test_goat_notifications.py
git commit -m "feat: finalize GOAT celebrations for Slack"
```

## Task 5: Connect the worker to lifecycle and preserve dashboard behavior

**Files:**

- Modify: `src/zira_dashboard/app.py`
- Modify: `src/zira_dashboard/goat_watch.py`
- Create: `tests/test_goat_notification_warmer.py`
- Modify: `CHANGELOG.md`

**Interfaces:**

- Consumes: `goat_notifications.run_due(now_utc, client)`.
- Produces: `("GOAT notifications", _tick_goat_notifications, 60)` and backward-compatible `goat_watch.maybe_finalize_today(today)`.
- Used by later tasks: existing templates continue calling `goat_watch.active_alerts(today)` unchanged.

- [ ] **Step 1: Write failing warmer and compatibility tests**

Create `tests/test_goat_notification_warmer.py`:

```python
import asyncio
from datetime import UTC

from zira_dashboard import app as app_module
from zira_dashboard import goat_watch


def test_goat_notification_warmer_is_registered_each_minute():
    entry = next(warmer for warmer in app_module._WARMERS if warmer[1] is app_module._tick_goat_notifications)
    assert entry == ("GOAT notifications", app_module._tick_goat_notifications, 60)


def test_tick_runs_the_sync_worker_off_the_event_loop(monkeypatch):
    seen = []

    async def fake_to_thread(func, *args):
        seen.append((func, args))

    monkeypatch.setattr(app_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(app_module, "_zira_client", lambda: "zira-client")
    monkeypatch.setattr("zira_dashboard.goat_notifications.run_due", lambda now_utc, client: None)

    asyncio.run(app_module._tick_goat_notifications())

    assert seen[0][1][1] == "zira-client"
    assert seen[0][1][0].tzinfo is UTC


def test_dashboard_finalization_delegates_to_notification_worker(monkeypatch):
    seen = []
    monkeypatch.setattr("zira_dashboard.goat_notifications.run_due", lambda now_utc, client: seen.append((now_utc, client)))
    monkeypatch.setattr(goat_watch, "_zira_client", lambda: "zira-client")

    goat_watch.maybe_finalize_today(None)

    assert seen[0][1] == "zira-client"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_goat_notification_warmer.py -q`

Expected: collection fails because `_tick_goat_notifications` does not exist.

- [ ] **Step 3: Register the non-blocking app tick**

Add next to the other worker adapters in `src/zira_dashboard/app.py`:

```python
async def _tick_goat_notifications():
    """Finalize due GOAT records and deliver pending celebrations."""
    from . import goat_notifications
    await asyncio.to_thread(goat_notifications.run_due, datetime.now(UTC), _zira_client())
```

Add this tuple immediately after the live-production cache warmer so current-day production is refreshed first:

```python
("GOAT notifications", _tick_goat_notifications, 60),
```

- [ ] **Step 4: Preserve the existing dashboard alert API**

In `src/zira_dashboard/goat_watch.py`, keep `active_alerts()` and `dismiss_alert()` unchanged. Delete the in-memory `_FINALIZED_DAYS` set and replace the old page-visit finalization with this bridge:

```python
def _zira_client():
    from .deps import client
    return client


def finalize_day(day: date) -> list[dict]:
    from . import goat_notifications
    return goat_notifications.finalize_day(day, _zira_client())


def maybe_finalize_today(today: date | None = None) -> None:
    from . import goat_notifications
    goat_notifications.run_due(datetime.now(UTC), _zira_client())
```

The optional `today` parameter remains for existing callers even though durable due-day calculation owns the choice now. This leaves dashboard behavior as a recovery trigger while the warmer becomes the dependable source of execution.

- [ ] **Step 5: Add the release note**

Insert above the current newest date in `CHANGELOG.md`:

```markdown
## 2026-07-29

### Features

- **New GOAT wins now get a celebration in #MGMT-Sups.** After the workday ends, Plant Manager shares the winner, where they worked, how many pallets they made, and the old record they beat. This helps supervisors notice great work and congratulate the person.
```

- [ ] **Step 6: Verify and commit the lifecycle slice**

Run: `uv run pytest tests/test_goat_notification_warmer.py tests/test_new_dashboard_template.py tests/test_operator_dashboard_day_links.py tests/test_slack_client.py -q`

Expected: PASS.

Run: `uv run ruff check src tests`

Expected: `All checks passed!`

```bash
git add src/zira_dashboard/app.py src/zira_dashboard/goat_watch.py tests/test_goat_notification_warmer.py CHANGELOG.md
git commit -m "feat: post finalized GOAT celebrations"
```

## Task 6: End-to-end verification and delivery

**Files:**

- Modify only scoped files from Tasks 1–5 when verification identifies a defect.

**Interfaces:**

- Consumes: completed category registry, durable finalizer/outbox, Slack sender, and app warmer.
- Produces: verified commits pushed to `origin/main`.

- [ ] **Step 1: Run the complete automated suite**

Run: `uv run pytest -q`

Expected: PASS, with only the repository's existing `DATABASE_URL`-gated and documented test-debt skips.

- [ ] **Step 2: Verify the exact message without sending Slack traffic**

Run:

```bash
uv run python -c "from datetime import date; from zira_dashboard.goat_notifications import message_payload; text, blocks = message_payload({'group_name':'Repairs','person':'Jose O.','wc_name':'Repair 3','units':898,'achieved_day':date(2026,7,28),'prior_record_holder':'Jose Ochoa','prior_record_units':891,'prior_record_day':date(2026,6,10)}); assert blocks[0]['text']['text'] == '🏆 NEW REPAIRS GOAT!'; assert 'previous = Jose Ochoa · 891 · Jun 10, 2026' in text; print(text)"
```

Expected output:

```text
🏆 NEW REPAIRS GOAT!
Jose O. — 898 pallets at Repair 3 on Jul 28, 2026
(previous = Jose Ochoa · 891 · Jun 10, 2026)
Congratulate Jose when you see them! 🎉
```

- [ ] **Step 3: Review only the intended worktree changes**

Run: `git status --short`

Expected: GOAT feature files are the only staged or committed work. Preserve unrelated user files such as local tool configuration and lock files; do not stage, alter, or remove them.

- [ ] **Step 4: Push the completed feature**

Run: `git push origin main`

Expected: the implementation commits and child-friendly What's New entry are available on `origin/main`.
