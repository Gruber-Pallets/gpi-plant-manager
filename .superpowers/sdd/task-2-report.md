# Task 2 Report: Fixed Schedule Tools Card and Responsive Fallback

## Implementation summary

Styled the existing Schedule Tools controls as a fixed lower-right utility card
above the 1100px breakpoint. The card has the requested fixed placement,
bounded responsive width, accent-tinted panel background, rounded border, and
shadow. Its mode row stays compact, adds an accent status dot, hides the inline
help text, and places the dynamic minimum-crew status and Reset/Clear actions
inside the card.

At 1100px and below, the controls return to normal document flow with automatic
width. No template or JavaScript was changed, so the existing keyboard, hover,
active, disabled/pending, Reset, Clear, and dynamic-status behavior is
preserved.

## Changed files

- `src/zira_dashboard/static/staffing.css`
- `tests/test_staffing_rotations.py`

## TDD evidence

### RED

Added the eight specified CSS contract assertions to
`test_staffing_has_rotation_mode_controls_without_automated_person_notes`, then
ran:

```text
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_staffing_has_rotation_mode_controls_without_automated_person_notes -q
```

Result: `1 failed in 0.25s`, as expected. The new assertion for
`position: fixed; right: 1.25rem; bottom: 1.25rem; z-index: 20;` was absent
before the stylesheet change.

### GREEN

Added the prescribed card declarations and responsive fallback, then ran:

```text
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_staffing_has_rotation_mode_controls_without_automated_person_notes tests/test_staffing_static.py::test_clear_schedule_remains_a_distinct_local_autosave_action -q
```

Result: `2 passed in 0.11s`.

The full staffing suite initially found a stale static assertion for the
pre-card action-row declaration. Updated it to include the required
`margin-top: 0.6rem;` declaration, then re-ran the suite.

## Verification

```text
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py tests/test_staffing_rotations.py -q
141 passed in 0.33s

git diff --check
exit 0
```

## Self-review

- The existing Schedule Tools DOM and JavaScript are untouched.
- Existing `.rotation-mode-btn` hover, active, and disabled rules remain
  unchanged.
- Existing `.clear-schedule-btn:hover` and Reset/Clear sizing rules remain in
  place.
- The card is fixed only above 1100px; the required mobile normal-flow override
  is inside the existing `@media (max-width: 1100px)` block.
- The full diff is limited to the specified stylesheet and static contract test.

## Concerns

No implementation concerns. The required task report is intentionally not part
of the feature commit; unrelated untracked documentation and `uv.lock` were
left untouched. The feature commit was not pushed because the sandbox could not
resolve GitHub and the escalated push request was not approved.

---

# Final-review correction — Minimum-crew status row

## Implementation

- Updated the desktop fixed-card override so `.day-context .rotation-mode`
  wraps its flex items.
- Updated `.day-context .minimum-crew-balance` with `flex: 0 0 100%`, retaining
  its existing `display`, margin, and white-space declarations. This reserves a
  full status row beneath the goal controls within the 17.5rem card.
- Added static CSS contract assertions covering both declarations.

## Changed files

- `src/zira_dashboard/static/staffing.css`
- `tests/test_staffing_rotations.py`
- `.superpowers/sdd/task-2-report.md`

## TDD evidence

### RED

After adding the two static-contract assertions and before modifying the
stylesheet, ran:

```text
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_staffing_has_rotation_mode_controls_without_automated_person_notes -q
```

Output:

```text
FAILED tests/test_staffing_rotations.py::test_staffing_has_rotation_mode_controls_without_automated_person_notes
1 failed in 0.22s
```

The failure was the expected missing contract for
`.day-context .rotation-mode { flex-wrap: wrap; ... }`; the existing CSS used
`flex-wrap: nowrap`.

### GREEN

Applied the two CSS declarations, then ran the focused test:

```text
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_staffing_has_rotation_mode_controls_without_automated_person_notes -q
```

Output:

```text
1 passed in 0.11s
```

## Full verification

```text
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py tests/test_staffing_rotations.py -q
........................................................................ [ 51%]
.....................................................................    [100%]
141 passed in 0.37s
```

## Self-review

- No DOM, IDs, JavaScript behavior, goal-button behavior, or Reset/Clear
  behavior changed.
- The change is limited to the desktop fixed-card override; the existing
  `@media (max-width: 1100px)` fallback remains untouched.
- The minimum-crew status retains its existing margin and status styling while
  being guaranteed a separate flex row.
- Unrelated untracked files were not modified or staged.

## Concerns

None.
