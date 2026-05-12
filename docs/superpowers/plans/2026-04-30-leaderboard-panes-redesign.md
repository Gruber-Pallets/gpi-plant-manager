# Leaderboard Panes Redesign

> Supersedes the linked-row layout from `2026-04-30-best-averages-leaderboard.md`. The pure compute helpers `averages_for_wc` and `averages_for_group` (Tasks 1-2 of the prior plan) are still used unchanged.

**Goal:** Replace the locked-together two-half row layout with two independent panes — Best Days on the left, Best Averages on the right, each independently orderable, hideable, with widgets laid out two-up inside the pane.

## Design

```
┌─────────────────────────────────────────────────────────┐
│  toolbar (range chips + Custom popover + metric)        │
├─────────────────────────┬───────────────────────────────┤
│  BEST DAYS              │  BEST AVERAGES                │
│  ┌─────────┬─────────┐  │  ┌─────────┬─────────┐        │
│  │ widget  │ widget  │  │  │ widget  │ widget  │        │
│  └─────────┴─────────┘  │  └─────────┴─────────┘        │
│  ┌─────────┬─────────┐  │  ┌─────────┬─────────┐        │
│  │ widget  │ widget  │  │  │ widget  │ widget  │        │
│  └─────────┴─────────┘  │  └─────────┴─────────┘        │
│                         │                               │
│  ▼ Inactive (n)         │  ▼ Inactive (n)               │
└─────────────────────────┴───────────────────────────────┘
```

- Top-level page is a 2-column grid with a visible vertical divider down the middle (e.g., `border-left` on the right pane).
- Each pane has its own header label, its own active widget grid, and its own inactive `<details>` block at the bottom.
- Inside each pane, widgets sit in a 2-column auto-fit grid (`repeat(auto-fit, minmax(420px, 1fr))`).
- Drag-reorder is independent per pane.
- Hide ✕ moves only that pane's widget to that pane's inactive block.
- Show all (N) on the right pane is unchanged from current behavior — already per-widget.

## Storage

The existing `leaderboard_settings_store` already stores per-kind settings keyed on `(kind, wc_name)`. The schema's `kind TEXT` accepts any string. Expand the kind values:

- `wc` → Best Days side, per-WC widget
- `group` → Best Days side, per-group widget
- `wc-avg` → Best Averages side, per-WC widget (NEW)
- `group-avg` → Best Averages side, per-group widget (NEW)

No schema change. Existing `wc`/`group` rows preserve the user's existing ordering for the days side. The avg side starts at sort_order=0 for every entry → bay-organized natural order via the loc_index tiebreak.

## Task: Single implementer task — `leaderboard-panes-redesign`

Tightly coupled changes across 4 files. Done as one implementer + one spec review + one quality review.

### Files to modify

1. **`src/zira_dashboard/leaderboard_settings_store.py`** — expand kind validation in `set_order` and `set_inactive` from `("wc", "group")` to `("wc", "group", "wc-avg", "group-avg")`. Init `out` in `snapshot()` to include all four keys.
2. **`src/zira_dashboard/routes/leaderboards.py`** — read `wc_avg_settings_dict` and `group_avg_settings_dict` from snapshot; use them in the avg pass instead of reusing `wc_settings_dict`/`group_settings_dict`. Expand kind validation in 3 endpoints (`/order`, `/inactive`, `/active`).
3. **`src/zira_dashboard/templates/leaderboards.html`** — remove `.lb-section-row` wrapping. Replace with two parallel panes. Each pane has its own active grid + inactive `<details>` block. `data-kind` becomes `wc`/`group`/`wc-avg`/`group-avg`. JS hooks already key off `data-kind`, so no JS rewrite needed.
4. **`src/zira_dashboard/static/leaderboards.css`** — drop `.lb-section-row` grid-area rules. Add `.lb-page-panes` 2-col grid with vertical divider. Each `.lb-pane-active` is `repeat(auto-fit, minmax(420px, 1fr))`. Each pane has its own `.lb-pane-header` text label.

### Steps

- [ ] **Step 1: Update `leaderboard_settings_store.py`**

In `src/zira_dashboard/leaderboard_settings_store.py`:

```python
def snapshot() -> dict[str, dict[str, dict]]:
    """Return {kind: {name: {sort_order, is_inactive}}}.

    Top-level keys are always present (possibly empty):
    'wc', 'group', 'wc-avg', 'group-avg'.
    """
    from . import db
    rows = db.query(
        "SELECT kind, wc_name, sort_order, is_inactive "
        "FROM leaderboard_wc_settings"
    )
    out: dict[str, dict[str, dict]] = {
        "wc": {}, "group": {}, "wc-avg": {}, "group-avg": {},
    }
    for r in rows:
        k = r["kind"] or "wc"
        out.setdefault(k, {})[r["wc_name"]] = {
            "sort_order": r["sort_order"],
            "is_inactive": r["is_inactive"],
        }
    return out
```

In `set_order` and `set_inactive`, change the validation:

```python
    if kind not in ("wc", "group", "wc-avg", "group-avg"):
        return
```

- [ ] **Step 2: Update `routes/leaderboards.py` GET handler**

Find where the GET handler reads settings:

```python
    snap = lstore.snapshot()
    wc_settings_dict = snap.get("wc", {})
    group_settings_dict = snap.get("group", {})
```

Add two more reads:

```python
    snap = lstore.snapshot()
    wc_settings_dict = snap.get("wc", {})
    group_settings_dict = snap.get("group", {})
    wc_avg_settings_dict = snap.get("wc-avg", {})
    group_avg_settings_dict = snap.get("group-avg", {})
```

In the per-WC loop, the avg side currently reuses `wc_settings_dict`. Switch it to `wc_avg_settings_dict`. Find the block that builds `avg_sections.append(...)` and change `wc_settings` lookup logic so the avg side uses ITS OWN settings:

```python
        # --- Best Averages (new) ---
        wc_avg_settings = wc_avg_settings_dict.get(loc.name, {"sort_order": 0, "is_inactive": False})
        avg_auto_inactive = not wc_records
        avg_rows = averages_for_wc(
            wc_records, target_per_hour, shift_config.productive_minutes_for, metric,
        )
        avg_sections.append({
            "loc_name": loc.name,
            "rows": avg_rows,
            "is_inactive": wc_avg_settings["is_inactive"] or avg_auto_inactive,
            "is_manually_inactive": wc_avg_settings["is_inactive"],
            "is_auto_empty": avg_auto_inactive and not wc_avg_settings["is_inactive"],
            "sort_order": wc_avg_settings["sort_order"],
        })
```

Same change for the per-group loop — switch the avg side from reusing `g_set` to a separate `g_avg_set = group_avg_settings_dict.get(group_name, {"sort_order": 0, "is_inactive": False})`:

```python
        # --- Best Averages for this group (new) ---
        g_avg_set = group_avg_settings_dict.get(group_name, {"sort_order": 0, "is_inactive": False})
        avg_auto_inactive = not g_records
        avg_rows = averages_for_group(
            g_records, target_per_hour_by_wc, shift_config.productive_minutes_for, metric,
        )
        avg_group_sections.append({
            "loc_name": group_name,
            "rows": avg_rows,
            "is_inactive": g_avg_set["is_inactive"] or avg_auto_inactive,
            "is_manually_inactive": g_avg_set["is_inactive"],
            "is_auto_empty": avg_auto_inactive and not g_avg_set["is_inactive"],
            "sort_order": g_avg_set["sort_order"],
        })
```

The existing sort/split for active_avg_sections/inactive_avg_sections/active_avg_groups/inactive_avg_groups already works because they sort by the new `sort_order` from the avg settings.

The `avg_sections_by_name` and `avg_groups_by_name` lookup dicts are no longer needed (the new template doesn't pair widgets by loc_name anymore — each pane just iterates its own list). Remove those two lines and remove `avg_sections_by_name` / `avg_groups_by_name` from the context dict.

- [ ] **Step 3: Update endpoint kind validation**

In each of the three endpoints (`/staffing/leaderboards/order`, `/staffing/leaderboards/wc/{name}/inactive`, `/staffing/leaderboards/wc/{name}/active`), change:

```python
    if kind not in ("wc", "group"):
        return JSONResponse(...)
```

to:

```python
    if kind not in ("wc", "group", "wc-avg", "group-avg"):
        return JSONResponse(...)
```

- [ ] **Step 4: Restructure `templates/leaderboards.html`**

The toolbar (`<form class="lb-toolbar">` block, lines 14-36) stays UNCHANGED.

Replace the entire content from `<div class="lb-active-list">` down to (and including) the `</details>` of `<details class="lb-inactive-wrap">` with this two-pane structure:

```jinja
<div class="lb-page-panes">

  <section class="lb-pane lb-pane-days">
    <h2 class="lb-pane-header">Best Days</h2>
    <div class="lb-pane-active">
      {% for s in active_groups %}
        <div class="lb-section" data-kind="group" data-wc="{{ s.loc_name }}" draggable="true">
          <div class="lb-section-header">
            <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
            <h3>{{ s.loc_name }} <span class="lb-section-tag">group</span></h3>
            <button type="button" class="lb-hide-btn" title="Mark inactive">&#10005;</button>
          </div>
          {% if s.rows %}
            <table class="lb-table">
              <thead>
                <tr>
                  <th>#</th><th>Operator</th><th>Date</th><th>WC</th>
                  <th class="num">Units</th><th class="num">% of Goal</th>
                </tr>
              </thead>
              <tbody>
                {% for r in s.rows %}
                  <tr>
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td>{{ r.day_label }}</td>
                    <td>{{ r.wc }}</td>
                    <td class="num">{{ r.units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.pct * 100) }}%</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          {% endif %}
        </div>
      {% endfor %}
      {% for s in active_sections %}
        <div class="lb-section" data-kind="wc" data-wc="{{ s.loc_name }}" draggable="true">
          <div class="lb-section-header">
            <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
            <h3>{{ s.loc_name }}</h3>
            <button type="button" class="lb-hide-btn" title="Mark inactive">&#10005;</button>
          </div>
          {% if s.rows %}
            <table class="lb-table">
              <thead>
                <tr>
                  <th>#</th><th>Operator</th><th>Date</th>
                  <th class="num">Units</th><th class="num">% of Goal</th>
                </tr>
              </thead>
              <tbody>
                {% for r in s.rows %}
                  <tr>
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td>{{ r.day_label }}</td>
                    <td class="num">{{ r.units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.pct * 100) }}%</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          {% endif %}
        </div>
      {% endfor %}
    </div>

    <details class="lb-inactive-wrap">
      <summary>Inactive ({{ (inactive_sections|length) + (inactive_groups|length) }})</summary>
      <div class="lb-inactive-content lb-pane-active">
        {% for s in inactive_groups %}
          <div class="lb-section lb-section-inactive" data-kind="group" data-wc="{{ s.loc_name }}" draggable="true">
            <div class="lb-section-header">
              <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
              <h3>{{ s.loc_name }} <span class="lb-section-tag">group</span></h3>
              {% if s.is_manually_inactive %}
                <button type="button" class="lb-show-btn" title="Mark active">&#8634;</button>
              {% else %}
                <span class="lb-auto-empty" title="No data in this range — auto-hidden">auto-empty</span>
              {% endif %}
            </div>
            {% if s.rows %}
              <table class="lb-table">
                <thead>
                  <tr>
                    <th>#</th><th>Operator</th><th>Date</th><th>WC</th>
                    <th class="num">Units</th><th class="num">% of Goal</th>
                  </tr>
                </thead>
                <tbody>
                  {% for r in s.rows %}
                    <tr>
                      <td class="rank">{{ r.rank }}</td>
                      <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                      <td>{{ r.day_label }}</td>
                      <td>{{ r.wc }}</td>
                      <td class="num">{{ r.units|round|int }}</td>
                      <td class="num pct">{{ '%.0f' % (r.pct * 100) }}%</td>
                    </tr>
                  {% endfor %}
                </tbody>
              </table>
            {% endif %}
          </div>
        {% endfor %}
        {% for s in inactive_sections %}
          <div class="lb-section lb-section-inactive" data-kind="wc" data-wc="{{ s.loc_name }}" draggable="true">
            <div class="lb-section-header">
              <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
              <h3>{{ s.loc_name }}</h3>
              {% if s.is_manually_inactive %}
                <button type="button" class="lb-show-btn" title="Mark active">&#8634;</button>
              {% else %}
                <span class="lb-auto-empty" title="No data in this range — auto-hidden">auto-empty</span>
              {% endif %}
            </div>
            {% if s.rows %}
              <table class="lb-table">
                <thead>
                  <tr>
                    <th>#</th><th>Operator</th><th>Date</th>
                    <th class="num">Units</th><th class="num">% of Goal</th>
                  </tr>
                </thead>
                <tbody>
                  {% for r in s.rows %}
                    <tr>
                      <td class="rank">{{ r.rank }}</td>
                      <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                      <td>{{ r.day_label }}</td>
                      <td class="num">{{ r.units|round|int }}</td>
                      <td class="num pct">{{ '%.0f' % (r.pct * 100) }}%</td>
                    </tr>
                  {% endfor %}
                </tbody>
              </table>
            {% endif %}
          </div>
        {% endfor %}
      </div>
    </details>
  </section>

  <section class="lb-pane lb-pane-avg">
    <h2 class="lb-pane-header">Best Averages</h2>
    <div class="lb-pane-active">
      {% for s in active_avg_groups %}
        <div class="lb-section" data-kind="group-avg" data-wc="{{ s.loc_name }}" draggable="true">
          <div class="lb-section-header">
            <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
            <h3>{{ s.loc_name }} <span class="lb-section-tag">group</span></h3>
            <button type="button" class="lb-hide-btn" title="Mark inactive">&#10005;</button>
          </div>
          {% if s.rows %}
            {% set top5 = s.rows[:5] %}
            {% set rest = s.rows[5:] %}
            <table class="lb-table">
              <thead>
                <tr>
                  <th>#</th><th>Operator</th><th>Top WC</th>
                  <th class="num">Avg/day{% if metric == 'units' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                  <th class="num">Avg %{% if metric == 'pct' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                </tr>
              </thead>
              <tbody>
                {% for r in top5 %}
                  <tr>
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td>{{ r.top_wc }}</td>
                    <td class="num">{{ r.avg_units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                  </tr>
                {% endfor %}
                {% for r in rest %}
                  <tr class="lb-row-hidden">
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td>{{ r.top_wc }}</td>
                    <td class="num">{{ r.avg_units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
            {% if rest %}
              <button type="button" class="lb-expand-btn" onclick="toggleAll(this)">
                &#9662; Show all ({{ s.rows|length }})
              </button>
            {% endif %}
          {% endif %}
        </div>
      {% endfor %}
      {% for s in active_avg_sections %}
        <div class="lb-section" data-kind="wc-avg" data-wc="{{ s.loc_name }}" draggable="true">
          <div class="lb-section-header">
            <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
            <h3>{{ s.loc_name }}</h3>
            <button type="button" class="lb-hide-btn" title="Mark inactive">&#10005;</button>
          </div>
          {% if s.rows %}
            {% set top5 = s.rows[:5] %}
            {% set rest = s.rows[5:] %}
            <table class="lb-table">
              <thead>
                <tr>
                  <th>#</th><th>Operator</th>
                  <th class="num">Avg/day{% if metric == 'units' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                  <th class="num">Avg %{% if metric == 'pct' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                </tr>
              </thead>
              <tbody>
                {% for r in top5 %}
                  <tr>
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td class="num">{{ r.avg_units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                  </tr>
                {% endfor %}
                {% for r in rest %}
                  <tr class="lb-row-hidden">
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td class="num">{{ r.avg_units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
            {% if rest %}
              <button type="button" class="lb-expand-btn" onclick="toggleAll(this)">
                &#9662; Show all ({{ s.rows|length }})
              </button>
            {% endif %}
          {% endif %}
        </div>
      {% endfor %}
    </div>

    <details class="lb-inactive-wrap">
      <summary>Inactive ({{ (inactive_avg_sections|length) + (inactive_avg_groups|length) }})</summary>
      <div class="lb-inactive-content lb-pane-active">
        {% for s in inactive_avg_groups %}
          <div class="lb-section lb-section-inactive" data-kind="group-avg" data-wc="{{ s.loc_name }}" draggable="true">
            <div class="lb-section-header">
              <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
              <h3>{{ s.loc_name }} <span class="lb-section-tag">group</span></h3>
              {% if s.is_manually_inactive %}
                <button type="button" class="lb-show-btn" title="Mark active">&#8634;</button>
              {% else %}
                <span class="lb-auto-empty" title="No data in this range — auto-hidden">auto-empty</span>
              {% endif %}
            </div>
            {% if s.rows %}
              {% set top5 = s.rows[:5] %}
              {% set rest = s.rows[5:] %}
              <table class="lb-table">
                <thead>
                  <tr>
                    <th>#</th><th>Operator</th><th>Top WC</th>
                    <th class="num">Avg/day{% if metric == 'units' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                    <th class="num">Avg %{% if metric == 'pct' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                  </tr>
                </thead>
                <tbody>
                  {% for r in top5 %}
                    <tr>
                      <td class="rank">{{ r.rank }}</td>
                      <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                      <td>{{ r.top_wc }}</td>
                      <td class="num">{{ r.avg_units|round|int }}</td>
                      <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                    </tr>
                  {% endfor %}
                  {% for r in rest %}
                    <tr class="lb-row-hidden">
                      <td class="rank">{{ r.rank }}</td>
                      <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                      <td>{{ r.top_wc }}</td>
                      <td class="num">{{ r.avg_units|round|int }}</td>
                      <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                    </tr>
                  {% endfor %}
                </tbody>
              </table>
              {% if rest %}
                <button type="button" class="lb-expand-btn" onclick="toggleAll(this)">
                  &#9662; Show all ({{ s.rows|length }})
                </button>
              {% endif %}
            {% endif %}
          </div>
        {% endfor %}
        {% for s in inactive_avg_sections %}
          <div class="lb-section lb-section-inactive" data-kind="wc-avg" data-wc="{{ s.loc_name }}" draggable="true">
            <div class="lb-section-header">
              <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
              <h3>{{ s.loc_name }}</h3>
              {% if s.is_manually_inactive %}
                <button type="button" class="lb-show-btn" title="Mark active">&#8634;</button>
              {% else %}
                <span class="lb-auto-empty" title="No data in this range — auto-hidden">auto-empty</span>
              {% endif %}
            </div>
            {% if s.rows %}
              {% set top5 = s.rows[:5] %}
              {% set rest = s.rows[5:] %}
              <table class="lb-table">
                <thead>
                  <tr>
                    <th>#</th><th>Operator</th>
                    <th class="num">Avg/day{% if metric == 'units' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                    <th class="num">Avg %{% if metric == 'pct' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                  </tr>
                </thead>
                <tbody>
                  {% for r in top5 %}
                    <tr>
                      <td class="rank">{{ r.rank }}</td>
                      <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                      <td class="num">{{ r.avg_units|round|int }}</td>
                      <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                    </tr>
                  {% endfor %}
                  {% for r in rest %}
                    <tr class="lb-row-hidden">
                      <td class="rank">{{ r.rank }}</td>
                      <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                      <td class="num">{{ r.avg_units|round|int }}</td>
                      <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                    </tr>
                  {% endfor %}
                </tbody>
              </table>
              {% if rest %}
                <button type="button" class="lb-expand-btn" onclick="toggleAll(this)">
                  &#9662; Show all ({{ s.rows|length }})
                </button>
              {% endif %}
            {% endif %}
          </div>
        {% endfor %}
      </div>
    </details>
  </section>

</div>
```

In the `{% block scripts %}` JavaScript, the `saveOrder()` function currently iterates kinds `['wc', 'group']`. Expand it to all four kinds:

```javascript
  function saveOrder() {
    for (const kind of ['wc', 'group', 'wc-avg', 'group-avg']) {
      const order = [];
      document.querySelectorAll(`.lb-pane-active .lb-section[data-kind="${kind}"]`).forEach(s => order.push(s.dataset.wc));
      document.querySelectorAll(`.lb-inactive-content .lb-section[data-kind="${kind}"]`).forEach(s => order.push(s.dataset.wc));
      fetch(`/staffing/leaderboards/order?kind=${kind}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({order}),
      }).catch(() => {});
    }
  }
```

(The `.lb-active-list` selector is replaced with `.lb-pane-active`.)

The drag/hide handlers don't need any changes — they work off `.lb-section` and `data-kind`.

- [ ] **Step 5: Update `static/leaderboards.css`**

Replace the entire `.lb-active-list, .lb-inactive-content` + `.lb-section-row` + `.lb-section-row > ...` rules block (currently around lines 54-116, ending right before `.lb-section { background: ... }`) with:

```css
  .lb-page-panes {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.5rem;
    align-items: start;
  }
  .lb-pane { min-width: 0; }
  .lb-pane-avg { border-left: 1px solid var(--border); padding-left: 1.5rem; }
  .lb-pane-header {
    margin: 0 0 0.6rem 0;
    font-size: 1rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-weight: 700;
  }

  .lb-pane-active {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
    gap: 0.7rem;
    align-items: start;
  }

  .lb-row-hidden { display: none; }
  .lb-sort-caret { color: var(--accent); font-size: 0.65rem; vertical-align: 1px; }

  .lb-expand-btn {
    margin-top: 0.4rem;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    border-radius: 4px;
    padding: 0.25rem 0.7rem;
    font: inherit; font-size: 0.8rem;
    cursor: pointer;
  }
  .lb-expand-btn:hover { color: var(--accent); border-color: var(--accent); }

  @media (max-width: 1100px) {
    .lb-page-panes {
      grid-template-columns: 1fr;
    }
    .lb-pane-avg { border-left: none; padding-left: 0; border-top: 1px solid var(--border); padding-top: 1rem; }
  }
```

- [ ] **Step 6: Smoke check**

```bash
.venv\Scripts\python.exe -m pytest tests/test_leaderboards_avg.py -v
.venv\Scripts\python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates')); env.get_template('leaderboards.html'); print('OK')"
```

Expected: 13 tests pass, Jinja prints `OK`.

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/leaderboard_settings_store.py src/zira_dashboard/routes/leaderboards.py src/zira_dashboard/templates/leaderboards.html src/zira_dashboard/static/leaderboards.css
git commit -m "Redesign leaderboards as two independent panes (Best Days / Best Averages)"
```

## Acceptance criteria

- Page is split vertically: Best Days on the left, Best Averages on the right, with a visible divider.
- Each pane has its own header label at the top.
- Each pane lays widgets out in 2 columns (auto-fit at minmax 380px).
- Drag-reorder within a pane reorders only that pane (POSTs `kind=wc|group|wc-avg|group-avg` accordingly).
- Hide ✕ moves only the targeted pane's widget to that pane's inactive `<details>`.
- Show all (N) on Best Averages widgets still works.
- Toolbar (range chips + Custom popover + metric) is unchanged and full-width above both panes.
- Below 1100px viewport, the panes stack vertically with a horizontal divider.
- Existing `wc`/`group` settings rows in DB still drive the Best Days side ordering.
- Avg side starts from natural bay order (no legacy rows for `wc-avg`/`group-avg`); user reorders persist via the existing endpoint.
