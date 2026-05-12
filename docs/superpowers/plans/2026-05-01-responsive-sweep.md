# Responsive Sweep — Cross-Device Scaling

**Goal:** Make the app render well across screen sizes from a 13" MacBook (~1280–1512px viewport) up through 27"+ external monitors. Existing breakpoints jump from 1100px straight to desktop, so 13" laptops sit in an awkward zone where layouts are too cramped.

**Approach:** Layered tactics applied across the four highest-traffic pages — scheduler, recycling, leaderboards, time-off. Add intermediate breakpoints, reduce fixed pixel widths, swap a few fixed font sizes for `clamp()` fluid sizing, tighten padding on narrow screens.

---

## Common tactics

1. **Intermediate breakpoint at 1300px** — between current 1100px (full mobile-stack) and desktop. Slightly compresses padding, narrows side panels, shrinks fixed-pixel ops-range columns.

2. **Side-panel width reductions** — 200px / 280px panels become 180px / 240px under 1400px and stack under 1100px (existing).

3. **Fluid font sizing for page titles** via `clamp(min, preferred, max)` — small enough for tablets, large enough for big monitors.

4. **Wide tables get horizontal scroll** — wrap in a `.table-scroll-x { overflow-x: auto }` container so they don't break the layout when the viewport is narrow.

5. **Reduce horizontal padding** on `.layout` and main containers under 1400px (1.5rem → 0.75rem).

---

## File touch map

- `src/zira_dashboard/static/staffing.css` — scheduler layout breakpoints
- `src/zira_dashboard/static/leaderboards.css` — already has 1100px breakpoint; add 1300px tightening
- `src/zira_dashboard/static/recycling.css` — Gridstack-based; mostly fine but tighten KPI cards & reduce padding
- `src/zira_dashboard/templates/time_off.html` — inline styles for the calendar

No JS changes. No template structural changes.

---

## Step 1 — Scheduler (staffing.css)

Current layout: `200px | 1fr | 280px` collapses to single column at 1100px. Add an intermediate step:

```css
@media (max-width: 1400px) {
  .layout {
    grid-template-columns: 180px minmax(0, 1fr) 240px;
    padding: 0.75rem 0.9rem;
    gap: 0.75rem;
  }
  .side .section h3 { font-size: 0.75rem; }
  .side li { font-size: 0.85rem; }
  .day-context { font-size: 0.85rem; }
  .panel { padding: 0.55rem 0.7rem; }
}
```

Also drop the `.title-bar h2` font-size to fluid:

```css
.title-bar h2 { font-size: clamp(1rem, 1.2vw + 0.6rem, 1.2rem); }
```

And tighten the table cell padding under 1400px (the WC table is dense):

```css
@media (max-width: 1400px) {
  .ops-table th, .ops-table td { padding: 0.3rem 0.4rem; font-size: 0.82rem; }
  .ops-table .station .name { font-size: 0.85rem; }
}
```

(Selectors should be checked against the actual current CSS — the implementer will read first and adjust.)

## Step 2 — Leaderboards (leaderboards.css)

Existing breakpoint at 1100px stacks the panes vertically. Add an intermediate step that tightens the auto-fit minmax:

```css
@media (max-width: 1400px) {
  .lb-pane-active {
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  }
  .lb-pane-header { font-size: 0.9rem; }
  .lb-section { padding: 0.5rem 0.65rem; }
  .lb-table { font-size: 0.78rem; }
  .lb-table th, .lb-table td { padding: 0.2rem 0.4rem; }
}
```

This makes widgets pack tighter on 13" laptops without forcing the full mobile-stack.

## Step 3 — Recycling (recycling.css)

The 12-column Gridstack handles most resizing. Tighten KPI fonts + reduce padding under 1400px:

```css
@media (max-width: 1400px) {
  .grid-stack-item-content { padding: 0.55rem 0.7rem; }
  .grid-stack-item-content .label { font-size: 0.7rem; }
  .grid-stack-item-content .val { font-size: clamp(1.2rem, 2.5vw, 2rem); }
  h3 { font-size: 0.85rem; }
}
```

The KPI numbers (Total Pallets, Up Time, etc.) use a fixed-rem `val`. Replace with `clamp()` so they scale fluidly.

## Step 4 — Time-off month view (time_off.html inline CSS)

Month view recently went to 11rem cell height + 6 columns. On a 13" laptop that's still about right. Add fluid pill sizes:

```css
.off-pill { font-size: clamp(0.7rem, 0.85vw, 0.78rem); }
@media (max-width: 1400px) {
  table.month td { padding: 0.4rem; height: 9rem; min-height: 9rem; }
  .day-num { font-size: 0.7rem; }
}
```

## Step 5 — Verify

Manual verification on different viewport widths via browser dev-tools responsive mode:

- **2560×1440** (27" external): should look like today
- **1512×945** (M2 MacBook 13"): should be tight but still 3-column on staffing, both panes side-by-side on leaderboards
- **1280×800** (older 13" Air): same as above but tighter
- **1100×700**: existing mobile-stack kicks in

Automated: existing test suite still passes; templates parse.

## Step 6 — Commit

```bash
git add src/zira_dashboard/static/staffing.css \
        src/zira_dashboard/static/leaderboards.css \
        src/zira_dashboard/static/recycling.css \
        src/zira_dashboard/templates/time_off.html
git commit -m "Responsive sweep: tighten layouts on 13in laptops, fluid font sizes"
git push origin main
```

---

## Acceptance criteria

- 13" MacBook (1280–1512px) renders the scheduler with all three columns visible, no overflow, comfortable density.
- Leaderboards page shows both panes side-by-side at 13" widths (currently stacks too eagerly given the 1100px breakpoint vs the 1600px max-width cap).
- Recycling KPIs scale smoothly without huge gaps.
- 27" monitor render unchanged (only narrow-viewport rules fire).
- No JS or template structural changes — pure CSS.
- All existing tests pass.
