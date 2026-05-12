# Scheduler Autosave + Slack Auto-Publish — Design

**Date:** 2026-04-30
**Status:** Approved (brainstorming → implementation planning)

## Context

The Plant Scheduler page (`/staffing`) currently uses an explicit
save/publish flow:

- A **Save Draft** button posts the form with `action=save` to persist
  the current edits as a draft.
- A **Publish** button posts the form with `action=publish` and
  redirects to **the next working day** — annoying because the user
  is usually still verifying what they just published.
- An **Edit** button appears when viewing a previously-published
  schedule's snapshot (`?view=posted`); clicking it switches back to
  draft mode.
- A **Post to Slack** button (icon-only) renders the current schedule
  to PDF and uploads to `#mgmt-sups` — but does NOT publish.

Dale wants the workflow to feel like Google Docs / Notion: edits
autosave silently, no manual Save Draft button, no Edit button to
"unlock" a posted view. The Post to Slack button should also
automatically publish (with a confirmation when re-publishing) so
sharing the day's schedule is one click.

## Goals

1. **Publish stays on the same day.** No auto-advance to the next
   working day after a successful publish.
2. **Autosave on every edit.** Any edit to assignments, time-off,
   custom hours, or day notes is debounced ~750ms then POST'd to the
   existing `/staffing` save endpoint via `fetch`.
3. **Visible dirty/saving indicator.** A small pill near the Publish
   button shows three states: clean (hidden / brief green check),
   dirty (red dot), saving (spinner + "Saving…").
4. **Remove Save Draft + Edit buttons.** Autosave makes Save Draft
   redundant. With autosave, editing IS the path back to draft mode,
   so the explicit Edit button is unnecessary.
5. **First edit on posted view → one-time toast + auto-drop to
   draft.** Show "Switched to draft — Re-publish to update the
   posted version" once per session, then proceed silently.
6. **Re-publish label kept.** Existing template already toggles
   between "Publish" and "Re-publish" based on the schedule's
   `published` flag — verify it still works through autosave changes.
7. **Post to Slack auto-publishes.** Clicking Post to Slack first
   POSTs the form via the publish endpoint, then on success POSTs
   the share-to-slack endpoint. If the schedule was already published
   (re-publish path), show a `confirm()` dialog first.

## Non-goals

- A persistent autosave history / undo across sessions. The existing
  in-page Undo/Redo continues to work; nothing new there.
- Optimistic UI for the autosave fetch. Server is the source of
  truth; the indicator reflects the actual save round-trip.
- Conflict detection for concurrent editors. Single-user scheduler
  for now; YAGNI.
- Server-side combined publish+share endpoint. The frontend
  orchestrates the two existing endpoints sequentially.
- Removing the database `published_snapshot` column. Posted snapshots
  still get saved on publish for the view-toggle and audit trail.
- Touching the per-WC notes textarea behavior — those still autosave
  with the rest of the form.

## Design

### Component 1 — Publish stays on the same day

In `routes/staffing.py`'s POST handler for the form, the publish
branch currently ends with:

```python
return RedirectResponse(f"/staffing?day={next_day.isoformat()}", status_code=303)
```

Change to:

```python
return RedirectResponse(f"/staffing?day={d.isoformat()}", status_code=303)
```

`next_day` and the helper that computes it stay defined for any
other use sites — the publish path just stops calling it.

### Component 2 — Autosave on every edit (frontend)

A new JS controller in the existing `staffing.html` `<script>` block:

```js
(function () {
  const form = document.getElementById('staffing-form');
  if (!form) return;

  const indicator = document.getElementById('autosave-indicator');
  const DEBOUNCE_MS = 750;
  let debounceTimer = null;
  let inFlight = null;
  let queued = false;

  function setState(state) {
    if (!indicator) return;
    indicator.classList.remove('clean', 'dirty', 'saving');
    indicator.classList.add(state);
    indicator.dataset.state = state;
  }

  function fireSave() {
    setState('saving');
    const formData = new FormData(form);
    formData.set('action', 'save');
    inFlight = fetch(form.action || window.location.pathname + window.location.search, {
      method: 'POST',
      body: formData,
      headers: { 'Accept': 'application/json' },
    })
      .then(r => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r;
      })
      .then(() => {
        inFlight = null;
        if (queued) {
          queued = false;
          fireSave();          // immediately fire the queued save
        } else {
          setState('clean');
        }
      })
      .catch(err => {
        inFlight = null;
        setState('dirty');     // stay dirty so retry happens on next edit
        if (window.showToast) {
          showToast('Autosave failed: ' + (err.message || 'unknown'), null, 'error');
        }
      });
  }

  function onEdit() {
    setState('dirty');
    if (inFlight) {
      queued = true;            // already saving — queue another after
      return;
    }
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      debounceTimer = null;
      fireSave();
    }, DEBOUNCE_MS);
  }

  form.addEventListener('input', onEdit);
  form.addEventListener('change', onEdit);

  // Hook publish + share buttons to flush autosave so they don't
  // race with an in-flight or pending save.
  window.flushAutosave = function () {
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    return inFlight || Promise.resolve();
  };
})();
```

The form's existing POST URL pattern (`<form method="post"
action="/staffing?day=...">`) means the autosave fetch posts back to
the same handler. The handler reads `action=save` and stores the
draft (existing path).

### Component 3 — Dirty indicator markup + CSS

Inserted in the toolbar, between the Posted/DRAFT pill and the
Print button:

```html
<span id="autosave-indicator" class="autosave clean" aria-live="polite"></span>
```

CSS:

```css
.autosave {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 0.75rem;
  font-weight: 600;
  margin-left: 0.5rem;
  min-width: 1.2rem;
  height: 1.2rem;
}
.autosave.clean {
  /* hidden — most of the time the indicator just isn't there */
  display: none;
}
.autosave.dirty {
  display: inline-flex;
  color: #dc2626;
}
.autosave.dirty::before {
  content: "";
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #dc2626;
}
.autosave.dirty::after {
  content: "Unsaved";
  font-size: 0.72rem;
  color: #6b7280;
}
.autosave.saving {
  display: inline-flex;
  color: #6b7280;
}
.autosave.saving::before {
  content: "";
  display: inline-block;
  width: 10px;
  height: 10px;
  border: 1.5px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: autosave-spin 0.8s linear infinite;
}
.autosave.saving::after {
  content: "Saving…";
  font-size: 0.72rem;
}
@keyframes autosave-spin {
  to { transform: rotate(360deg); }
}
```

State transitions are driven entirely by the JS controller in
Component 2 — no server work.

### Component 4 — Remove Save Draft + Edit buttons

In `staffing.html`'s `.title-bar` toolbar:

- Delete the `<button type="submit" name="action" value="save"
  class="save-draft-btn">Save Draft</button>` element.
- Delete the `<button type="button" id="edit-posted-btn"
  class="posted-edit-btn">Edit</button>` element.
- Delete any JS that handles `#edit-posted-btn` (a small click
  handler that swaps `?view=posted` to `?view=draft`).

The Posted pill (`<span class="pub-pill on">Posted</span>`) stays
visible when `published === true` and `view_mode === 'posted'`. It
gets hidden by JS the first time the user edits in posted view (see
Component 5).

### Component 5 — First-edit-on-posted toast

A small page-init script:

```js
(function () {
  const form = document.getElementById('staffing-form');
  if (!form) return;
  // viewing_posted is rendered into a data attribute or
  // window.SCHEDULE_VIEW_MODE so JS knows the current mode.
  if (window.SCHEDULE_VIEW_MODE !== 'posted') return;

  let warned = false;
  function onFirstEdit() {
    if (warned) return;
    warned = true;
    if (window.showToast) {
      showToast(
        'Switched to draft — Re-publish to update the posted version.',
        null,
        'info'
      );
    }
    // Drop ?view=posted from the URL.
    const url = new URL(window.location.href);
    url.searchParams.delete('view');
    history.replaceState({}, '', url.toString());
    // Hide the Posted pill in the toolbar; the dirty indicator from
    // Component 3 will take its place visually.
    const pill = document.querySelector('.title-bar .pub-pill.on');
    if (pill) pill.style.display = 'none';
  }
  form.addEventListener('input', onFirstEdit);
  form.addEventListener('change', onFirstEdit);
})();
```

The route handler renders `window.SCHEDULE_VIEW_MODE = '{{ view_mode
}}'` in the existing `<script>` block right next to
`window.SCHEDULE_DAY`.

### Component 6 — Re-publish label

Already implemented in `staffing.html`:

```html
<button type="submit" name="action" value="publish" class="publish-btn">
  {{ 'Re-publish' if published else 'Publish' }}
</button>
```

`published` is True the moment a schedule has been posted at least
once today, regardless of whether subsequent edits were made. So
after the first publish, the button stays "Re-publish" even after
the first-edit-toast in Component 5 drops the user back to draft.
No additional logic needed — verify with smoke test.

### Component 7 — Post to Slack auto-publishes

Modify the existing `postToSlack()` JS function in `staffing.html`.
Current behavior: POST `/staffing/share-to-slack` directly. New
behavior:

```js
async function postToSlack(btn) {
  const originalContent = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span style="font-size:0.85rem;font-weight:600">Posting…</span>';
  try {
    const day = window.SCHEDULE_DAY
      || new URLSearchParams(window.location.search).get('day');
    if (!day) {
      showToast('No day available — refresh the page', null, 'error');
      return;
    }

    // Re-publish confirmation.
    if (window.SCHEDULE_PUBLISHED) {
      const ok = confirm(
        'Re-publish and post to Slack? Anyone with the previous version will see the revised schedule.'
      );
      if (!ok) return;
    }

    // Make sure any pending autosave finishes before publishing.
    if (window.flushAutosave) {
      await window.flushAutosave();
    }

    // Step 1: publish the current form.
    const form = document.getElementById('staffing-form');
    const fd = new FormData(form);
    fd.set('action', 'publish');
    const pubRes = await fetch(form.action || ('/staffing?day=' + encodeURIComponent(day)), {
      method: 'POST',
      body: fd,
      headers: { 'Accept': 'application/json' },
    });
    if (!pubRes.ok && !(pubRes.status >= 300 && pubRes.status < 400)) {
      // Treat 303 redirect as success; everything else is an error.
      throw new Error('Publish failed: HTTP ' + pubRes.status);
    }

    // Step 2: post the resulting PDF to Slack.
    const r = await fetch('/staffing/share-to-slack?day=' + encodeURIComponent(day), {
      method: 'POST',
      headers: { 'Accept': 'application/json' },
    });
    const data = await r.json();
    if (data.ok) {
      showToast('Published & posted to #' + data.channel_name, data.permalink);
    } else {
      showToast('Slack post failed: ' + data.error, null, 'error');
    }
  } catch (e) {
    showToast(e.message || 'Slack post failed', null, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalContent;
  }
}
```

The route handler renders two new globals next to the existing
`window.SCHEDULE_DAY`:

```html
<script>
  window.SCHEDULE_DAY = {{ day|tojson }};
  window.SCHEDULE_VIEW_MODE = {{ view_mode|tojson }};
  window.SCHEDULE_PUBLISHED = {{ published|tojson }};
</script>
```

The redirect-following behavior: a 303 redirect from the publish
POST is followed by `fetch` automatically (it lands on the new
`/staffing?day=...` GET, returns 200). The 200 means publish
succeeded.

`flushAutosave()` is exposed as `window.flushAutosave` by the
autosave controller (Component 2). It clears any pending debounce
timer and returns the in-flight save's promise so we await it before
hitting publish.

### Edge case — what if there's nothing to publish?

Today, clicking Publish always tries to save+publish whatever's in
the form. With autosave continuously persisting drafts, every
publish is meaningful. Even on a truly empty schedule (no
assignments), the publish handler creates a posted snapshot and
returns 303. No new edge case introduced.

## Acceptance criteria

- After a successful publish, the URL stays at `/staffing?day=<the
  day you just published>` (no advance to next day).
- Editing any input in the form (assignment toggle, time-off pill,
  custom hour, day note, WC note) triggers autosave within ~750ms.
- The autosave indicator goes from clean → dirty (red dot, "Unsaved")
  → saving (spinner + "Saving…") → clean.
- Save Draft button is gone; Edit button is gone; both removed from
  the toolbar with no leftover handlers.
- When the page loads with `?view=posted` and the user makes an
  edit, a one-time toast appears: "Switched to draft — Re-publish to
  update the posted version." The URL drops the `view=posted` param
  and the Posted pill hides. Subsequent edits in the same session
  fire no further toasts.
- The Publish button reads "Publish" for never-published schedules
  and "Re-publish" once a schedule has been published at least
  once — even after the toast drops the user from posted view back
  to draft.
- Clicking Post to Slack:
  - For a never-published schedule: silently publishes, then posts
    the PDF to `#mgmt-sups`, shows the success toast.
  - For an already-published schedule: shows a confirm() dialog
    first; on cancel, nothing happens; on OK, re-publishes then
    posts.
- Network/server errors during autosave: the indicator stays dirty
  and an error toast appears. Next edit retries.
- Network/server errors during the Slack two-step flow: error toast
  with the specific failure reason. No half-state rollback attempted.

## Risks

- **Autosave races with publish.** Mitigation: `flushAutosave()` is
  awaited before publish/share fires. In-flight save completes
  first; debounce timer is cleared.
- **First-edit-on-posted detection.** The check uses
  `window.SCHEDULE_VIEW_MODE === 'posted'`. If the route ever
  renders the page in posted view without setting this global, the
  toast won't fire and edits silently overwrite. Mitigation:
  templates render the global unconditionally on every page load.
- **303 redirect parsing in `fetch`.** Some browsers expose the
  redirect's status differently. Mitigation: treat any 200-or-3xx
  response from the publish POST as success; only 4xx/5xx is an
  error.
- **`confirm()` dialog UX.** Browser-native confirm dialogs are
  ugly but reliable. Acceptable for a manager-facing dashboard.
  Could later swap to a custom modal if Dale wants prettier; YAGNI.
- **Many concurrent edits across multiple browser tabs.** The
  autosave fetch always sends the WHOLE form, so the last save
  wins. Two tabs editing simultaneously would yo-yo the schedule.
  Single-user product → not a real concern today.

## File touch list

- Modify: `src/zira_dashboard/routes/staffing.py`
  - Publish branch: redirect to same day instead of next working day.
- Modify: `src/zira_dashboard/templates/staffing.html`
  - Remove Save Draft button HTML.
  - Remove Edit button HTML + click handler JS.
  - Add `<span id="autosave-indicator">` markup in toolbar.
  - Add autosave CSS rules in the inline `<style>` block (or in
    `static/staffing.css`).
  - Add autosave JS controller (Component 2).
  - Add first-edit-on-posted JS (Component 5).
  - Update `postToSlack()` JS for the auto-publish flow (Component 7).
  - Render `window.SCHEDULE_VIEW_MODE` and `window.SCHEDULE_PUBLISHED`
    globals next to existing `window.SCHEDULE_DAY`.
- Modify: `src/zira_dashboard/static/staffing.css`
  - Optionally house the new autosave CSS rules instead of inline
    (decided during implementation; either fine).
