/* GPI Plant Manager — inline-assign popover for "(no assignment)" bars.
 *
 * Shared by /recycling and /new (screen mode only — tv-mode.css hides the
 * .no-assign-btn triggers on TVs, so the templates gate the include behind
 * `{% if not tv_mode %}`). Configured via data attributes on its own
 * <script> tag:
 *   data-people    JSON array of active people names for the picker
 *   data-endpoint  POST target for the attribution
 *                  (defaults to /api/staffing/attribute)
 */
(function () {
  "use strict";
  var script = document.currentScript;
  var endpoint = (script && script.dataset.endpoint) || '/api/staffing/attribute';
  var people = [];
  try {
    people = JSON.parse((script && script.dataset.people) || '[]');
  } catch (e) {
    people = [];
  }
  if (!people || !people.length) return;
  var popover = null;
  var onEscape = null;
  function buildPopover() {
    var p = document.createElement('div');
    p.className = 'assign-popover';
    p.innerHTML = '<select class="assign-pick"><option value="">— pick person —</option>'
      + people.map(function (n) { return '<option value="' + n.replace(/"/g, '&quot;') + '">' + n + '</option>'; }).join('')
      + '</select> <button type="button" class="assign-save">Save</button>'
      + '<span class="assign-status" hidden></span>';
    document.body.appendChild(p);
    return p;
  }
  function close() {
    if (popover) { popover.remove(); popover = null; }
    document.removeEventListener('mousedown', onOutside, true);
    // Remove the Escape listener on EVERY close path (outside click,
    // re-open, save-reload), not just when Escape itself fired —
    // otherwise each open leaked one keydown listener.
    if (onEscape) { document.removeEventListener('keydown', onEscape); onEscape = null; }
  }
  function onOutside(e) {
    if (popover && !popover.contains(e.target) && !e.target.classList.contains('no-assign-btn')) close();
  }
  document.addEventListener('mousedown', function (e) {
    if (!e.target.classList.contains('no-assign-btn')) return;
    e.preventDefault(); e.stopPropagation();
    var btn = e.target;
    if (popover) close();
    popover = buildPopover();
    var rect = btn.getBoundingClientRect();
    popover.style.position = 'absolute';
    popover.style.left = (rect.left + window.scrollX) + 'px';
    popover.style.top = (rect.bottom + 4 + window.scrollY) + 'px';
    var sel = popover.querySelector('.assign-pick');
    var save = popover.querySelector('.assign-save');
    var status = popover.querySelector('.assign-status');
    save.addEventListener('click', function () {
      var person = sel.value;
      if (!person) { status.hidden = false; status.textContent = 'Pick a person.'; return; }
      save.disabled = true; sel.disabled = true; status.hidden = true;
      fetch(endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          day: btn.dataset.day,
          wc_name: btn.dataset.wc,
          person_name: person,
          start_utc: btn.dataset.start,
        }),
      }).then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            status.hidden = false; status.textContent = 'Saved ✓ ' + person;
            if (!(window.gpiTransferToast && window.gpiTransferToast(data.transfer))) {
              setTimeout(function () { location.reload(); }, 600);
            }
          } else {
            save.disabled = false; sel.disabled = false;
            status.hidden = false; status.textContent = 'Failed: ' + (data.error || 'unknown');
          }
        }).catch(function () {
          save.disabled = false; sel.disabled = false;
          status.hidden = false; status.textContent = 'Network error.';
        });
    });
    // close on Escape (close() removes the listener on every close path)
    onEscape = function (e2) { if (e2.key === 'Escape') close(); };
    document.addEventListener('keydown', onEscape);
    setTimeout(function () { document.addEventListener('mousedown', onOutside, true); }, 0);
  }, true);
})();
