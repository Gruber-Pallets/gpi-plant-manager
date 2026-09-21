# Invited users and roles

Approved by Dale in the task conversation on September 21, 2026.

Personal access requires both a gruberpallets.com Microsoft identity and an active invitation. Existing domain users receive no automatic access. Seed dale@gruberpallets.com as the initial Owner/Admin once, in durable storage. Subsequent role and access changes are authoritative on the next request, including existing sessions.

| Role | Permissions |
| --- | --- |
| Owner/Admin | All information and actions, including users, roles, integrations, and system settings |
| HR | Operational and HR information and edits; no user administration or system-wide changes |
| Manager | Operational information and edits; no pay, leave reasons/balances, employee notice history, or other sensitive HR information |
| Visitor | Read-only operational dashboards; no staffing administration or sensitive HR information |

Shop-floor TVs must continue exactly as they do today. Their anonymous display routes and existing device/IP fallback remain unchanged. The existing bearer-authenticated integration API retains its independent authentication; only admins may manage its credentials.

Admins use Users & Access to add an exact company email, select a role, change roles, and revoke access. Adding an invitation authorizes that Microsoft identity; the UI supplies the normal sign-in link to share, without sending email. Record who changed access and when. Prevent removing or demoting the last active admin, including concurrent changes. Do not silently recreate a revoked initial owner.

Enforce access on the server for pages, partials, JSON, exports, and writes. Classify routes explicitly and deny unclassified routes to non-admin roles. Mixed operational screens retain attendance availability and production information, but hide HR/pay details before rendering. HR-only workflows include leave approval/pay treatment, payroll hours, and employee acknowledgement history. Managers retain scheduling, skills, production attribution, and operational exception handling.

Navigation and editing controls reflect permissions. Separate rendered response caches by role, keeping TV/background rendering separate; use private no-store responses for signed-in browsers. Invitations and role changes must be validated server-side, with same-origin/CSRF protection on administration forms. Database failures deny personal access instead of falling back to domain-only access.

Validate the permission matrix, uninvited and revoked sessions, domain rejection, role changes, last-admin protection, CSRF, cross-role caching, sensitive operational fields, and unchanged TV access. Run relevant existing auth/route tests and the full available test suite. Commit and push implementation to origin/main only after required checks pass. A pushed design or plan is not feature completion.
