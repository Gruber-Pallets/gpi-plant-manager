# Shared Inbox Navigation Alert Style Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every normal desktop page display a positive Inbox count and label in red.

**Architecture:** `_topnav.html` already supplies the `has-open` state in server-rendered markup. Move its presentation rules from `footer.css`, which selected pages omit, to `topnav.css`, which `_base_app.html` loads for every normal page with the shared navigation. Keep all count, API, polling, footer, and TV behavior unchanged.

**Tech Stack:** Jinja templates, shared CSS, pytest static-file regression tests, Ruff.

## Global Constraints

- A positive Inbox total must use the red `has-open` treatment on every normal desktop page.
- TV pages remain unchanged.
- Do not change Inbox summary data, urgency logic, API calls, or polling.
- Keep Inbox navigation styles defined once, in `topnav.css`.
- Do not add the shared footer bundle to templates that intentionally suppress it.

---

## File structure

| File | Responsibility |
| --- | --- |
| `src/zira_dashboard/static/topnav.css` | Shared header and Inbox navigation presentation loaded by the base app shell. |
| `src/zira_dashboard/static/footer.css` | Footer/modal presentation only; no longer owns Inbox navigation styling. |
| `tests/test_exception_inbox.py` | Static regression coverage for the Inbox summary client logic and its shared navigation style ownership. |
| `CHANGELOG.md` | A short, child-friendly note explaining that Inbox alerts now stay red on every normal page. |

### Task 1: Put the open-Inbox alert style in the shared top navigation

**Files:**
- Modify: `tests/test_exception_inbox.py:1181-1201`
- Modify: `src/zira_dashboard/static/topnav.css:43` (append after the existing active-link rule)
- Modify: `src/zira_dashboard/static/footer.css:191-219` (remove the Inbox navigation rule block)
- Modify: `CHANGELOG.md:1` (add a new top entry)

**Interfaces:**
- Consumes: `_topnav.html` renders `.inbox-nav-link`, `.inbox-nav-count`, `has-open`, and `is-degraded`; `footer.js` applies the same classes after summary refreshes.
- Produces: `topnav.css` owns all Inbox navigation presentation, so footer-suppressed normal pages receive the red open state.

- [ ] **Step 1: Write the failing regression test**

  In `tests/test_exception_inbox.py`, add `Path` to the imports if it is not already available, then replace the CSS portion of `test_footer_enhances_inbox_nav_with_summary_count` with a dedicated test that proves the shared top-navigation stylesheet owns the visual rules:

  ```python
  def test_inbox_nav_styles_are_available_without_the_footer_bundle():
      topnav_css = (STATIC_DIR / "topnav.css").read_text(encoding="utf-8")
      footer_css = (STATIC_DIR / "footer.css").read_text(encoding="utf-8")

      assert ".inbox-nav-count" in topnav_css
      assert ".inbox-nav-link.has-open" in topnav_css
      assert ".brand-row nav a.inbox-nav-link.has-open" in topnav_css
      assert ".inbox-nav-link.has-open .inbox-nav-count" in topnav_css
      assert ".inbox-nav-link.is-degraded .inbox-nav-count" in topnav_css
      assert ".inbox-nav-link.has-open" not in footer_css
  ```

  Leave the existing JavaScript assertions in `test_footer_enhances_inbox_nav_with_summary_count`, but remove its `footer.css` read and CSS assertions because `footer.js` remains the owner of refresh behavior while `topnav.css` becomes the owner of navigation presentation.

- [ ] **Step 2: Run the new test and verify it fails for the intended reason**

  Run:

  ```bash
  pytest tests/test_exception_inbox.py::test_inbox_nav_styles_are_available_without_the_footer_bundle -v
  ```

  Expected: FAIL because `topnav.css` does not yet contain `.inbox-nav-count` or `.inbox-nav-link.has-open`.

- [ ] **Step 3: Move only the Inbox navigation presentation block**

  Append this block to `src/zira_dashboard/static/topnav.css` immediately after `.brand-row nav a.active`, preserving the existing selector names and values:

  ```css
  /* Inbox navigation state is shared by every normal page that uses _topnav. */
  .inbox-nav-link {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .inbox-nav-count {
    min-width: 1.35rem;
    padding: 0.04rem 0.36rem;
    border-radius: 999px;
    background: var(--accent-dim, #dcfce7);
    color: var(--accent, #16a34a);
    font-size: 0.68rem;
    font-weight: 800;
    line-height: 1.25;
    text-align: center;
  }
  .inbox-nav-link.has-open,
  .brand-row nav a.inbox-nav-link.has-open {
    color: #b91c1c;
    font-weight: 700;
  }
  .inbox-nav-link.has-open .inbox-nav-count {
    background: #fee2e2;
    color: #b91c1c;
  }
  .inbox-nav-link.is-degraded .inbox-nav-count {
    background: var(--warn-dim, #fef3c7);
    color: var(--warn, #a16207);
  }
  ```

  Delete precisely this same Inbox navigation block from `src/zira_dashboard/static/footer.css`. Do not change `footer.js`, `_topnav.html`, `_base_app.html`, or any dashboard template.

  At the top of `CHANGELOG.md`, add a short entry such as:

  ```markdown
  - **Inbox alerts now stay red everywhere.** When there is work to do, the Inbox word and number stay red on every normal app page, so it is easier to spot.
  ```

- [ ] **Step 4: Run the focused regression test and verify it passes**

  Run:

  ```bash
  pytest tests/test_exception_inbox.py::test_inbox_nav_styles_are_available_without_the_footer_bundle -v
  ```

  Expected: PASS.

- [ ] **Step 5: Run the focused Inbox test file and lint the changed Python test**

  Run:

  ```bash
  pytest tests/test_exception_inbox.py tests/test_topnav_inbox_count.py -v
  ruff check tests/test_exception_inbox.py
  ```

  Expected: both pytest files pass and Ruff reports no diagnostics.

- [ ] **Step 6: Inspect the complete diff, commit, and push**

  Run:

  ```bash
  git diff --check
  git diff -- src/zira_dashboard/static/topnav.css src/zira_dashboard/static/footer.css tests/test_exception_inbox.py CHANGELOG.md
  git add src/zira_dashboard/static/topnav.css src/zira_dashboard/static/footer.css tests/test_exception_inbox.py CHANGELOG.md
  git commit -m "fix: keep open inbox alert red across pages"
  git push origin main
  ```

  Expected: the diff moves only the shared Inbox navigation styles, the regression test and changelog entry are included, and `origin/main` receives the commit.
