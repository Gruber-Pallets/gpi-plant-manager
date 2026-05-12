# Retro WC Attributions — "Assignments to Do"

**Goal:** When a metered work center produced units but had no one scheduled there, surface it as a pending assignment. The user picks a person (any active employee, even if scheduled elsewhere); that person then gets credit for the WC's units in leaderboards, dashboards, and the published schedule.

**Architecture:** New Postgres table `wc_time_attributions(id, day, wc_name, person_name, start_utc, end_utc)`. A helper computes "unattributed WCs" by checking each metered WC on the day against (a) the schedule's assignments and (b) the attribution table. Surface count as a badge on the scheduler header + clickable popovers on dashboards. The production-attribution layer extends `attribute_for_day` to treat attributed people as operators on unscheduled WCs, so leaderboards/dashboards pick them up automatically.

**Tech Stack:** FastAPI, Jinja2, Postgres, vanilla JS.

---

## Scope (v1)

- Time-windowed at the data layer (we store `start_utc`/`end_utc`) but the v1 UX inserts ONE attribution row per WC covering "the production window" (first sample to last sample). That keeps the picker simple.
- Multiple operators on one WC = multiple attribution rows. They split units evenly (same rule as scheduled multi-operator WCs).
- v1 only attributes UNSCHEDULED WCs (no one in `assignments[wc]`). If a WC is scheduled, the schedule wins; we don't add retro-attributions on top.
- Scheduled-elsewhere people CAN be picked. (Lauro is on Forklift; that's fine — he can also be attributed to Dismantler 3.)
- Edit/delete attributions deferred to a follow-up. v1 is add-only.

---

## File touch map

- **`src/zira_dashboard/db.py`** — new table `wc_time_attributions` in the bootstrap schema.
- **`src/zira_dashboard/wc_attributions.py`** (new) — data layer: `add(day, wc, person, start, end)`, `for_day(day) → list[dict]`, `unattributed_for_day(day, client) → list[dict]`.
- **`src/zira_dashboard/production_history.py`** — extend `attribute_for_day` to merge `wc_time_attributions` into the assignments dict for unscheduled WCs.
- **`src/zira_dashboard/routes/staffing.py`** — pass `assignments_todo_count` and `assignments_todo` into the scheduler context. Add `POST /api/staffing/attribute` endpoint.
- **`src/zira_dashboard/templates/staffing.html`** — badge in title bar + modal panel listing unattributed WCs with picker.
- **`src/zira_dashboard/static/staffing.css`** — styles for badge + modal + picker.
- **`src/zira_dashboard/templates/recycling.html`** — `(no assignment)` text becomes clickable when there are pending intervals; popover with picker.
- **`src/zira_dashboard/templates/new_vs.html`** — same (if applicable).
- **Tests:** `tests/test_wc_attributions.py` (new) — pure logic for unattributed-detection + attribute_for_day extension.

---

## Step 1 — DB schema

In `src/zira_dashboard/db.py`, append to the bootstrap schema (next to `schedule_wc_notes`):

```sql
CREATE TABLE IF NOT EXISTS wc_time_attributions (
  id              BIGSERIAL PRIMARY KEY,
  day             DATE NOT NULL,
  wc_name         TEXT NOT NULL,
  person_name     TEXT NOT NULL,
  start_utc       TIMESTAMPTZ NOT NULL,
  end_utc         TIMESTAMPTZ NOT NULL,
  source          TEXT NOT NULL DEFAULT 'manual',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wc_time_attributions_day_idx ON wc_time_attributions(day);
CREATE INDEX IF NOT EXISTS wc_time_attributions_day_wc_idx ON wc_time_attributions(day, wc_name);
```

No FK to `schedules.day` because the attribution can predate the schedule entry.

## Step 2 — Data layer module

Create `src/zira_dashboard/wc_attributions.py`:

```python
"""Retro time-windowed WC attribution for production that happened at
unscheduled work centers."""

from __future__ import annotations

from datetime import date, datetime, timezone


def add(day: date, wc_name: str, person_name: str,
        start_utc: datetime, end_utc: datetime, source: str = "manual") -> int:
    """Insert one attribution row. Returns the new row id."""
    from . import db
    rows = db.query(
        "INSERT INTO wc_time_attributions "
        "(day, wc_name, person_name, start_utc, end_utc, source) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (day, wc_name, person_name, start_utc, end_utc, source),
    )
    return rows[0]["id"] if rows else 0


def for_day(day: date) -> list[dict]:
    """All attributions for a day. Returns list of dicts with keys
    id, wc_name, person_name, start_utc, end_utc, source."""
    from . import db
    return db.query(
        "SELECT id, wc_name, person_name, start_utc, end_utc, source "
        "FROM wc_time_attributions WHERE day = %s ORDER BY wc_name, start_utc",
        (day,),
    )


def people_by_wc(day: date) -> dict[str, list[str]]:
    """Aggregated view: {wc_name: [person, ...]} — convenience for joining
    into attribute_for_day's assignments dict."""
    out: dict[str, list[str]] = {}
    for r in for_day(day):
        out.setdefault(r["wc_name"], []).append(r["person_name"])
    return out


def delete(attribution_id: int) -> None:
    from . import db
    db.execute("DELETE FROM wc_time_attributions WHERE id = %s", (attribution_id,))


def unattributed_for_day(day: date, client) -> list[dict]:
    """Walk metered WCs for `day`. Return rows for WCs that:
      1. Produced units > 0 (not just transient noise)
      2. Are NOT in the schedule's assignments
      3. Are NOT in the attributions table

    Each result dict: {wc_name, units, first_sample_utc, last_sample_utc}.
    """
    from . import staffing
    from .leaderboard import cached_leaderboard as leaderboard
    from .stations import recycling_stations

    sched = staffing.load_schedule(day)
    scheduled_wcs = {wc for wc, ops in sched.assignments.items() if ops and wc != staffing.TIME_OFF_KEY}
    attributed_wcs = set(people_by_wc(day).keys())

    stations = recycling_stations()
    # Don't pass now_utc for past days; for today use now.
    today = datetime.now(timezone.utc).date()
    now_arg = datetime.now(timezone.utc) if day == today else None
    results = leaderboard(client, stations, day, now_utc=now_arg)

    out: list[dict] = []
    for r in results:
        if r.units <= 0:
            continue
        wc = r.station.name
        if wc in scheduled_wcs or wc in attributed_wcs:
            continue
        # Pull first/last sample times from active_intervals for time bounds.
        ais = r.active_intervals
        if not ais:
            continue
        first_utc = min(s for s, _ in ais)
        last_utc = max(e for _, e in ais)
        out.append({
            "wc_name": wc,
            "units": int(r.units),
            "first_sample_utc": first_utc,
            "last_sample_utc": last_utc,
        })
    return out
```

## Step 3 — Extend `attribute_for_day`

In `src/zira_dashboard/production_history.py`, change `attribute_for_day` to optionally accept retro attributions:

```python
def attribute_for_day(
    assignments: dict[str, list[str]],
    wc_totals: dict[str, tuple[int, int]],
    elapsed_minutes: int,
    extra_assignments: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """As before, plus an optional `extra_assignments` map that adds
    operators to UNSCHEDULED WCs only. Used to flow retro time-window
    attributions into leaderboards/dashboards.
    """
    from .staffing import TIME_OFF_KEY

    out: dict[str, dict[str, dict[str, float]]] = {}
    hours = elapsed_minutes / 60.0

    # Merge: scheduled wins; extras only fire when a WC has no scheduled people.
    merged: dict[str, list[str]] = {}
    for wc_name, operators in assignments.items():
        if wc_name == TIME_OFF_KEY or not operators:
            continue
        merged[wc_name] = list(operators)
    if extra_assignments:
        for wc_name, ppl in extra_assignments.items():
            if wc_name in merged:  # scheduled — skip
                continue
            if not ppl:
                continue
            merged[wc_name] = list(ppl)

    for wc_name, operators in merged.items():
        units, downtime = wc_totals.get(wc_name, (0, 0))
        n = len(operators)
        per_units = units / n
        per_downtime = downtime / n
        for person in operators:
            wc_map = out.setdefault(person, {})
            wc_map[wc_name] = {
                "units": per_units,
                "downtime": per_downtime,
                "hours": hours,
                "days_worked": 1,
            }
    return out
```

In `attribution_for(d, client)` (the wrapper at the bottom of the file), pass `extra_assignments=wc_attributions.people_by_wc(d)`:

```python
from . import wc_attributions
extra = wc_attributions.people_by_wc(d)
return attribute_for_day(assignments, wc_totals, elapsed, extra_assignments=extra)
```

The leaderboards code already calls `attribution_for(...)` for each day in the range, so leaderboards pick up retro attributions automatically.

## Step 4 — Scheduler badge + modal

In `src/zira_dashboard/routes/staffing.py`, after the existing context-prep (around the `attendance_by_name` block), add:

```python
# Retro WC attributions ("Assignments to Do").
assignments_todo: list[dict] = []
try:
    from .. import wc_attributions
    todo = wc_attributions.unattributed_for_day(d, client)
    # Convert UTC datetimes into site-local "h:mm a" strings for the UI.
    site_tz = shift_config.SITE_TZ
    for item in todo:
        first = item["first_sample_utc"].astimezone(site_tz)
        last = item["last_sample_utc"].astimezone(site_tz)
        assignments_todo.append({
            "wc_name": item["wc_name"],
            "units": item["units"],
            "first_label": first.strftime("%I:%M %p").lstrip("0"),
            "last_label": last.strftime("%I:%M %p").lstrip("0"),
            "first_iso": item["first_sample_utc"].isoformat(),
            "last_iso": item["last_sample_utc"].isoformat(),
        })
except Exception:
    assignments_todo = []
```

Add `"assignments_todo": assignments_todo,` and `"all_active_people": [p.name for p in roster if p.active],` to the template context.

Also, add a new endpoint:

```python
@router.post("/api/staffing/attribute")
async def staffing_attribute(request: Request):
    from datetime import date as _date, datetime as _dt
    from .. import wc_attributions
    body = await request.json()
    try:
        day = _date.fromisoformat(body["day"])
        wc = str(body["wc_name"]).strip()
        person = str(body["person_name"]).strip()
        start_utc = _dt.fromisoformat(body["start_utc"])
        end_utc = _dt.fromisoformat(body["end_utc"])
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"bad body: {e}"}, status_code=400)
    if not (wc and person and end_utc > start_utc):
        return JSONResponse({"ok": False, "error": "missing/invalid fields"}, status_code=400)
    new_id = wc_attributions.add(day, wc, person, start_utc, end_utc)
    return JSONResponse({"ok": True, "id": new_id})
```

## Step 5 — Scheduler template UI

In `src/zira_dashboard/templates/staffing.html`, just inside the `<div class="title-bar-actions">` (top of the actions row), before the date picker, add:

```jinja
{% if assignments_todo %}
  <button type="button" class="todo-btn" id="assignments-todo-btn"
          aria-label="Assignments to do" title="Unscheduled WCs that produced today">
    Assignments to Do <span class="todo-count">{{ assignments_todo|length }}</span>
  </button>
{% endif %}
```

At the very bottom of the `<form>` block (before the Hours editor close, or as a sibling to the Hours editor), add the modal markup:

```jinja
<div id="assignments-todo-modal" class="ats-modal" hidden>
  <div class="ats-backdrop" id="ats-backdrop"></div>
  <div class="ats-card" role="dialog" aria-modal="true" aria-label="Assignments to do">
    <div class="ats-head">
      <h3>Assignments to Do</h3>
      <button type="button" id="ats-close" class="ats-close" aria-label="Close">×</button>
    </div>
    <div class="ats-body">
      {% if assignments_todo %}
        <p class="ats-help">These work centers produced units today but had no one scheduled. Pick the person who actually worked there. (Any employee — including someone scheduled elsewhere.)</p>
        <ul class="ats-list">
          {% for item in assignments_todo %}
            <li class="ats-item" data-wc="{{ item.wc_name }}" data-day="{{ day }}"
                data-start="{{ item.first_iso }}" data-end="{{ item.last_iso }}">
              <div class="ats-item-head">
                <strong>{{ item.wc_name }}</strong>
                <span class="ats-meta">{{ item.units }} pallets · {{ item.first_label }}–{{ item.last_label }}</span>
              </div>
              <div class="ats-pick">
                <select class="ats-person">
                  <option value="">— pick person —</option>
                  {% for n in all_active_people %}
                    <option value="{{ n }}">{{ n }}</option>
                  {% endfor %}
                </select>
                <button type="button" class="ats-save">Save</button>
                <span class="ats-status" hidden></span>
              </div>
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="ats-help">Nothing to attribute right now — every WC with production today has someone assigned.</p>
      {% endif %}
    </div>
  </div>
</div>
```

In the same template's `{% block scripts %}` (or wherever the inline `<script>` lives), add:

```javascript
(function () {
  var btn = document.getElementById('assignments-todo-btn');
  var modal = document.getElementById('assignments-todo-modal');
  if (!btn || !modal) return;
  var backdrop = document.getElementById('ats-backdrop');
  var closeBtn = document.getElementById('ats-close');
  function open(e) {
    if (e) e.preventDefault();
    modal.hidden = false;
    document.documentElement.style.overflow = 'hidden';
  }
  function close() {
    modal.hidden = true;
    document.documentElement.style.overflow = '';
  }
  btn.addEventListener('click', open);
  backdrop.addEventListener('click', close);
  closeBtn.addEventListener('click', close);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !modal.hidden) close();
  });
  // Save handlers
  document.querySelectorAll('.ats-save').forEach(function (btnEl) {
    btnEl.addEventListener('click', function () {
      var li = btnEl.closest('.ats-item');
      var sel = li.querySelector('.ats-person');
      var status = li.querySelector('.ats-status');
      var person = sel.value;
      if (!person) { status.hidden = false; status.textContent = 'Pick a person first.'; return; }
      btnEl.disabled = true; sel.disabled = true; status.hidden = true;
      fetch('/api/staffing/attribute', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          day: li.dataset.day,
          wc_name: li.dataset.wc,
          person_name: person,
          start_utc: li.dataset.start,
          end_utc: li.dataset.end,
        }),
      }).then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            li.classList.add('ats-saved');
            status.hidden = false; status.textContent = 'Saved ✓ ' + person;
          } else {
            btnEl.disabled = false; sel.disabled = false;
            status.hidden = false; status.textContent = 'Failed: ' + (data.error || 'unknown');
          }
        }).catch(function () {
          btnEl.disabled = false; sel.disabled = false;
          status.hidden = false; status.textContent = 'Network error.';
        });
    });
  });
})();
```

## Step 6 — Scheduler CSS

Append to `src/zira_dashboard/static/staffing.css`:

```css
  .todo-btn {
    background: var(--warn-dim); color: var(--warn);
    border: 1px solid var(--warn); border-radius: 6px;
    padding: 0.3rem 0.7rem; font: inherit; font-size: 0.82rem; font-weight: 700;
    cursor: pointer; display: inline-flex; align-items: center; gap: 0.4rem;
  }
  .todo-btn:hover { background: var(--warn); color: white; }
  .todo-btn .todo-count {
    background: var(--warn); color: white;
    border-radius: 999px; padding: 0 0.4rem; font-size: 0.7rem;
  }
  .todo-btn:hover .todo-count { background: white; color: var(--warn); }

  .ats-modal[hidden] { display: none; }
  .ats-modal { position: fixed; inset: 0; z-index: 9000; display: flex; align-items: center; justify-content: center; }
  .ats-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,0.45); }
  .ats-card {
    position: relative; background: var(--panel); color: var(--fg);
    border: 1px solid var(--border); border-radius: 12px;
    width: min(640px, 92vw); max-height: 80vh;
    display: flex; flex-direction: column;
    box-shadow: 0 18px 48px rgba(0,0,0,0.25);
  }
  .ats-head { display: flex; justify-content: space-between; align-items: center; padding: 0.85rem 1rem; border-bottom: 1px solid var(--border); }
  .ats-head h3 { margin: 0; font-size: 1rem; font-weight: 700; }
  .ats-close { background: transparent; border: none; cursor: pointer; color: var(--muted); font-size: 1.3rem; padding: 0.1rem 0.5rem; border-radius: 6px; }
  .ats-close:hover { background: var(--panel-2); color: var(--fg); }
  .ats-body { padding: 0.8rem 1rem; overflow-y: auto; }
  .ats-help { color: var(--muted); font-size: 0.85rem; margin: 0 0 0.7rem; }
  .ats-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
  .ats-item {
    border: 1px solid var(--border); border-radius: 8px;
    padding: 0.55rem 0.75rem; background: var(--panel-2);
  }
  .ats-item.ats-saved { background: var(--accent-dim); border-color: var(--accent); }
  .ats-item-head { display: flex; align-items: baseline; gap: 0.6rem; margin-bottom: 0.4rem; }
  .ats-item-head strong { font-size: 0.95rem; }
  .ats-meta { color: var(--muted); font-size: 0.78rem; }
  .ats-pick { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .ats-pick select {
    background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
    padding: 0.3rem 0.5rem; font: inherit; font-size: 0.85rem; color: var(--fg);
    flex: 1 1 auto; min-width: 12rem;
  }
  .ats-pick button {
    background: var(--accent); color: white; border: 1px solid var(--accent);
    border-radius: 6px; padding: 0.3rem 0.8rem; font: inherit; font-weight: 600; cursor: pointer;
  }
  .ats-pick button:disabled { opacity: 0.6; cursor: not-allowed; }
  .ats-status { font-size: 0.8rem; color: var(--muted); }
  .ats-saved .ats-status { color: var(--accent); font-weight: 600; }
```

## Step 7 — Tests

Create `tests/test_wc_attributions.py`:

```python
from datetime import datetime, timezone

from zira_dashboard.production_history import attribute_for_day


def test_attribute_for_day_includes_extras_for_unscheduled_wc():
    assignments = {"Forklift": ["Lauro"]}
    extra = {"Dismantler 3": ["Lauro"]}
    wc_totals = {"Forklift": (10, 0), "Dismantler 3": (7, 0)}
    out = attribute_for_day(assignments, wc_totals, 480, extra_assignments=extra)
    assert out["Lauro"]["Forklift"]["units"] == 10
    assert out["Lauro"]["Dismantler 3"]["units"] == 7


def test_attribute_for_day_extras_skipped_when_wc_already_scheduled():
    assignments = {"Repair 1": ["Iban"]}
    extra = {"Repair 1": ["Lauro"]}  # should be ignored — Repair 1 is scheduled
    wc_totals = {"Repair 1": (12, 0)}
    out = attribute_for_day(assignments, wc_totals, 480, extra_assignments=extra)
    assert out["Iban"]["Repair 1"]["units"] == 12
    assert "Lauro" not in out


def test_attribute_for_day_extras_split_among_multiple_attributions():
    assignments = {}
    extra = {"Dismantler 3": ["Lauro", "Iban"]}
    wc_totals = {"Dismantler 3": (10, 0)}
    out = attribute_for_day(assignments, wc_totals, 480, extra_assignments=extra)
    assert out["Lauro"]["Dismantler 3"]["units"] == 5
    assert out["Iban"]["Dismantler 3"]["units"] == 5


def test_attribute_for_day_no_extras_argument_unchanged():
    """Backward-compat: not passing extra_assignments should behave like before."""
    assignments = {"Forklift": ["Lauro"]}
    wc_totals = {"Forklift": (8, 0)}
    out = attribute_for_day(assignments, wc_totals, 480)
    assert out["Lauro"]["Forklift"]["units"] == 8
```

## Step 8 — Verify

```bash
.venv/Scripts/python.exe -m pytest tests/test_wc_attributions.py tests/test_stratustime_client.py -v
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates')); env.get_template('staffing.html'); print('OK')"
.venv/Scripts/python.exe -c "from zira_dashboard import wc_attributions, production_history; print('Imports OK')"
```

Expected: 4 new tests pass plus the 26 stratustime tests; template parses; imports clean.

## Step 9 — Commit + push

```bash
git add src/zira_dashboard/db.py \
        src/zira_dashboard/wc_attributions.py \
        src/zira_dashboard/production_history.py \
        src/zira_dashboard/routes/staffing.py \
        src/zira_dashboard/templates/staffing.html \
        src/zira_dashboard/static/staffing.css \
        tests/test_wc_attributions.py
git commit -m "Retro WC attributions: Assignments to Do badge + modal on scheduler"
git push origin main
```

## Out of scope for this plan (follow-ups)

- Inline "(no assignment)" click on /recycling and /new-vs dashboards.
- Edit/delete attribution rows.
- Multi-person split per time-window (Lauro 9-10, Iban 10-11).
- Surfacing attributions on the published / printed schedule.

These can layer on once v1 is solid.

---

## Acceptance criteria

- After someone produces units at an unscheduled WC, scheduler shows "Assignments to Do (N)" badge.
- Clicking opens a modal listing each unattributed WC with its units total + production time window.
- Picker includes ALL active employees, even those scheduled elsewhere.
- Save inserts a row in `wc_time_attributions` and visibly marks the row done.
- Leaderboards (Best Days, Best Averages) credit the picked person for those units within ~5 min (cache TTL).
- Recycling/new-vs dashboards' man-hours and per-WC totals reflect the attribution within the same TTL.
- StratusTime / time-off integrations untouched.
- Page renders fine if Postgres is unreachable.
