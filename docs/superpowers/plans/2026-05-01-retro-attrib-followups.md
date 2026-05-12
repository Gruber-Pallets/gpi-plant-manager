# Retro WC Attributions — v1.1 Follow-ups

Three layered improvements to the v1 retro attribution feature shipped earlier today.

## Goals

1. **Edit/delete saved attributions** — list saved attributions for the day in the scheduler's "Assignments to Do" modal with × buttons. Click → DELETE → row removed.
2. **Broaden detection to all metered WCs** — `unattributed_for_day` currently only walks the Recycling cell. Should consider every metered station so unscheduled work at Junior 2 / Hand Build / etc. surfaces too.
3. **Inline assign on `/new-vs`** — mirror the just-shipped `/recycling` inline-popover behavior on the New value-stream dashboard.

---

## File touch map

- **`src/zira_dashboard/wc_attributions.py`** — broaden `unattributed_for_day` to all metered stations.
- **`src/zira_dashboard/routes/staffing.py`** — populate `assignments_done` (saved-today list) for the modal; add `DELETE /api/staffing/attribute/{id}` endpoint.
- **`src/zira_dashboard/templates/staffing.html`** — render the "Saved today" sub-list with × buttons + JS handler.
- **`src/zira_dashboard/static/staffing.css`** — small style tweaks for the saved-list rows.
- **`src/zira_dashboard/routes/value_streams.py`** — new_vs route now also computes `assignments_todo_by_wc` + `all_active_people`.
- **`src/zira_dashboard/templates/new_vs.html`** — `(no assignment)` becomes a clickable button + inline-popover JS (copy from recycling) + popover CSS (copy from recycling.css or share via new_vs.css).
- **`src/zira_dashboard/static/new_vs.css`** — add the popover styles.

---

## Step 1 — Broaden detection

In `src/zira_dashboard/wc_attributions.py`, replace:

```python
from .stations import recycling_stations
...
stations = recycling_stations()
```

With:

```python
from .stations import STATIONS
...
stations = [s for s in STATIONS if s.meter_id]
```

(`STATIONS` is the master list defined in `stations.py`. We want every station that has a meter, regardless of cell.)

## Step 2 — Saved-today list + delete endpoint

In `src/zira_dashboard/routes/staffing.py`, after the existing `assignments_todo` block, also fetch saved attributions for today:

```python
assignments_done: list[dict] = []
try:
    from .. import wc_attributions
    site_tz = shift_config.SITE_TZ
    for r in wc_attributions.for_day(d):
        s_local = r["start_utc"].astimezone(site_tz)
        e_local = r["end_utc"].astimezone(site_tz)
        assignments_done.append({
            "id": r["id"],
            "wc_name": r["wc_name"],
            "person_name": r["person_name"],
            "first_label": s_local.strftime("%I:%M %p").lstrip("0"),
            "last_label": e_local.strftime("%I:%M %p").lstrip("0"),
        })
except Exception:
    assignments_done = []
```

Pass `"assignments_done": assignments_done,` into the context.

Add the delete endpoint near the existing `/api/staffing/attribute` POST:

```python
@router.delete("/api/staffing/attribute/{attribution_id}")
def staffing_attribute_delete(attribution_id: int):
    from .. import wc_attributions
    try:
        wc_attributions.delete(attribution_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})
```

## Step 3 — Modal "Saved today" sub-list

In `src/zira_dashboard/templates/staffing.html`, inside the modal's `.ats-body` block, AFTER the existing `<ul class="ats-list">` (the todo list) and AFTER the {% else %} fallback, add a separator + saved-today list:

```jinja
{% if assignments_done %}
  <h4 class="ats-section-title">Saved today</h4>
  <ul class="ats-saved-list">
    {% for r in assignments_done %}
      <li class="ats-saved-item" data-attribution-id="{{ r.id }}">
        <span class="ats-saved-text">
          <strong>{{ r.wc_name }}</strong> — {{ r.person_name }}
          <span class="ats-saved-meta">{{ r.first_label }}–{{ r.last_label }}</span>
        </span>
        <button type="button" class="ats-delete" title="Remove this attribution" aria-label="Remove">×</button>
      </li>
    {% endfor %}
  </ul>
{% endif %}
```

Extend the modal's existing JS IIFE to also wire the delete buttons:

```javascript
document.querySelectorAll('.ats-delete').forEach(function (btnEl) {
  btnEl.addEventListener('click', function () {
    var li = btnEl.closest('.ats-saved-item');
    if (!li) return;
    var id = li.dataset.attributionId;
    if (!id) return;
    if (!confirm('Remove this attribution?')) return;
    btnEl.disabled = true;
    fetch('/api/staffing/attribute/' + encodeURIComponent(id), {method: 'DELETE'})
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          li.style.opacity = '0.4';
          li.style.textDecoration = 'line-through';
          setTimeout(function () { location.reload(); }, 400);
        } else {
          btnEl.disabled = false;
          alert('Delete failed: ' + (data.error || 'unknown'));
        }
      }).catch(function () {
        btnEl.disabled = false;
        alert('Network error.');
      });
  });
});
```

If the modal currently shows the todo list inside `<ul class="ats-list">`, adjust its else-branch so the "Saved today" list still renders even when there's nothing in the todo list.

## Step 4 — Modal CSS

Append to `src/zira_dashboard/static/staffing.css`:

```css
  .ats-section-title {
    font-size: 0.7rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.6px;
    color: var(--muted);
    margin: 1.1rem 0 0.4rem;
    border-top: 1px solid var(--border);
    padding-top: 0.6rem;
  }
  .ats-saved-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.3rem; }
  .ats-saved-item {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.4rem 0.6rem;
    border: 1px solid var(--border); border-radius: 6px;
    background: var(--accent-dim);
  }
  .ats-saved-text { flex: 1 1 auto; font-size: 0.88rem; }
  .ats-saved-meta { color: var(--muted); font-size: 0.78rem; margin-left: 0.4rem; }
  .ats-delete {
    background: transparent; color: var(--muted);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 0 0.5rem; font: inherit; font-size: 1.1rem; line-height: 1.4;
    cursor: pointer;
  }
  .ats-delete:hover { color: var(--bad); border-color: var(--bad); }
  .ats-delete:disabled { opacity: 0.4; cursor: not-allowed; }
```

## Step 5 — `/new-vs` inline assign

In `src/zira_dashboard/routes/value_streams.py`, find the `new_vs()` handler. Right before its `templates.TemplateResponse(...)` call, add the same block we have in `recycling()`:

```python
# Inline-assign popover: today only.
assignments_todo_by_wc: dict[str, dict] = {}
all_active_people: list[str] = []
if is_today:
    try:
        from .. import staffing as _staffing, wc_attributions
        todo = wc_attributions.unattributed_for_day(today, client)
        site_tz = shift_config.SITE_TZ
        for item in todo:
            first = item["first_sample_utc"].astimezone(site_tz)
            last = item["last_sample_utc"].astimezone(site_tz)
            assignments_todo_by_wc[item["wc_name"]] = {
                "wc_name": item["wc_name"],
                "units": item["units"],
                "first_label": first.strftime("%I:%M %p").lstrip("0"),
                "last_label": last.strftime("%I:%M %p").lstrip("0"),
                "first_iso": item["first_sample_utc"].isoformat(),
                "last_iso": item["last_sample_utc"].isoformat(),
            }
        roster = _staffing.load_roster()
        all_active_people = sorted((p.name for p in roster if p.active), key=str.lower)
    except Exception:
        assignments_todo_by_wc = {}
        all_active_people = []
```

Pass `"assignments_todo_by_wc": assignments_todo_by_wc, "all_active_people": all_active_people, "today": today.isoformat(),` into the template context.

(Note: `is_today` may not exist in `new_vs()` — check. If the route only has `day` and `today` vars, set `is_today = (d == today)` first.)

## Step 6 — `/new-vs` template + CSS

In `src/zira_dashboard/templates/new_vs.html`, find every `<span class="name-secondary"><em>(no assignment)</em></span>` and replace with the same conditional pattern used in `recycling.html`:

```jinja
{% if assignments_todo_by_wc and b.name in assignments_todo_by_wc %}
  {% set _todo = assignments_todo_by_wc[b.name] %}
  <button type="button" class="no-assign-btn" data-wc="{{ b.name }}" data-day="{{ today }}" data-start="{{ _todo.first_iso }}" data-end="{{ _todo.last_iso }}" title="Assign person to this WC's work">↪ assign</button>
{% else %}
  <span class="name-secondary"><em>(no assignment)</em></span>
{% endif %}
```

(The bar variable is `b` in both vertical and horizontal contexts. Downtime widget uses `d` — leave it alone.)

Copy the inline-assign JS IIFE from `recycling.html` (the `// Inline-assign popover...` block) into `new_vs.html`'s closing `<script>` block.

In `src/zira_dashboard/static/new_vs.css`, append the same `.no-assign-btn` and `.assign-popover` styles from `recycling.css`. (Copy the block; don't extract to a shared file in this PR — small duplication is fine.)

---

## Verify

```bash
.venv/Scripts/python.exe -m pytest tests/test_wc_attributions.py tests/test_stratustime_client.py -v
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'));
for t in ['staffing.html','recycling.html','new_vs.html']: env.get_template(t); print(t,'OK')"
.venv/Scripts/python.exe -c "from zira_dashboard import wc_attributions; print('Imports OK')"
```

Expected: 30 tests pass; templates parse; imports clean.

## Commit + push

```bash
git add src/zira_dashboard/wc_attributions.py \
        src/zira_dashboard/routes/staffing.py \
        src/zira_dashboard/templates/staffing.html \
        src/zira_dashboard/static/staffing.css \
        src/zira_dashboard/routes/value_streams.py \
        src/zira_dashboard/templates/new_vs.html \
        src/zira_dashboard/static/new_vs.css
git commit -m "Retro WC attributions v1.1: edit/delete + broader detection + new-vs inline"
git push origin main
```

---

## Acceptance criteria

- "Saved today" sub-list appears in the scheduler modal listing each saved attribution with × delete buttons.
- Click × → confirm prompt → DELETE → page reloads with the row gone.
- `unattributed_for_day` now flags ANY metered WC without a schedule entry, not just Recycling-cell WCs.
- `/new-vs` today view: `(no assignment)` bars become `↪ assign` buttons with the same inline-popover behavior as `/recycling`.
- All existing tests pass; templates parse; nothing else regresses.
