# Send-Feedback-First Light Bulb Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the header light bulb open one tabbed modal on Send feedback, with My feedback and What’s new available in the same window.

**Architecture:** `_feedback.html` becomes the only light-bulb modal and contains three accessible tab panels. `feedback.js` owns modal, tab, draft, form, and personal-feedback state; `footer.js` keeps the header launcher and changelog read-state logic but exposes that logic to the shared modal instead of opening a second popup. Existing feedback and changelog endpoints remain unchanged.

**Tech Stack:** FastAPI/Jinja templates, browser-native JavaScript, CSS, pytest static tests, Playwright browser tests.

## Global Constraints

- The modal opens on **Send feedback** every time.
- The visible tabs are exactly **Send feedback**, **My feedback**, and **What’s new**.
- Switching tabs preserves an unfinished description, submitter selection, and screenshot.
- Closing clears the unsent draft and returns focus to the light-bulb opener.
- A confirmed submission refreshes My feedback, selects that tab, and does not close the modal.
- Repair continues to open `https://www.gpimaintenance.com/request` and creates no feedback record.
- The unread dot changes only through What’s new read state; opening the light bulb or My feedback must not clear it.
- No Odoo workflow, task creation, reference synchronization, endpoint, database model, or custom Odoo code changes.
- The three full tab labels may wrap but may not be shortened, clipped, or hidden.
- Preserve the six existing feedback choices and all existing submission fields.

---

## File Structure

- Modify `src/zira_dashboard/templates/_feedback.html` — single modal shell, tabs, three panels, retry controls, and existing Send/My content.
- Modify `src/zira_dashboard/templates/_footer.html` — remove the old changelog modal and keep the shared component include.
- Modify `src/zira_dashboard/static/feedback.js` — shared modal state, tabs, draft lifetime, submit-to-My transition, personal feedback loading, focus management.
- Modify `src/zira_dashboard/static/footer.js` — header light-bulb launcher plus reusable changelog loading/read-state controller for the What’s new panel.
- Modify `src/zira_dashboard/static/feedback.css` — modal, tabs, panels, retry state, and responsive layout.
- Modify `src/zira_dashboard/static/footer.css` — retain launcher and changelog-card styles; delete obsolete second-modal/header styles.
- Modify `tests/test_feedback_chooser_static.py` — template and JavaScript ownership contracts.
- Modify `tests/test_feedback_chooser_browser.py` — real browser behavior for tabs, draft lifetime, submission, Repair, focus, and failures.
- Modify `tests/test_whatsnew_panel_static.py` — launcher/changelog bridge and removal of the old modal.
- Modify `CHANGELOG.md` — child-readable release note.

---

### Task 1: Build the single accessible modal shell

**Files:**
- Modify: `src/zira_dashboard/templates/_feedback.html`
- Modify: `src/zira_dashboard/templates/_footer.html`
- Modify: `src/zira_dashboard/static/feedback.css`
- Modify: `src/zira_dashboard/static/footer.css`
- Test: `tests/test_feedback_chooser_static.py`
- Test: `tests/test_whatsnew_panel_static.py`

**Interfaces:**
- Consumes: existing Jinja `feedback_types_for_chooser()` and existing feedback type markup.
- Produces: `#lightbulb-modal`, `[role="tablist"]`, tabs `#lightbulb-tab-send`, `#lightbulb-tab-mine`, `#lightbulb-tab-news`, and matching panels `#lightbulb-panel-send`, `#lightbulb-panel-mine`, `#lightbulb-panel-news`.

- [ ] **Step 1: Write failing static structure tests**

Add exact assertions:

```python
def test_lightbulb_is_one_send_first_tabbed_modal():
    html = FEEDBACK_TEMPLATE.read_text(encoding="utf-8")
    assert html.count('role="dialog"') == 1
    assert 'id="lightbulb-modal"' in html
    assert 'role="tablist" aria-label="Light bulb sections"' in html
    assert 'id="lightbulb-tab-send"' in html
    assert '>Send feedback<' in html
    assert 'id="lightbulb-tab-mine"' in html
    assert '>My feedback<' in html
    assert 'id="lightbulb-tab-news"' in html
    assert '>What’s new<' in html
    assert 'id="lightbulb-panel-send"' in html
    assert 'id="lightbulb-panel-mine"' in html
    assert 'id="lightbulb-panel-news"' in html
    assert 'id="fb-view-modal"' not in html


def test_footer_has_no_second_lightbulb_modal():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="changelog-modal"' not in html
    assert 'id="fb-open"' not in html
    assert 'id="fb-view-open"' not in html
    assert html.count("{% include '_feedback.html' %}") == 1
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_feedback_chooser_static.py tests/test_whatsnew_panel_static.py
```

Expected: failures report the missing tab shell and the still-present `changelog-modal` / `fb-view-modal` markup.

- [ ] **Step 3: Replace the two modal shells with one tabbed shell**

Keep the existing chooser and detail form inside the Send panel. Use this exact outer structure in `_feedback.html`:

```html
<div id="lightbulb-modal" class="fb-modal" hidden>
  <div class="fb-backdrop" id="lightbulb-backdrop"></div>
  <div class="fb-card" role="dialog" aria-modal="true" aria-labelledby="lightbulb-title">
    <div class="fb-head">
      <h3 id="lightbulb-title">Light bulb</h3>
      <button type="button" id="lightbulb-close" class="fb-close" aria-label="Close">Close</button>
    </div>
    <div class="fb-tabs" role="tablist" aria-label="Light bulb sections">
      <button type="button" id="lightbulb-tab-send" class="fb-tab is-active"
              role="tab" aria-selected="true" aria-controls="lightbulb-panel-send">Send feedback</button>
      <button type="button" id="lightbulb-tab-mine" class="fb-tab"
              role="tab" aria-selected="false" aria-controls="lightbulb-panel-mine">My feedback</button>
      <button type="button" id="lightbulb-tab-news" class="fb-tab"
              role="tab" aria-selected="false" aria-controls="lightbulb-panel-news">What’s new</button>
    </div>
    <section id="lightbulb-panel-send" class="fb-panel" role="tabpanel"
             aria-labelledby="lightbulb-tab-send">
```

Keep the complete current `#fb-type-step` followed by the complete current `#fb-detail-step` immediately after that opening tag, then close the Send panel and add the other panels with this exact markup:

```html
    </section>
    <section id="lightbulb-panel-mine" class="fb-panel" role="tabpanel"
             aria-labelledby="lightbulb-tab-mine" hidden>
      <div id="fb-view-body" class="fb-view-body" aria-live="polite"></div>
      <button type="button" id="fb-view-retry" class="fb-retry" hidden>Retry</button>
    </section>
    <section id="lightbulb-panel-news" class="fb-panel" role="tabpanel"
             aria-labelledby="lightbulb-tab-news" hidden>
      <div class="changelog-toolbar">
        <button type="button" id="changelog-markall" class="changelog-markall">Mark all read</button>
      </div>
      <div id="changelog-body" class="changelog-body" aria-live="polite"></div>
      <button type="button" id="changelog-retry" class="fb-retry" hidden>Retry</button>
    </section>
  </div>
</div>
```

Remove the complete `#changelog-modal` block from `_footer.html`; leave the `_feedback.html` include and asset links once.

- [ ] **Step 4: Add tab and responsive styles, then remove obsolete second-modal styles**

Add to `feedback.css`:

```css
.fb-tabs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 4px; margin: 0 -0.25rem 1rem; padding: 4px; border-radius: 10px;
  background: var(--muted-bg, #f3f4f6); }
.fb-tab { min-width: 0; min-height: 44px; padding: 8px; border: 0;
  border-radius: 8px; background: transparent; color: var(--muted, #6b7280);
  font: inherit; font-weight: 600; white-space: normal; cursor: pointer; }
.fb-tab.is-active { background: var(--panel, #fff); color: var(--fg, #1f2937);
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.12); }
.fb-tab:focus-visible { outline: 2px solid var(--accent, #16a34a); outline-offset: 2px; }
.fb-panel[hidden] { display: none; }
.fb-retry { min-height: 44px; margin-top: 0.75rem; }
@media (max-width: 520px) {
  .fb-tabs { gap: 2px; }
  .fb-tab { padding-inline: 4px; }
}
```

Move the reusable `.changelog-toolbar`, `.changelog-markall`, `.cl-entry`, `.cl-*`, and `.changelog-body` rules into `feedback.css` or retain them in `footer.css` if their selectors remain global. Delete only `.changelog-modal`, `.changelog-backdrop`, `.changelog-card`, `.changelog-head`, `.changelog-head-actions`, and `.changelog-feedback-btn` rules that no longer have elements.

- [ ] **Step 5: Run static tests and verify GREEN**

Run the same pytest command from Step 2.

Expected: all selected tests pass.

- [ ] **Step 6: Commit the modal shell**

```bash
git add src/zira_dashboard/templates/_feedback.html src/zira_dashboard/templates/_footer.html src/zira_dashboard/static/feedback.css src/zira_dashboard/static/footer.css tests/test_feedback_chooser_static.py tests/test_whatsnew_panel_static.py
git commit -m "feat: combine light-bulb sections"
```

---

### Task 2: Add send-first tab and draft lifetime behavior

**Files:**
- Modify: `src/zira_dashboard/static/feedback.js`
- Modify: `src/zira_dashboard/static/footer.js`
- Test: `tests/test_feedback_chooser_browser.py`
- Test: `tests/test_feedback_chooser_static.py`

**Interfaces:**
- Consumes: Task 1 tab and panel IDs.
- Produces: `window.gpiLightbulb.open(opener)`, local `selectTab(name, options)`, and a single focus trap for the unified modal.

- [ ] **Step 1: Add failing browser tests for default tab, switching, draft preservation, close reset, and focus**

Create a shared document helper and tests using the existing Playwright fixture pattern:

```python
def _page_with_lightbulb(page):
    page.set_content(
        '<button id="feedback-opener">Open feedback</button>'
        + _render_enabled_chooser()
    )
    page.add_script_tag(content=FEEDBACK_JS.read_text(encoding="utf-8"))
    page.locator("#feedback-opener").evaluate(
        "button => button.addEventListener('click', () => window.gpiLightbulb.open(button))"
    )


def test_lightbulb_opens_send_first_and_switching_tabs_keeps_draft():
    # launch Chromium using the existing with/finally pattern
    _page_with_lightbulb(page)
    page.locator("#feedback-opener").click()
    assert page.locator("#lightbulb-tab-send").get_attribute("aria-selected") == "true"
    page.locator('[data-type="bug"]').click()
    page.locator("#fb-desc").fill("The count is wrong")
    page.locator("#lightbulb-tab-mine").click()
    page.locator("#lightbulb-tab-send").click()
    assert page.locator("#fb-desc").input_value() == "The count is wrong"


def test_close_clears_draft_and_reopen_returns_to_send():
    _page_with_lightbulb(page)
    page.locator("#feedback-opener").click()
    page.locator('[data-type="bug"]').click()
    page.locator("#fb-desc").fill("Unsaved")
    page.locator("#lightbulb-tab-mine").click()
    page.locator("#lightbulb-close").click()
    assert page.evaluate("document.activeElement.id") == "feedback-opener"
    page.locator("#feedback-opener").click()
    assert page.locator("#lightbulb-tab-send").get_attribute("aria-selected") == "true"
    page.locator('[data-type="bug"]').click()
    assert page.locator("#fb-desc").input_value() == ""
```

Add a keyboard test that ArrowRight moves among tabs, Escape closes, and focus returns to `#feedback-opener`.

- [ ] **Step 2: Run the browser tests and verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_feedback_chooser_browser.py
```

Expected: failures report missing `window.gpiLightbulb`, inactive tabs, or draft reset during switching.

- [ ] **Step 3: Implement one modal controller in `feedback.js`**

Replace the old separate send/view openers with these responsibilities:

```javascript
var activeTab = 'send';
var tabs = { send: $('lightbulb-tab-send'), mine: $('lightbulb-tab-mine'), news: $('lightbulb-tab-news') };
var panels = { send: $('lightbulb-panel-send'), mine: $('lightbulb-panel-mine'), news: $('lightbulb-panel-news') };

function selectTab(name, options) {
  options = options || {};
  activeTab = name;
  Object.keys(tabs).forEach(function (key) {
    var selected = key === name;
    tabs[key].classList.toggle('is-active', selected);
    tabs[key].setAttribute('aria-selected', selected ? 'true' : 'false');
    panels[key].hidden = !selected;
  });
  if (name === 'mine') loadMyFeedback(!!options.refresh);
  if (name === 'news' && window.gpiLightbulbChangelog) {
    window.gpiLightbulbChangelog.show();
  }
  if (options.focus !== false) {
    var target = panels[name].querySelector('button:not([hidden]), textarea, select, [tabindex]');
    (target || tabs[name]).focus();
  }
}

function openLightbulb(opener) {
  resetSendForm();
  loadSubmitters();
  selectTab('send', {focus: false});
  openModal($('lightbulb-modal'), opener, tabs.send);
}

window.gpiLightbulb = { open: openLightbulb };
```

Tab click and keyboard handlers must use `selectTab`. ArrowRight/ArrowLeft wrap across the three tabs. Update the existing focus trap, backdrop, Close, Cancel, and Escape handlers to target only `#lightbulb-modal`. Do not call `resetSendForm()` when changing tabs; call it only on open and close.

- [ ] **Step 4: Make the header launcher call the shared controller**

In `footer.js`, replace `btn.addEventListener('click', openPanel)` with:

```javascript
btn.addEventListener('click', function () {
  if (window.gpiLightbulb) window.gpiLightbulb.open(btn);
});
```

Remove old modal open/close wiring from `footer.js`; do not remove unread-dot startup polling.

- [ ] **Step 5: Run browser and static tests and verify GREEN**

```bash
.venv/bin/python -m pytest -q tests/test_feedback_chooser_browser.py tests/test_feedback_chooser_static.py tests/test_whatsnew_panel_static.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit tab behavior**

```bash
git add src/zira_dashboard/static/feedback.js src/zira_dashboard/static/footer.js tests/test_feedback_chooser_browser.py tests/test_feedback_chooser_static.py tests/test_whatsnew_panel_static.py
git commit -m "feat: open light bulb on send feedback"
```

---

### Task 3: Refresh My feedback after a confirmed submission

**Files:**
- Modify: `src/zira_dashboard/static/feedback.js`
- Modify: `src/zira_dashboard/templates/_feedback.html`
- Test: `tests/test_feedback_chooser_browser.py`

**Interfaces:**
- Consumes: Task 2 `selectTab('mine', {refresh: true})`.
- Produces: `loadMyFeedback(forceRefresh)` with independent loading, retry, and refresh state.

- [ ] **Step 1: Write failing browser tests for lazy loading, retry, successful submission, and failed submission**

Stub `window.gpiFetch` before loading `feedback.js`. Assert these exact behaviors:

```python
def test_successful_submit_switches_to_refreshed_my_feedback():
    # POST /feedback resolves {ok: True}; /api/feedback/mine returns the new item.
    page.locator("#feedback-opener").click()
    page.locator('[data-type="bug"]').click()
    page.locator("#fb-desc").fill("Count is wrong")
    page.locator("#fb-submit").click()
    page.locator("#lightbulb-tab-mine[aria-selected='true']").wait_for()
    assert page.locator("#fb-view-body").get_by_text("Count is wrong").is_visible()
    assert page.locator("#lightbulb-modal").get_attribute("hidden") is None


def test_failed_submit_keeps_form_and_draft():
    # POST resolves {ok: False, error: "Try again"}.
    page.locator("#fb-submit").click()
    assert page.locator("#lightbulb-tab-send").get_attribute("aria-selected") == "true"
    assert page.locator("#fb-desc").input_value() == "Count is wrong"
    assert page.get_by_text("Failed: Try again").is_visible()


def test_my_feedback_failure_is_retryable_without_hiding_other_tabs():
    # First mine request rejects; Retry succeeds.
    page.locator("#lightbulb-tab-mine").click()
    assert page.get_by_text("Could not load your feedback.").is_visible()
    page.locator("#fb-view-retry").click()
    assert page.get_by_text("Count is wrong").is_visible()
    assert page.locator("#lightbulb-tab-send").is_enabled()
```

Also assert `/api/feedback/mine` is not requested before the My feedback tab is selected or a submission succeeds.

- [ ] **Step 2: Run the new tests and verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_feedback_chooser_browser.py -k "submit or my_feedback"
```

Expected: the current code closes the modal after success and lacks retry state.

- [ ] **Step 3: Implement independent My feedback state**

Use explicit state and no timer-based close:

```javascript
var myFeedbackLoaded = false;
var myFeedbackLoading = false;

function loadMyFeedback(forceRefresh) {
  if (myFeedbackLoading || (myFeedbackLoaded && !forceRefresh)) return;
  myFeedbackLoading = true;
  $('fb-view-body').textContent = 'Loading…';
  $('fb-view-retry').hidden = true;
  window.gpiFetch('/api/feedback/mine')
    .then(function (response) { return response.json(); })
    .then(function (data) {
      renderMyFeedback(data);
      myFeedbackLoaded = true;
    })
    .catch(function () {
      $('fb-view-body').innerHTML = '<p class="fb-view-empty">Could not load your feedback.</p>';
      $('fb-view-retry').hidden = false;
    })
    .then(function () { myFeedbackLoading = false; });
}
```

Wire Retry to `loadMyFeedback(true)`. In the existing successful POST branch, remove `setTimeout(...closeModal...)` and use:

```javascript
if (status) status.textContent = 'Thanks — your feedback was saved.';
myFeedbackLoaded = false;
selectTab('mine', {refresh: true});
```

Do not clear the just-submitted form until `resetSendForm()` runs on the next close/open cycle. This preserves the accepted rule that tab switching itself never erases a draft.

- [ ] **Step 4: Run the focused tests and verify GREEN**

```bash
.venv/bin/python -m pytest -q tests/test_feedback_chooser_browser.py tests/test_feedback_chooser_static.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the personal-feedback transition**

```bash
git add src/zira_dashboard/static/feedback.js src/zira_dashboard/templates/_feedback.html tests/test_feedback_chooser_browser.py
git commit -m "feat: show submitted feedback status"
```

---

### Task 4: Put What’s new inside the modal without changing unread semantics

**Files:**
- Modify: `src/zira_dashboard/static/footer.js`
- Modify: `src/zira_dashboard/static/feedback.js`
- Test: `tests/test_feedback_chooser_browser.py`
- Test: `tests/test_whatsnew_panel_static.py`

**Interfaces:**
- Consumes: Task 1 `#changelog-body`, `#changelog-markall`, `#changelog-retry`; Task 2 `selectTab('news')`.
- Produces: `window.gpiLightbulbChangelog.show()`, `.retry()`, and unchanged header-dot behavior.

- [ ] **Step 1: Write failing tests for lazy load, unread preservation, read actions, and retry**

Add static assertions that `footer.js` exports `window.gpiLightbulbChangelog`, has no `openPanel`/`closePanel`, and still owns `changelog_cutoff`, `changelog_read`, `refreshDot`, `markAllRead`, and `wireCards`.

Add browser tests with `/changelog/latest` and `/changelog?fragment=1` stubs:

```python
def test_opening_lightbulb_and_my_feedback_do_not_clear_news_dot():
    page.locator("#feedback-opener").click()
    page.locator("#lightbulb-tab-mine").click()
    assert page.locator(".whatsnew-dot").is_visible()


def test_news_loads_on_first_visit_and_mark_all_clears_dot():
    assert page.evaluate("window.changelogRequests") == 0
    page.locator("#lightbulb-tab-news").click()
    assert page.evaluate("window.changelogRequests") == 1
    assert page.get_by_text("Floor ideas now use one shared review task").is_visible()
    page.locator("#changelog-markall").click()
    assert page.locator(".whatsnew-dot").is_hidden()


def test_news_failure_retries_without_closing_modal():
    page.locator("#lightbulb-tab-news").click()
    assert page.get_by_text("Could not load What’s new.").is_visible()
    page.locator("#changelog-retry").click()
    assert page.locator(".cl-entry").first.is_visible()
    assert page.locator("#lightbulb-modal").get_attribute("hidden") is None
```

- [ ] **Step 2: Run the changelog-focused tests and verify RED**

```bash
.venv/bin/python -m pytest -q tests/test_whatsnew_panel_static.py tests/test_feedback_chooser_browser.py -k "news or dot or changelog"
```

Expected: failures show the old modal-coupled controller and missing retry bridge.

- [ ] **Step 3: Refactor `footer.js` into a panel controller**

Keep local-storage functions, `refreshDot`, `applyReadState`, `refreshDotFromCards`, `wireCards`, and `markAllRead`. Replace `openPanel`/`closePanel` with:

```javascript
function showChangelog(forceRefresh) {
  body = document.getElementById('changelog-body');
  var retry = document.getElementById('changelog-retry');
  if (!body || (panelLoaded && !forceRefresh)) {
    if (body) applyReadState();
    return;
  }
  body.textContent = 'Loading…';
  if (retry) retry.hidden = true;
  window.gpiFetch('/changelog?fragment=1')
    .then(function (response) {
      if (!response.ok) throw new Error('changelog unavailable');
      return response.text();
    })
    .then(function (htmlText) {
      body.innerHTML = htmlText;
      panelLoaded = true;
      wireCards();
      applyReadState();
    })
    .catch(function () {
      body.innerHTML = '<p>Could not load What’s new.</p>';
      if (retry) retry.hidden = false;
    });
}

window.gpiLightbulbChangelog = {
  show: function () { showChangelog(false); },
  retry: function () { panelLoaded = false; showChangelog(true); }
};
```

Wire `#changelog-markall` once during initialization and `#changelog-retry` to `.retry()`. Do not call `setCutoff`, `setRead`, `applyReadState`, or `markAllRead` when the light bulb opens, Send feedback is active, or My feedback is selected.

- [ ] **Step 4: Wire What’s new selection without affecting unread state**

In `feedback.js`, selecting `news` calls `.show()` only. It must not clear the dot. The read state changes only through existing per-card Mark read actions or Mark all read.

- [ ] **Step 5: Run the focused tests and verify GREEN**

```bash
.venv/bin/python -m pytest -q tests/test_whatsnew_panel_static.py tests/test_feedback_chooser_browser.py tests/test_feedback_chooser_static.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the embedded changelog**

```bash
git add src/zira_dashboard/static/footer.js src/zira_dashboard/static/feedback.js tests/test_feedback_chooser_browser.py tests/test_whatsnew_panel_static.py
git commit -m "feat: show whats new inside light bulb"
```

---

### Task 5: Verify the complete flow and publish the user-facing note

**Files:**
- Modify: `CHANGELOG.md`
- Test: `tests/test_feedback_chooser_static.py`
- Test: `tests/test_feedback_chooser_browser.py`
- Test: `tests/test_whatsnew_panel_static.py`
- Test: `tests/test_feedback_routes.py`

**Interfaces:**
- Consumes: Tasks 1–4 complete implementation.
- Produces: verified, documented Send-feedback-first light-bulb behavior on `main`.

- [ ] **Step 1: Add the release note**

Add the newest child-readable entry:

```markdown
### Send feedback opens first

- **The light bulb now opens on Send feedback.** Use the tabs to see My feedback or What’s new without opening another window. After you send something, My feedback shows it and its status right away.
```

- [ ] **Step 2: Run focused feedback verification**

```bash
.venv/bin/python -m pytest -q tests/test_feedback_chooser_static.py tests/test_feedback_chooser_browser.py tests/test_whatsnew_panel_static.py tests/test_feedback_routes.py
```

Expected: all selected tests pass.

- [ ] **Step 3: Run the full repository checks**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
git diff --check
```

Expected: all tests and lint checks pass. If Playwright cannot launch inside the sandbox, rerun only the failed Playwright tests with the approved browser permission and require them to pass before continuing.

- [ ] **Step 4: Perform desktop and phone browser verification**

At a desktop viewport and at 360 px wide, verify:

1. The light bulb opens Send feedback.
2. All three full tab labels are readable without horizontal scrolling.
3. A Bug draft and screenshot survive a round trip through both other tabs.
4. Closing and reopening clears the draft.
5. Repair opens Maintenance and does not POST feedback.
6. A successful disposable local/test submission switches to My feedback.
7. My feedback and What’s new each recover from one simulated failed request.
8. Opening Send/My does not clear the unread dot; Mark all read does.
9. Tab arrows, Tab/Shift+Tab, Escape, and opener focus restoration work.

Expected: all nine checks pass with no console errors.

- [ ] **Step 5: Commit final verification note**

```bash
git add CHANGELOG.md
git commit -m "docs: announce send-first light bulb"
```

- [ ] **Step 6: Rebase, push, and verify the exact remote commit**

```bash
git fetch origin main
git rebase origin/main
git push origin HEAD:main
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

Expected: push is a fast-forward; local HEAD and remote `refs/heads/main` SHAs match exactly. Never force-push.
