/* GPI Plant Manager — shared Gridstack glue for the widget dashboards
 * (/recycling and /wc/{slug}, plus their /tv/ variants).
 *
 * Load AFTER gridstack-all.js. Configured via data attributes on the
 * .grid-stack container:
 *   data-layout-page    URL segment for persistence — /api/layout/{page}
 *                       and /api/widget/{page}/{id}
 *                       ("recycling" | "new" | "operator")
 *   data-tv-mode        "1" on /tv/* — static grid + fit-to-viewport
 *   data-fallback-rows  safety row count used only if the grid has no items
 */
(function () {
  "use strict";

  const gridEl = document.querySelector('.grid-stack');
  if (!gridEl) return;
  const layoutPage = gridEl.dataset.layoutPage;
  const tvMode = gridEl.dataset.tvMode === '1';
  const fallbackRows = parseInt(gridEl.dataset.fallbackRows || '30', 10);
  const tvMargin = 2;
  // Opted-in production dashboards have a separate phone presentation. Never initialize or save
  // the desktop grid there; crossing the breakpoint starts the correct mode.
  const productionPhone = !tvMode
    && (document.documentElement.hasAttribute('data-recycling-mobile')
      || document.documentElement.hasAttribute('data-production-mobile'))
    ? window.matchMedia('(max-width: 760px)') : null;
  let switchingPresentation = false;
  if (productionPhone) {
    let wasPhone = productionPhone.matches;
    productionPhone.addEventListener('change', event => {
      if (event.matches === wasPhone) return;
      wasPhone = event.matches;
      switchingPresentation = true;
      window.location.reload();
    });
    if (productionPhone.matches) return;
  }

  const grid = GridStack.init({
    column: 12,
    cellHeight: 60,
    margin: tvMode ? tvMargin : 8,
    float: false,
    handle: '.grid-stack-item-content > h3, .grid-stack-item-content > .label',
    staticGrid: tvMode || !!(window.gpiAccess && !window.gpiAccess.admin),
  });

  if (tvMode) {
    function fitGridToViewport() {
      // 1. How many rows of grid does the actual layout occupy?
      //    Initialize to 0 so we measure the TRUE max from the saved
      //    layout — initializing to the default-layout extent made
      //    layouts smaller than the default leave dead space at the
      //    bottom.
      const items = grid.save(false);
      let maxRows = 0;
      for (const it of items) {
        if (it.id) {
          const bottom = (it.y || 0) + (it.h || 1);
          if (bottom > maxRows) maxRows = bottom;
        }
      }
      if (maxRows < 1) maxRows = fallbackRows;  // safety fallback only if grid is empty
      // 2. Measure ACTUAL TV-header height (it scales with root font).
      const headerEl = document.querySelector('.tv-header');
      const headerHeight = headerEl ? Math.ceil(headerEl.getBoundingClientRect().height) : 80;
      // 3. Subtract header + per-row margin + a few px of safety from viewport.
      const safety = 8;
      const available = window.innerHeight - headerHeight - (maxRows - 1) * tvMargin - safety;
      const target = Math.max(16, Math.floor(available / maxRows));
      grid.cellHeight(target);
    }
    // Run now (so cellHeight is correct before paint) AND again after
    // the next frame in case the TV header height changed once fonts
    // rendered. Cheap, and handles edge cases where the first call
    // measures pre-font-load.
    fitGridToViewport();
    requestAnimationFrame(fitGridToViewport);
    // Fonts change the measured TV-header height; re-fit once they settle so
    // cellHeight isn't computed against a stale (pre-font) header measurement.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(fitGridToViewport);
    }
    window.addEventListener('resize', fitGridToViewport);
    return; // TVs are read-only — none of the editor wiring below applies.
  }

  // WC picker (operator dashboard only): navigate to the chosen WC.
  const picker = document.getElementById('wc-picker');
  if (picker) {
    picker.addEventListener('change', (e) => {
      const params = new URLSearchParams(window.location.search);
      const day = params.get('day');
      const next = new URL('/wc/' + e.target.value, window.location.origin);
      if (day) next.searchParams.set('day', day);
      window.location.href = next.pathname + next.search;
    });
  }

  if (window.gpiAccess && !window.gpiAccess.admin) return;

  const indicator = document.getElementById('save-indicator');
  let saveTimer = null;

  function persistLayout() {
    if (switchingPresentation || (productionPhone && productionPhone.matches)) return;
    const items = grid.save(false).map(it => ({
      id: it.id,
      x: it.x, y: it.y, w: it.w, h: it.h,
    })).filter(it => it.id);
    fetch('/api/layout/' + layoutPage, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(items),
    }).then(r => {
      if (r.ok) {
        indicator.textContent = 'Saved';
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
          indicator.textContent = 'Drag / resize — layout auto-saves';
        }, 1500);
      } else {
        indicator.textContent = 'Save failed';
      }
    }).catch(() => indicator.textContent = 'Save failed (network)');
  }

  grid.on('change', persistLayout);
  grid.on('resizestop', persistLayout);
  grid.on('dragstop', persistLayout);

  document.getElementById('reset-layout').addEventListener('click', () => {
    fetch('/api/layout/' + layoutPage, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify([]),
    }).then(() => location.reload());
  });

  // Per-widget edit controls — attached to window because
  // _widget_edit_controls.html wires them via inline onclick= handlers.
  window.openEdit = function (btn) {
    const content = btn.closest('.grid-stack-item-content');
    content.querySelector('.widget-edit').hidden = false;
  };
  window.closeEdit = function (btn) {
    const content = btn.closest('.grid-stack-item-content');
    content.querySelector('.widget-edit').hidden = true;
  };
  window.saveWidget = function (btn, id) {
    const panel = btn.closest('.widget-edit');
    const cfg = {};
    panel.querySelectorAll('input[name], select[name]').forEach(el => {
      const k = el.name;
      if (el.type === 'checkbox') {
        cfg[k] = el.checked;
      } else {
        cfg[k] = el.value;
      }
    });
    fetch('/api/widget/' + layoutPage + '/' + encodeURIComponent(id), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cfg),
    }).then(r => { if (r.ok) location.reload(); });
  };
  window.resetWidget = function (btn, id) {
    fetch('/api/widget/' + layoutPage + '/' + encodeURIComponent(id), {method: 'DELETE'})
      .then(r => { if (r.ok) location.reload(); });
  };

  // Prevent Gridstack from starting a drag when clicking inside the edit panel
  document.querySelectorAll('.widget-edit, .widget-edit-btn').forEach(el => {
    el.addEventListener('mousedown', e => e.stopPropagation());
    el.addEventListener('touchstart', e => e.stopPropagation());
  });
})();
