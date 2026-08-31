# Delivery and completion rules

- When a plan is finalized, commit and push it to `origin/main` automatically. Do not wait for approval to perform the routine plan commit or push.
- Treat a pushed plan as a record of intended work only. It never means the related feature, task, or implementation is complete.
- Do not archive or mark a task complete just because its plan is committed or pushed. Before archiving, verify that every scoped implementation item is complete, the implementation commits are pushed to `origin/main`, and the required validation has passed.
- If any planned work is deferred, incomplete, or unverified, keep the task active and state that it is partially complete.
- Continue routine implementation, commits, and pushes without requesting confirmation; surface only genuine blockers, failed verification, or choices that materially change scope.

# Odoo 2s Improvement completion tracking

- This workflow applies when a user-requested task corresponds to an existing row in Odoo's **2s Improvement Reference Data** table.
- A plan is intended work only. Do not update Odoo until every scoped implementation item is complete, required validation passes, and the implementation commits are pushed to `origin/main`.
- Locate exactly one matching existing improvement. Prefer an Odoo ID or Plant Manager Source ID supplied with the task; otherwise require one clear title-and-description match. Never create a row or guess between matches.
- If Source is `GPI Plant Manager` and Source ID is `GPI-PM-FB-<positive id>`, complete the authoritative local feedback through Plant Manager's authenticated admin lifecycle with the current actor and a short plain-language resolution note. Wait for its existing mirror to synchronize; do not edit only the Odoo mirror.
- For an existing row not owned by Plant Manager, use the authenticated Odoo completion workflow to set Status to `Completed`. Fill only completion fields required by that workflow, using the current date, authenticated actor, and a short plain-language result. Do not change unrelated fields.
- Read the same Odoo row back and verify Status is `Completed`. State the verified Odoo result in the final handoff.
- If there is no single match, access is unavailable, a write or synchronization fails, or the readback is not `Completed`, leave Odoo unchanged where possible, keep the task active or partially complete, and report the blocker to Dale without exposing sensitive details.
- Never create, delete, archive, merge, or otherwise rewrite Odoo improvement rows as part of completion tracking.

# What's New patch notes

- For every push to `main`, write any new `CHANGELOG.md` / What's New patch
  notes so a 10-year-old can understand them.
- Use short sentences and common words. Say what changed and how it helps the
  person using the app. Leave out developer-only details, code names, routes,
  and implementation steps. If an unfamiliar word is needed, explain it
  right away.
- Keep historical patch notes unchanged; apply this rule only to new entries.

# Coding request repository safety

This repository's canonical identity is `gpi-plant-manager`.

When a pasted coding request contains `TARGET REPOSITORY`, compare it with this
repository before editing files or running mutating commands. If it is not
`gpi-plant-manager`, stop and tell Dale which repository the request requires.
Never use the task's Odoo Project to override `TARGET REPOSITORY`.
