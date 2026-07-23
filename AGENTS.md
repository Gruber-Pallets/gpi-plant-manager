# Delivery and completion rules

- When a plan is finalized, commit and push it to `origin/main` automatically. Do not wait for approval to perform the routine plan commit or push.
- Treat a pushed plan as a record of intended work only. It never means the related feature, task, or implementation is complete.
- Do not archive or mark a task complete just because its plan is committed or pushed. Before archiving, verify that every scoped implementation item is complete, the implementation commits are pushed to `origin/main`, and the required validation has passed.
- If any planned work is deferred, incomplete, or unverified, keep the task active and state that it is partially complete.
- Continue routine implementation, commits, and pushes without requesting confirmation; surface only genuine blockers, failed verification, or choices that materially change scope.

# What's New patch notes

- For every push to `main`, write any new `CHANGELOG.md` / What's New patch
  notes so a 10-year-old can understand them.
- Use short sentences and common words. Say what changed and how it helps the
  person using the app. Leave out developer-only details, code names, routes,
  and implementation steps. If an unfamiliar word is needed, explain it
  right away.
- Keep historical patch notes unchanged; apply this rule only to new entries.
