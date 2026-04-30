"""POST /staffing/share-to-slack — render the day's scheduler in
print mode, convert to PDF, upload to the configured Slack channel.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .. import slack_client
from .staffing import staffing_page

router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _format_comment(day: str) -> str:
    """Return e.g. 'Schedule for Tue 4/30' for the given YYYY-MM-DD."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return f"Schedule for {day}"
    weekday = d.strftime("%a")  # 'Mon', 'Tue', ...
    return f"Schedule for {weekday} {d.month}/{d.day}"


def _inline_static_css(html: str) -> str:
    """Replace each <link rel="stylesheet" href="/static/X.css..."> with an
    inline <style> block holding the file contents.

    WeasyPrint otherwise has to fetch the stylesheet over HTTP — which on
    Railway (TLS terminated at the edge) can fail or return wrong-scheme
    URLs, leaving the PDF unstyled. Reading from disk avoids the network
    round-trip and guarantees the PDF uses the same CSS the browser sees.
    """
    def _replace(match: re.Match) -> str:
        filename = match.group(1)
        css_path = _STATIC_DIR / filename
        try:
            css = css_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return match.group(0)  # leave the original link tag in place
        return f"<style>\n{css}\n</style>"

    pattern = re.compile(
        r'<link[^>]+href="/static/([^"?]+)(?:\?[^"]*)?"[^>]*>',
        re.IGNORECASE,
    )
    return pattern.sub(_replace, html)


def _render_pdf(html: str, base_url: str) -> bytes:
    """Render HTML to PDF bytes via Playwright (headless Chromium).

    Same rendering engine as the user's browser, so the PDF is
    byte-for-byte equivalent to what the browser's print preview shows.
    `base_url` is unused (kept for interface stability with the previous
    WeasyPrint implementation) — Playwright loads the document via
    `set_content` so external relative URLs would need a full page.goto.
    We pre-inline the /static CSS so no external fetches are required.
    """
    from playwright.sync_api import sync_playwright  # lazy — heavy dep
    inlined = _inline_static_css(html)
    with sync_playwright() as p:
        # --no-sandbox is required inside Railway's container — no
        # privileged user namespace available for Chromium's sandbox.
        browser = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page()
            page.set_content(inlined, wait_until="domcontentloaded")
            # page.pdf() defaults to print media — no need to call
            # emulate_media. print_background=True keeps colored
            # backgrounds (the schedule's row striping etc.).
            return page.pdf(
                format="Letter",
                landscape=False,
                print_background=True,
                margin={
                    "top": "0.4in",
                    "right": "0.4in",
                    "bottom": "0.4in",
                    "left": "0.4in",
                },
            )
        finally:
            browser.close()


@router.post("/staffing/share-to-slack")
def share_to_slack(
    request: Request,
    day: str = Query(...),
):
    """Render the day's scheduler -> PDF -> upload to Slack."""
    channel_id = os.environ.get("SLACK_CHANNEL_ID")
    if not channel_id:
        return JSONResponse(
            {"ok": False, "error": "Slack not configured (SLACK_CHANNEL_ID missing)"},
            status_code=500,
        )

    # 1. Render the scheduler page for this day by calling the existing
    #    handler as a regular function. The handler returns an
    #    HTMLResponse; we read its body for the HTML string.
    response = staffing_page(request, day=day)
    html = response.body.decode("utf-8")

    # 2. Render the HTML to PDF.
    try:
        pdf_bytes = _render_pdf(html, base_url=str(request.base_url))
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"PDF render failed: {e}"},
            status_code=500,
        )

    # 3. Upload to Slack.
    try:
        result = slack_client.upload_pdf(
            pdf_bytes,
            filename=f"schedule-{day}.pdf",
            channel_id=channel_id,
            initial_comment=_format_comment(day),
        )
    except slack_client.SlackError as e:
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=502,
        )

    return JSONResponse({
        "ok": True,
        "channel_name": result["channel_name"],
        "permalink": result["permalink"],
    })
