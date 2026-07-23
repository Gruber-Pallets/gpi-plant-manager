# Kid-Friendly What's New Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require every future main-branch What's New entry to use short,
plain, user-focused language that a 10-year-old can understand.

**Architecture:** Add one durable source-of-truth instruction to the project
rules and repeat its practical writing checklist beside the `CHANGELOG.md`
format documentation. The app continues to render the same Markdown; only the
authoring standard changes.

**Tech Stack:** Markdown project documentation; Git.

## Global Constraints

- Apply the new standard only to patch notes added for future pushes to `main`.
- Leave historical `CHANGELOG.md` entries unchanged.
- Explain what changed and how it helps; use short sentences and common words.
- Do not include developer-only details, code names, routes, or implementation
  steps in user-facing patch notes.
- Explain any necessary unfamiliar word immediately.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `AGENTS.md` | Permanent project rule for anyone preparing a main-branch push. |
| `CHANGELOG.md` | At-the-point-of-writing reminder of the plain-language format. |

### Task 1: Add the permanent patch-note writing standard

**Files:**

- Modify: `AGENTS.md:1-15`
- Modify: `CHANGELOG.md:4-8`
- Test: No automated test applies; this task changes authoring documentation
  only, so use the explicit content checks below.

**Interfaces:**

- Consumes: The changelog format established in `CHANGELOG.md`.
- Produces: A mandatory writing rule for future main-branch patch notes.

- [ ] **Step 1: Add the project-level rule to `AGENTS.md`**

  Add this section after the existing delivery rules:

  ```markdown
  # What's New patch notes

  - For every push to `main`, write any new `CHANGELOG.md` / What's New patch
    notes so a 10-year-old can understand them.
  - Use short sentences and common words. Say what changed and how it helps the
    person using the app. Leave out developer-only details, code names, routes,
    and implementation steps. If an unfamiliar word is needed, explain it
    right away.
  - Keep historical patch notes unchanged; apply this rule only to new entries.
  ```

- [ ] **Step 2: Add the beside-the-changelog reminder to `CHANGELOG.md`**

  Extend the existing HTML format comment immediately below the title so it
  reads:

  ```markdown
  <!-- Format: `## YYYY-MM-DD` date, then `### TIME - Optional Title` per deploy.
       Within a deploy, optional `#### Features` / `#### Fixes` group the bullets
       (anything ungrouped renders as "Highlights"). An entry with a Features
       group shows a green "New feature" badge in the What's New panel.
       Write new entries for a 10-year-old: use short, common words; say what
       changed and how it helps; skip developer-only details, code names, routes,
       and implementation steps; explain any unfamiliar word right away. -->
  ```

- [ ] **Step 3: Verify the exact new guidance without altering old entries**

  Run:

  ```bash
  rg -n -A 8 "What's New patch notes|Format:" AGENTS.md CHANGELOG.md
  git diff --check
  git diff -- AGENTS.md CHANGELOG.md
  ```

  Expected: both files contain the child-friendly writing rule; there are no
  whitespace errors; the diff changes only the instructions and format comment,
  not historical entries.

- [ ] **Step 4: Commit and push the implementation**

  Run:

  ```bash
  git add AGENTS.md CHANGELOG.md
  git commit -m "docs: simplify whats new patch note guidance"
  git push origin main
  ```

  Expected: the commit is created and `main` is pushed to `origin`.

## Plan Self-Review

- **Spec coverage:** Task 1 adds the standard to both required locations,
  limits it to future main-branch notes, preserves history, and includes every
  approved writing rule.
- **Placeholder scan:** Every step has its exact content and verification
  command; nothing is deferred.
- **Consistency:** The exact rule in `AGENTS.md` matches the practical reminder
  in `CHANGELOG.md`.
