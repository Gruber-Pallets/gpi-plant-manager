# Training Sidebar Panel

## Goal

Managers manage Recycled training protocols from the Staffing right rail, under day Notes: see progress, edit an in-progress protocol, start a new one, and finish or stop it without opening a separate modal.

## User experience

- A **Training** panel sits under Notes and above Schedule Goal.
- The panel lists every **active** and **paused** protocol plant-wide (not limited to the viewed day).
- Each compact card shows trainee, work center, trainer, status, and progress as **attended of planned** (for example `2 of 5`) with a simple progress bar.
- Card actions: **Edit** (expands fields inline), **Pause** / **Resume**, **Complete**, and **End**.
- **+ Start training** at the bottom expands the create form in the same panel (trainee, trainer, work center, start day, planned workdays).
- The header **+ Training** control and training-protocol modal are removed.
- When the schedule is posted / read-only, the panel shows the list and progress only; create, edit, and lifecycle actions are hidden or disabled.

### Complete vs End

- **Complete** finishes the protocol immediately and promotes the trainee to level 1 for the protocol’s target skills, even if planned attended days remain. It does not invent missing attended-day records.
- **End** stops the protocol without promoting (existing behavior).

## Edit rules

Editable while status is `active` or `paused`:

- Trainer (must be level 3 for every required skill of the selected work center)
- Work center (re-derives required skills; trainee must still be level 0 for those skills)
- Start day
- Planned attended days (must be at least the number of days already recorded as attended)

Saving an edit keeps existing attended / absent / conflict day history. Future reservations follow the updated plan; past resolved days are not rewritten. Create validation rules continue to apply for new protocols.

## Design

Day-scoped training effects for Auto/seeding stay on `active_blocks_for_day` (active only, for the viewed day). The sidebar uses a separate plant-wide list of every `active` and `paused` protocol, including those that have not started yet or do not cover the viewed day. That payload includes an explicit attended count plus planned (and remaining) so cards can render progress without extra client fetches.

Add two JSON endpoints beside the existing training-block lifecycle routes:

1. **Update** — patch trainer, work center, start day, and/or planned attended days with the validation above; return the updated block payload used by the sidebar.
2. **Complete** — promote target skills to level 1 through the shared skill writer, mark the block completed, return success; the sidebar removes the card.

Pause, resume, end, and create keep their current routes and meanings. Starting a protocol from the sidebar still triggers the same start-day schedule rebuild used today when the create succeeds.

Client ownership stays in `staffing.js`: render the compact list, expand/collapse edit and create forms, call the APIs, and refresh local protocol state. Template/CSS move the panel into `day-context` under Notes and delete the modal markup and header button.

## Safety and error handling

- Validation failures return `422` with a clear error string shown in the panel live region; prior values stay on screen.
- Complete failures (for example skill promotion errors) leave the block active so the manager can retry; no partial “completed without promotion” terminal state.
- Posted view never mutates protocols from this panel.
- Ending remains irreversible without promotion; Complete remains the only sidebar path that promotes early.

## Tests

- Page context includes attended/planned progress fields for active and paused blocks.
- Update rejects planned days below attended count, invalid trainer/work-center skill levels, and unknown centers; accepts a valid field change and returns the new payload.
- Complete promotes skills and marks the block completed even when attended days are fewer than planned.
- End still does not promote.
- Staffing template/JS/CSS contracts: Training panel under Notes, no training modal / header `+ Training` button, create and lifecycle actions wired from the sidebar.
