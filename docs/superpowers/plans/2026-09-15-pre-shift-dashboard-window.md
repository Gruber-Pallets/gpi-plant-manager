# Pre-shift Dashboard Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep live department dashboards available before the day's shift starts by rendering an empty work window instead of raising an internal server error.

**Architecture:** Normalize the effective live-dashboard window in `routes/departments.py`, where shift and current-time bounds are assembled. Preserve the strict contract of `assignment_windows.work_segments_from_timeline` by returning an empty canonical projection before calling it when the normalized window has zero duration.

**Tech Stack:** Python 3.13, FastAPI, pytest, Railway

## Global Constraints

- Before shift start, a live "today" dashboard represents zero elapsed work.
- Historical and at/after-shift calculations remain unchanged.
- `work_segments_from_timeline` continues rejecting non-positive windows supplied directly.
- The existing 60-second dashboard refresh cadence and lightweight probe remain unchanged.
- New What's New text uses short, common words and does not expose implementation details.

---

## File Structure

- `src/zira_dashboard/routes/departments.py` — normalize department dashboard time bounds and short-circuit empty canonical projections.
- `tests/test_current_operator_surfaces.py` — verify an empty canonical window does not reach timeline conversion.
- `tests/test_department_operator_labels.py` — verify pre-shift live-window normalization.
- `CHANGELOG.md` — explain the shipped overnight-dashboard fix in plain language.

### Task 1: Make pre-shift department windows empty and valid

**Files:**
- Modify: `src/zira_dashboard/routes/departments.py:80-140,439-450`
- Modify: `tests/test_current_operator_surfaces.py:239-269`
- Modify: `tests/test_department_operator_labels.py`
- Modify: `CHANGELOG.md:1-19`

**Interfaces:**
- Consumes: timezone-aware `now_local`, `shift_start_local`, and `shift_end_local` values.
- Produces: `_live_window_end(now_local, shift_start_local, shift_end_local, is_today) -> datetime`, clamped to `[shift_start_local, shift_end_local]` for today and unchanged at `shift_end_local` for historical days.
- Preserves: `_canonical_department_segments(...) -> _CanonicalDepartmentProjection | None`.

- [ ] **Step 1: Add the failing live-window boundary test**

Add to `tests/test_department_operator_labels.py`:

```python
def test_live_department_window_is_empty_before_shift():
    from zira_dashboard.routes import departments

    day = date(2026, 9, 15)
    shift_start = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
    shift_end = datetime(2026, 9, 15, 15, tzinfo=timezone.utc)
    before_shift = datetime(2026, 9, 15, 0, 19, tzinfo=timezone.utc)

    assert departments._live_window_end(
        before_shift,
        shift_start,
        shift_end,
        is_today=True,
    ) == shift_start
```

- [ ] **Step 2: Add the failing empty-projection regression test**

Add to `tests/test_current_operator_surfaces.py` after the existing canonical projection test:

```python
def test_canonical_department_projection_skips_empty_window(monkeypatch):
    snapshot = SimpleNamespace(
        policy=live_cache.AttendanceReadPolicy(
            mirror_owned=True,
            available=True,
            refreshed_at=NOW,
            mode="shadow",
        ),
        spans=(),
        verified_cap_utc=NOW,
        current_attendance_ids=frozenset(),
    )
    monkeypatch.setattr(
        "zira_dashboard.attendance_location_snapshot.read_location_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        assignment_windows,
        "work_segments_from_timeline",
        lambda *_args, **_kwargs: pytest.fail(
            "an empty pre-shift window must not reach timeline conversion"
        ),
    )
    shift_start = datetime.combine(DAY, time(7), tzinfo=UTC)

    projection = departments._canonical_department_segments(
        DAY,
        shift_start,
        shift_start,
        now_utc=shift_start,
    )

    assert projection.segments == ()
    assert projection.cap_utc == shift_start
    assert projection.location_snapshot is snapshot
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_department_operator_labels.py::test_live_department_window_is_empty_before_shift \
  tests/test_current_operator_surfaces.py::test_canonical_department_projection_skips_empty_window \
  -v
```

Expected: both tests fail because `_live_window_end` does not exist and the zero-duration canonical projection calls `work_segments_from_timeline`.

- [ ] **Step 4: Implement the minimal window normalization**

Add near `_canonical_department_segments` in `src/zira_dashboard/routes/departments.py`:

```python
def _live_window_end(
    now_local: datetime,
    shift_start_local: datetime,
    shift_end_local: datetime,
    *,
    is_today: bool,
) -> datetime:
    if not is_today:
        return shift_end_local
    return max(shift_start_local, min(now_local, shift_end_local))
```

Replace the current `window_end_local` assignment in `_department_day_data` with:

```python
window_end_local = _live_window_end(
    now_local,
    shift_start_local,
    shift_end_local,
    is_today=is_today_d,
)
```

Before calling `assignment_windows.work_segments_from_timeline`, return an empty projection when the canonical cap equals the start:

```python
if canonical_cap <= window_start_utc:
    return _CanonicalDepartmentProjection(
        (),
        window_start_utc,
        projection_snapshot,
    )
```

- [ ] **Step 5: Run the regression tests and verify GREEN**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_department_operator_labels.py::test_live_department_window_is_empty_before_shift \
  tests/test_current_operator_surfaces.py::test_canonical_department_projection_skips_empty_window \
  -v
```

Expected: 2 passed.

- [ ] **Step 6: Add the plain-language patch note**

Insert at the top of `CHANGELOG.md`, after its format comment:

```markdown
## 2026-09-15

### Dashboards stay open overnight

#### Fixes

- **The Recycling dashboard no longer turns into an error page before the morning shift starts.** It stays open and shows a fresh, empty day until work begins.
```

- [ ] **Step 7: Run focused regression coverage**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_current_operator_surfaces.py \
  tests/test_department_operator_labels.py \
  tests/test_attendance_location_end_to_end.py \
  tests/test_dashboards_polish.py \
  tests/test_tv_dashboards_vs.py \
  -q
```

Expected: all runnable tests pass; database-backed tests may skip when `DATABASE_URL` is not configured.

- [ ] **Step 8: Run lint and the full suite**

Run:

```bash
.venv/bin/python -m ruff check src tests scripts
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
```

Expected: lint exits 0; tests exit 0 with only the repository's expected skips.

- [ ] **Step 9: Commit, push, and verify production**

Commit only the plan, implementation, regression tests, and changelog; leave the pre-existing untracked `.cursorignore`, `.python-version`, and `uv.lock` untouched.

```bash
git add \
  docs/superpowers/plans/2026-09-15-pre-shift-dashboard-window.md \
  src/zira_dashboard/routes/departments.py \
  tests/test_current_operator_surfaces.py \
  tests/test_department_operator_labels.py \
  CHANGELOG.md
git commit -m "fix: keep dashboards available before shift"
git push origin main
railway deployment list --service web --json
```

Verify the new `web` deployment reaches `SUCCESS`, then confirm `/healthz` returns HTTP 200 and the production logs contain no new pre-shift positive-duration exception.

## Plan Self-Review

- **Spec coverage:** The single task covers pre-shift normalization, empty canonical projection handling, unchanged timeline strictness, focused and full verification, plain-language release notes, deployment, and production checks.
- **Placeholder scan:** No unresolved markers or deferred implementation steps remain.
- **Type consistency:** `_live_window_end` accepts and returns timezone-aware `datetime` values already constructed by `_department_day_data`; `_canonical_department_segments` keeps its existing return type.
