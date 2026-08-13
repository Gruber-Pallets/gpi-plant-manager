# Monthly Ribbon Winners Announce

**Date:** 2026-08-13
**Status:** Approved (brainstorming → implementation planning)

## Context

Monthly ribbons (gold / silver / bronze per production group) already
exist via `awards.monthly_badges` and show on operator dashboards, the
Trophy Case, and leaderboard TVs. There is no deliberate celebration
when a month closes.

Dale wants a short, visible moment on the first operating business day
of the new month so operators and managers can celebrate last month’s
ribbon winners together.

## Goals

1. On the **first plant workday on or after the 1st** of a month, show
   a plant-wide banner announcing the **previous calendar month’s**
   ribbon podiums.
2. Show the banner on **operator TVs** (`/tv/wc/...`) and on the
   **Recycling** and **New** dashboards for managers (desktop and TV
   variants of `/recycling` and `/new`).
3. Show **gold, silver, and bronze** for every included group; the
   **same content on every surface**.
4. Include only groups/areas with **> 0 production** for that month so
   unfinished / not-yet-piped areas stay hidden.
5. Keep the banner in the **TV header center** (same slot as GOAT
   Watch) so it never covers floor widgets. Stay up **all day** with
   **no dismiss**.

## Non-goals

- No Slack, email, or Inbox notification.
- No dismiss / per-browser “seen” state.
- No change to how ribbons are computed or overridden.
- No announce on the interactive operator editor (`/wc/...` non-TV).
- No Forklift-only or other Performance pages in v1.
- No multi-day window — only that single first workday.

## Binding decisions

| Topic | Decision |
|---|---|
| Timing | First `shift_config.is_workday` day of the new month (on or after the 1st). One calendar day only. |
| Month shown | Previous calendar month |
| Surfaces | Operator TV; Recycling + New (desktop + TV) |
| Content | Full podium per included group; plant-wide identical payload |
| Empty groups | Omit groups with no production (> 0) / no podium entries |
| Placement | Header center slot (GOAT Watch pattern); desktop Recycling/New use the existing non-TV banner spot |
| Persistence | All day; not dismissible |
| GOAT collision | Stack: ribbon winners above GOAT Watch, still inside the header column |
| Data | Live from `awards.monthly_badges` + existing overrides; no new tables |

## Design

### 1. Announce-day gate

```python
def is_ribbon_announce_day(today: date) -> bool:
    """True iff `today` is the first plant workday of its month."""
```

- Walk days from the 1st of `today.month` through `today`.
- The announce day is the first `d` where `shift_config.is_workday(d)`.
- Return whether `today` equals that day.
- If the entire month somehow has no workday before/at `today`, the
  gate is false (banner stays off).

### 2. Payload

```python
def previous_month(today: date) -> tuple[int, int]:
    """(year, month) for the calendar month before `today`."""

def ribbon_announce_payload(today: date) -> dict | None:
    """
    None when not announce day or nothing to show.
    Else:
      {
        "year": int,
        "month": int,
        "label": str,   # e.g. "July 2026"
        "groups": [
          {
            "group": str,
            "entries": [  # up to 3 from monthly_badges
              {"position": 1|2|3, "name": str, "day": date,
               "units": float, "pph": float},
              ...
            ],
          },
          ...
        ],
      }
    """
```

Rules:

- Call only when `is_ribbon_announce_day(today)`.
- Iterate `work_centers_store.registered_groups()` in stable order.
- For each group, if total production units for the previous month
  across that group’s WCs is **not** `> 0`, skip the group.
- Otherwise call `awards.monthly_badges(group, year, month)` (overrides
  already applied). Skip groups with an empty podium.
- Prefer one covering `daily_records` fetch for the previous month so
  the page path does not fan out per group.
- On any unexpected error in the route wiring, omit the banner; never
  fail the dashboard.

Helpers live in a small dedicated module `ribbon_announce.py` (thin
wrappers over `awards` / `shift_config` / `work_centers_store`); keep
them pure and unit-tested.

### 3. Surfaces & templates

| Surface | Route / render | Include? |
|---|---|---|
| Operator TV | `/tv/wc/{slug}` | Yes — header center |
| Operator editor | `/wc/{slug}` | No |
| Recycling desktop | `/recycling` | Yes — existing non-TV banner spot |
| Recycling TV | `/tv/recycling` | Yes — header center |
| New desktop | `/new` | Yes — existing non-TV banner spot |
| New TV | `/tv/new` | Yes — header center |

New partial: `templates/_ribbon_winners_banner.html`.

- Renders only when `ribbon_announce` is present and has groups.
- Title like “{Month} Ribbon Winners”.
- Per group: group name + 🥇 / 🥈 / 🥉 chips with person name; optional
  compact units/day meta when it fits without overflowing the header.
- Styles in a dedicated CSS file (sibling to `goat_watch.css`), linked
  from the host templates’ `<head>`.
- Compact enough for the TV header center; must not spill over the
  widget grid.

Wiring mirrors GOAT Watch: routes pass `ribbon_announce` into template
context; TV templates put the partial inside `{% call tv_header(...) %}
...{% endcall %}`; desktop Recycling/New include it in the same place
they already show `_goat_watch_banner.html` outside TV mode.

### 4. GOAT Watch coexistence

When both banners have content on the same day, the header center
**stacks**:

1. Ribbon winners banner (top)
2. GOAT Watch banner (below)

Both remain inside `tv-header-center` / the desktop banner strip. Neither
covers widgets.

### 5. Testing

Focused unit tests for:

- Announce day when the 1st is a workday.
- Announce day when the 1st–2nd are non-workdays and the 3rd is the
  first workday (only the 3rd is true; the 4th is false).
- Mid-month days are never announce days.
- Groups with zero production are omitted; groups with production and
  podium entries appear.
- Payload is `None` off announce day.
- Route/context smoke: operator TV + Recycling + New receive the
  payload on announce day and omit it otherwise (and operator non-TV
  never requests it).

## Out of scope / later

- Celebrating on other leaderboards or the Trophy Case landing.
- Auto-rotating TV “slide” distinct from the header banner.
- Per-person highlight when the winner is scheduled on that WC’s TV.
