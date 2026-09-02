# Anniversary PTO Employee Reminders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require employees with unused Paid Time Off to acknowledge a reminder during the 30 days before their work anniversary, expose the audit history on a restored Staffing Employee tab, and keep existing employee-profile links under Staffing navigation.

**Architecture:** A six-hour background reconciler computes observed anniversaries, batch-refreshes only the candidate employees' Odoo balances, and writes idempotent rows into the existing `employee_notifications` queue. The existing secure kiosk interstitial records first presentation and acknowledgement before allowing access to punch controls. Employee profiles keep their cached performance response and load a small uncached acknowledgement-history partial.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, PostgreSQL/psycopg2, HTMX, pytest, Ruff.

## Global Constraints

- Repository identity must remain `gpi-plant-manager`.
- Feedback identity is `GPI-PM-FB-40`; Task ID 3606 is not lifecycle authority.
- The reminder window is inclusive from 30 calendar days before the observed anniversary through the anniversary date.
- Use exactly one active, allocation-backed leave type named `Paid Time Off`; never guess between matches.
- Use fresh `available_practical` balances and retain the source unit (`days` or `hours`).
- Never call Odoo from the time-clock sign-in or punch path.
- One employee receives at most one reminder for each anniversary date.
- Once a notice has been presented, its anniversary and balance snapshot never change.
- Existing `/staffing/people/<url-encoded-name>` URLs remain valid.
- New What's New text must use short, common words and explain user benefit.
- Preserve all unrelated working-tree changes.

---

### Task 1: Add audited anniversary fields and notification-store operations

**Files:**
- Modify: `src/zira_dashboard/_schema.py:1369-1389,1508-1512`
- Modify: `src/zira_dashboard/employee_notifications.py:1-280`
- Modify: `tests/test_schema_employee_notifications.py`
- Modify: `tests/test_employee_notifications.py`

**Interfaces:**
- Consumes: existing `db.cursor`, `db.query`, and employee-scoped notification rows.
- Produces: `AnniversaryPtoNotice`, `reconcile_anniversary_pto(notices)`, and `list_history(person_odoo_id)`; `list_unacknowledged(person_odoo_id)` also records first presentation.

- [ ] **Step 1: Write failing schema tests**

Add exact assertions:

```python
def test_schema_adds_anniversary_pto_audit_fields():
    for column in (
        "anniversary_date DATE",
        "balance_amount NUMERIC(8,2)",
        "balance_unit TEXT",
        "presented_at TIMESTAMPTZ",
    ):
        assert column in SCHEMA_DDL
    assert "employee_notifications_anniversary_pto_dedupe" in SCHEMA_DDL
    assert "(person_odoo_id, anniversary_date, kind)" in SCHEMA_DDL
    assert "anniversary_date IS NOT NULL" in SCHEMA_DDL
```

- [ ] **Step 2: Write failing store tests**

Cover first presentation, person scoping, ordered history, deduplication, pre-display updates/removal, and frozen presented rows:

```python
def test_list_unacknowledged_records_first_presentation(fake_db):
    fake_db["query_result"] = [{"id": 9, "kind": "anniversary_pto_reminder"}]
    assert en.list_unacknowledged(5)[0]["id"] == 9
    sql, params = fake_db["executes"][0]
    assert "presented_at = COALESCE(presented_at, now())" in sql
    assert "person_odoo_id = %s" in sql
    assert params == ([9], 5)


def test_history_is_person_scoped_and_newest_first(fake_db):
    en.list_history(5)
    sql, params = fake_db["queries"][0]
    assert "WHERE person_odoo_id = %s" in sql
    assert "ORDER BY created_at DESC, id DESC" in sql
    assert "anniversary_date" in sql and "presented_at" in sql
    assert params == (5,)


def test_reconcile_updates_only_unpresented_anniversary_notice(monkeypatch):
    # Use a fake transaction cursor. Assert the upsert conflict target is the
    # anniversary partial index and its DO UPDATE arm requires both audit
    # timestamps to be null.
    notice = en.AnniversaryPtoNotice(5, date(2026, 10, 2), Decimal("2.5"), "days")
    en.reconcile_anniversary_pto((notice,))
    assert "ON CONFLICT (person_odoo_id, anniversary_date, kind)" in insert_sql
    assert "employee_notifications.presented_at IS NULL" in insert_sql
    assert "employee_notifications.acknowledged_at IS NULL" in insert_sql
```

Also test that stale cleanup targets only `kind = 'anniversary_pto_reminder'`,
`presented_at IS NULL`, and `acknowledged_at IS NULL`, and that a presented row
is not deleted or updated.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_schema_employee_notifications.py \
  tests/test_employee_notifications.py -q
```

Expected: failures for the missing columns, dataclass, reconciliation API,
history API, and presentation write.

- [ ] **Step 4: Add the additive schema migration**

Append idempotent DDL after the existing `saturday_day` notification migration:

```sql
ALTER TABLE employee_notifications ADD COLUMN IF NOT EXISTS anniversary_date DATE;
ALTER TABLE employee_notifications ADD COLUMN IF NOT EXISTS balance_amount NUMERIC(8,2);
ALTER TABLE employee_notifications ADD COLUMN IF NOT EXISTS balance_unit TEXT;
ALTER TABLE employee_notifications ADD COLUMN IF NOT EXISTS presented_at TIMESTAMPTZ;
ALTER TABLE employee_notifications DROP CONSTRAINT IF EXISTS employee_notifications_balance_unit_check;
ALTER TABLE employee_notifications ADD CONSTRAINT employee_notifications_balance_unit_check
  CHECK (balance_unit IS NULL OR balance_unit IN ('days', 'hours'));
CREATE UNIQUE INDEX IF NOT EXISTS employee_notifications_anniversary_pto_dedupe
  ON employee_notifications (person_odoo_id, anniversary_date, kind)
  WHERE anniversary_date IS NOT NULL;
```

- [ ] **Step 5: Implement the store interfaces**

Add:

```python
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AnniversaryPtoNotice:
    person_odoo_id: int
    anniversary_date: date
    balance_amount: Decimal
    balance_unit: str


def reconcile_anniversary_pto(notices: tuple[AnniversaryPtoNotice, ...]) -> None:
    expected = {(n.person_odoo_id, n.anniversary_date): n for n in notices}
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT id, person_odoo_id, anniversary_date FROM employee_notifications "
            "WHERE kind = 'anniversary_pto_reminder' AND presented_at IS NULL "
            "AND acknowledged_at IS NULL FOR UPDATE"
        )
        for row in cursor.fetchall():
            if (row["person_odoo_id"], row["anniversary_date"]) not in expected:
                cursor.execute(
                    "DELETE FROM employee_notifications WHERE id = %s "
                    "AND kind = 'anniversary_pto_reminder' "
                    "AND presented_at IS NULL AND acknowledged_at IS NULL",
                    (row["id"],),
                )
        for notice in notices:
            amount = format(notice.balance_amount.normalize(), "f")
            title = "Your work anniversary is coming up"
            body = (
                f"Your work anniversary is {_md(notice.anniversary_date)}. "
                f"You have {amount} {notice.balance_unit} of unused Paid Time Off. "
                "Please plan to use your time or talk with your supervisor if you have questions."
            )
            cursor.execute(
                "INSERT INTO employee_notifications "
                "(person_odoo_id, kind, title, body, anniversary_date, "
                "balance_amount, balance_unit) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (person_odoo_id, anniversary_date, kind) "
                "WHERE anniversary_date IS NOT NULL DO UPDATE SET "
                "title = EXCLUDED.title, body = EXCLUDED.body, "
                "balance_amount = EXCLUDED.balance_amount, "
                "balance_unit = EXCLUDED.balance_unit "
                "WHERE employee_notifications.presented_at IS NULL "
                "AND employee_notifications.acknowledged_at IS NULL",
                (notice.person_odoo_id, "anniversary_pto_reminder", title, body,
                 notice.anniversary_date, notice.balance_amount, notice.balance_unit),
            )
```

Extend `list_unacknowledged` to select the new fields, then call a private
`_mark_presented(person_odoo_id, ids)` using:

```python
db.execute(
    "UPDATE employee_notifications "
    "SET presented_at = COALESCE(presented_at, now()) "
    "WHERE id = ANY(%s) AND person_odoo_id = %s AND acknowledged_at IS NULL",
    (ids, person_odoo_id),
)
```

Add `list_history` with an explicit select of title, body, anniversary/balance,
created, presented, and acknowledged fields ordered newest first. Do not expose
an unscoped history query.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 3 command. Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/employee_notifications.py \
  tests/test_schema_employee_notifications.py tests/test_employee_notifications.py
git commit -m "feat: audit employee notification acknowledgements"
git push origin main
```

---

### Task 2: Add an explicit batched balance refresh API

**Files:**
- Modify: `src/zira_dashboard/time_off_balances.py:45-150`
- Modify: `tests/test_time_off_balances.py`

**Interfaces:**
- Consumes: `odoo_client.fetch_balances_for_many(employee_ids)` and `_upsert_balance_rows`.
- Produces: `refresh_for_employees(employee_ids: list[int]) -> dict[int, list[dict]] | None`; `None` uniquely means refresh failure.

- [ ] **Step 1: Write the failing batch tests**

```python
def test_refresh_for_employees_returns_fresh_rows_after_one_batch(monkeypatch, fake_db):
    fresh = {5: [_balance(1, "days", 2.5)], 9: [_balance(1, "hours", 6)]}
    fetch = MagicMock(return_value=fresh)
    monkeypatch.setattr(time_off_balances.odoo_client, "fetch_balances_for_many", fetch)
    assert time_off_balances.refresh_for_employees([5, 9]) == fresh
    fetch.assert_called_once_with([5, 9])
    assert len(fake_db["execute_values"]) == 1


def test_refresh_for_employees_distinguishes_failure(monkeypatch, fake_db):
    monkeypatch.setattr(
        time_off_balances.odoo_client,
        "fetch_balances_for_many",
        MagicMock(side_effect=RuntimeError("unavailable")),
    )
    assert time_off_balances.refresh_for_employees([5]) is None
    assert fake_db["execute_values"] == []
```

Also assert an empty input returns `{}` without an Odoo call.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_time_off_balances.py -q
```

Expected: `AttributeError` for `refresh_for_employees`.

- [ ] **Step 3: Implement the minimal helper**

```python
def refresh_for_employees(
    employee_odoo_ids: list[int],
) -> dict[int, list[dict]] | None:
    ids = list(dict.fromkeys(employee_odoo_ids))
    if not ids:
        return {}
    try:
        by_employee = odoo_client.fetch_balances_for_many(ids)
    except Exception as error:  # noqa: BLE001 -- scheduled refresh retries later
        _log.info("Balance refresh for %d employees failed: %s", len(ids), error)
        return None
    rows = [
        row
        for person_odoo_id, balances in by_employee.items()
        for row in _balance_rows(person_odoo_id, balances)
    ]
    _upsert_balance_rows(rows)
    return by_employee
```

Refactor `refresh_stale` to delegate to this helper without changing its return
contract. Keep the existing single-employee interactive helper behavior stable.

- [ ] **Step 4: Run and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/time_off_balances.py tests/test_time_off_balances.py
git commit -m "feat: refresh employee PTO balances in one batch"
git push origin main
```

---

### Task 3: Reconcile anniversary reminders from observed dates and fresh PTO

**Files:**
- Create: `src/zira_dashboard/anniversary_pto_reminders.py`
- Create: `tests/test_anniversary_pto_reminders.py`

**Interfaces:**
- Consumes: `employee_celebrations.event_day_for`, `time_off_balances.refresh_for_employees`, `employee_notifications.reconcile_anniversary_pto`, `employee_notifications.notifications_enabled`.
- Produces: `upcoming_anniversary(first_contract_date, today) -> date | None` and `run(today: date | None = None) -> int`.

- [ ] **Step 1: Write failing pure date tests**

```python
@pytest.mark.parametrize(
    ("contract", "today", "expected"),
    [
        (date(2020, 10, 2), date(2026, 9, 2), date(2026, 10, 2)),
        (date(2020, 10, 3), date(2026, 9, 2), None),
        (date(2020, 9, 2), date(2026, 9, 2), date(2026, 9, 2)),
        (date(2020, 1, 5), date(2026, 12, 20), date(2027, 1, 5)),
        (date(2024, 2, 29), date(2027, 1, 29), date(2027, 2, 28)),
        (date(2026, 9, 2), date(2026, 9, 2), None),
    ],
)
def test_upcoming_anniversary(contract, today, expected):
    assert reminders.upcoming_anniversary(contract, today) == expected
```

- [ ] **Step 2: Write failing reconciliation tests**

Use monkeypatched local people/type reads, a fake batch refresh, and a capture of
`reconcile_anniversary_pto`. Cover inactive people, malformed dates, disabled
notifications, exact one-type matching, missing/ambiguous types, positive days,
positive hours, zero/negative balances, one batch call, and no write after a
refresh failure.

```python
def test_run_reconciles_positive_fresh_paid_time_off(monkeypatch):
    install_people(monkeypatch, [(5, date(2020, 10, 2)), (9, date(2021, 9, 20))])
    install_one_pto_type(monkeypatch, holiday_status_id=7)
    refresh = MagicMock(return_value={
        5: [{"holiday_status_id": 7, "available_practical": 2.5, "unit": "days"}],
        9: [{"holiday_status_id": 7, "available_practical": 6, "unit": "hours"}],
    })
    monkeypatch.setattr(reminders.time_off_balances, "refresh_for_employees", refresh)
    captured = []
    monkeypatch.setattr(reminders.employee_notifications, "reconcile_anniversary_pto",
                        lambda notices: captured.extend(notices))
    assert reminders.run(date(2026, 9, 2)) == 2
    refresh.assert_called_once_with([5, 9])
    assert [(n.person_odoo_id, n.balance_unit) for n in captured] == [(5, "days"), (9, "hours")]
```

- [ ] **Step 3: Run and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_anniversary_pto_reminders.py -q
```

Expected: import failure because the module does not exist.

- [ ] **Step 4: Implement the reconciler**

Use this public shape:

```python
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from . import db, employee_celebrations, employee_notifications, time_off_balances
from .plant_day import today as plant_today

WINDOW_DAYS = 30


def upcoming_anniversary(first_contract_date: date, today: date) -> date | None:
    for year in (today.year, today.year + 1):
        event = employee_celebrations.event_day_for(
            year, first_contract_date.month, first_contract_date.day
        )
        if year - first_contract_date.year > 0 and today <= event <= today + timedelta(days=WINDOW_DAYS):
            return event
    return None


def _paid_time_off_type_id() -> int | None:
    rows = db.query(
        "SELECT holiday_status_id, name, request_unit, requires_allocation, active "
        "FROM leave_types_cache ORDER BY holiday_status_id"
    )
    matches = [
        row for row in rows
        if row.get("name") == "Paid Time Off"
        and row.get("request_unit") in {"day", "half_day", "hour"}
        and row.get("requires_allocation") == "yes"
        and row.get("active") is True
    ]
    return int(matches[0]["holiday_status_id"]) if len(matches) == 1 else None


def run(today: date | None = None) -> int:
    if not employee_notifications.notifications_enabled():
        return 0
    today = today or plant_today()
    people = db.query(
        "SELECT odoo_id, first_contract_date FROM people "
        "WHERE active = TRUE AND excluded = FALSE AND odoo_id IS NOT NULL "
        "AND first_contract_date IS NOT NULL ORDER BY odoo_id"
    )
    candidates = [
        (int(row["odoo_id"]), anniversary)
        for row in people
        if isinstance(row.get("first_contract_date"), date)
        if (anniversary := upcoming_anniversary(row["first_contract_date"], today)) is not None
    ]
    type_id = _paid_time_off_type_id()
    if type_id is None:
        return 0
    fresh = time_off_balances.refresh_for_employees([person_id for person_id, _ in candidates])
    if fresh is None:
        return 0
    notices = []
    for person_id, anniversary in candidates:
        matches = [row for row in fresh.get(person_id, []) if row.get("holiday_status_id") == type_id]
        if len(matches) != 1:
            continue
        balance = Decimal(str(matches[0].get("available_practical", 0)))
        unit = matches[0].get("unit")
        if balance > 0 and unit in {"days", "hours"}:
            notices.append(employee_notifications.AnniversaryPtoNotice(
                person_id, anniversary, balance, unit
            ))
    employee_notifications.reconcile_anniversary_pto(tuple(notices))
    return len(notices)
```

Keep unexpected failures visible to the warmer wrapper; only the documented
Odoo refresh failure becomes a no-change return.

- [ ] **Step 5: Run and verify GREEN**

Run the Step 3 command. Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/anniversary_pto_reminders.py \
  tests/test_anniversary_pto_reminders.py
git commit -m "feat: generate anniversary PTO reminders"
git push origin main
```

---

### Task 4: Schedule the reconciliation every six hours

**Files:**
- Modify: `src/zira_dashboard/app.py:205-225,506-540`
- Create: `tests/test_anniversary_pto_warmer.py`

**Interfaces:**
- Consumes: `anniversary_pto_reminders.run()`.
- Produces: `_tick_anniversary_pto_reminders()` and registry entry `("anniversary PTO reminders", ..., 21600)`.

- [ ] **Step 1: Write the failing warmer tests**

```python
def test_anniversary_pto_warmer_runs_every_six_hours():
    entry = next(
        warmer for warmer in app_module._WARMERS
        if warmer[1] is app_module._tick_anniversary_pto_reminders
    )
    assert entry == (
        "anniversary PTO reminders",
        app_module._tick_anniversary_pto_reminders,
        21600,
    )


def test_anniversary_pto_tick_runs_off_event_loop(monkeypatch):
    seen = []
    async def fake_to_thread(func, *args):
        seen.append((func, args))
    monkeypatch.setattr(app_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr("zira_dashboard.anniversary_pto_reminders.run", lambda: 0)
    asyncio.run(app_module._tick_anniversary_pto_reminders())
    assert seen[0][0] is anniversary_pto_reminders.run
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_anniversary_pto_warmer.py -q
```

Expected: missing tick function.

- [ ] **Step 3: Add the tick and registry entry**

```python
async def _tick_anniversary_pto_reminders():
    from . import anniversary_pto_reminders
    await asyncio.to_thread(anniversary_pto_reminders.run)
```

Register it at `21600` seconds near the existing time-off balance warmer.

- [ ] **Step 4: Run and verify GREEN**

Run the Step 2 command plus `tests/test_page_warmer.py`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/app.py tests/test_anniversary_pto_warmer.py
git commit -m "feat: schedule anniversary PTO reminders"
git push origin main
```

---

### Task 5: Render and require the bilingual kiosk acknowledgement

**Files:**
- Modify: `src/zira_dashboard/routes/timeclock.py:700-755`
- Modify: `src/zira_dashboard/templates/timeclock_notifications.html`
- Modify: `src/zira_dashboard/timeclock_i18n.py:50-66`
- Modify: `tests/test_timeclock_notifications_routes.py`
- Modify: `tests/test_timeclock_bilingual_render.py`

**Interfaces:**
- Consumes: new fields returned by `employee_notifications.list_unacknowledged`.
- Produces: bilingual `anniversary_pto_reminder` card and **I acknowledge** submission through the existing secure acknowledgement route.

- [ ] **Step 1: Write failing English and Spanish route tests**

```python
def test_anniversary_pto_notice_blocks_dashboard_and_renders_snapshot(monkeypatch):
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _pid: PERSON)
    monkeypatch.setattr(employee_notifications, "list_unacknowledged", lambda _oid: [{
        "id": 4,
        "kind": "anniversary_pto_reminder",
        "anniversary_date": date(2026, 10, 2),
        "balance_amount": Decimal("2.5"),
        "balance_unit": "days",
    }])
    response = client.get(f"/timeclock/notifications/{timeclock._mint_token(1)}")
    assert "Your work anniversary is coming up" in response.text
    assert "October 2" in response.text
    assert "2.5 days of unused Paid Time Off" in response.text
    assert "I acknowledge" in response.text
    assert "/timeclock/dashboard/" not in response.text
```

Add a Spanish-primary case asserting the Spanish title/body/button precede the
English fallback and an hours-unit case asserting `6 hours`.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_timeclock_notifications_routes.py \
  tests/test_timeclock_bilingual_render.py -q
```

Expected: anniversary card and translation assertions fail.

- [ ] **Step 3: Shape anniversary render values in the route**

For anniversary rows, add `anniversary_label` using the full month/day format
and `balance_label` using a trailing-zero-free amount plus stored unit. Do not
fetch a current balance or call Odoo here.

```python
if n.get("kind") == "anniversary_pto_reminder":
    n["anniversary_label"] = n["anniversary_date"].strftime("%B %-d")
    n["balance_label"] = (
        f"{format(n['balance_amount'].normalize(), 'f')} {n['balance_unit']}"
    )
```

Use the existing Windows-safe date-format convention where required.

- [ ] **Step 4: Add the template branch and translations**

Render the anniversary branch before generic cancellation fallback:

```jinja2
{% elif n.kind == 'anniversary_pto_reminder' %}
  {% set card_title = t("Your work anniversary is coming up") %}
  {% set card_body = t(
    "Your work anniversary is {anniversary}. You have {balance} of unused Paid Time Off. Please plan to use your time or talk with your supervisor if you have questions.",
    anniversary=n.anniversary_label,
    balance=n.balance_label
  ) %}
{% endif %}
```

Change the form button to `t("I acknowledge")`. Add these exact Mexican/Latin
American Spanish translations to `TRANSLATIONS`:

```python
"I acknowledge": "Confirmo que lo leí",
"Your work anniversary is coming up": "Se acerca tu aniversario de trabajo",
"Your work anniversary is {anniversary}. You have {balance} of unused Paid Time Off. Please plan to use your time or talk with your supervisor if you have questions.": (
    "Tu aniversario de trabajo es el {anniversary}. Tienes {balance} de tiempo "
    "libre pagado sin usar. Planea usar tu tiempo o habla con tu supervisor si "
    "tienes preguntas."
),
```

Keep the existing English fallback behavior.

- [ ] **Step 5: Run and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/timeclock.py \
  src/zira_dashboard/templates/timeclock_notifications.html \
  src/zira_dashboard/timeclock_i18n.py \
  tests/test_timeclock_notifications_routes.py \
  tests/test_timeclock_bilingual_render.py
git commit -m "feat: require anniversary PTO acknowledgement"
git push origin main
```

---

### Task 6: Restore the Staffing Employee tab and add live acknowledgement history

**Files:**
- Modify: `src/zira_dashboard/templates/_staffing_subnav.html`
- Modify: `src/zira_dashboard/routes/people.py:140-325`
- Modify: `src/zira_dashboard/templates/player_card.html:1-180`
- Create: `src/zira_dashboard/templates/_employee_acknowledgement_history.html`
- Modify: `tests/test_player_card.py`
- Create: `tests/test_employee_acknowledgement_history.py`
- Modify: `tests/test_page_views.py`
- Modify: `src/zira_dashboard/page_views.py:45-65`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `employee_notifications.list_history(person_odoo_id)` and `staffing.Person.employee_id`.
- Produces: active Staffing `employee` subtab, unchanged existing profile URLs, `GET /staffing/people/{name}/acknowledgements`, and `#acknowledgement-history` on the profile.

- [ ] **Step 1: Write failing navigation tests**

```python
def test_player_card_uses_staffing_employee_navigation(client, monkeypatch):
    install_bare_player_card_sources(monkeypatch, name="Carlos", employee_id=5)
    html = client.get("/staffing/people/Carlos").text
    assert 'href="/staffing"' in html
    assert 'href="/staffing/people" class="active">Employee</a>' in html
    assert 'href="/people-performance" class="active"' not in html


def test_staffing_subnav_exposes_employee_landing():
    html = Path("src/zira_dashboard/templates/_staffing_subnav.html").read_text()
    assert 'href="/staffing/people"' in html
    assert ">Employee</a>" in html
```

Keep existing Skills Matrix hyperlink tests unchanged to prove URL
compatibility.

- [ ] **Step 2: Write failing history endpoint and partial tests**

```python
def test_employee_history_endpoint_is_person_scoped(client, monkeypatch):
    install_roster(monkeypatch, [Person("Carlos", active=True, employee_id=5)])
    history = MagicMock(return_value=[{
        "kind": "anniversary_pto_reminder",
        "title": "Your work anniversary is coming up",
        "body": "snapshot",
        "anniversary_date": date(2026, 10, 2),
        "balance_amount": Decimal("2.5"),
        "balance_unit": "days",
        "presented_at": aware_datetime(2026, 9, 2, 6, 54),
        "acknowledged_at": aware_datetime(2026, 9, 2, 6, 55),
    }])
    monkeypatch.setattr(employee_notifications, "list_history", history)
    response = client.get("/staffing/people/Carlos/acknowledgements")
    assert response.status_code == 200
    history.assert_called_once_with(5)
    assert "Anniversary PTO reminder" in response.text
    assert "2.5 days" in response.text
    assert "Acknowledged" in response.text
```

Add cases for waiting, legacy `presented_at=None`, empty history, unknown
employee, and a caught history-read failure that renders
`Acknowledgement history is unavailable right now.` without breaking the main
profile.

- [ ] **Step 3: Run and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_player_card.py \
  tests/test_employee_acknowledgement_history.py \
  tests/test_roster_filter.py \
  tests/test_staffing_static.py \
  tests/test_page_views.py -q
```

Expected: missing Employee tab, active-navigation, endpoint, and history
section failures; existing hyperlink tests remain green.

- [ ] **Step 4: Fix the navigation ownership**

Add the new subtab immediately after Plant Scheduler:

```jinja2
<a href="/staffing/people" class="{% if active == 'employee' %}active{% endif %}">Employee</a>
```

Change the player-card context from `"active": "people"` to
`"active": "employee"`. Do not change the landing or detail route paths.

- [ ] **Step 5: Add the live history section and endpoint**

Insert after `.pc-totals`:

```jinja2
<section id="acknowledgement-history"
         class="pc-ack-history"
         hx-get="/staffing/people/{{ name|urlencode }}/acknowledgements"
         hx-trigger="load"
         hx-swap="innerHTML">
  <h3>Acknowledgement history</h3>
  <p class="hint">Loading acknowledgement history…</p>
</section>
```

Add the route after the landing route:

```python
@router.get("/staffing/people/{name}/acknowledgements", response_class=HTMLResponse)
def employee_acknowledgement_history(request: Request, name: str):
    roster = {person.name: person for person in staffing.load_roster()}
    person = roster.get(name)
    if person is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    available = True
    rows = []
    if person.employee_id is not None:
        try:
            rows = employee_notifications.list_history(person.employee_id)
        except Exception:
            log.exception("Could not load notification history for employee %s", person.employee_id)
            available = False
    return templates.TemplateResponse(request, "_employee_acknowledgement_history.html", {
        "history_available": available,
        "notification_history": _history_context(rows),
    })
```

Import `HTTPException`, `logging`, `employee_notifications`, and use
`shift_config.SITE_TZ` in these view helpers:

```python
def _history_time(value) -> str | None:
    if value is None:
        return None
    local = value.astimezone(shift_config.SITE_TZ)
    fmt = "%b %#d, %Y · %#I:%M %p" if os.name == "nt" else "%b %-d, %Y · %-I:%M %p"
    return local.strftime(fmt)


def _history_context(rows: list[dict]) -> list[dict]:
    labels = {
        "anniversary_pto_reminder": "Anniversary PTO reminder",
        "time_off_approved": "Time off approved",
        "time_off_denied": "Time off denied",
        "time_off_cancelled": "Time off cancelled",
        "saturday_work_cancelled": "Optional work cancelled",
    }
    result = []
    for row in rows:
        anniversary = row.get("anniversary_date")
        amount = row.get("balance_amount")
        unit = row.get("balance_unit")
        detail = row.get("body") or "—"
        if anniversary is not None and amount is not None and unit in {"days", "hours"}:
            detail = f"{anniversary:%b %d} · {format(amount.normalize(), 'f')} {unit}"
        acknowledged = _history_time(row.get("acknowledged_at"))
        result.append({
            "notice": labels.get(row.get("kind"), row.get("title") or "Employee notice"),
            "displayed": _history_time(row.get("presented_at")) or "Not recorded",
            "details": detail,
            "status": f"Acknowledged {acknowledged}" if acknowledged else "Waiting for acknowledgement",
            "acknowledged": acknowledged is not None,
        })
    return result
```

Use Windows-safe date formatting for `detail` in the real implementation rather
than the abbreviated f-string shown for readability.

The new partial is complete with this semantic structure:

```jinja2
<div class="pc-section-head">
  <h3>Acknowledgement history</h3>
  <span class="hint">Employee notices shown before the punch screen</span>
</div>
{% if not history_available %}
  <p class="hint">Acknowledgement history is unavailable right now.</p>
{% elif not notification_history %}
  <p class="hint">No acknowledgement history yet.</p>
{% else %}
  <div class="pc-table-wrap">
    <table class="pc pc-ack-table">
      <thead><tr><th>Notice</th><th>Displayed</th><th>Details</th><th>Status</th></tr></thead>
      <tbody>
      {% for row in notification_history %}
        <tr>
          <td>{{ row.notice }}</td>
          <td>{{ row.displayed }}</td>
          <td>{{ row.details }}</td>
          <td><span class="pc-ack-status {{ 'seen' if row.acknowledged else 'waiting' }}">{{ row.status }}</span></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
{% endif %}
```

Add focused `.pc-ack-history`, `.pc-section-head`, `.pc-table-wrap`, and
`.pc-ack-status` styles to `player_card.html`; stack the section heading and
allow the semantic table wrapper to scroll at narrow widths. Do not add a
manager-wide dashboard.

Add focused `.pc-ack-history` styles to `player_card.html`; stack or hide only
the least important details column at narrow widths. Do not add a manager-wide
dashboard.

- [ ] **Step 6: Update page-view tracking coverage and What's New**

Add `"/staffing/people/{name}/acknowledgements"` to `_EXCLUDE_EXACT` in
`page_views.py`, with this regression assertion:

```python
assert page_views.should_track("/staffing/people/{name}/acknowledgements") is False
```

This prevents the background HTMX load from double-counting a profile visit.

Add a newest-first plain-language entry:

```markdown
### Employees can confirm anniversary PTO reminders

- **Employees now see a reminder before punching in when their work anniversary is close and they still have PTO.** They must confirm it before continuing. Managers can see the confirmation history on each employee's new Staffing page.
```

- [ ] **Step 7: Run and verify GREEN**

Run the Step 3 command. Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/templates/_staffing_subnav.html \
  src/zira_dashboard/routes/people.py \
  src/zira_dashboard/templates/player_card.html \
  src/zira_dashboard/templates/_employee_acknowledgement_history.html \
  tests/test_player_card.py tests/test_employee_acknowledgement_history.py \
  src/zira_dashboard/page_views.py tests/test_page_views.py CHANGELOG.md
git commit -m "feat: show employee acknowledgement history"
git push origin main
```

---

### Task 7: Run complete verification and close feedback 40

**Files:**
- Verify: all files changed in Tasks 1-6
- Lifecycle: `scripts/resolve_feedback.py` through the deployed Railway service

**Interfaces:**
- Consumes: all preceding behavior and the authenticated local feedback lifecycle.
- Produces: fresh verification evidence, pushed implementation on `origin/main`, and verified Completed status for `GPI-PM-FB-40`.

- [ ] **Step 1: Run all focused feature tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_schema_employee_notifications.py \
  tests/test_employee_notifications.py \
  tests/test_time_off_balances.py \
  tests/test_anniversary_pto_reminders.py \
  tests/test_anniversary_pto_warmer.py \
  tests/test_timeclock_notifications_routes.py \
  tests/test_timeclock_bilingual_render.py \
  tests/test_player_card.py \
  tests/test_employee_acknowledgement_history.py \
  tests/test_roster_filter.py \
  tests/test_staffing_static.py \
  tests/test_page_views.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run lint, template loading, and whitespace checks**

```bash
.venv/bin/ruff check src tests scripts
PYTHONPATH=src .venv/bin/python -c "from zira_dashboard.deps import templates; templates.get_template('timeclock_notifications.html'); templates.get_template('player_card.html'); templates.get_template('_employee_acknowledgement_history.html'); print('templates ok')"
git diff --check
```

Expected: Ruff passes, output includes `templates ok`, and `git diff --check`
prints nothing.

- [ ] **Step 3: Run the full suite**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: zero failures, with only documented skips.

- [ ] **Step 4: Verify Git delivery**

```bash
git status --short --branch
git log --oneline origin/main..HEAD
```

Expected: feature files are committed, `origin/main..HEAD` is empty, and only
pre-existing unrelated working-tree changes remain.

- [ ] **Step 5: Preview and apply the exact feedback completion**

After production has the implementation commits, run inside the deployed
service so the configured database is authoritative:

```bash
railway ssh python -m scripts.resolve_feedback \
  --source-id GPI-PM-FB-40 \
  --note "Employees are reminded before work anniversaries when they have unused PTO, must acknowledge the notice before punching in, and managers can review the history on each Staffing employee page." \
  --by "dale@gruberpallets.com"
```

Expected dry run: current `in_progress`, proposed `completed`, `applied:false`.
Then repeat with `--yes`; expected: `applied:true`, task update queued.

- [ ] **Step 6: Verify the same Odoo lifecycle row**

Wait for the normal feedback worker, then read the exact
`GPI-PM-FB-40` Improvement back through the authenticated lifecycle tooling.
Do not create, delete, merge, archive, or guess an Odoo row. Completion requires
the readback Status to be `Completed`; otherwise leave the task partially
complete and report the blocker.
