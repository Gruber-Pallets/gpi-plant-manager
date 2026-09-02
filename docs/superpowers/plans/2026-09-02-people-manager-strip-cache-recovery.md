# People Manager Strip Cache Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the People page's two-band manager bar for browsers holding an incompatible stylesheet and prevent rolling deployments from leaving stale static files trusted for a year.

**Architecture:** Change the shared static-response policy from immutable caching to mandatory revalidation, while keeping the existing `static_v(...)` URLs and Starlette conditional-response support. In the same release, make the People strip's one-column grid explicit so its stylesheet receives a fresh version URL, then strengthen the real-browser fixture to prove the strip contains every visible child without overlap at all supported widths.

**Tech Stack:** FastAPI/Starlette middleware and `StaticFiles`, Jinja2 templates, CSS Grid/Flexbox, pytest, Playwright Chromium.

## Global Constraints

- The visible layout is totals and controls in the first band, with source warnings in a separate wrapping band below it.
- No manager-strip group uses horizontal scrolling.
- The manager strip remains sticky and may grow vertically to fit its content.
- Counts, warning generation, filter behavior, polling, focus restoration, person rows, and performance calculations remain unchanged.
- Static files remain publicly cacheable but must be revalidated before reuse.
- New What's New text must use short sentences and common words that a 10-year-old can understand.
- Preserve unrelated user changes already present in the worktree.
- Commit and push the complete implementation to `origin/main`; do not update completion tracking unless one exact existing Odoo improvement is identified.

## File Map

- `tests/test_static_cache_headers.py`: integration contract for headers returned by the mounted static-file application through shared middleware.
- `src/zira_dashboard/app.py`: shared HTTP security and cache headers; changes static files from immutable reuse to revalidation.
- `tests/test_people_performance_static.py`: source-level contract for the People manager strip's explicit one-column grid and automatic height.
- `scripts/preview_people_performance.py`: deterministic busy fixture using the three warning strings from the reported failure.
- `tests/test_preview_people_performance.py`: Playwright geometry assertions that every visible manager-bar descendant remains inside the strip and the first people section begins below it.
- `src/zira_dashboard/static/people-performance.css`: explicit one-column grid declaration; its content change creates a fresh `static_v(...)` URL.
- `CHANGELOG.md`: plain-language shipped fix note.

---

### Task 1: Recover stale assets and lock the manager strip inside its bounds

**Files:**
- Create: `tests/test_static_cache_headers.py`
- Modify: `src/zira_dashboard/app.py:638-688`
- Modify: `tests/test_people_performance_static.py:58-76`
- Modify: `scripts/preview_people_performance.py:529-533`
- Modify: `tests/test_preview_people_performance.py:128-225`
- Modify: `src/zira_dashboard/static/people-performance.css:11-23`
- Modify: `CHANGELOG.md:12-20`

**Interfaces:**
- Consumes: `app: FastAPI`, mounted `/static` files, Jinja `static_v(filename: str) -> str`, and the existing `.pp-manager-strip`, `.pp-manager-primary`, `.pp-manager-actions`, `.pp-source-warnings`, and `.pp-section` elements.
- Produces: static responses with `Cache-Control: public, no-cache`; `.pp-manager-strip` with `grid-template-columns: minmax(0, 1fr)` and no fixed height; Playwright booleans `managerDescendantsContained` and `managerClearsFirstSection`.

- [ ] **Step 1: Add a failing integration test for static revalidation**

Create `tests/test_static_cache_headers.py`:

```python
from fastapi.testclient import TestClient

from zira_dashboard.app import app


def test_static_assets_require_revalidation_instead_of_staying_immutable():
    response = TestClient(app).get("/static/people-performance.css")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, no-cache"
    assert "immutable" not in response.headers["cache-control"]
    assert response.headers.get("etag") or response.headers.get("last-modified")
```

- [ ] **Step 2: Run the cache test and confirm the current one-year policy fails it**

Run:

```bash
.venv/bin/python -m pytest tests/test_static_cache_headers.py -q
```

Expected: FAIL because the response currently contains `public, max-age=31536000, immutable` instead of `public, no-cache`.

- [ ] **Step 3: Make static responses revalidate**

In `src/zira_dashboard/app.py`, revise the middleware documentation so it says static responses require revalidation and can use Starlette's ETag/Last-Modified handling for inexpensive unchanged responses. Replace only the static cache header value:

```python
    if request.url.path.startswith("/static/"):
        response.headers.setdefault(
            "Cache-Control",
            "public, no-cache",
        )
```

Do not change HSTS, robot, content-type, referrer, or page-view behavior.

- [ ] **Step 4: Run the cache test and confirm it passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_static_cache_headers.py -q
```

Expected: PASS with one test.

- [ ] **Step 5: Strengthen the CSS contract before changing production CSS**

In `tests/test_people_performance_static.py::test_manager_strip_is_sticky_and_manager_groups_wrap_without_scrollbars`, add these assertions after `assert "display: grid" in strip.group(1)`:

```python
    assert "grid-template-columns: minmax(0, 1fr)" in strip.group(1)
    assert not re.search(r"(?m)^\s*(?:min-)?height:", strip.group(1))
```

Keep the existing wrapping, overflow, sticky, and horizontal-scroll assertions.

- [ ] **Step 6: Run the CSS contract and confirm the explicit-column assertion fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_people_performance_static.py::test_manager_strip_is_sticky_and_manager_groups_wrap_without_scrollbars -q
```

Expected: FAIL because `.pp-manager-strip` does not yet declare `grid-template-columns`.

- [ ] **Step 7: Make the manager strip's one-column grid explicit**

In `src/zira_dashboard/static/people-performance.css`, add the column declaration immediately after `display: grid`:

```css
.pp-manager-strip {
  position: sticky;
  top: 0;
  z-index: 30;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  box-sizing: border-box;
  gap: .35rem;
```

Do not add a fixed or minimum height. This content change is also the cache-busting stylesheet release needed by affected browsers.

- [ ] **Step 8: Run the CSS contract and confirm it passes**

Run:

```bash
.venv/bin/python -m pytest tests/test_people_performance_static.py::test_manager_strip_is_sticky_and_manager_groups_wrap_without_scrollbars -q
```

Expected: PASS with one test.

- [ ] **Step 9: Change the deterministic preview to the reported warning load**

In `tests/test_preview_people_performance.py`, add the reported screenshot's
approximately `1195px` CSS viewport width to the permanent browser matrix:

```python
VIEWPORTS = ((1440, 900), (1195, 768), (1024, 768), (768, 1024), (390, 844))
```

In `scripts/preview_people_performance.py::_context`, replace `source_warnings` with:

```python
        "source_warnings": (
            "Production metric unavailable: Trim Saw 1",
            "Production metric unavailable: Hand Build #1",
            "Unmatched forklift calls: 107",
        ),
```

In `tests/test_preview_people_performance.py::test_preview_contains_busy_people_fixture`, replace the old attendance-warning assertion with:

```python
    assert "Production metric unavailable: Trim Saw 1" in html
    assert "Production metric unavailable: Hand Build #1" in html
    assert "Unmatched forklift calls: 107" in html
```

- [ ] **Step 10: Add containment checks to the Playwright geometry test**

In the object returned by `page.evaluate` inside `test_preview_fits_all_manager_viewports_with_two_nonoverlapping_bands`, add:

```javascript
                      managerDescendantsContained: (() => {
                        const strip = document.querySelector('.pp-manager-strip');
                        const stripBox = strip.getBoundingClientRect();
                        return [...strip.querySelectorAll('*:not(.sr-only)')]
                          .filter(node => {
                            const box = node.getBoundingClientRect();
                            return box.width > 0 && box.height > 0;
                          })
                          .every(node => {
                            const box = node.getBoundingClientRect();
                            return box.left >= stripBox.left - 0.5
                              && box.right <= stripBox.right + 0.5
                              && box.top >= stripBox.top - 0.5
                              && box.bottom <= stripBox.bottom + 0.5;
                          });
                      })(),
                      managerClearsFirstSection: (() => {
                        const strip = document.querySelector('.pp-manager-strip');
                        const section = document.querySelector('.pp-section');
                        return section.getBoundingClientRect().top
                          >= strip.getBoundingClientRect().bottom - 0.5;
                      })(),
```

Add the corresponding assertions after `primaryGroupsDoNotOverlap`:

```python
                assert geometry["managerDescendantsContained"] is True
                assert geometry["managerClearsFirstSection"] is True
```

These assertions permanently cover the geometry that the preceding stylesheet
violated. The cache-header test and explicit-grid test already provide this
task's required red tests before their corresponding production changes.

- [ ] **Step 11: Run the current-asset browser tests and confirm the layout passes**

Run outside the filesystem sandbox so Chromium can register its macOS process services:

```bash
.venv/bin/python -m pytest \
  tests/test_preview_people_performance.py::test_preview_contains_busy_people_fixture \
  tests/test_preview_people_performance.py::test_preview_fits_all_manager_viewports_with_two_nonoverlapping_bands \
  -q
```

Expected: PASS with two tests and no console errors.

- [ ] **Step 12: Add the shipped What's New note**

At the top of the `2026-09-02` entries in `CHANGELOG.md`, add:

```markdown
### Keep the People page bar in neat rows

- **The People page now checks for fresh styles after Plant Manager is updated.** This keeps totals, controls, and warnings in their own rows instead of letting an old saved style mix them together.
```

- [ ] **Step 13: Run the complete focused verification suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_static_cache_headers.py \
  tests/test_people_performance_static.py \
  tests/test_people_performance_template.py \
  tests/test_people_performance_route.py \
  tests/test_preview_people_performance.py \
  -q
```

Expected: all tests PASS with zero failures.

Run:

```bash
git diff --check
```

Expected: exit code 0 and no whitespace errors.

- [ ] **Step 14: Review the generated screenshots**

Inspect these files produced by the Playwright test:

```text
scripts/_preview_out/people_performance/people-performance-1440x900.png
scripts/_preview_out/people_performance/people-performance-1195x768.png
scripts/_preview_out/people_performance/people-performance-1024x768.png
scripts/_preview_out/people_performance/people-performance-768x1024.png
scripts/_preview_out/people_performance/people-performance-390x844.png
```

Confirm that totals and controls finish before the warning band begins, all three reported warnings are readable, the first green section begins below the manager strip, and no sideways page scroll appears.

- [ ] **Step 15: Commit and push the complete repair**

Stage only the scoped implementation files, preserving unrelated worktree changes:

```bash
git add \
  CHANGELOG.md \
  src/zira_dashboard/app.py \
  src/zira_dashboard/static/people-performance.css \
  scripts/preview_people_performance.py \
  tests/test_static_cache_headers.py \
  tests/test_people_performance_static.py \
  tests/test_preview_people_performance.py
git commit -m "fix: recover People bar styles after deploy"
git push origin main
```

Expected: the commit succeeds and `origin/main` advances to the implementation commit.

- [ ] **Step 16: Verify repository and completion state**

Run:

```bash
git status -sb
git log -2 --oneline
```

Expected: `main` is even with `origin/main`; only the unrelated pre-existing worktree files remain modified or untracked. No Odoo completion update is made unless the request provides or reveals exactly one matching existing improvement row.
