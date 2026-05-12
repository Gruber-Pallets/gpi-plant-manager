# Scheduler Autosave + Slack Auto-Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual Save Draft / Edit-to-unlock flow with autosave, keep Publish on the same day, and have Post to Slack auto-publish (with confirm() for re-publishes).

**Architecture:** All edits route through a debounced `fetch` POST to the existing `/staffing` save endpoint. A small toolbar indicator shows clean/dirty/saving state. The frontend orchestrates the publish-then-share flow by calling the two existing endpoints sequentially. One-line route change keeps publish on the same day.

**Tech Stack:** FastAPI · Jinja2 · vanilla JS (no new deps)

---

## File Structure

**Modified files:**
- `src/zira_dashboard/routes/staffing.py` — publish redirect target
- `src/zira_dashboard/templates/staffing.html` — markup + JS + CSS for indicator, autosave controller, first-edit toast, postToSlack auto-publish flow, button removals, new JS globals

No new files.

---

## Task 1: Publish stays on the same day

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py` — the publish branch's redirect target

- [ ] **Step 1: Find the publish branch**

In `src/zira_dashboard/routes/staffing.py`, locate the POST handler that processes the form. The publish branch ends with a redirect to the next working day. The exact line currently looks like:

```python
return RedirectResponse(f"/staffing?day={next_day.isoformat()}", status_code=303)
```

(Search for `next_day.isoformat` — there's likely just one match.)

- [ ] **Step 2: Replace `next_day` with `d`**

Change to:

```python
return RedirectResponse(f"/staffing?day={d.isoformat()}", status_code=303)
```

`d` is the variable already in scope holding the current schedule's date. Don't remove the `_next_working_day` helper or any other code — the publish path just stops calling it.

- [ ] **Step 3: Smoke test**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py
git commit -m "feat(scheduler): keep day after publish (no auto-advance)"
```

---

## Task 2: Render new JS globals + autosave indicator + JS controller

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html`

- [ ] **Step 1: Render two new JS globals next to the existing `window.SCHEDULE_DAY`**

Find the `<script>` block in `staffing.html` containing:

```html
<script>
  window.PERSON_CERTS = {{ person_certs|tojson }};
  window.CERT_ICON_DATA = {{ cert_icon_data()|tojson }};
  window.SCHEDULE_DAY = {{ day|tojson }};
</script>
```

Append two lines:

```html
<script>
  window.PERSON_CERTS = {{ person_certs|tojson }};
  window.CERT_ICON_DATA = {{ cert_icon_data()|tojson }};
  window.SCHEDULE_DAY = {{ day|tojson }};
  window.SCHEDULE_VIEW_MODE = {{ view_mode|tojson }};
  window.SCHEDULE_PUBLISHED = {{ published|tojson }};
</script>
```

`view_mode` is already in the template context (the `staffing_page` route passes it). `published` is already a context variable too. No route changes needed for this step.

- [ ] **Step 2: Add autosave indicator markup**

Find the `.title-bar > .title-actions` div in `staffing.html` (the toolbar containing Print, Post to Slack, Publish buttons). The Posted/DRAFT pill area is just before this div, structured roughly:

```jinja
{% if published %}
  <span class="pub-pill on">Posted</span>
  <button type="button" id="edit-posted-btn" class="posted-edit-btn">Edit</button>
{% else %}
  <span class="draft-label">DRAFT</span>
  <button type="submit" name="action" value="save" class="save-draft-btn">Save Draft</button>
{% endif %}
```

Immediately AFTER this if/else block (still inside the `.title-actions` container, before the Print button), insert:

```html
<span id="autosave-indicator" class="autosave clean" aria-live="polite"></span>
```

Don't remove the existing pill/buttons in this task — Task 5 strips them.

- [ ] **Step 3: Add autosave indicator CSS**

In the inline `<style>` block at the top of `staffing.html` (or `static/staffing.css` — pick whichever the template's CSS already uses for the toolbar), append:

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

- [ ] **Step 4: Add the autosave JS controller**

Find the existing `<script>` block in `staffing.html` (the large one toward the bottom of the file containing `printSchedule`, `postToSlack`, `showToast`, etc.). Append this IIFE near the top of that block (so it sets up before other handlers):

```js
// ---------- Autosave controller ----------
// Debounced fetch POST of the scheduler form. Three states reflected
// in #autosave-indicator: clean (hidden), dirty (red dot), saving
// (spinner). Exposes window.flushAutosave() for the publish/share
// flow to await any in-flight save.
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
    const url = form.getAttribute('action')
      || (window.location.pathname + window.location.search);
    inFlight = fetch(url, {
      method: 'POST',
      body: formData,
      headers: { 'Accept': 'application/json' },
    })
      .then(r => {
        if (!r.ok && !(r.status >= 300 && r.status < 400)) {
          throw new Error('HTTP ' + r.status);
        }
        return r;
      })
      .then(() => {
        inFlight = null;
        if (queued) {
          queued = false;
          fireSave();
        } else {
          setState('clean');
        }
      })
      .catch(err => {
        inFlight = null;
        setState('dirty');
        if (window.showToast) {
          showToast('Autosave failed: ' + (err.message || 'unknown'), null, 'error');
        }
      });
    return inFlight;
  }

  function onEdit() {
    setState('dirty');
    if (inFlight) {
      queued = true;
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

  // Exposed so the publish/share flow can await any pending save
  // before submitting.
  window.flushAutosave = function () {
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
      // Fire immediately if not already saving.
      if (!inFlight) fireSave();
    }
    return inFlight || Promise.resolve();
  };
})();
```

- [ ] **Step 5: Smoke tests**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('staffing.html')"
```
Expected: no exception.

If you can run the dev server with a live `DATABASE_URL`, also browse to `/staffing` and verify:
- Open DevTools, check that `window.SCHEDULE_VIEW_MODE` and `window.SCHEDULE_PUBLISHED` are defined.
- Make any edit (e.g., toggle a person in a WC). After ~750ms a POST to `/staffing?day=...` fires (visible in Network tab); the autosave-indicator briefly shows "Saving…" then disappears.

If you can't run the server, the import + template-compile checks are the substitute.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html
git commit -m "feat(scheduler): autosave indicator + debounced fetch save"
```

---

## Task 3: First-edit-on-posted toast

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html`

- [ ] **Step 1: Add the first-edit toast IIFE**

In the same `<script>` block where Task 2 added the autosave controller, append (AFTER the autosave controller — order matters because the toast handler also listens to input events):

```js
// ---------- First-edit-on-posted: one-time toast + drop ?view=posted ----------
// When the page is loaded with ?view=posted, the first input/change
// event flips us back to draft mode silently after a brief toast.
// Subsequent edits in the same session are silent.
(function () {
  if (window.SCHEDULE_VIEW_MODE !== 'posted') return;
  const form = document.getElementById('staffing-form');
  if (!form) return;

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
    const url = new URL(window.location.href);
    url.searchParams.delete('view');
    history.replaceState({}, '', url.toString());
    const pill = document.querySelector('.title-bar .pub-pill.on');
    if (pill) pill.style.display = 'none';
  }
  form.addEventListener('input', onFirstEdit);
  form.addEventListener('change', onFirstEdit);
})();
```

- [ ] **Step 2: Verify `showToast` accepts a third "severity" arg `'info'`**

Read the existing `showToast(message, link, severity)` definition in the same `<script>` block. Today it treats `severity === 'error'` as red and everything else as green. The toast in this task uses `'info'` — that'll fall into the green/non-error branch. Cosmetically green is fine for an info-level message. If you want a separate neutral style, that's a follow-up; for now, leave green as the default.

- [ ] **Step 3: Smoke tests**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('staffing.html')"
```
Expected: no exception.

If you can run the dev server with `DATABASE_URL`, also test the flow:
- Publish any day's schedule.
- Reload `/staffing?day=<that day>&view=posted`. The Posted pill is visible.
- Make any edit. The toast appears; URL drops `view=posted`; Posted pill hides.
- Make a second edit. No toast.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html
git commit -m "feat(scheduler): first-edit on posted view drops back to draft (one-time toast)"
```

---

## Task 4: Post to Slack auto-publish

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html`

- [ ] **Step 1: Replace the existing `postToSlack` function**

Find the existing `async function postToSlack(btn)` in the `<script>` block. Replace its entire body with the auto-publish flow:

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

    // Re-publish confirmation if the schedule has been published before.
    if (window.SCHEDULE_PUBLISHED) {
      const ok = confirm(
        'Re-publish and post to Slack? Anyone with the previous version will see the revised schedule.'
      );
      if (!ok) return;
    }

    // Make sure any pending autosave finishes before we publish.
    if (window.flushAutosave) {
      await window.flushAutosave();
    }

    // Step 1: publish via the form's POST endpoint with action=publish.
    const form = document.getElementById('staffing-form');
    const fd = new FormData(form);
    fd.set('action', 'publish');
    const formAction = form.getAttribute('action')
      || ('/staffing?day=' + encodeURIComponent(day));
    const pubRes = await fetch(formAction, {
      method: 'POST',
      body: fd,
      headers: { 'Accept': 'application/json' },
    });
    // 303 redirect on success; treat any 2xx/3xx as success, 4xx/5xx as error.
    if (pubRes.status >= 400) {
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

- [ ] **Step 2: Smoke tests**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('staffing.html')"
```
Expected: no exception.

If you can run the dev server with `DATABASE_URL`, test:
- Click Post to Slack on a never-published day. Verify it publishes silently then posts (toast: "Published & posted to #mgmt-sups").
- Click Post to Slack on an already-published day with edits. Verify the confirm() dialog. Cancel: nothing happens. OK: re-publish + post.

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html
git commit -m "feat(scheduler): Post to Slack auto-publishes (with re-publish confirm)"
```

---

## Task 5: Remove Save Draft + Edit buttons

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html`

- [ ] **Step 1: Find and remove the Save Draft button**

In the toolbar's draft branch, the line:

```html
<button type="submit" name="action" value="save" class="save-draft-btn">Save Draft</button>
```

Delete it entirely. The `<span class="draft-label">DRAFT</span>` stays as the visible "draft" indicator (the autosave indicator from Task 2 augments it).

- [ ] **Step 2: Find and remove the Edit button (posted branch)**

```html
<button type="button" id="edit-posted-btn" class="posted-edit-btn">Edit</button>
```

Delete this `<button>`. The `<span class="pub-pill on">Posted</span>` stays — Task 3's first-edit handler hides it on first edit.

- [ ] **Step 3: Find and remove the Edit button click handler**

In the `<script>` block, search for `edit-posted-btn`. There's a small click handler that swaps `?view=posted` → `?view=draft`. Delete that entire event listener (or the whole IIFE if it lives in its own block).

- [ ] **Step 4: Verify no orphan references remain**

Run:

```bash
grep -nE "save-draft-btn|edit-posted-btn|class=\"posted-edit-btn\"" src/zira_dashboard/templates/staffing.html
```
Expected: zero matches.

- [ ] **Step 5: Smoke tests**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('staffing.html')"
```
Expected: no exception.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html
git commit -m "feat(scheduler): remove Save Draft + Edit buttons (autosave replaces them)"
```

---

## Task 6: Smoke test on live deploy

This task is operator-only — push and walk through every flow in a browser.

- [ ] **Step 1: Push**

```bash
git push
```

Wait for Railway to redeploy (~2-3 min — pure template/route changes).

- [ ] **Step 2: Verify publish stays on the same day**

Pick a day, make any edit, click Publish. URL should stay at `/staffing?day=<that same day>` (no advance).

- [ ] **Step 3: Verify autosave on every edit**

On any day, watch the toolbar near the Publish button:
- Make an edit (toggle a person in a WC, type in the day-notes textarea, etc.).
- Within ~750ms, the indicator briefly shows "Saving…" then becomes invisible (clean state).
- Refresh the page — the edit should persist.

- [ ] **Step 4: Verify no Save Draft / Edit buttons**

The toolbar should have only: DRAFT or Posted pill, autosave indicator, Print, Post to Slack, Publish/Re-publish. No Save Draft. No Edit.

- [ ] **Step 5: Verify first-edit-on-posted toast**

- Publish the day's schedule.
- Navigate to `/staffing?day=<that day>&view=posted`. Posted pill shows.
- Make any edit. Toast appears: "Switched to draft — Re-publish to update the posted version."
- URL drops `view=posted`. Posted pill hides.
- Edit again. No additional toast.

- [ ] **Step 6: Verify Re-publish label persists**

- After Step 5, the Publish button still reads "Re-publish" (because the schedule HAS been published, regardless of the dropped view param).
- After clicking Re-publish, URL stays at the same day (Step 2 verified). Button still reads "Re-publish".

- [ ] **Step 7: Verify Post to Slack auto-publish**

- On a never-published day: edit something. Click Post to Slack. No confirm dialog. Toast: "Published & posted to #mgmt-sups". The day's schedule is now published.
- On the same day after publish: edit something. Click Post to Slack. Confirm dialog appears: "Re-publish and post to Slack? Anyone with the previous version will see the revised schedule." Cancel: nothing happens. OK: re-publishes + posts.

- [ ] **Step 8: Done**

If steps 2–7 pass, the feature is shipped. If anything looks off, follow up with a targeted fix.

---

## Acceptance Recap

After all tasks merge and deploy:

- ✅ Publish stays on the same day (no auto-advance).
- ✅ Every edit autosaves within ~750ms; toolbar indicator reflects clean / dirty (red dot, "Unsaved") / saving (spinner, "Saving…").
- ✅ Save Draft button is gone. Edit button is gone.
- ✅ Loading `?view=posted` and editing → one-time toast + drop view param + hide Posted pill. No further toasts.
- ✅ Publish button reads "Publish" pre-publish, "Re-publish" once the schedule has been published at least once.
- ✅ Post to Slack always publishes first; for already-published schedules, asks confirm() before proceeding.
