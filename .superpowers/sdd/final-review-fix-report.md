# Final review fix report

## Scope

Fixed the Saturday recruiting client-side committed-volunteer guard without changing recruiting or scheduling behavior.

## Root cause

`SATURDAY_COMMITTED_NAMES` was derived in the template from the partial-only
`saturday_availability_by_name` badge model. Full-shift volunteers were thus
absent from the JavaScript guard set even though they remained committed.

## Change

- Added `saturday_committed_names` to the staffing view model from the complete
  Saturday commitment set.
- Rendered `SATURDAY_COMMITTED_NAMES` from that complete model value.
- Kept `SATURDAY_AVAILABILITY_BY_NAME` unchanged and partial-only.

## TDD evidence

- RED: `.venv/bin/pytest tests/test_staffing_saturday_recruiting.py::test_only_commitments_enter_saturday_unassigned tests/test_staffing_saturday_recruiting.py::test_staffing_template_has_saturday_off_availability_and_publish_lock tests/test_staffing_view.py::test_return_keys_are_exactly_the_bands_ab_context_keys` → 3 failed, each due to the missing complete committed-name model/template binding.
- GREEN: `.venv/bin/pytest tests/test_staffing_saturday_recruiting.py tests/test_staffing_view.py` → 37 passed.

## Regression coverage

The Saturday model test establishes that full-shift `Ana` is in
`saturday_committed_names` but absent from `saturday_availability_by_name`,
while partial-shift `Bob` remains in the badge map. The template assertion pins
the client guard input to the complete committed-name model, and the existing
guard assertion verifies the guard semantics remain unchanged.
