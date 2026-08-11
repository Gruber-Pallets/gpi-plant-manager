# Odoo Work-Center Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Link every kiosk-created Odoo attendance interval to the correct Manufacturing Work Center and continue the employee's exact pre-break work center after Auto-Lunch.

**Architecture:** Store an explicit mapping from each app work center to an active `mrp.workcenter` ID and label in Postgres. `odoo_client` resolves that mapping for every attendance write and passes IDs to the Many2one field; Odoo attendance readers reverse-resolve IDs to app names before feeding the live cache, timeclock state, dashboards, and Auto-Lunch.

**Tech Stack:** FastAPI, Jinja, PostgreSQL/psycopg2, Odoo XML-RPC, pytest, python-dotenv.

## Global Constraints

- The Odoo attendance field is `x_studio_work_center`, an optional Many2one to `mrp.workcenter`; write an integer ID, never a text label.
- `ODOO_KIOSK_WC_FIELD=x_studio_work_center` must be set in Railway only with the code that writes Many2one IDs.
- Use an explicit validated mapping. Never fuzzy-match names or infer a work center from Staffing.
- Keep the existing Auto-Lunch state machine and its local-punch fallback unchanged; its persisted `auto_lunch_runs.wc_name` remains an app work-center name.
- When a mapping is missing or stale, create the optional Odoo work-center field blank so timekeeping continues; the existing Missing Work Center workflow must retain the row for correction.
- Do not reuse `work_centers.odoo_id`; it is reserved for the app's future general Odoo sync. Add purpose-specific mapping columns.
- Preserve the whole-form Settings autosave behavior: an unavailable Odoo catalog must never clear a saved mapping.
- Add a child-friendly `CHANGELOG.md` entry for the production push.

---

## File structure

| File | Responsibility |
| --- | --- |
| `src/zira_dashboard/_schema.py` | Add idempotent persistence and a uniqueness guard for explicit Odoo work-center mappings. |
| `src/zira_dashboard/work_centers_store.py` | Read, write, cache, and reverse-resolve the mapping strictly from local Postgres state. |
| `src/zira_dashboard/odoo_client.py` | Fetch active Odoo Manufacturing Work Centers and translate app names into Many2one IDs in attendance writes. |
| `src/zira_dashboard/_odoo_attendance.py` | Normalize Odoo Many2one values back into app work-center names without importing database code. |
| `src/zira_dashboard/routes/settings.py` | Load active Odoo choices, validate Settings posts before any mutation, and pass map state to the template. |
| `src/zira_dashboard/settings_context.py` | Expose each saved mapping in the Work Centers row shape. |
| `src/zira_dashboard/templates/settings.html` | Render a safe per-work-center Odoo Manufacturing Work Center selector and mapping-health messages. |
| `src/zira_dashboard/routes/missing_wc.py` | Do not suppress an alert if an attempted manual assignment has no valid mapping. |
| `tests/test_work_centers_store_odoo_mapping.py` | Postgres-backed persistence, uniqueness, and reverse-lookup tests. |
| `tests/test_settings_odoo_work_centers.py` | Settings rendering and active-catalog validation tests. |
| `tests/test_odoo_open_attendance.py` | Pure Odoo write/read contract tests for Many2one IDs and reverse mapping. |
| `tests/test_auto_lunch_worker.py` | Regression test that the automatic post-lunch log record keeps the exact pre-break app work center. |
| `tests/test_missing_wc_routes.py` | Protect the correction alert when the selected mapping is unavailable. |
| `.env.example`, `docs/odoo-setup.md`, `CHANGELOG.md` | Explain the required field configuration, activation order, and user-visible behavior. |

## Interfaces established by this plan

```python
# src/zira_dashboard/work_centers_store.py
def odoo_work_center_id_for(app_wc_name: str | None) -> int | None: ...
def app_work_center_name_for_odoo_id(odoo_wc_id: int | None) -> str | None: ...
def set_odoo_work_center(loc: Location, *, odoo_id: int | None,
                         odoo_name: str | None) -> None: ...
def replace_odoo_work_center_mappings(updates: Mapping[str, Mapping[str, object]]) -> None: ...

# src/zira_dashboard/odoo_client.py
def fetch_manufacturing_work_centers(*, force: bool = False) -> list[dict]: ...
def set_attendance_wc(attendance_id: int, wc_name: str | None) -> bool: ...

# src/zira_dashboard/_odoo_attendance.py
def fetch_open_attendances(execute_fn, wc_field, department_field,
                           app_wc_name_for_odoo_id) -> list[dict]: ...
def fetch_attendance_intervals_for_day(execute_fn, day, wc_field,
                                      app_wc_name_for_odoo_id) -> list[dict]: ...
```

`set_attendance_wc` returns `True` only when it wrote the configured work-center field. Callers that merely adopt an existing Odoo attendance may still mark their local punch synced on `False`; the blank Odoo attendance then remains eligible for Missing Work Center. The manual Missing Work Center route instead returns a conflict and leaves the alert unresolved on `False`.

### Task 1: Persist and reverse-resolve the explicit mapping

**Files:**
- Modify: `src/zira_dashboard/_schema.py:111-128`
- Modify: `src/zira_dashboard/work_centers_store.py:129-236, 445-548`
- Create: `tests/test_work_centers_store_odoo_mapping.py`

**Consumes:** Existing `work_centers` rows keyed by static app `Location.name` and the store's 60-second cache invalidation hook.

**Produces:** `odoo_work_center_id_for`, `app_work_center_name_for_odoo_id`, and atomic mapping writers, which all later tasks use instead of display-name matching.

- [ ] **Step 1: Write the failing persistence and lookup tests**

  Create `tests/test_work_centers_store_odoo_mapping.py` with the database gate and cleanup fixture below. Use `Repair 1` because it is a real configured location, and choose test IDs outside the live Odoo range.

  ```python
  import os

  import pytest

  from zira_dashboard import db, staffing, work_centers_store

  pytestmark = pytest.mark.skipif(
      not os.environ.get("DATABASE_URL"), reason="needs Postgres"
  )

  LOC = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 1")
  REPAIR_2 = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 2")

  @pytest.fixture(autouse=True)
  def mapping_cleanup():
      db.execute(
          "UPDATE work_centers SET odoo_work_center_id = NULL, "
          "odoo_work_center_name = NULL WHERE name IN (%s, %s)",
          (LOC.name, REPAIR_2.name),
      )
      work_centers_store._invalidate_caches()
      yield
      db.execute(
          "UPDATE work_centers SET odoo_work_center_id = NULL, "
          "odoo_work_center_name = NULL WHERE name IN (%s, %s)",
          (LOC.name, REPAIR_2.name),
      )
      work_centers_store._invalidate_caches()

  def test_mapping_round_trips_in_both_directions():
      work_centers_store.set_odoo_work_center(
          LOC, odoo_id=987_654, odoo_name="Repair #1"
      )
      assert work_centers_store.odoo_work_center_id_for("Repair 1") == 987_654
      assert work_centers_store.app_work_center_name_for_odoo_id(987_654) == "Repair 1"

  def test_mapping_can_be_cleared():
      work_centers_store.set_odoo_work_center(
          LOC, odoo_id=987_654, odoo_name="Repair #1"
      )
      work_centers_store.set_odoo_work_center(LOC, odoo_id=None, odoo_name=None)
      assert work_centers_store.odoo_work_center_id_for("Repair 1") is None
      assert work_centers_store.app_work_center_name_for_odoo_id(987_654) is None

  def test_batch_replace_allows_two_work_centers_to_swap_targets():
      work_centers_store.set_odoo_work_center(LOC, odoo_id=987_654, odoo_name="Repair #1")
      work_centers_store.set_odoo_work_center(REPAIR_2, odoo_id=987_655, odoo_name="Repair #2")
      work_centers_store.replace_odoo_work_center_mappings({
          "Repair 1": {"odoo_id": 987_655, "odoo_name": "Repair #2"},
          "Repair 2": {"odoo_id": 987_654, "odoo_name": "Repair #1"},
      })
      assert work_centers_store.odoo_work_center_id_for("Repair 1") == 987_655
      assert work_centers_store.odoo_work_center_id_for("Repair 2") == 987_654
  ```

- [ ] **Step 2: Run the new test file to verify the mapping APIs do not exist yet**

  Run: `uv run pytest tests/test_work_centers_store_odoo_mapping.py -q`

  Expected: collection fails because `set_odoo_work_center` and the two lookup functions do not exist. If `DATABASE_URL` is not configured locally, confirm pytest reports the module as skipped and run the same command in CI/Railway before merging.

- [ ] **Step 3: Add idempotent schema and local mapping APIs**

  In `SCHEMA_DDL`, directly after the `work_centers` table, add nullable mapping fields and a partial unique index. The index prevents two kiosk work centers from silently pointing to the same Odoo work center while allowing unmapped rows.

  ```sql
  ALTER TABLE work_centers
    ADD COLUMN IF NOT EXISTS odoo_work_center_id INTEGER;
  ALTER TABLE work_centers
    ADD COLUMN IF NOT EXISTS odoo_work_center_name TEXT;
  CREATE UNIQUE INDEX IF NOT EXISTS work_centers_odoo_work_center_id_unique
    ON work_centers (odoo_work_center_id)
    WHERE odoo_work_center_id IS NOT NULL;
  ```

  Add a dedicated cached two-way map in `work_centers_store.py`; clear it inside the existing `_invalidate_caches()` function. The lookup API must not make an Odoo call.

  ```python
  _ODOO_WORK_CENTER_MAP_CACHE = TTLCache(ttl_seconds=60.0, max_entries=1)

  def _odoo_work_center_maps() -> tuple[dict[str, int], dict[int, str]]:
      def build() -> tuple[dict[str, int], dict[int, str]]:
          from . import db
          rows = db.query(
              "SELECT name, odoo_work_center_id FROM work_centers "
              "WHERE odoo_work_center_id IS NOT NULL"
          )
          forward = {r["name"]: int(r["odoo_work_center_id"]) for r in rows}
          reverse = {odoo_id: name for name, odoo_id in forward.items()}
          return forward, reverse
      return _ODOO_WORK_CENTER_MAP_CACHE.get_or_compute("maps", build)

  def odoo_work_center_id_for(app_wc_name: str | None) -> int | None:
      if not app_wc_name:
          return None
      return _odoo_work_center_maps()[0].get(app_wc_name)

  def app_work_center_name_for_odoo_id(odoo_wc_id: int | None) -> str | None:
      if odoo_wc_id is None:
          return None
      return _odoo_work_center_maps()[1].get(int(odoo_wc_id))

  def set_odoo_work_center(loc: Location, *, odoo_id: int | None,
                           odoo_name: str | None) -> None:
      from . import db
      db.execute(
          "INSERT INTO work_centers (name, category, cell, meter_id, min_ops, max_ops) "
          "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
          (loc.name, loc.skill, loc.bay, loc.meter_id, loc.min_ops, loc.max_ops),
      )
      db.execute(
          "UPDATE work_centers SET odoo_work_center_id = %s, "
          "odoo_work_center_name = %s WHERE name = %s",
          (odoo_id, odoo_name, loc.name),
      )
      _invalidate_caches()
  ```

  Add `replace_odoo_work_center_mappings(updates)` for the Settings whole-form
  save. It must use one cursor transaction, clear both mapping columns for all
  posted location names first, then set every selected pair. That ordering
  makes a valid swap of two Odoo targets succeed while the partial unique
  index still prevents duplicate mappings at rest.

  ```python
  def replace_odoo_work_center_mappings(updates: Mapping[str, Mapping[str, object]]) -> None:
      if not updates:
          return
      from . import db
      names = list(updates)
      with db.cursor() as cur:
          cur.execute(
              "UPDATE work_centers SET odoo_work_center_id = NULL, "
              "odoo_work_center_name = NULL WHERE name = ANY(%s)",
              (names,),
          )
          for name, mapping in updates.items():
              odoo_id = mapping["odoo_id"]
              if odoo_id is None:
                  continue
              cur.execute(
                  "UPDATE work_centers SET odoo_work_center_id = %s, "
                  "odoo_work_center_name = %s WHERE name = %s",
                  (int(odoo_id), str(mapping["odoo_name"]), name),
              )
      _invalidate_caches()
  ```

  Extend both `_effective_map_uncached()` and `_effective_uncached()` to select the two new columns, then add `odoo_work_center_id` and `odoo_work_center_name` to `_shape_effective()`'s returned dictionary. This lets the existing Settings context expose a saved mapping without a per-row query.

- [ ] **Step 4: Run the store tests and the affected existing store tests**

  Run: `uv run pytest tests/test_work_centers_store_odoo_mapping.py tests/test_work_centers_store_required_skills.py -q`

  Expected: all configured database tests pass; without a local database, they skip cleanly with no import or collection errors.

- [ ] **Step 5: Commit the independently testable persistence layer**

  ```bash
  git add src/zira_dashboard/_schema.py src/zira_dashboard/work_centers_store.py \
    tests/test_work_centers_store_odoo_mapping.py
  git commit -m "feat: store Odoo work-center mappings"
  ```

### Task 2: Expose active Odoo choices and validate Settings updates

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py:35-111, 297-485`
- Modify: `src/zira_dashboard/routes/settings.py:145-420, 911-1020`
- Modify: `src/zira_dashboard/settings_context.py:23-84`
- Modify: `src/zira_dashboard/templates/settings.html:58-216`
- Modify: `tests/test_settings_context.py:30-110`
- Modify: `tests/test_settings_auto_work_centers.py:29-75`
- Create: `tests/test_settings_odoo_work_centers.py`

**Consumes:** Task 1's atomic mapping writers and the active Odoo `mrp.workcenter` catalog.

**Produces:** A Settings selector whose values are active Odoo record IDs and a pre-mutation validator that gives every later write path a trusted local mapping.

- [ ] **Step 1: Write failing Odoo catalog and Settings validation tests**

  In `tests/test_settings_odoo_work_centers.py`, test the pure validation helper before the HTTP route. It must reject missing IDs, inactive IDs, and duplicate selections before `save_one`, group edits, or default-person replacement run.

  ```python
  import pytest

  from zira_dashboard.routes import settings

  OPTIONS = [
      {"id": 41, "name": "Repair #1"},
      {"id": 42, "name": "Dismantler #1"},
  ]

  def test_mapping_updates_resolve_posted_ids_to_trusted_option_names():
      posted = {"wc__name:Repair 1__odoo_work_center_id": "41"}
      assert settings._odoo_work_center_updates(posted, OPTIONS) == {
          "Repair 1": {"odoo_id": 41, "odoo_name": "Repair #1"}
      }

  def test_mapping_updates_reject_unknown_or_duplicate_ids():
      unknown = {"wc__name:Repair 1__odoo_work_center_id": "999"}
      with pytest.raises(settings.InvalidOdooWorkCenterMapping):
          settings._odoo_work_center_updates(unknown, OPTIONS)
  ```

  Add a route test that patches `odoo_client.fetch_manufacturing_work_centers` to `OPTIONS`, posts a mapping, and asserts `work_centers_store.replace_odoo_work_center_mappings` receives the trusted numeric ID and Odoo name. Add another test in `tests/test_settings_context.py` proving a preexisting `odoo_work_center_id` and `odoo_work_center_name` appear in `work_center_rows()`, and that a blank ID sets `odoo_mapping_missing` to `True`.

- [ ] **Step 2: Run the new Settings tests to confirm they fail before implementation**

  Run: `uv run pytest tests/test_settings_odoo_work_centers.py tests/test_settings_context.py -q`

  Expected: `InvalidOdooWorkCenterMapping`, `_odoo_work_center_updates`, and the mapping fields in the row context are missing.

- [ ] **Step 3: Implement the active catalog, rendered selector, and atomic validation gate**

  Add this Odoo client read method. It is a Settings-only catalog lookup; it must not be called from a punch path. Cache normal GETs for five minutes and clear the cache in `_reset_cache_for_tests`; a POST uses `force=True` so an archived work center cannot be saved from an old page.

  ```python
  _manufacturing_work_centers_cache: tuple[list[dict], float] | None = None
  _MANUFACTURING_WORK_CENTERS_TTL_SECONDS = 300

  def fetch_manufacturing_work_centers(*, force: bool = False) -> list[dict]:
      global _manufacturing_work_centers_cache
      now = time.monotonic()
      if (
          not force
          and _manufacturing_work_centers_cache is not None
          and _manufacturing_work_centers_cache[1] > now
      ):
          return _manufacturing_work_centers_cache[0]
      rows = execute(
          "mrp.workcenter", "search_read", [("active", "=", True)],
          fields=["id", "name"], order="name",
      )
      result = [
          {"id": int(row["id"]), "name": str(row.get("name") or "").strip()}
          for row in rows
          if row.get("id") and str(row.get("name") or "").strip()
      ]
      _manufacturing_work_centers_cache = (result, now + _MANUFACTURING_WORK_CENTERS_TTL_SECONDS)
      return result
  ```

  In `settings_context.work_center_rows()`, append `effective["odoo_work_center_id"]`, `effective["odoo_work_center_name"]`, and `odoo_mapping_missing = effective["odoo_work_center_id"] is None` to each row. In `settings_page()`, fetch the catalog only for the Work Centers section, pass `odoo_work_centers` and an `odoo_work_centers_error` string to the template, and leave them as `[]` and a visible error when Odoo is unavailable.

  Define the validator in `routes/settings.py`. It must only inspect form keys that are present, which means an unavailable catalog on page load cannot erase existing values during a different Settings autosave.

  ```python
  class InvalidOdooWorkCenterMapping(ValueError):
      pass

  def _odoo_work_center_updates(form, options: list[dict]) -> dict[str, dict]:
      active = {int(option["id"]): option["name"] for option in options}
      result: dict[str, dict] = {}
      claimed: dict[int, str] = {}
      for loc in staffing.LOCATIONS:
          key = loc.meter_id or f"name:{loc.name}"
          field = f"wc__{key}__odoo_work_center_id"
          if field not in form:
              continue
          raw = (form.get(field) or "").strip()
          if not raw:
              result[loc.name] = {"odoo_id": None, "odoo_name": None}
              continue
          try:
              odoo_id = int(raw)
          except ValueError as exc:
              raise InvalidOdooWorkCenterMapping(f"Invalid Odoo work center for {loc.name}.") from exc
          odoo_name = active.get(odoo_id)
          if odoo_name is None:
              raise InvalidOdooWorkCenterMapping(
                  f"{loc.name} points to an inactive or unknown Odoo work center."
              )
          if odoo_id in claimed:
              raise InvalidOdooWorkCenterMapping(
                  f"{loc.name} and {claimed[odoo_id]} cannot use the same Odoo work center."
              )
          claimed[odoo_id] = loc.name
          result[loc.name] = {"odoo_id": odoo_id, "odoo_name": odoo_name}
      return result
  ```

  At the top of the route's `_work()` function, before group or default calculations, call `fetch_manufacturing_work_centers(force=True)` only if a posted mapping field exists. Build `mapping_updates` with the helper. On its controlled validation error, return JSON `422` or redirect back to `?section=work_centers&defaults_error=<message>` using the existing page alert; on catalog fetch failure, return JSON `503` or the same redirect with a clear unavailable message. After the existing per-location `save_one` loop completes, call `replace_odoo_work_center_mappings(mapping_updates)` once. Do not save each mapping independently: the batch update must support swapping two valid Odoo targets in one Settings autosave.

  Add an **Odoo Work Center** column immediately after **Work Center** in `settings.html`. When the catalog is available, each row must post a `<select name="{{ p }}odoo_work_center_id">` whose values are `option.id` and whose current mapping is selected. A row with `r.odoo_mapping_missing` must visibly show **Needs mapping** beside its blank selector so every readiness gap is visible before rollout. If the saved ID is absent from the active catalog, render it as the selected first option labelled `{{ r.odoo_work_center_name }} (inactive — choose a replacement)` so the next save is explicitly rejected instead of silently clearing it. When the catalog is unavailable, render the saved label as read-only text and no mapping form control.

- [ ] **Step 4: Run the Settings contract suite**

  Run: `uv run pytest tests/test_settings_odoo_work_centers.py tests/test_settings_context.py tests/test_settings_auto_work_centers.py tests/test_settings_default_people_preservation.py -q`

  Expected: the selector preserves saved mappings across a whole-form autosave, invalid or duplicate selections reject before writes, and all existing default-person preservation tests still pass.

- [ ] **Step 5: Commit the Settings mapping capability**

  ```bash
  git add src/zira_dashboard/odoo_client.py src/zira_dashboard/routes/settings.py \
    src/zira_dashboard/settings_context.py src/zira_dashboard/templates/settings.html \
    tests/test_settings_odoo_work_centers.py tests/test_settings_context.py \
    tests/test_settings_auto_work_centers.py
  git commit -m "feat: map kiosk work centers to Odoo"
  ```

### Task 3: Translate all attendance writes and reads through the mapping

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py:309-489`
- Modify: `src/zira_dashboard/_odoo_attendance.py:59-167, 231-269`
- Modify: `src/zira_dashboard/routes/missing_wc.py:33-58`
- Modify: `tests/test_odoo_open_attendance.py:15-210`
- Modify: `tests/test_odoo_attendance_for_day.py:65-115`
- Modify: `tests/test_odoo_facade_contract.py:1-32`
- Modify: `tests/test_missing_wc_routes.py:27-63`

**Consumes:** Task 1's local forward/reverse mapping and Task 2's validated ID/name persistence.

**Produces:** Odoo attendance records with `x_studio_work_center: <integer>` and app-name-only data returned to all internal consumers.

- [ ] **Step 1: Add failing Many2one write/read tests**

  Replace the existing string-field assertions in `tests/test_odoo_open_attendance.py` with ID-based assertions. Patch `work_centers_store.odoo_work_center_id_for` to return `41` for `Repair 1` and patch `app_work_center_name_for_odoo_id` to return `Repair 1` for `41`.

  ```python
  def test_clock_in_writes_mapped_many2one_id(monkeypatch):
      monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_studio_work_center")
      monkeypatch.setattr(
          "zira_dashboard.work_centers_store.odoo_work_center_id_for",
          lambda name: 41 if name == "Repair 1" else None,
      )
      fake = MagicMock(return_value=123)
      monkeypatch.setattr(odoo_client, "execute", fake)

      odoo_client.clock_in(5, "Repair 1", datetime(2026, 6, 16, 16, 30, tzinfo=timezone.utc))

      assert fake.call_args.args == (
          "hr.attendance", "create",
          {
              "employee_id": 5,
              "check_in": "2026-06-16 16:30:00",
              "in_mode": "kiosk",
              "overtime_status": "approved",
              "x_studio_work_center": 41,
          },
      )

  def test_open_attendance_reverse_maps_many2one_id(monkeypatch):
      monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_studio_work_center")
      monkeypatch.setattr(
          "zira_dashboard.work_centers_store.app_work_center_name_for_odoo_id",
          lambda odoo_id: {41: "Repair 1"}.get(odoo_id),
      )
      monkeypatch.setattr(odoo_client, "execute", lambda *_a, **_kw: [
          {"id": 88, "employee_id": [5, "Bob"],
           "check_in": "2026-06-01 11:02:00",
           "x_studio_work_center": [41, "Repair #1"]},
      ])

      assert odoo_client.fetch_open_attendances()[0]["wc_name"] == "Repair 1"
  ```

  Add assertions that an unmapped app name omits the optional field but still creates attendance; `set_attendance_wc` returns `False` without writing; interval reads map `[41, "Repair #1"]` to `Repair 1`; and an unknown Odoo ID returns `None` rather than its Odoo display label.

- [ ] **Step 2: Run the pure attendance tests to verify they fail with the current Char-field behavior**

  Run: `uv run pytest tests/test_odoo_open_attendance.py tests/test_odoo_attendance_for_day.py tests/test_odoo_facade_contract.py tests/test_missing_wc_routes.py -q`

  Expected: the tests expecting an integer ID, reverse mapping, and boolean setter result fail before implementation; database-gated Missing Work Center route tests may skip locally.

- [ ] **Step 3: Implement the shared conversion at the Odoo boundary**

  In `odoo_client.py`, import `work_centers_store` and add small wrappers so no renderer, worker, or route ever needs to know the database mapping details.

  ```python
  def _odoo_work_center_id_for_wc(wc_name: str | None) -> int | None:
      return work_centers_store.odoo_work_center_id_for(wc_name)

  def _app_wc_name_for_odoo_id(odoo_id: int | None) -> str | None:
      return work_centers_store.app_work_center_name_for_odoo_id(odoo_id)
  ```

  Update `clock_in()` and `set_attendance_wc()` to resolve the app name before writing. Add `wc_field: odoo_id` only when both the env field and a mapped ID exist. `clock_in()` still writes a department ID independently, so an unmapped optional work center cannot block payroll time or the existing Kiosk Department behavior. Make `set_attendance_wc()` return `True` only after writing a nonempty mapping and `False` for a missing env field, blank name, or missing mapping.

  Keep `_odoo_attendance.py` database-free by adding an `app_wc_name_for_odoo_id` callback to `fetch_open_attendances()` and `fetch_attendance_intervals_for_day()`. For each value, first call `_unwrap_m2o()` and then the callback. Never return `value[1]` as `wc_name`; it is only an Odoo display label. Pass `_app_wc_name_for_odoo_id` through the `odoo_client` facade, and update its facade-contract test to assert the added callback argument.

  In `routes/missing_wc.py`, only call `missing_wc.resolve()` and write the inbox event when `set_attendance_wc()` returns `True`. Return this exact conflict otherwise:

  ```python
  return JSONResponse(
      {"ok": False, "error": "That work center has no active Odoo mapping yet."},
      status_code=409,
  )
  ```

  Do not change `timeclock_sync._retry_one`: its existing call to `clock_in` now gains the conversion automatically; its adoption path deliberately records a successful local sync even if `set_attendance_wc()` is `False`, leaving the Odoo row visible to Missing Work Center.

- [ ] **Step 4: Run attendance, retry, and correction tests**

  Run: `uv run pytest tests/test_odoo_open_attendance.py tests/test_odoo_attendance_for_day.py tests/test_odoo_facade_contract.py tests/test_timeclock_sync_dedup.py tests/test_missing_wc_routes.py -q`

  Expected: every pure test passes; DB-backed route tests pass where `DATABASE_URL` is configured or skip cleanly otherwise. Verify the test names cover mapped write, unmapped timekeeping fallback, reverse mapping, sync adoption, and manual correction rejection.

- [ ] **Step 5: Commit the attendance conversion boundary**

  ```bash
  git add src/zira_dashboard/odoo_client.py src/zira_dashboard/_odoo_attendance.py \
    src/zira_dashboard/routes/missing_wc.py tests/test_odoo_open_attendance.py \
    tests/test_odoo_attendance_for_day.py tests/test_odoo_facade_contract.py \
    tests/test_missing_wc_routes.py
  git commit -m "feat: write Odoo attendance work centers"
  ```

### Task 4: Lock down automatic break continuity end to end

**Files:**
- Modify: `tests/test_auto_lunch_worker.py:30-151`
- Modify: `tests/test_timeclock_state_reconciliation.py:55-115`
- Modify: `tests/test_timeclock_windows.py:120-170`

**Consumes:** Task 3's guarantee that live Odoo attendance reads yield an app work-center name and every queued clock-in converts that name to a Many2one ID.

**Produces:** Regression coverage that a worker returns to the work center they actually held before lunch, even when Odoo's display name differs.

- [ ] **Step 1: Add a failing post-lunch continuity test**

  Extend `test_scheduled_auto_out_then_auto_in` so it selects both auto-lunch log rows in timestamp order and asserts the second row preserves the pre-break app work-center name.

  ```python
  rows = db.query(
      "SELECT action, wc_name FROM timeclock_punches_log "
      "WHERE person_odoo_id = %s ORDER BY COALESCE(rounded_at, occurred_at), id",
      (PID,),
  )
  assert [(row["action"], row["wc_name"]) for row in rows] == [
      ("clock_out", None),
      ("clock_in", "Bay 3"),
  ]
  ```

  Add a reconciliation test whose Odoo cache entry contains an app name returned by the reverse mapping layer (`Repair 1`), then verify `_advance_person()` persists `Repair 1`, not the Odoo display name `Repair #1`. Keep the existing test for the local-punch fallback where Odoo reports no known mapping.

- [ ] **Step 2: Run the Auto-Lunch and state-reconciliation tests to verify the new assertion first**

  Run: `uv run pytest tests/test_auto_lunch_worker.py tests/test_auto_lunch_decide.py tests/test_timeclock_state_reconciliation.py tests/test_timeclock_windows.py -q`

  Expected: the new regression assertion fails before the reverse-mapping behavior from Task 3 is in place; DB-backed Auto-Lunch worker tests require `DATABASE_URL` and otherwise skip.

- [ ] **Step 3: Make only the minimal compatibility adjustments revealed by the tests**

  Do not alter `auto_lunch.decide`, `auto_lunch._apply`, or `auto_lunch_runs` schema. They already implement the required sequence:

  ```text
  open Odoo attendance -> reverse map to app name -> auto_lunch_runs.wc_name
  -> local auto clock_in with that app name -> odoo_client.clock_in maps to ID
  ```

  If a test exposes raw `[id, display_name]` reaching `attendance_state.current_state`, fix the conversion in Task 3's `_odoo_attendance` callback path instead of adding list handling or string normalization in Auto-Lunch. If an Odoo ID is unmapped, preserve `None` so existing `_latest_in_wc()` remains the only fallback source.

- [ ] **Step 4: Run the full timeclock-focused regression suite**

  Run: `uv run pytest tests/test_auto_lunch_decide.py tests/test_auto_lunch_worker.py tests/test_auto_lunch_flex_sync.py tests/test_timeclock_state_reconciliation.py tests/test_timeclock_sync_dedup.py tests/test_timeclock_windows.py tests/test_odoo_open_attendance.py -q`

  Expected: no duplicate automatic return, no raw Odoo display name in the local punch log, and existing lunch cancellation, stale-cache, and retry behavior stay green.

- [ ] **Step 5: Commit the regression coverage**

  ```bash
  git add tests/test_auto_lunch_worker.py tests/test_timeclock_state_reconciliation.py \
    tests/test_timeclock_windows.py
  git commit -m "test: preserve work center after auto lunch"
  ```

### Task 5: Document activation and verify the production rollout

**Files:**
- Modify: `.env.example:24-32`
- Modify: `docs/odoo-setup.md:8-28`
- Modify: `CHANGELOG.md:1`

**Consumes:** Completed Settings mapping, Odoo write/read conversion, and Auto-Lunch regression coverage from Tasks 1-4.

**Produces:** A safe activation sequence that does not turn on the new Many2one env setting before the application is able to write IDs.

- [ ] **Step 1: Write the documentation assertions as a review checklist**

  Add this exact review checklist to the pull request description and execute it before pushing to `main`:

  ```text
  [ ] Every kiosk-selectable app work center has one active Odoo Manufacturing Work Center mapping.
  [ ] Railway's web service has ODOO_KIOSK_WC_FIELD=x_studio_work_center.
  [ ] A normal kiosk clock-in shows the selected Odoo Work Center on hr.attendance.
  [ ] A kiosk transfer creates the new attendance with its selected Odoo Work Center.
  [ ] An Auto-Lunch return creates the afternoon attendance with the pre-lunch Work Center.
  [ ] The Missing Work Center inbox remains empty after the three checks.
  ```

- [ ] **Step 2: Confirm the documentation changes are needed**

  Run: `rg -n "x_studio_work_center|Many2one|Manufacturing Work Center|pre-lunch" .env.example docs/odoo-setup.md CHANGELOG.md`

  Expected: no current documentation identifies `x_studio_work_center` as a linked `mrp.workcenter` field or explains that the environment setting and ID-writing code must be released together.

- [ ] **Step 3: Update setup guidance and What's New**

  In `.env.example`, replace the generic Kiosk WC comment with:

  ```dotenv
  ODOO_KIOSK_WC_FIELD=         # optional Many2one hr.attendance field to mrp.workcenter; use x_studio_work_center
  ```

  In `docs/odoo-setup.md`, add an **Attendance work-center field** subsection after Required env vars. State that `x_studio_work_center` is an optional Many2one to `mrp.workcenter`, every kiosk work center must be mapped in Settings first, and the Railway variable must be enabled only with this release.

  Add this new top entry to `CHANGELOG.md`, keeping all historical entries unchanged:

  ```markdown
  ## 2026-08-11

  ### Work center stays after breaks

  #### Fixes

  - **Your work area now stays with you after lunch.** When the clock signs you back in, it uses the same work area you had before the break. This helps your time land in the right place.
  ```

- [ ] **Step 4: Run final automated verification and the production checklist**

  Run: `uv run pytest tests/test_work_centers_store_odoo_mapping.py tests/test_settings_odoo_work_centers.py tests/test_odoo_open_attendance.py tests/test_odoo_attendance_for_day.py tests/test_auto_lunch_decide.py tests/test_auto_lunch_worker.py tests/test_timeclock_state_reconciliation.py tests/test_timeclock_sync_dedup.py tests/test_missing_wc_routes.py -q`

  Expected: all available tests pass and DB-gated tests skip only when no local `DATABASE_URL` is supplied. Then perform the six Step 1 checks in Railway/Odoo after deployment; do not claim rollout complete until each check succeeds.

- [ ] **Step 5: Commit documentation and prepare the release**

  ```bash
  git add .env.example docs/odoo-setup.md CHANGELOG.md
  git commit -m "docs: explain Odoo work-center attendance"
  git push origin main
  ```

## Plan self-review

### Spec coverage

- Explicit, active, one-to-one mapping: Tasks 1 and 2.
- Odoo Many2one ID writes on normal punch, transfer, automatic return, retry, and correction: Task 3.
- Reverse mapping from Odoo `[id, display_name]` to app label for live state and dashboard intervals: Task 3.
- Existing Auto-Lunch capture, fallback, cancellation, and idempotency preserved: Task 4.
- Timekeeping-first fallback and unresolvable manual correction behavior: Task 3.
- Environment activation, user-facing notes, tests, and live verification: Task 5.

### Placeholder scan

The plan contains no deferred implementation markers. Each code change identifies its owning file, exact API, test, command, and expected result.

### Type consistency

The mapping's external identity is consistently `int | None`; local logs, run rows, live cache, and dashboard intervals consistently use the app `str | None` name. Only `odoo_client` converts between those representations.
