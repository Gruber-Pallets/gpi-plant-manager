# GOAT Slack Delivery Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop test, future, and stale GOAT outbox rows from posting to Slack while allowing real celebrations through their next business day.

**Architecture:** Validate each claimed delivery just before its Slack call. Invalid rows transition to an audited `suppressed` status. The dashboard applies the same canonical-category and non-future checks. Real Postgres tests run only against an explicitly opted-in local test database and roll back every write.

**Tech Stack:** Python, PostgreSQL, pytest, psycopg2.

## Global Constraints

- A delivery may post only for a configured GOAT category, a day no later than today, and the existing through-next-business-day window.
- Invalid rows must never call Slack and must remain auditable as `suppressed`.
- Database-writing tests require `PAYROLL_GUARD_TEST_DATABASE=1`, a loopback host, and a database name ending `_test`; all writes roll back.
- Do not stage unrelated user changes.
- New `CHANGELOG.md` text uses short, plain language.

---

## File Structure

- `src/zira_dashboard/goat_categories.py`: canonical category-key predicate.
- `src/zira_dashboard/goat_notification_store.py`: fenced suppression transition.
- `src/zira_dashboard/goat_notifications.py`: pre-Slack validation.
- `src/zira_dashboard/goat_watch.py`: dashboard filtering.
- `src/zira_dashboard/_schema.py`: the `suppressed` status migration.
- `tests/test_goat_notifications.py`, `tests/test_goat_notification_store.py`, `tests/test_goat_notification_store_safety.py`, and `tests/test_goat_watch.py`: regression coverage.
- `CHANGELOG.md`: plain-language patch note.

### Task 1: Write the Failing Safety Tests

**Files:**

- Modify: `tests/test_goat_notifications.py`
- Modify: `tests/test_goat_notification_store.py`
- Modify: `tests/test_goat_notification_store_safety.py`
- Modify: `tests/test_goat_watch.py`

**Interfaces:**

- Produces: `drain_deliveries(today)`, `suppress_delivery(id, token, reason)`, and a safe real-Postgres test gate.

- [ ] **Step 1: Test unsafe and safe delivery behavior**

Add a `_delivery(**changes)` fixture to `tests/test_goat_notifications.py` that includes a canonical `category_key`, a current `achieved_day`, the existing UUID `client_msg_id`, and `claim_token`. Update existing delivery dictionaries to include `"category_key": "repairs"` and give all existing `drain_deliveries()` calls their test date.

Add three tests:

```python
def test_drain_suppresses_a_noncanonical_delivery_without_posting(monkeypatch):
    delivery = _delivery(category_key="pytest-goat-4e10e3564cd543bfac6924d796bbc864")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-MGMT")
    monkeypatch.setattr(goat_notifications.store, "claim_delivery", iter([delivery, None]).__next__)
    suppressed = []
    monkeypatch.setattr(goat_notifications.store, "suppress_delivery", lambda *args: suppressed.append(args))
    monkeypatch.setattr(goat_notifications.slack_client, "post_message", lambda **_: pytest.fail("unsafe delivery reached Slack"))

    assert goat_notifications.drain_deliveries(date(2026, 7, 29)) == 0
    assert suppressed == [(delivery["id"], delivery["claim_token"], "unknown GOAT category")]
```

The second test uses a canonical category with `achieved_day=date(2099, 1, 2)` and asserts suppression reason `"achieved day is in the future"` with no Slack call. The third uses a current canonical delivery, returns `{"message_ts": "1722280000.000100"}` from mocked Slack, and asserts exactly one `mark_delivery_sent(id, token, message_ts)` call.

- [ ] **Step 2: Test storage, dashboard, and test-DB protection**

Add this test to `tests/test_goat_notification_store.py`:

```python
def test_suppress_delivery_records_the_reason_only_for_its_current_claim(monkeypatch):
    cursor = _RecordingCursor()
    _patch_cursors(monkeypatch, cursor)
    token = "a02b5f81-2c89-4f2d-bcdf-c9f0f431838d"

    store.suppress_delivery(41, token, "unknown GOAT category")

    sql, params = cursor.executed[0]
    assert "SET status = 'suppressed'" in sql
    assert "suppressed_at = now()" in sql
    assert "WHERE id = %s AND status = 'sending' AND claim_token = %s::uuid" in sql
    assert params == ("unknown GOAT category", 41, token)
```

Replace `needs_postgres` with the safe-DSN predicate pattern in `tests/test_payroll_work_entry_store.py`: use `psycopg2.extensions.parse_dsn`, reject `hostaddr`, `service`, and `servicefile`, require `localhost`, `127.0.0.1`, or `::1`, require a database name ending `_test`, and require `PAYROLL_GUARD_TEST_DATABASE == "1"`.

Add this reusable test helper, then call it from each database integration test with that test's existing insert/query assertions as `assertions`:

```python
class _RollbackIntegrationData(Exception):
    pass

def _assert_in_rolled_back_transaction(monkeypatch, assertions):
    db.bootstrap_schema()
    with pytest.raises(_RollbackIntegrationData):
        with db.cursor() as cur:
            def query_in_transaction(sql, params=None):
                cur.execute(sql, params)
                return list(cur.fetchall())

            with monkeypatch.context() as patch:
                patch.setattr(db, "cursor", lambda: nullcontext(cur))
                patch.setattr(db, "query", query_in_transaction)
                assertions()
            raise _RollbackIntegrationData
```

Import `nullcontext` and `psycopg2`; remove the `finally` cleanup blocks. For example, `test_alert_and_delivery_are_single_transactional_unit` defines an `assertions()` closure containing its two inserts and delivery-row assertion, then calls `_assert_in_rolled_back_transaction(monkeypatch, assertions)`. Update `tests/test_goat_notification_store_safety.py` to assert the opt-in variable, loopback hosts, `_test` name condition, rollback exception, and absence of `DELETE FROM goat_alerts`.

In `tests/test_goat_watch.py`, supply `"category_key": "repairs"` in the existing valid row. Add a mock query that returns a future `repairs` row and a current `pytest-goat` row, then assert `active_alerts(date(2026, 8, 12)) == []`.

- [ ] **Step 3: Run the tests red**

Run:

```bash
DATABASE_URL= PAYROLL_GUARD_TEST_DATABASE= .venv/bin/python -m pytest tests/test_goat_notifications.py tests/test_goat_notification_store.py tests/test_goat_notification_store_safety.py tests/test_goat_watch.py -q
```

Expected: FAIL because the current code neither accepts a delivery date nor suppresses invalid rows; dashboard filtering is absent. The real-Postgres tests skip.

### Task 2: Implement the Audited Delivery Gate

**Files:**

- Modify: `src/zira_dashboard/_schema.py`
- Modify: `src/zira_dashboard/goat_categories.py`
- Modify: `src/zira_dashboard/goat_notification_store.py`
- Modify: `src/zira_dashboard/goat_notifications.py`
- Modify: `src/zira_dashboard/goat_watch.py`
- Modify: `CHANGELOG.md`

**Interfaces:**

- Produces: `has_category_key(key) -> bool`, `suppress_delivery(id, token, reason) -> None`, and `drain_deliveries(today) -> int`.

- [ ] **Step 1: Add category and store helpers**

Add to `goat_categories.py`:

```python
def has_category_key(key: str | None) -> bool:
    return any(category.key == key for category in _CATEGORIES)
```

Add to `goat_notification_store.py`:

```python
def suppress_delivery(delivery_id: int, claim_token: str, reason: str) -> None:
    from . import db
    with db.cursor() as cur:
        cur.execute(
            "UPDATE goat_slack_deliveries "
            "SET status = 'suppressed', suppressed_at = now(), last_error = %s "
            "WHERE id = %s AND status = 'sending' AND claim_token = %s::uuid",
            (reason, delivery_id, str(claim_token)),
        )
```

Include `alert.category_key` in `claim_delivery()`'s `RETURNING` list.

- [ ] **Step 2: Make the schema accept and audit suppression**

In `_schema.py`, define the delivery status check as named and allow `suppressed`; add `suppressed_at TIMESTAMPTZ`:

```sql
status TEXT NOT NULL DEFAULT 'pending' CONSTRAINT goat_slack_deliveries_status_check
  CHECK (status IN ('pending', 'sending', 'sent', 'suppressed')),
suppressed_at TIMESTAMPTZ,
```

After the current claim-token migration, add an idempotent existing-database migration:

```sql
ALTER TABLE goat_slack_deliveries ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ;
ALTER TABLE goat_slack_deliveries DROP CONSTRAINT IF EXISTS goat_slack_deliveries_status_check;
ALTER TABLE goat_slack_deliveries ADD CONSTRAINT goat_slack_deliveries_status_check
  CHECK (status IN ('pending', 'sending', 'sent', 'suppressed'));
```

- [ ] **Step 3: Gate Slack and dashboard output**

In `goat_notifications.py`, import `goat_watch` and add:

```python
def delivery_suppression_reason(delivery: dict, today: date) -> str | None:
    if not goat_categories.has_category_key(delivery.get("category_key")):
        return "unknown GOAT category"
    achieved_day = delivery["achieved_day"]
    if achieved_day > today:
        return "achieved day is in the future"
    if today > goat_watch.next_business_day(achieved_day):
        return "delivery window expired"
    return None
```

Change `drain_deliveries` to require `today`. Immediately after each claim and before `message_payload` or Slack, suppress and continue when the helper returns a reason. Change `run_due` to call `drain_deliveries(today)`.

In `goat_watch.py`, select `category_key`, locally import `goat_categories`, and only append when `has_category_key(r.get("category_key")) and ach <= today <= next_business_day(ach)`.

- [ ] **Step 4: Add the patch note and run green**

Add this to the top of `CHANGELOG.md`:

```markdown
## 2026-08-12

### Safer GOAT celebrations

#### Fixes

- **GOAT celebration messages now ignore test, future, and old records.** This keeps pretend results out of Slack while real new records can still be celebrated on time.
```

Run:

```bash
DATABASE_URL= PAYROLL_GUARD_TEST_DATABASE= .venv/bin/python -m pytest tests/test_goat_categories.py tests/test_goat_notifications.py tests/test_goat_notification_store.py tests/test_goat_notification_store_safety.py tests/test_goat_notification_warmer.py tests/test_goat_watch.py -q
.venv/bin/ruff check src/zira_dashboard/_schema.py src/zira_dashboard/goat_categories.py src/zira_dashboard/goat_notification_store.py src/zira_dashboard/goat_notifications.py src/zira_dashboard/goat_watch.py tests/test_goat_notifications.py tests/test_goat_notification_store.py tests/test_goat_notification_store_safety.py tests/test_goat_watch.py
git diff --check
```

Expected: tests pass, database tests skip without an explicit safe test database, and lint/diff checks are clean.

### Task 3: Commit and Push the Verified Fix

**Files:**

- Modify: only the files listed in Tasks 1 and 2.

- [ ] **Step 1: Review scope, commit, and push**

Run:

```bash
git diff --check
git diff -- CHANGELOG.md src/zira_dashboard/_schema.py src/zira_dashboard/goat_categories.py src/zira_dashboard/goat_notification_store.py src/zira_dashboard/goat_notifications.py src/zira_dashboard/goat_watch.py tests/test_goat_notifications.py tests/test_goat_notification_store.py tests/test_goat_notification_store_safety.py tests/test_goat_watch.py
git add CHANGELOG.md src/zira_dashboard/_schema.py src/zira_dashboard/goat_categories.py src/zira_dashboard/goat_notification_store.py src/zira_dashboard/goat_notifications.py src/zira_dashboard/goat_watch.py tests/test_goat_notifications.py tests/test_goat_notification_store.py tests/test_goat_notification_store_safety.py tests/test_goat_watch.py
git commit -m "fix: suppress unsafe GOAT Slack deliveries"
git push origin main
```

Expected: the first production worker boot claims the leaked `pytest-goat` row, marks it `suppressed`, and does not call Slack.
