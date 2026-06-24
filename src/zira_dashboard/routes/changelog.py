"""GET /changelog — renders CHANGELOG.md as HTML for the What's New modal.

Tiny inline renderer; no external markdown lib. Supports:
- ## Heading 2 (date YYYY-MM-DD), ### Heading 3 (deploy time, e.g. "9:43 AM")
- #### Features/Fixes group headings; otherwise bullets render as Highlights
- **bold** and *italic*
- Backtick `code`

Each `### TIME` heading renders one `<article class="cl-entry">` card with a
stable key for read-state tracking in the richer What's New panel.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

router = APIRouter()

CHANGELOG_PATH = Path("CHANGELOG.md")


def _parse_time_to_24h(s: str) -> str | None:
    """Parse '9:43 AM', '2:15 PM', '11:00am' etc. into 24h 'HH:MM'. None on fail."""
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([apAP]\.?[mM]\.?)\s*$", s)
    if not m:
        return None
    h = int(m.group(1))
    mm = int(m.group(2) or 0)
    period = m.group(3).lower().replace(".", "")
    if h == 12:
        h = 0
    if period.startswith("p"):
        h += 12
    return f"{h:02d}:{mm:02d}"


def _heading_time_text(s: str) -> str:
    return re.split(r"\s+[—-]\s+", s.strip(), maxsplit=1)[0].strip()


def _fmt_inline(text: str) -> str:
    """Escape, then apply the small inline markdown subset."""
    line = html.escape(text.rstrip())
    line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
    line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
    line = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", line)
    return line


def _parse_entries(text: str) -> list[dict]:
    """Parse CHANGELOG.md into one card entry per deploy heading."""
    entries: list[dict] = []
    cur_date: str | None = None
    date_counts: dict[str | None, int] = {}
    entry: dict | None = None
    group = "highlights"

    def push() -> None:
        nonlocal entry
        if entry is not None:
            entries.append(entry)
            entry = None

    for raw in text.splitlines():
        s = raw.rstrip()
        if s.startswith("#### "):
            label = s[5:].strip().lower()
            if entry is None:
                continue
            if label.startswith("feature"):
                group = "features"
            elif label.startswith("fix"):
                group = "fixes"
            else:
                group = "highlights"
        elif s.startswith("### "):
            push()
            head = s[4:].strip()
            parts = re.split(r"\s+[—-]\s+", head, maxsplit=1)
            time_text = _heading_time_text(head)
            title = parts[1].strip() if len(parts) > 1 else None
            t24 = _parse_time_to_24h(time_text)
            idx = date_counts.get(cur_date, 0)
            date_counts[cur_date] = idx + 1
            if cur_date and t24:
                key = f"{cur_date}T{t24}"
            elif cur_date:
                key = f"{cur_date}#{idx}"
            else:
                key = f"entry#{len(entries)}"
            entry = {
                "date": cur_date,
                "title": title,
                "key": key,
                "features": [],
                "fixes": [],
                "highlights": [],
            }
            group = "highlights"
        elif s.startswith("## "):
            push()
            m = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})", s)
            cur_date = m.group(1) if m else (s[3:].strip() or None)
        elif s.startswith("# "):
            push()
        elif s.startswith("- ") and entry is not None:
            entry[group].append(_fmt_inline(s[2:]))
    push()
    return entries


def _render_entry(e: dict) -> str:
    has_feature = bool(e["features"])
    out = [
        f'<article class="cl-entry" data-key="{html.escape(e["key"], quote=True)}" '
        f'data-feature="{"1" if has_feature else "0"}">',
        '<header class="cl-entry-head">',
    ]
    if e["title"]:
        out.append(f'<span class="cl-entry-title">{_fmt_inline(e["title"])}</span>')
    if e["date"]:
        out.append(f'<span class="cl-entry-date">{html.escape(e["date"])}</span>')
    if has_feature:
        out.append('<span class="cl-badge">New feature</span>')
    out.append("</header>")

    def group_html(label: str, items: list[str]) -> str:
        if not items:
            return ""
        lis = "".join(f"<li>{x}</li>" for x in items)
        return (
            f'<div class="cl-group"><h4 class="cl-group-title">{label}</h4>'
            f"<ul>{lis}</ul></div>"
        )

    out.append(group_html("Features", e["features"]))
    out.append(group_html("Fixes", e["fixes"]))
    out.append(group_html("Highlights", e["highlights"]))
    out.append(
        f'<button type="button" class="cl-markread" '
        f'data-key="{html.escape(e["key"], quote=True)}">Mark read</button>'
    )
    out.append("</article>")
    return "".join(out)


def _md_to_html(text: str) -> str:
    """Render CHANGELOG.md as a stack of per-deploy cards."""
    return "\n".join(_render_entry(e) for e in _parse_entries(text))


def _latest_deploy_when(text: str) -> str | None:
    """Return the most recent 'YYYY-MM-DDTHH:MM' identifier in the changelog,
    or just 'YYYY-MM-DD' if the latest day has no time-of-day sub-headings yet.
    """
    latest_date: str | None = None
    latest_time: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            if latest_date is not None:
                # We've already captured the most-recent date; if it has a
                # time-of-day, return that combo. Stop walking — earlier dates
                # don't matter for "latest".
                break
            m = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})", line)
            if m:
                latest_date = m.group(1)
        elif line.startswith("### ") and latest_date is not None and latest_time is None:
            t = _parse_time_to_24h(_heading_time_text(line[4:]))
            if t:
                latest_time = t
    if latest_date and latest_time:
        return f"{latest_date}T{latest_time}"
    return latest_date


@router.get("/changelog", response_class=HTMLResponse)
def changelog_html() -> HTMLResponse:
    if not CHANGELOG_PATH.exists():
        return HTMLResponse("<p>No changelog yet.</p>")
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    return HTMLResponse(_md_to_html(text))


# Parsed /changelog/latest result keyed on the file's mtime — every page's
# footer polls the endpoint, but the file only changes on deploy.
_latest_memo: tuple[float, str | None] | None = None


@router.get("/changelog/latest")
def changelog_latest() -> JSONResponse:
    """Return the most recent deployment identifier as ISO date or
    date+time ('YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM').

    Frontend uses this to show an unread dot when there's an entry newer
    than localStorage.changelog_seen, and to highlight new sections inside
    the modal.
    """
    global _latest_memo
    try:
        mtime = CHANGELOG_PATH.stat().st_mtime
    except OSError:
        return JSONResponse({"latest_date": None})
    if _latest_memo is None or _latest_memo[0] != mtime:
        text = CHANGELOG_PATH.read_text(encoding="utf-8")
        _latest_memo = (mtime, _latest_deploy_when(text))
    return JSONResponse({"latest_date": _latest_memo[1]})


@router.get("/changelog.md", response_class=PlainTextResponse)
def changelog_raw() -> PlainTextResponse:
    if not CHANGELOG_PATH.exists():
        return PlainTextResponse("")
    return PlainTextResponse(CHANGELOG_PATH.read_text(encoding="utf-8"),
                             media_type="text/markdown; charset=utf-8")
