# Send-Feedback-First Light Bulb Design

**Date:** 2026-09-04
**Status:** Approved design; implementation not started

## Goal

Make the light bulb open directly to Send feedback. Keep My feedback and What’s new one clear tab away inside the same window.

## Scope

This change reorganizes the existing light-bulb interface. It does not change feedback storage, Odoo task creation, reference-table synchronization, review actions, or the Maintenance request flow.

## Chosen layout

Use one modal with a shared header and three equal-width tabs:

1. **Send feedback** — selected every time the light bulb opens.
2. **My feedback** — shows only the signed-in person’s submissions and statuses.
3. **What’s new** — shows the existing release notes and read controls.

The header says **Light bulb** and contains one Close button. Selecting a tab replaces only the modal’s content area. It must not open another modal.

## Send feedback flow

Send feedback first shows the existing six choices:

- Bug
- New Feature
- Floor Issue
- Floor Suggestion
- Repair
- 2s Improvement

Selecting a non-Repair choice opens the existing description step inside the Send feedback tab. The Back control returns to the six choices without closing the modal. The current submitter selection and optional screenshot behavior remain unchanged.

Repair continues to open `https://www.gpimaintenance.com/request` in a protected new tab. It creates no Plant Manager feedback, Odoo reference row, review task, or work order itself.

## After submission

The interface switches to My feedback only after the server confirms the submission was saved. The new request appears first with its current status. The prior automatic-close behavior is removed.

If submission fails, the person stays on the form. Their description, submitter selection, and screenshot remain intact, and the existing error area explains the failure.

## Draft behavior

Switching among Send feedback, My feedback, and What’s new does not erase an unfinished description or attached screenshot. Closing the light-bulb modal clears the unsent draft, matching the current close behavior. Reopening always starts on Send feedback with a fresh form.

## My feedback

My feedback reuses the existing signed-in-person request list and status labels. It loads only when first opened during a modal session. After a successful submission, it refreshes before showing the new request at the top.

If loading fails, only the My feedback panel shows an error and Retry control. Send feedback and What’s new remain usable.

## What’s new and unread state

What’s new reuses the existing release-note cards and Mark all read control. It loads only when first opened during a modal session.

The unread dot remains on the light bulb while unread release notes exist. Opening the light bulb or viewing My feedback does not clear it. The dot changes only when the person actually views or marks release notes read through What’s new.

If loading fails, only the What’s new panel shows an error and Retry control.

## Responsive and accessible behavior

The three tabs remain in one equal-width row on supported phone widths. Their visible labels always stay **Send feedback**, **My feedback**, and **What’s new**. Labels may wrap within their tab but may not be shortened, clipped, or hidden.

The tab row uses native buttons with tab semantics, selected state, matching tab panels, and normal keyboard order. Selecting a tab moves focus to the selected panel’s first useful control or heading. Focus remains trapped inside the open modal, Escape closes it, and Close returns focus to the light-bulb opener.

Loading messages use polite status announcements. Submission errors use the existing visible status area and do not remove entered data.

## Component boundaries

- The light-bulb launcher owns the unread indicator and opens the shared modal.
- The shared modal owns opening, closing, active-tab state, focus restoration, and draft reset on close.
- The Send feedback panel owns the existing chooser and form steps.
- The My feedback panel owns its request fetch, rendering, retry state, and post-submit refresh.
- The What’s new panel owns its release-note fetch, read state, retry state, and Mark all read action.

Each panel fails independently. No panel failure may close the modal or disable another panel.

## Data flow

1. The light bulb opens the modal on Send feedback.
2. Tab selection changes local interface state only.
3. My feedback fetches the existing personal feedback endpoint when needed.
4. What’s new fetches the existing changelog endpoint when needed.
5. A successful feedback POST triggers a My feedback refresh, then selects that tab.
6. What’s new read actions update the existing stored read state and unread dot.

No new backend model, Odoo field, Odoo code, webhook, task, or reference-table write is introduced.

## Verification

Automated and browser-level checks must cover:

- the light bulb defaults to Send feedback on every open;
- all three tabs switch within one modal;
- tab switching preserves an unsent description and screenshot;
- closing clears the draft and restores focus;
- a confirmed submission refreshes and selects My feedback;
- a failed submission preserves the form;
- Repair opens Maintenance and creates no feedback record;
- My feedback and What’s new load independently and can retry independently;
- opening the modal or My feedback does not clear the What’s new unread dot;
- What’s new viewing and read controls update the dot correctly;
- keyboard tab semantics, focus trapping, Escape, and focus restoration work;
- the full labels and controls fit at desktop and supported phone widths.

## Out of scope

- Changing the six feedback types or their Odoo outcomes.
- Changing who reviews a request.
- Changing task actions in Sales Manager or OS Manager.
- Saving unfinished drafts after the modal closes or across page reloads.
- Adding search, filters, counters, or new status summaries.
