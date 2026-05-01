"""GET /changelog — renders CHANGELOG.md as HTML for the footer modal.

Tiny inline renderer; no external markdown lib. Supports:
- # Heading 1, ## Heading 2, ### Heading 3
- - bullet (one level)
- **bold** and *italic*
- Blank line separates blocks
- Backtick `code`

Plenty for the changelog format we use. Anything fancier — link, table —
authors should write the HTML inline (it gets escaped first, so they'd
need to use the actual HTML safely; for now we just don't use them).
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

router = APIRouter()

CHANGELOG_PATH = Path("CHANGELOG.md")


def _md_to_html(text: str) -> str:
    """Convert a small subset of markdown to HTML. Escape input first."""
    out_lines: list[str] = []
    in_list = False
    for raw in text.splitlines():
        line = html.escape(raw.rstrip())
        # Inline transforms (operate on escaped text so injected HTML stays inert).
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        # Italic (avoid clashing with bold; bold already consumed)
        line = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", line)

        if line.startswith("### "):
            if in_list:
                out_lines.append("</ul>")
                in_list = False
            out_lines.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            if in_list:
                out_lines.append("</ul>")
                in_list = False
            out_lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            if in_list:
                out_lines.append("</ul>")
                in_list = False
            out_lines.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("- "):
            if not in_list:
                out_lines.append("<ul>")
                in_list = True
            out_lines.append(f"<li>{line[2:]}</li>")
        elif line.strip() == "":
            if in_list:
                out_lines.append("</ul>")
                in_list = False
            out_lines.append("")
        else:
            if in_list:
                out_lines.append("</ul>")
                in_list = False
            out_lines.append(f"<p>{line}</p>")
    if in_list:
        out_lines.append("</ul>")
    return "\n".join(out_lines)


@router.get("/changelog", response_class=HTMLResponse)
def changelog_html() -> HTMLResponse:
    if not CHANGELOG_PATH.exists():
        return HTMLResponse("<p>No changelog yet.</p>")
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    return HTMLResponse(_md_to_html(text))


@router.get("/changelog.md", response_class=PlainTextResponse)
def changelog_raw() -> PlainTextResponse:
    if not CHANGELOG_PATH.exists():
        return PlainTextResponse("")
    return PlainTextResponse(CHANGELOG_PATH.read_text(encoding="utf-8"),
                             media_type="text/markdown; charset=utf-8")
