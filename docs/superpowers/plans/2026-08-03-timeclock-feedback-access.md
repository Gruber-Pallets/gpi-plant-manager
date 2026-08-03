# Timeclock Feedback Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the existing screenshot-capable Bug / Feature request form on every Timeclock screen without loading desktop-only alerts or What's New behavior in the kiosk.

**Architecture:** Extract the feedback markup, CSS, and JavaScript from the desktop footer into a shared component. Include that component from both desktop and Timeclock bases, and place one persistent Timeclock trigger outside the HTMX swap target so it survives every kiosk navigation.

**Tech Stack:** Jinja2 templates, vanilla JavaScript and CSS, HTMX, pytest static contract tests.

## Global Constraints

- Preserve the existing `/feedback` and `/api/feedback/mine` endpoints and payloads.
- Preserve desktop Send feedback and View Feedback behavior and appearance.
- Keep Timeclock feedback markup and handlers outside `#timeclock-screen` so HTMX does not replace them.
- Keep What's New and desktop inbox/alert code out of the Timeclock.
- Preserve Bug / Feature selection, image/PDF upload, pasted screenshots, attachment previews, current-page capture, errors, and success confirmation.
- Do not add automatic full-page screenshot capture, database fields, authentication paths, or TV-display controls.
- New `CHANGELOG.md` text must use short, common words and explain the user benefit.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `src/zira_dashboard/templates/_feedback.html` | Shared Send/View feedback modal markup and asset links | Create |
| `src/zira_dashboard/static/feedback.css` | Shared feedback modal styles | Create |
| `src/zira_dashboard/static/feedback.js` | Shared feedback form, attachments, screenshot paste, submit, and status list | Create |
| `src/zira_dashboard/templates/_footer.html` | Desktop What's New shell and shared feedback include | Modify |
| `src/zira_dashboard/static/footer.css` | Desktop-only What's New and inbox/alert styles | Modify |
| `src/zira_dashboard/static/footer.js` | Desktop-only What's New and inbox/alert behavior | Modify |
| `src/zira_dashboard/templates/timeclock_base.html` | Persistent kiosk feedback bar and shared component include | Modify |
| `tests/test_whatsnew_panel_static.py` | Desktop/shared feedback extraction contract | Modify |
| `tests/test_timeclock_feedback_static.py` | Every-Timeclock-screen feedback contract | Create |
| `CHANGELOG.md` | Plain-language What's New note | Modify |

---

### Task 1: Extract the shared feedback component without changing desktop behavior

**Files:**
- Create: `src/zira_dashboard/templates/_feedback.html`
- Create: `src/zira_dashboard/static/feedback.css`
- Create: `src/zira_dashboard/static/feedback.js`
- Modify: `src/zira_dashboard/templates/_footer.html:21-61`
- Modify: `src/zira_dashboard/static/footer.css:359-431`
- Modify: `src/zira_dashboard/static/footer.js:1105-1327`
- Modify: `tests/test_whatsnew_panel_static.py`

**Interfaces:**
- Consumes: existing DOM IDs (`fb-modal`, `fb-open`, `fb-view-open`, `fb-desc`, `fb-file-input`) and existing `POST /feedback` / `GET /api/feedback/mine` response shapes.
- Produces: reusable `_feedback.html`, `feedback.css`, and `feedback.js`; any trigger with `data-feedback-open` opens the Send feedback modal.

- [ ] **Step 1: Point the static contract at the proposed shared component**

Add shared paths beside the existing constants in
`tests/test_whatsnew_panel_static.py`:

```python
FEEDBACK_TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "_feedback.html"
FEEDBACK_CSS = ROOT / "src" / "zira_dashboard" / "static" / "feedback.css"
FEEDBACK_JS = ROOT / "src" / "zira_dashboard" / "static" / "feedback.js"
```

Replace the feedback assertions in the existing footer tests with these focused
contracts, leaving the What's New assertions against `TEMPLATE`, `CSS`, and
`JS` intact:

```python
def test_footer_includes_shared_feedback_component():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "{% include '_feedback.html' %}" in html
    assert 'id="fb-open"' in html
    assert 'id="fb-view-open"' in html


def test_shared_feedback_component_keeps_modal_contract():
    html = FEEDBACK_TEMPLATE.read_text(encoding="utf-8")

    assert 'id="fb-modal"' in html
    assert 'id="fb-view-modal"' in html
    assert 'id="fb-desc"' in html
    assert 'data-type="bug"' in html
    assert 'data-type="feature"' in html
    assert 'id="fb-file-input"' in html
    assert "/static/feedback.css" in html
    assert "/static/feedback.js" in html


def test_shared_feedback_assets_keep_submit_and_screenshot_support():
    css = FEEDBACK_CSS.read_text(encoding="utf-8")
    js = FEEDBACK_JS.read_text(encoding="utf-8")

    assert ".fb-modal" in css
    assert ".fb-card" in css
    assert ".fb-type-btn" in css
    assert ".fb-submit" in css
    assert ".fb-attachment-chip" in css
    assert ".fb-status-pill" in css
    assert "function submitFeedback" in js
    assert "FormData" in js
    assert "window.gpiFetch('/feedback'" in js
    assert "/api/feedback/mine" in js
    assert "function renderMyFeedback" in js
    assert "'paste'" in js
    assert "window.location.href" in js
    assert "[data-feedback-open]" in js
```

Update `test_feedback_modal_stacks_above_whatsnew_panel` to read the feedback
z-index from `FEEDBACK_CSS` and the panel z-index from `CSS`:

```python
fb = _rule_zindex(FEEDBACK_CSS.read_text(encoding="utf-8"), ".fb-modal")
panel = _rule_zindex(CSS.read_text(encoding="utf-8"), ".changelog-modal")
```

- [ ] **Step 2: Run the focused contract to verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_whatsnew_panel_static.py -q
```

Expected: FAIL because `_feedback.html`, `feedback.css`, and `feedback.js` do
not exist and `_footer.html` does not include the shared partial.

- [ ] **Step 3: Extract the shared modal markup**

Create `src/zira_dashboard/templates/_feedback.html` with the existing Send and
View modal blocks currently at `_footer.html:21-59`. Preserve every existing
ID and label. Add `data-feedback-open` to the desktop Send button in
`_footer.html`, replace the modal blocks there with the include, and load the
new assets from the shared partial:

```html
<div id="fb-modal" class="fb-modal" hidden>
  <div class="fb-backdrop" id="fb-backdrop"></div>
  <div class="fb-card" role="dialog" aria-modal="true" aria-label="Send feedback">
    <div class="fb-head">
      <h3>Send feedback</h3>
      <button type="button" id="fb-close" class="fb-close" aria-label="Close">Close</button>
    </div>
    <div class="fb-type" role="group" aria-label="Feedback type">
      <button type="button" class="fb-type-btn is-active" data-type="bug" aria-pressed="true">Bug</button>
      <button type="button" class="fb-type-btn" data-type="feature" aria-pressed="false">Feature request</button>
    </div>
    <label class="fb-label" for="fb-desc">Description</label>
    <textarea id="fb-desc" class="fb-desc" rows="5"
              placeholder="What broke, and what did you expect?"></textarea>
    <div class="fb-attachments" id="fb-attachments"></div>
    <div class="fb-actions-row">
      <button type="button" id="fb-upload-btn" class="fb-upload">Upload files</button>
      <input type="file" id="fb-file-input" class="fb-file-input" multiple
             accept="image/*,application/pdf" hidden>
      <span class="fb-hint">or paste a screenshot</span>
    </div>
    <div class="fb-footer">
      <span id="fb-status" class="fb-status" hidden></span>
      <button type="button" id="fb-cancel" class="fb-cancel">Cancel</button>
      <button type="button" id="fb-submit" class="fb-submit">Send feedback</button>
    </div>
  </div>
</div>

<div id="fb-view-modal" class="fb-modal" hidden>
  <div class="fb-backdrop" id="fb-view-backdrop"></div>
  <div class="fb-card" role="dialog" aria-modal="true" aria-label="Your feedback">
    <div class="fb-head">
      <h3>Your feedback</h3>
      <button type="button" id="fb-view-close" class="fb-close" aria-label="Close">Close</button>
    </div>
    <div id="fb-view-body" class="fb-view-body">Loading…</div>
  </div>
</div>
<link rel="stylesheet" href="/static/feedback.css?v={{ static_v('feedback.css') }}">
<script src="/static/feedback.js?v={{ static_v('feedback.js') }}"></script>
```

The desktop trigger becomes:

```html
<button type="button" id="fb-open" class="changelog-feedback-btn"
        data-feedback-open>Send feedback</button>
```

The end of `_footer.html` becomes:

```jinja2
{% include '_feedback.html' %}
<link rel="stylesheet" href="/static/footer.css?v={{ static_v('footer.css') }}">
<script src="/static/footer.js?v={{ static_v('footer.js') }}"></script>
```

- [ ] **Step 4: Extract shared styles and behavior**

Move the complete CSS block beginning
`/* ---------- Feedback modals (Send + View) ---------- */` through the final
`.fb-status-pill.is-rejected` rule from `footer.css` into `feedback.css`
unchanged. Add text selection inside the modal for the kiosk's global
`user-select: none` rule:

```css
.fb-card,
.fb-card input,
.fb-card textarea { user-select: text; -webkit-user-select: text; }
```

Create `feedback.js` from the guarded `window.gpiFetch` bootstrap at
`footer.js:1-29` plus the complete final feedback IIFE beginning
`// ---------- Feedback modal (Send) + View Feedback list ----------`.
Remove that final feedback IIFE from `footer.js`, leaving its guarded fetch
bootstrap in place for desktop inbox code.

Replace the single Send button lookup in `feedback.js`:

```javascript
var openBtn = $('fb-open');
if (openBtn) openBtn.addEventListener('click', function () {
  resetSendForm(); openModal($('fb-modal')); var d = $('fb-desc'); if (d) d.focus();
});
```

with reusable trigger wiring:

```javascript
Array.prototype.forEach.call(document.querySelectorAll('[data-feedback-open]'), function (openBtn) {
  openBtn.addEventListener('click', function () {
    resetSendForm();
    openModal($('fb-modal'));
    var d = $('fb-desc');
    if (d) d.focus();
  });
});
```

Keep the View button lookup, attachment list, pasted-image handler, multipart
submit, current URL, success/error messages, and Escape handlers unchanged.

- [ ] **Step 5: Run the focused contract to verify GREEN**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_whatsnew_panel_static.py -q
```

Expected: all tests in the file PASS.

- [ ] **Step 6: Commit the behavior-preserving extraction**

```bash
git add src/zira_dashboard/templates/_feedback.html \
  src/zira_dashboard/templates/_footer.html \
  src/zira_dashboard/static/feedback.css \
  src/zira_dashboard/static/feedback.js \
  src/zira_dashboard/static/footer.css \
  src/zira_dashboard/static/footer.js \
  tests/test_whatsnew_panel_static.py
git commit -m "refactor: share feedback form across layouts"
```

---

### Task 2: Keep a Feedback button on every Timeclock screen

**Files:**
- Create: `tests/test_timeclock_feedback_static.py`
- Modify: `src/zira_dashboard/templates/timeclock_base.html:1-279`

**Interfaces:**
- Consumes: `_feedback.html` and the `data-feedback-open` trigger contract from Task 1.
- Produces: `#timeclock-feedback-open`, a persistent button outside `#timeclock-screen` that opens `#fb-modal` on every template extending `timeclock_base.html`.

- [ ] **Step 1: Write the failing Timeclock feedback contract**

Create `tests/test_timeclock_feedback_static.py`:

```python
from pathlib import Path


BASE = Path("src/zira_dashboard/templates/timeclock_base.html")


def _html() -> str:
    return BASE.read_text(encoding="utf-8")


def test_timeclock_has_persistent_feedback_trigger_outside_htmx_swap():
    html = _html()

    screen_end = html.index("</div>", html.index('<div id="timeclock-screen">'))
    trigger = html.index('id="timeclock-feedback-open"')
    assert trigger > screen_end
    assert 'data-feedback-open' in html[trigger:]
    assert 'aria-controls="fb-modal"' in html[trigger:]
    assert "{% include '_feedback.html' %}" in html[trigger:]


def test_timeclock_feedback_bar_reserves_space_instead_of_covering_controls():
    html = _html()

    assert ".k-feedback-bar" in html
    assert "flex: 0 0 auto" in html
    assert "position: fixed" not in html[
        html.index(".k-feedback-bar"):html.index("}", html.index(".k-feedback-bar"))
    ]
    assert ".k-feedback-trigger" in html
    assert "min-height: 48px" in html
```

- [ ] **Step 2: Run the focused contract to verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_timeclock_feedback_static.py -q
```

Expected: FAIL because the Timeclock base has no feedback trigger or shared
component include.

- [ ] **Step 3: Add the non-overlapping persistent feedback bar**

Add these kiosk-only styles before `</style>` in `timeclock_base.html`:

```css
  .k-feedback-bar {
    flex: 0 0 auto;
    display: flex;
    justify-content: flex-end;
    padding: 0.45rem max(1rem, env(safe-area-inset-right))
             max(0.45rem, env(safe-area-inset-bottom));
    background: #ffffff;
    border-top: 1px solid #e2e8f0;
  }
  .k-feedback-trigger {
    min-height: 48px;
    padding: 0.65rem 1rem;
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    border: 1px solid #cbd5e1;
    border-radius: 0.6rem;
    background: #ffffff;
    color: #0f172a;
    font: inherit;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
  }
  .k-feedback-trigger:active { background: #f1f5f9; }
  .k-feedback-trigger:focus-visible {
    outline: 3px solid #2563eb;
    outline-offset: 2px;
  }
```

Immediately after the closing `</div>` for `#timeclock-screen`, add:

```jinja2
<div class="k-feedback-bar">
  <button type="button" id="timeclock-feedback-open" class="k-feedback-trigger"
          data-feedback-open aria-haspopup="dialog" aria-controls="fb-modal">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="2" stroke-linecap="round"
         stroke-linejoin="round" aria-hidden="true">
      <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"></path>
    </svg>
    Feedback
  </button>
</div>
{% include '_feedback.html' %}
```

Do not place the trigger or include inside `#timeclock-screen` and do not load
`footer.js` or `footer.css` from the kiosk.

- [ ] **Step 4: Run the Timeclock and shared-component contracts to verify GREEN**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_timeclock_feedback_static.py \
  tests/test_whatsnew_panel_static.py \
  tests/test_timeclock_home_static.py \
  tests/test_timeclock_time_off_static.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 5: Commit the Timeclock entry point**

```bash
git add src/zira_dashboard/templates/timeclock_base.html \
  tests/test_timeclock_feedback_static.py
git commit -m "feat: add feedback to every timeclock screen"
```

---

### Task 3: Document and verify the complete change

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: completed shared component and Timeclock trigger from Tasks 1-2.
- Produces: plain-language release note and regression evidence for the finished feature.

- [ ] **Step 1: Add the What's New note**

Add a new entry under the current date in `CHANGELOG.md`:

```markdown
- **The time clock now has a Feedback button on every screen.** You can report a bug or ask for a new feature without leaving the time clock. You can also paste or upload a screenshot to show what you saw.
```

- [ ] **Step 2: Verify every new static asset reference exists**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py -q
```

Expected: all base-template ratchet tests PASS.

- [ ] **Step 3: Run focused feedback, Timeclock, and route tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_whatsnew_panel_static.py \
  tests/test_timeclock_feedback_static.py \
  tests/test_timeclock_home_static.py \
  tests/test_timeclock_dashboard_static.py \
  tests/test_timeclock_pick_wc_static.py \
  tests/test_timeclock_time_off_static.py \
  tests/test_feedback_routes.py \
  tests/test_feedback_mine_route.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 4: Run lint on the touched Python tests**

Run:

```bash
.venv/bin/ruff check tests/test_whatsnew_panel_static.py \
  tests/test_timeclock_feedback_static.py
```

Expected: `All checks passed!`

- [ ] **Step 5: Run the full test suite**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
```

Expected: all runnable tests PASS; database-backed tests may SKIP when
`DATABASE_URL` is not configured.

- [ ] **Step 6: Inspect the final diff and whitespace**

Run:

```bash
git diff --check
git status --short
git diff --stat HEAD~2..HEAD
```

Expected: no whitespace errors; only the scoped feedback, Timeclock, tests,
design/plan, and changelog files are changed. Leave the pre-existing untracked
`.cursorignore`, `.python-version`, and `uv.lock` untouched.

- [ ] **Step 7: Commit the release note if it is not already included**

```bash
git add CHANGELOG.md
git commit -m "docs: explain timeclock feedback button"
```

- [ ] **Step 8: Push the finished implementation**

```bash
git push origin main
```

Expected: `origin/main` advances to include the shared feedback component,
Timeclock trigger, regression tests, and plain-language changelog note.
