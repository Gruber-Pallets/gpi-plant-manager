# Employee Celebrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a private, one-time birthday or completed work-anniversary celebration to an employee at their next Timeclock kiosk use.

**Architecture:** The hourly Odoo roster sync will safely mirror only the birthday month/day and first-contract date into `people`. A feature-owned durable queue will pre-create the next 370 days of valid events, retain an event until its owner acknowledges it, and expose a local indexed read to the kiosk. A dedicated Timeclock screen presents one celebration at a time after existing operational notices.

**Tech Stack:** Python 3, FastAPI, Jinja2, PostgreSQL/psycopg2, Odoo XML-RPC, pytest, vanilla CSS.

## Global Constraints

- Target repository and app: `gpi-plant-manager` / GPI Plant Manager; source data is Odoo `hr.employee`.
- Private means only the signed-in employee can see their own event; add no TV, staffing, manager, coworker, email, or Slack announcement.
- Request-path reads must use local PostgreSQL only; an Odoo outage or unavailable Odoo field must never block Timeclock.
- Persist birthday month/day only, never the birth year; retain first-contract date only to calculate completed anniversary years.
- Queue only events dated from the local plant day through the next 370 calendar days. Already queued events stay pending until acknowledged; do not create an historical backlog at rollout.
- Birthdays and first-contract dates on February 29 are celebrated on February 28 in non-leap years.
- Existing time-off notification acknowledgement behavior remains isolated. Time-off notices stay ahead of celebrations.
- Use the existing English/Spanish-primary Timeclock translation behavior, large touch targets, and `prefers-reduced-motion` support. Add no dependencies.
- Every commit pushed to `main` must include a new, child-friendly `CHANGELOG.md` entry for the work in that commit.

---

## File structure

| Path | Responsibility |
| --- | --- |
| `src/zira_dashboard/_schema.py` | Idempotent people-field migration and durable celebration queue/indexes. |
| `src/zira_dashboard/employee_celebrations.py` | Date parsing, event generation/reconciliation, due lookup, and atomic acknowledgement. |
| `src/zira_dashboard/odoo_client.py` | Best-effort Odoo metadata discovery and celebration-date read. |
| `src/zira_dashboard/odoo_sync.py` | Safely persists Odoo celebration dates and reconciles future events after an otherwise successful roster sync. |
| `src/zira_dashboard/routes/timeclock.py` | Sign-in ordering and protected celebration GET/POST handlers. |
| `src/zira_dashboard/templates/timeclock_base.html` | Shared kiosk celebration styling and reduced-motion rule. |
| `src/zira_dashboard/templates/timeclock_celebration.html` | Private, one-event celebration interstitial. |
| `src/zira_dashboard/timeclock_i18n.py` | English/Spanish-primary celebration copy. |
| `tests/test_employee_celebrations.py` | Pure date, queue, due, and acknowledgement behavior. |
| `tests/test_schema_employee_celebrations.py` | Static schema contract. |
| `tests/test_odoo_client.py` and `tests/test_odoo_sync.py` | Odoo field capability and last-known-safe-sync behavior. |
| `tests/test_timeclock_celebrations_routes.py` | Sign-in order, token protection, and one-time route behavior. |
| `tests/test_timeclock_bilingual_render.py` | Spanish-primary celebration rendering. |
| `CHANGELOG.md` | Child-friendly release note for each pushed implementation commit. |

## Interfaces

| Module | Name | Signature / fields |
| --- | --- | --- |
| `employee_celebrations` | `CelebrationEvent` | `person_odoo_id: int`, `kind: Literal["birthday", "work_anniversary"]`, `event_day: date`, `completed_years: int | None` |
| `employee_celebrations` | `Celebration` | `id: int`, `person_odoo_id: int`, `kind: Literal["birthday", "work_anniversary"]`, `event_day: date`, `completed_years: int | None` |
| `employee_celebrations` | `normalize_birthday` | `(raw: object) -> tuple[int, int] | None` |
| `employee_celebrations` | `normalize_first_contract_date` | `(raw: object) -> date | None` |
| `employee_celebrations` | `event_day_for` | `(year: int, month: int, day: int) -> date` |
| `employee_celebrations` | `future_events_for_person` | `(person_odoo_id: int, birthday: tuple[int, int] | None, first_contract_date: date | None, today: date, end_day: date) -> tuple[CelebrationEvent, ...]` |
| `employee_celebrations` | `reconcile_future` | `(today: date | None = None) -> None` |
| `employee_celebrations` | `next_due` | `(person_odoo_id: int, today: date | None = None) -> Celebration | None` |
| `employee_celebrations` | `acknowledge` | `(celebration_id: int, person_odoo_id: int) -> bool` |
| `odoo_client` | `EmployeeCelebrationSource` | `birthday_available: bool`, `first_contract_date_available: bool`, `rows_by_employee_id: dict[int, dict[str, object]]` |
| `odoo_client` | `fetch_employee_celebration_dates` | `() -> EmployeeCelebrationSource` |

### Task 1: Add the local celebration queue and deterministic date rules

**Files:**
- Modify: `src/zira_dashboard/_schema.py:67-89` and after the existing employee-notification schema near `:1122`
- Create: `src/zira_dashboard/employee_celebrations.py`
- Create: `tests/test_schema_employee_celebrations.py`
- Create: `tests/test_employee_celebrations.py`
- Modify: `CHANGELOG.md`

**Consumes:** `db.query`, `db.execute`, `plant_day.today`, and the existing local `people.odoo_id` identity.

**Produces:** The `CelebrationEvent` and `Celebration` models plus `normalize_birthday`, `normalize_first_contract_date`, `event_day_for`, `future_events_for_person`, `reconcile_future`, `next_due`, and `acknowledge` for the sync and Timeclock tasks.

- [ ] **Step 1: Write the failing static-schema and pure-date tests.**

```python
from datetime import date
from zira_dashboard import employee_celebrations as celebrations
from zira_dashboard._schema import SCHEMA_DDL


def test_schema_has_private_deduplicated_celebration_queue():
    assert "CREATE TABLE IF NOT EXISTS employee_celebrations" in SCHEMA_DDL
    assert "UNIQUE (person_odoo_id, kind, event_day)" in SCHEMA_DDL
    assert "employee_celebrations_unack" in SCHEMA_DDL
    assert "WHERE acknowledged_at IS NULL" in SCHEMA_DDL


def test_normalize_birthday_discards_year_and_rejects_bad_dates():
    assert celebrations.normalize_birthday("1991-07-04") == (7, 4)
    assert celebrations.normalize_birthday("2026-02-29") is None
    assert celebrations.normalize_birthday(False) is None


def test_event_day_uses_feb_28_for_non_leap_years():
    assert celebrations.event_day_for(2027, 2, 29) == date(2027, 2, 28)
```

- [ ] **Step 2: Run the new tests to verify they fail.**

Run: `pytest tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py -q`

Expected: FAIL during collection because `employee_celebrations` and its schema do not exist.

- [ ] **Step 3: Add idempotent schema migrations and the feature-owned module.**

Add the local columns without a birth-year column:

```sql
ALTER TABLE people ADD COLUMN IF NOT EXISTS birthday_month SMALLINT;
ALTER TABLE people ADD COLUMN IF NOT EXISTS birthday_day SMALLINT;
ALTER TABLE people ADD COLUMN IF NOT EXISTS first_contract_date DATE;

CREATE TABLE IF NOT EXISTS employee_celebrations (
  id BIGSERIAL PRIMARY KEY,
  person_odoo_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('birthday', 'work_anniversary')),
  event_day DATE NOT NULL,
  completed_years SMALLINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  acknowledged_at TIMESTAMPTZ,
  UNIQUE (person_odoo_id, kind, event_day),
  CHECK ((kind = 'birthday' AND completed_years IS NULL)
      OR (kind = 'work_anniversary' AND completed_years > 0))
);
CREATE INDEX IF NOT EXISTS employee_celebrations_unack
  ON employee_celebrations (person_odoo_id, event_day)
  WHERE acknowledged_at IS NULL;
```

Implement parsing and the leap-day rule without accepting invalid Odoo data:

```python
def normalize_birthday(raw: object) -> tuple[int, int] | None:
    try:
        value = date.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return (value.month, value.day) if value is not None else None


def event_day_for(year: int, month: int, day: int) -> date:
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)
```

`reconcile_future()` must read active profiles from `people`, generate birthday and anniversary events from `today` through `today + timedelta(days=370)`, and insert with `ON CONFLICT (person_odoo_id, kind, event_day) DO NOTHING`. For every active person/kind, remove only rows with `event_day > today` and `acknowledged_at IS NULL` that are no longer in that person's expected future set. Remove future unacknowledged rows for inactive people too. Never delete an event that is due or acknowledged.

Use these local-only queries for delivery:

```python
def next_due(person_odoo_id: int, today: date | None = None) -> Celebration | None:
    rows = db.query(
        "SELECT id, person_odoo_id, kind, event_day, completed_years "
        "FROM employee_celebrations "
        "WHERE person_odoo_id = %s AND acknowledged_at IS NULL "
        "AND event_day <= %s ORDER BY event_day, id LIMIT 1",
        (person_odoo_id, today or plant_today()),
    )
    return _to_celebration(rows[0]) if rows else None


def acknowledge(celebration_id: int, person_odoo_id: int) -> bool:
    rows = db.query(
        "UPDATE employee_celebrations SET acknowledged_at = now() "
        "WHERE id = %s AND person_odoo_id = %s AND acknowledged_at IS NULL "
        "RETURNING id",
        (celebration_id, person_odoo_id),
    )
    return bool(rows)
```

- [ ] **Step 4: Add queue-behavior tests and make them pass.**

Cover these exact queue and delivery cases:

```python
def test_future_events_start_today_without_backfilling_old_events():
    events = celebrations.future_events_for_person(
        7, (7, 4), date(2021, 8, 20), date(2026, 8, 27), date(2027, 9, 1)
    )
    assert [event.event_day for event in events] == [date(2027, 7, 4), date(2027, 8, 20)]


def test_future_events_keep_first_completed_anniversary_only_after_year_one():
    events = celebrations.future_events_for_person(
        7, None, date(2026, 9, 1), date(2026, 8, 27), date(2027, 9, 1)
    )
    assert [(event.kind, event.completed_years) for event in events] == [("work_anniversary", 1)]


def test_next_due_returns_only_the_oldest_row_for_the_signed_in_person(monkeypatch):
    monkeypatch.setattr(celebrations.db, "query", lambda _sql, _params: [{
        "id": 2, "person_odoo_id": 7, "kind": "birthday",
        "event_day": date(2026, 7, 4), "completed_years": None,
    }])
    assert celebrations.next_due(7, date(2026, 8, 27)).id == 2


def test_acknowledge_uses_event_and_owner_in_the_update(monkeypatch):
    seen = []
    monkeypatch.setattr(celebrations.db, "query", lambda sql, params: seen.append((sql, params)) or [{"id": 2}])
    assert celebrations.acknowledge(2, 7) is True
    assert seen[0][1] == (2, 7)
```

Use the integration test database for reconciliation assertions: seed one past unacknowledged row and one future unacknowledged row, change the future source date, call `reconcile_future(date(2026, 8, 27))`, then assert the past row remains while the stale future row is removed.

Run: `pytest tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py -q`

Expected: PASS.

- [ ] **Step 5: Add the required user-facing patch note and commit.**

Add a new topmost `2026-08-27` entry explaining in child-friendly language that Plant Manager is preparing private birthday and work-anniversary celebrations, and that nothing has appeared at the clock yet.

Run: `git add src/zira_dashboard/_schema.py src/zira_dashboard/employee_celebrations.py tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py CHANGELOG.md && git commit -m "feat: add employee celebration queue" && git push origin main`

Expected: the focused test command is green and only this task's files are committed.

### Task 2: Safely import Odoo dates and reconcile the queue after roster sync

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py:276-287`
- Modify: `src/zira_dashboard/odoo_sync.py:316-510`
- Modify: `tests/test_odoo_client.py`
- Modify: `tests/test_odoo_sync.py`
- Modify: `CHANGELOG.md`

**Consumes:** Task 1's normalizers and `reconcile_future`; existing Odoo `execute()` and roster-sync validation.

**Produces:** `EmployeeCelebrationSource`, `fetch_employee_celebration_dates()`, and an hourly-safe local mirror refresh.

- [ ] **Step 1: Write failing Odoo-client and sync-isolation tests.**

```python
from datetime import date

from zira_dashboard import db, odoo_client, odoo_sync


def test_fetch_employee_celebration_dates_reads_only_available_fields(monkeypatch):
    calls = []
    def fake_execute(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if (model, method) == ("hr.employee", "fields_get"):
            return {"birthday": {"type": "date"}}
        if (model, method) == ("hr.employee", "search_read"):
            return [{"id": 7, "birthday": "1991-07-04"}]
        raise AssertionError((model, method))
    monkeypatch.setattr(odoo_client, "execute", fake_execute)
    source = odoo_client.fetch_employee_celebration_dates()
    assert source.birthday_available is True
    assert source.first_contract_date_available is False
    assert source.rows_by_employee_id[7]["birthday"] == "1991-07-04"
    assert calls[1][3]["fields"] == ["id", "birthday"]


def test_celebration_date_read_failure_preserves_last_safe_local_dates(monkeypatch):
    _stub_client(monkeypatch, [{"id": 99002, "name": "TestBob", "active": True}], {}, [], {})
    db.execute("INSERT INTO people (odoo_id, name, active, birthday_month, birthday_day, first_contract_date) VALUES (99002, 'TestBob', TRUE, 7, 4, '2021-08-20')")
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employee_celebration_dates",
                        lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    assert odoo_sync.sync(force=True).ok is True
    row = db.query("SELECT birthday_month, birthday_day, first_contract_date FROM people WHERE odoo_id = 99002")[0]
    assert row == {"birthday_month": 7, "birthday_day": 4, "first_contract_date": date(2021, 8, 20)}


def test_sync_clears_only_a_confirmed_unavailable_field_and_reconciles(monkeypatch):
    _stub_client(monkeypatch, [{"id": 99002, "name": "TestBob", "active": True}], {}, [], {})
    db.execute("INSERT INTO people (odoo_id, name, active, birthday_month, birthday_day) VALUES (99002, 'TestBob', TRUE, 7, 4)")
    source = odoo_client.EmployeeCelebrationSource(False, True, {99002: {"first_contract_date": "2021-08-20"}})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employee_celebration_dates", lambda: source)
    assert odoo_sync.sync(force=True).ok is True
    row = db.query("SELECT birthday_month, birthday_day FROM people WHERE odoo_id = 99002")[0]
    assert row == {"birthday_month": None, "birthday_day": None}
```

- [ ] **Step 2: Run the tests to verify they fail.**

Run: `pytest tests/test_odoo_client.py tests/test_odoo_sync.py -q`

Expected: FAIL because the capability read and celebration-date sync path do not exist.

- [ ] **Step 3: Implement a capability-aware, best-effort client read.**

Add this data shape next to the Odoo client helpers:

```python
@dataclass(frozen=True)
class EmployeeCelebrationSource:
    birthday_available: bool
    first_contract_date_available: bool
    rows_by_employee_id: dict[int, dict[str, object]]


def fetch_employee_celebration_dates() -> EmployeeCelebrationSource:
    metadata = execute("hr.employee", "fields_get", [], attributes=["type"])
    available = {name for name in ("birthday", "first_contract_date") if name in metadata}
    if not available:
        return EmployeeCelebrationSource(False, False, {})
    rows = execute(
        "hr.employee", "search_read", [("active", "=", True)],
        fields=["id", *sorted(available)],
    )
    return EmployeeCelebrationSource(
        birthday_available="birthday" in available,
        first_contract_date_available="first_contract_date" in available,
        rows_by_employee_id={int(row["id"]): row for row in rows if isinstance(row.get("id"), int)},
    )
```

In `odoo_sync.sync()`, invoke this read in its own `try` block only after the main roster payload has passed validation. On exception, record a generic log message without values, set the source to `None`, and complete the existing roster sync unchanged. When a source is returned, update `birthday_month`, `birthday_day`, and `first_contract_date` from Task 1's normalizers. A confirmed unavailable field writes `NULL`; an RPC failure leaves prior values untouched. After the successful people transaction commits and only when a source was returned, call `employee_celebrations.reconcile_future(plant_today())` in a guarded block that cannot convert a good roster sync into a failed one.

- [ ] **Step 4: Make the Odoo and sync tests pass.**

Run: `pytest tests/test_odoo_client.py tests/test_odoo_sync.py tests/test_employee_celebrations.py -q`

Expected: PASS, including preservation of last-safe values when the optional read fails.

- [ ] **Step 5: Add the required patch note and commit.**

Add a new topmost `2026-08-27` entry explaining that birthday and work-anniversary dates now stay private and are checked safely, while the celebration screen is still not live.

Run: `git add src/zira_dashboard/odoo_client.py src/zira_dashboard/odoo_sync.py tests/test_odoo_client.py tests/test_odoo_sync.py CHANGELOG.md && git commit -m "feat: sync employee celebration dates" && git push origin main`

Expected: the targeted test command is green and the commit does not include credentials or unrelated worktree changes.

### Task 3: Insert the private celebration into the protected Timeclock flow

**Files:**
- Modify: `src/zira_dashboard/routes/timeclock.py:549-705`
- Create: `tests/test_timeclock_celebrations_routes.py`
- Modify: `tests/test_timeclock_notifications_routes.py`
- Modify: `CHANGELOG.md`

**Consumes:** Task 1's `next_due()` and `acknowledge()` plus existing `_verify_token`, `_person_by_id`, and `employee_notifications` routing.

**Produces:** Protected GET/POST celebration routes and correct sign-in priority.

- [ ] **Step 1: Write failing route tests.**

```python
from datetime import date

from zira_dashboard import employee_celebrations, employee_notifications
from zira_dashboard.routes import timeclock


PERSON = {"id": 1, "name": "Test Person", "odoo_id": 5, "wage_type": "hourly"}


def test_start_keeps_time_off_notice_before_celebration(monkeypatch):
    celebration = employee_celebrations.Celebration(11, 5, "birthday", date(2026, 8, 27), None)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_notifications, "has_unacknowledged", lambda _id: True)
    monkeypatch.setattr(employee_celebrations, "next_due", lambda *_: celebration)
    response = client.get("/timeclock/start/1", follow_redirects=False)
    assert "/timeclock/notifications/" in response.headers["location"]


def test_start_routes_due_celebration_before_dashboard(monkeypatch):
    celebration = employee_celebrations.Celebration(11, 5, "birthday", date(2026, 8, 27), None)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_notifications, "has_unacknowledged", lambda _id: False)
    monkeypatch.setattr(employee_celebrations, "next_due", lambda *_: celebration)
    response = client.get("/timeclock/start/1", follow_redirects=False)
    assert "/timeclock/celebration/" in response.headers["location"]


def test_acknowledging_celebration_restarts_priority_flow(monkeypatch):
    token = timeclock._mint_token(1)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_celebrations, "acknowledge", lambda event_id, oid: True)
    response = client.post(f"/timeclock/celebration/ack/{token}", data={"celebration_id": 11}, follow_redirects=False)
    assert response.headers["location"] == "/timeclock/start/1"
```

Add four named regression tests: `test_celebration_screen_rejects_bad_token`, `test_celebration_ack_rejects_another_persons_event`, `test_celebration_screen_restarts_when_no_due_event_remains`, and `test_notifications_ack_restarts_sign_in_priority_flow`. Their assertions must respectively prove a 303 to `/timeclock`, `acknowledge(event_id, other_odoo_id)` is false before redirecting, an empty due lookup returns `/timeclock/start/1`, and a cleared time-off card redirects to `/timeclock/start/1`.

- [ ] **Step 2: Run route tests to verify they fail.**

Run: `pytest tests/test_timeclock_celebrations_routes.py tests/test_timeclock_notifications_routes.py -q`

Expected: FAIL because celebration routes and the new priority hop do not exist.

- [ ] **Step 3: Implement the routing and acknowledgement boundary.**

Keep the existing time-off check first in `kiosk_start`; place the local celebration lookup immediately after it and before salaried/Saturday routing:

```python
if p.get("odoo_id"):
    due = employee_celebrations.next_due(p["odoo_id"], plant_today())
    if due is not None:
        return RedirectResponse(
            url=f"/timeclock/celebration/{_mint_token(person_id)}", status_code=303
        )
```

Add `GET /timeclock/celebration/{token}` to validate the token and person, reload `next_due`, and redirect to `/timeclock/start/{person_id}` if it raced away. Otherwise render `timeclock_celebration.html` with the person, fresh token, and one `Celebration`.

Add `POST /timeclock/celebration/ack/{token}` with `celebration_id: int = Form(...)`. Validate token/person, call the person-scoped `acknowledge`, and always 303 to `/timeclock/start/{person_id}`. This restart shows the next pending celebration, then preserves the existing salaried and Saturday decisions. Change `timeclock_notifications_ack` to use the same start URL after it clears time-off notices, so notices cannot bypass a due celebration.

- [ ] **Step 4: Make the route tests pass.**

Run: `pytest tests/test_timeclock_celebrations_routes.py tests/test_timeclock_notifications_routes.py tests/test_timeclock_saturday_recruiting.py -q`

Expected: PASS; the existing notification-before-Saturday regression remains green.

- [ ] **Step 5: Add the required patch note and commit.**

Add a new topmost `2026-08-27` note saying that the time clock now keeps private special-day messages separate from important work updates, without naming internal routes or data systems.

Run: `git add src/zira_dashboard/routes/timeclock.py tests/test_timeclock_celebrations_routes.py tests/test_timeclock_notifications_routes.py CHANGELOG.md && git commit -m "feat: show employee celebrations at sign-in" && git push origin main`

Expected: the targeted route suite is green and only Task 3 files are committed.

### Task 4: Build the celebratory screen, translations, and accessibility coverage

**Files:**
- Modify: `src/zira_dashboard/templates/timeclock_base.html:1-260`
- Create: `src/zira_dashboard/templates/timeclock_celebration.html`
- Modify: `src/zira_dashboard/timeclock_i18n.py:31-220`
- Modify: `tests/test_timeclock_bilingual_render.py`
- Create: `tests/test_timeclock_celebration_static.py`
- Modify: `CHANGELOG.md`

**Consumes:** Task 3's template context `{person, token, celebration}` and existing `t()` bilingual renderer.

**Produces:** Touch-friendly, Spanish-primary, reduced-motion-safe birthday and anniversary presentation.

- [ ] **Step 1: Write the failing rendering and static accessibility tests.**

```python
def test_celebration_template_renders_birthday_and_hidden_event_id():
    html = _env().get_template("timeclock_celebration.html").render(
        person={"name": "Maria Garcia", "spanish_level": 0}, token="t",
        celebration={"id": 8, "kind": "birthday", "completed_years": None},
        timeclock_language="en",
    )
    assert "Happy Birthday, Maria!" in html
    assert 'name="celebration_id" value="8"' in html


def test_celebration_template_is_spanish_primary_for_level_three():
    spanish_context = {
        "person": {"name": "Maria Garcia", "spanish_level": 3}, "token": "t",
        "celebration": {"id": 8, "kind": "birthday", "completed_years": None},
        "timeclock_language": "es_primary",
    }
    html = _env().get_template("timeclock_celebration.html").render(**spanish_context)
    assert html.index("¡Feliz cumpleaños") < html.index("Happy Birthday")


def test_celebration_styles_disable_confetti_for_reduced_motion():
    source = Path("src/zira_dashboard/templates/timeclock_base.html").read_text()
    assert "@media (prefers-reduced-motion: reduce)" in source
    assert ".celebration-confetti" in source
```

- [ ] **Step 2: Run rendering tests to verify they fail.**

Run: `pytest tests/test_timeclock_bilingual_render.py tests/test_timeclock_celebration_static.py -q`

Expected: FAIL because the celebration template, copy, and styles do not exist.

- [ ] **Step 3: Add the template, kiosk CSS, and translations.**

Add these exact translation keys to `TRANSLATIONS`:

```python
"Happy Birthday, {name}! 🎉": "¡Feliz cumpleaños, {name}! 🎉",
"Happy {years}-Year Work Anniversary, {name}! 🎉": "¡Feliz aniversario de {years} años de trabajo, {name}! 🎉",
"We're glad you're on the team.": "Nos alegra que seas parte del equipo.",
"Continue": "Continuar",
```

Render a single event, not a list, using the existing escaping translation helper:

```jinja2
{% if celebration.kind == "birthday" %}
  <h1>{{ t("Happy Birthday, {name}! 🎉", name=person.name.split(" ")[0]) }}</h1>
{% else %}
  <h1>{{ t("Happy {years}-Year Work Anniversary, {name}! 🎉",
            years=celebration.completed_years, name=person.name.split(" ")[0]) }}</h1>
{% endif %}
<p>{{ t("We're glad you're on the team.") }}</p>
<form method="post" action="/timeclock/celebration/ack/{{ token }}">
  <input type="hidden" name="celebration_id" value="{{ celebration.id }}">
  <button type="submit" class="k-btn celebration-continue">{{ t("Continue") }}</button>
</form>
```

Use CSS-only decorative `.celebration-confetti` spans, `aria-hidden="true"`, a large centered `.celebration-card`, and an explicit reduced-motion rule that removes animation and transforms. Keep the normal kiosk page scroll/idle behavior and do not add a JavaScript or image dependency.

- [ ] **Step 4: Make rendering and static tests pass.**

Run: `pytest tests/test_timeclock_i18n.py tests/test_timeclock_bilingual_render.py tests/test_timeclock_celebration_static.py tests/test_timeclock_celebrations_routes.py -q`

Expected: PASS; English remains single-language and Spanish-primary output puts Spanish first.

- [ ] **Step 5: Add the required release note and commit.**

Add a new topmost `2026-08-27` `#### Features` entry: “Workers can now see a private birthday or work-anniversary celebration when they use the time clock. If they are away, it waits until they come back.”

Run: `git add src/zira_dashboard/templates/timeclock_base.html src/zira_dashboard/templates/timeclock_celebration.html src/zira_dashboard/timeclock_i18n.py tests/test_timeclock_bilingual_render.py tests/test_timeclock_celebration_static.py CHANGELOG.md && git commit -m "feat: celebrate employee milestones" && git push origin main`

Expected: the template suite is green and the What's New panel describes the completed employee benefit in plain language.

### Task 5: Run the full regression suite and verify the release boundary

**Files:**
- Modify only if verification exposes a real scoped defect: files listed in Tasks 1–4
- Modify: `CHANGELOG.md` only if a final user-visible correction is made

**Consumes:** All completed tasks.

**Produces:** Verified feature with no unintended Timeclock, roster-sync, or schema regression.

- [ ] **Step 1: Run the full celebration-focused regression suite.**

Run: `pytest tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py tests/test_odoo_client.py tests/test_odoo_sync.py tests/test_timeclock_celebrations_routes.py tests/test_timeclock_notifications_routes.py tests/test_timeclock_saturday_recruiting.py tests/test_timeclock_i18n.py tests/test_timeclock_bilingual_render.py tests/test_timeclock_celebration_static.py -q`

Expected: PASS.

- [ ] **Step 2: Run the complete automated test suite.**

Run: `pytest -q`

Expected: PASS with no pre-existing failure newly introduced by this feature.

- [ ] **Step 3: Inspect the final diff and schema safety.**

Run: `git diff 09b12c41..HEAD --check && git status --short`

Expected: no whitespace errors; no unrelated files staged or committed; all migrations remain idempotent `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` changes.

- [ ] **Step 4: If any scoped correction was needed, test, document, commit, and push it.**

Run the smallest failing test first, then the focused suite above. If the correction changes what a worker sees, add a new child-friendly `CHANGELOG.md` entry before:

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/employee_celebrations.py src/zira_dashboard/odoo_client.py src/zira_dashboard/odoo_sync.py src/zira_dashboard/routes/timeclock.py src/zira_dashboard/templates/timeclock_base.html src/zira_dashboard/templates/timeclock_celebration.html src/zira_dashboard/timeclock_i18n.py tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py tests/test_odoo_client.py tests/test_odoo_sync.py tests/test_timeclock_celebrations_routes.py tests/test_timeclock_notifications_routes.py tests/test_timeclock_bilingual_render.py tests/test_timeclock_celebration_static.py CHANGELOG.md
git commit -m "fix: harden employee celebrations"
git push origin main
```

Expected: `origin/main` contains every implementation commit and every user-visible patch note; unrelated worktree changes remain untouched.

## Plan self-review

- **Spec coverage:** Task 1 implements private durable storage, date rules, a 370-day no-backlog window, delayed return, deduplication, and leap-day behavior. Task 2 covers safe Odoo field capability handling, data minimization, and last-safe behavior. Task 3 covers protected sign-in ordering and acknowledgement isolation. Task 4 covers the private bilingual animated screen and reduced-motion support. Task 5 covers focused and full regression validation.
- **Placeholder scan:** No future-work markers, unspecified tests, or generic error-handling directives remain; each task has named interfaces, code, commands, and expected results.
- **Type consistency:** `Celebration`, `EmployeeCelebrationSource`, `reconcile_future`, `next_due`, and `acknowledge` are introduced before their consuming tasks. Event kinds use the same `birthday` and `work_anniversary` values across schema, module, route, and template.
