"""GET /changelog — renders CHANGELOG.md as HTML for the footer modal.

Tiny inline renderer; no external markdown lib. Supports:
- # Heading 1, ## Heading 2 (date YYYY-MM-DD), ### Heading 3 (deploy time, e.g. "9:43 AM")
- - bullet (one level)
- **bold** and *italic*
- Blank line separates blocks
- Backtick `code`

Each `### TIME` heading opens a `<section class="changelog-deploy" data-when="YYYY-MM-DDTHH:MM">`
that the footer JS uses to highlight unread deployments when the modal opens.
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


def _md_to_html(text: str) -> str:
    """Convert a small subset of markdown to HTML. Escape input first.

    Wraps each `### TIME` block (heading + following bullets) in a
    <section class="changelog-deploy" data-when="..."> so the footer JS
    can identify which deployments are newer than what the user has seen.
    """
    out_lines: list[str] = []
    in_list = False
    in_section = False
    last_h2_date: str | None = None

    def _close_list():
        nonlocal in_list
        if in_list:
            out_lines.append("</ul>")
            in_list = False

    def _close_section():
        nonlocal in_section
        if in_section:
            out_lines.append("</section>")
            in_section = False

    for raw in text.splitlines():
        line = html.escape(raw.rstrip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", line)

        if line.startswith("### "):
            _close_list()
            _close_section()
            time_text = line[4:]
            data_when = ""
            if last_h2_date:
                t24 = _parse_time_to_24h(time_text)
                if t24:
                    data_when = f"{last_h2_date}T{t24}"
            attr = f' data-when="{data_when}"' if data_when else ""
            out_lines.append(f'<section class="changelog-deploy"{attr}>')
            out_lines.append(f"<h3>{time_text}</h3>")
            in_section = True
        elif line.startswith("## "):
            _close_list()
            _close_section()
            heading = line[3:]
            m = re.match(r"^(\d{4}-\d{2}-\d{2})", heading)
            last_h2_date = m.group(1) if m else None
            out_lines.append(f"<h2>{heading}</h2>")
        elif line.startswith("# "):
            _close_list()
            _close_section()
            out_lines.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("- "):
            if not in_list:
                out_lines.append("<ul>")
                in_list = True
            out_lines.append(f"<li>{line[2:]}</li>")
        elif line.strip() == "":
            _close_list()
            out_lines.append("")
        else:
            _close_list()
            out_lines.append(f"<p>{line}</p>")

    _close_list()
    _close_section()
    return "\n".join(out_lines)


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
            t = _parse_time_to_24h(line[4:])
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
