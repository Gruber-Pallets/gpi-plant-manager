/* GPI Plant Manager — shared Gridstack glue for the widget dashboards
 * (/recycling and /wc/{slug}, plus their /tv/ variants).
 *
 * Load AFTER gridstack-all.js. Configured via data attributes on the
 * .grid-stack container:
 *   data-layout-page    URL segment for persistence — /api/layout/{page}
 *                       and /api/widget/{page}/{id} ("recycling" | "operator")
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

  const grid = GridStack.init({
    column: 12,
    cellHeight: 60,
    margin: tvMode ? tvMargin : 8,
    float: false,
    handle: '.grid-stack-item-content > h3, .grid-stack-item-content > .label',
    staticGrid: tvMode,
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
    window.addEventListener('resize', fitGridToViewport);
    return; // TVs are read-only — none of the editor wiring below applies.
  }

  const indicator = document.getElementById('save-indicator');
  let saveTimer = null;

  function persistLayout() {
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

  // WC picker (operator dashboard only): navigate to the chosen WC.
  const picker = document.getElementById('wc-picker');
  if (picker) {
    picker.addEventListener('change', (e) => {
      window.location.href = '/wc/' + e.target.value;
    });
  }

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
