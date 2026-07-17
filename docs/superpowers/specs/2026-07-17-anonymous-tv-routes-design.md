# Anonymous TV Routes

## Goal

Physical plant TVs must continue to load their existing `/tv/...` URLs without
requiring a Microsoft sign-in, device token, or network-specific IP allowlist.

## Scope

- Permit unauthenticated requests to every route under `/tv/`.
- Preserve all current URL shapes, including saved display slugs, legacy
  `/tv/d/...` redirects, direct TV dashboard links, and `/tv/ping` refresh
  probes.
- Keep Microsoft authentication unchanged for every non-TV route.
- Keep device-token and IP-allowlist support compatible but no longer required
  for TV access.

## Security boundary

TV dashboard pages will be accessible to anyone who knows a `/tv/...` URL.
This is an explicit product decision to guarantee unattended displays work
without per-screen setup. Routes outside that prefix remain authenticated.

## Implementation and verification

The authentication middleware will treat `/tv/` as a tightly scoped bypass
prefix, alongside existing static and OIDC paths. Regression tests will prove
that an unauthenticated request to representative current TV URL forms reaches
its route, while an unauthenticated non-TV route still redirects to Microsoft
login.
