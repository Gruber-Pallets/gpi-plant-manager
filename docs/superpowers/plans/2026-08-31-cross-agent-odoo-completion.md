# Cross-Agent Odoo Completion Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Codex, Claude Code, and Cursor one shared, safe rule for marking the exactly matching 2s Improvement Reference Data row complete only after implementation is fully delivered and verified.

**Architecture:** Keep the complete workflow in root `AGENTS.md`, which Codex and Cursor read directly. Import that canonical file from the first line of `CLAUDE.md`, preserving the existing Claude-only handoff below it; make no application, credential, connector, or Odoo-write code changes.

**Tech Stack:** Markdown project instructions, Claude Code `@AGENTS.md` import syntax, Cursor root `AGENTS.md` support, shell-based static validation, Git.

## Global Constraints

- `AGENTS.md` is the only canonical copy of the cross-agent Odoo completion rule.
- A plan is intended work only and never qualifies an improvement for completion.
- Complete every scoped implementation item, pass required validation, commit, and push to `origin/main` before attempting any lifecycle update.
- Match exactly one existing 2s Improvement Reference Data row; never create a row or guess between rows.
- Prefer an explicit Odoo ID or Plant Manager Source ID; otherwise require one clear title-and-description match.
- When Source is `GPI Plant Manager` and Source ID is `GPI-PM-FB-<positive id>`, complete the authoritative local Plant Manager feedback first and verify its mirror in Odoo. Never bypass local truth with an Odoo-only edit.
- For rows not owned by Plant Manager, use the authenticated Odoo completion workflow and change only completion fields required by that workflow.
- Verify the same Odoo row reads back with Status `Completed` before reporting Odoo as current.
- No match, multiple matches, unavailable access, failed write, failed synchronization, or failed readback leaves the task active or partially complete and must be reported to Dale.
- Never create, delete, archive, or merge Odoo improvement rows in this workflow.
- Do not add credentials, API keys, private URLs, a background service, or a new Odoo integration.
- Preserve the existing Claude handoff content below the import.
- Do not add a duplicate `.cursor/rules` file.
- Every push to `main` includes a short child-readable `CHANGELOG.md` note.
- Preserve the user's existing changes in `.superpowers/sdd/task-7-report.md`, `.cursorignore`, `.python-version`, and `uv.lock`; do not stage them.

---

## File structure

| File | Responsibility |
|---|---|
| `AGENTS.md` | Canonical completion gate, authoritative-source routing, exact-match rule, verification, and failure behavior for all agents. |
| `CLAUDE.md` | Import `AGENTS.md` once, before the existing Claude-specific handoff. |
| `CHANGELOG.md` | Explain the new work-helper behavior in short, plain language. |
| `docs/superpowers/specs/2026-08-31-cross-agent-odoo-completion-design.md` | Record that implementation is complete after final verification. |

### Task 1: Share and verify the Odoo completion rule

**Files:**
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md:1`
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-08-31-cross-agent-odoo-completion-design.md:4`

**Interfaces:**
- Consumes: Codex and Cursor root `AGENTS.md` discovery; Claude Code `@AGENTS.md` import expansion; existing Plant Manager feedback ownership contract from `docs/odoo-2s-feedback-operations.md`.
- Produces: one repository-wide instruction section named `Odoo 2s Improvement completion tracking`, imported once by Claude and read directly by Codex and Cursor.

- [ ] **Step 1: Add the canonical completion workflow to `AGENTS.md`**

Append this exact section after the existing delivery rules and before `What's New patch notes`:

```markdown
# Odoo 2s Improvement completion tracking

- This workflow applies when a user-requested task corresponds to an existing row in Odoo's **2s Improvement Reference Data** table.
- A plan is intended work only. Do not update Odoo until every scoped implementation item is complete, required validation passes, and the implementation commits are pushed to `origin/main`.
- Locate exactly one matching existing improvement. Prefer an Odoo ID or Plant Manager Source ID supplied with the task; otherwise require one clear title-and-description match. Never create a row or guess between matches.
- If Source is `GPI Plant Manager` and Source ID is `GPI-PM-FB-<positive id>`, complete the authoritative local feedback through Plant Manager's authenticated admin lifecycle with the current actor and a short plain-language resolution note. Wait for its existing mirror to synchronize; do not edit only the Odoo mirror.
- For an existing row not owned by Plant Manager, use the authenticated Odoo completion workflow to set Status to `Completed`. Fill only completion fields required by that workflow, using the current date, authenticated actor, and a short plain-language result. Do not change unrelated fields.
- Read the same Odoo row back and verify Status is `Completed`. State the verified Odoo result in the final handoff.
- If there is no single match, access is unavailable, a write or synchronization fails, or the readback is not `Completed`, leave Odoo unchanged where possible, keep the task active or partially complete, and report the blocker to Dale without exposing sensitive details.
- Never create, delete, archive, merge, or otherwise rewrite Odoo improvement rows as part of completion tracking.
```

- [ ] **Step 2: Make Claude load the canonical rule**

Insert this exact import and blank line before the existing first heading in `CLAUDE.md`:

```markdown
@AGENTS.md

# Claude Handoff — Recycled Smart Rotations
```

Do not rewrite, remove, or reorder the existing Claude handoff below the import.

- [ ] **Step 3: Add the implementation patch note and close the design status**

At the top of the existing `## 2026-08-31` changelog group, add:

```markdown
### Finished work stays matched with Odoo

- **The work helpers now check off the matching 2s improvement after a change is finished, checked, and saved.** They stop and ask for help if they cannot find or confirm one clear match.
```

Change the design header from:

```markdown
**Status:** Approved design, pending implementation plan
```

to:

```markdown
**Status:** Implemented
```

- [ ] **Step 4: Run static instruction validation**

Run:

```bash
test "$(sed -n '1p' CLAUDE.md)" = '@AGENTS.md'
test "$(rg -c '^@AGENTS\.md$' CLAUDE.md)" -eq 1
test "$(rg -c '^# Odoo 2s Improvement completion tracking$' AGENTS.md)" -eq 1
rg -F '# Claude Handoff — Recycled Smart Rotations' CLAUDE.md
rg -F 'complete the authoritative local feedback' AGENTS.md
rg -F 'verify Status is `Completed`' AGENTS.md
test ! -e .cursor/rules/odoo-completion.mdc
git diff --check
echo 'cross-agent Odoo completion instructions verified'
```

Expected output:

```text
# Claude Handoff — Recycled Smart Rotations
- If Source is `GPI Plant Manager` and Source ID is `GPI-PM-FB-<positive id>`, complete the authoritative local feedback through Plant Manager's authenticated admin lifecycle with the current actor and a short plain-language resolution note. Wait for its existing mirror to synchronize; do not edit only the Odoo mirror.
- Read the same Odoo row back and verify Status is `Completed`. State the verified Odoo result in the final handoff.
cross-agent Odoo completion instructions verified
```

- [ ] **Step 5: Review the focused diff and confirm unrelated files remain unstaged**

Run:

```bash
git diff -- AGENTS.md CLAUDE.md CHANGELOG.md docs/superpowers/specs/2026-08-31-cross-agent-odoo-completion-design.md
git status --short
```

Expected: the focused diff contains only the approved instruction, import, patch note, and design status. The pre-existing `.superpowers/sdd/task-7-report.md`, `.cursorignore`, `.python-version`, and `uv.lock` changes remain present but unstaged.

- [ ] **Step 6: Commit and push the implementation**

Run:

```bash
git add AGENTS.md CLAUDE.md CHANGELOG.md docs/superpowers/specs/2026-08-31-cross-agent-odoo-completion-design.md
git diff --cached --check
git commit -m "docs: share Odoo completion workflow across agents"
git push origin main
```

Expected: the commit and push succeed; `origin/main` contains the canonical rule and Claude import. Do not stage `.superpowers/sdd/task-7-report.md`, `.cursorignore`, `.python-version`, or `uv.lock`.
