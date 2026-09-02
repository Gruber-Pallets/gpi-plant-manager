# People Manager Strip Cache Recovery Design

## Goal

Restore the People page manager bar when a browser has cached an older
stylesheet, and prevent a short rolling deployment from leaving that browser
with mismatched page structure and styles for a year.

The visible result remains the approved two-band layout: totals and controls
form the first band, while source warnings form a separate wrapping band below
it. Nothing overlaps, all controls remain reachable, and the page does not
gain horizontal scrolling.

## Confirmed root cause

The current source contains the correct two-band HTML and CSS. The screenshot
is reproduced exactly when the current nested manager-bar HTML is rendered
with the stylesheet from the preceding release.

During a Railway rolling deployment, one request can reach the new app
instance and receive the new HTML while its subsequent stylesheet request
reaches an old instance. Starlette serves the static path regardless of the
query-string version, so the old instance can return the old file for the new
versioned URL. The app currently marks every static response as immutable for
one year. The browser therefore keeps the incompatible response even after all
new instances are healthy.

The existing browser fixture verifies only a page whose HTML and CSS come from
the same checkout, so it cannot detect this deployment-boundary failure.

## Approved approach

Use browser revalidation for static assets instead of a year-long immutable
cache, and change the People stylesheet so affected browsers request a fresh
URL.

Static responses will remain publicly cacheable, but their cache policy will
require revalidation before reuse. The existing version query remains useful:
unchanged assets can receive a lightweight conditional response, while a
changed file gets fetched again. If an old instance answers during a rolling
deployment, that response is no longer trusted indefinitely; the next page
load revalidates it against the healthy release.

The People stylesheet will receive an explicit, useful layout declaration so
its version token changes. The declaration will make the manager strip's
single full-width grid column explicit and preserve automatic row height. This
both clears the already cached bad response and reinforces the intended
two-band layout without duplicating the stylesheet inline.

## Layout contract

- `.pp-manager-strip` is an automatically sized one-column grid.
- `.pp-manager-primary` contains totals and the action group. Its children may
  wrap when needed.
- `.pp-manager-actions` contains the updated time and filters. Its children may
  wrap without escaping the primary band's measured height.
- `.pp-source-warnings` follows the primary group as its own full-width grid
  row and wraps its pills.
- No manager-strip group uses horizontal scrolling.
- With no source warnings, only the primary band is rendered.
- The manager strip remains sticky and may grow vertically to fit its content.

Counts, warning text, filter behavior, polling, focus restoration, person rows,
and performance calculations remain unchanged.

## Cache behavior

Static files will use a cache policy equivalent to `public, no-cache`. Browsers
may retain a local copy, but must validate it before using it again. Existing
ETag or Last-Modified handling can then return an inexpensive not-modified
response when the file is unchanged.

This deliberately trades a small conditional request for reliable upgrades.
The app is an authenticated internal tool, so preventing a broken interface
after deployment is more important than eliminating those requests.

The HTML page and People stylesheet continue to use `static_v(...)`. Changing
the stylesheet produces a new URL and releases browsers that already stored
the incompatible response under the preceding URL.

## Verification

Automated coverage will verify:

- static responses require revalidation and are no longer marked immutable;
- the People stylesheet URL still uses `static_v(...)`;
- the manager strip explicitly uses one full-width grid column with automatic
  height;
- the busy fixture includes several representative production and forklift
  warnings;
- totals, action controls, and warning pills have non-overlapping bounding
  boxes at desktop, tablet, portrait-tablet, and phone widths;
- every manager-strip descendant remains inside the strip's measured bounds;
- the page has no manager-strip horizontal overflow; and
- the focused People tests and full relevant browser checks pass.

A final screenshot review will use the warning strings shown in the reported
failure. It must show the first band completely above the warning band, with
the person rows beginning below the full manager strip.

## Rejected alternatives

### Change only the stylesheet URL

This clears the current bad cache but leaves the same year-long poisoning path
available during a future rolling deployment.

### Put the manager layout inline

Inline critical styles would override an older external stylesheet, but they
would duplicate page CSS inside the template and solve only this component.

### Fingerprinted static filenames

Content-addressed filenames are the strongest general asset pipeline, but the
current per-instance static server cannot guarantee that an old instance has a
new filename during a rolling deployment. Adopting shared asset storage or a
separate deployment pipeline is much larger than this repair requires.
