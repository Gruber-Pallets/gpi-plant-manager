"""One-off: render all kiosk screens to a single HTML preview file.

Usage:
    python scripts/render_kiosk_preview.py

Outputs kiosk_preview.html at the project root with all 6 kiosk screens
rendered into iframes. Pure Jinja2 — no DB / env vars / running server
required. Buttons and forms won't navigate; this is for visual review.
"""

from __future__ import annotations

import base64
import html as html_module
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path("src/zira_dashboard/templates")
STATIC_DIR = Path("src/zira_dashboard/static")
env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=True,
)
# The live app registers static_v() to cache-bust /static URLs by mtime.
# Stub it to a constant for the standalone preview render.
env.globals["static_v"] = lambda _filename: "preview"


def _logo_data_url() -> str:
    """Inline the Gruber logo as a data URL so the static preview server
    (which doesn't have FastAPI's /static mount) can render it."""
    b = (STATIC_DIR / "gpi-logo.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(b).decode("ascii")


_LOGO_DATA_URL = _logo_data_url()


mock_people = sorted(
    [
        {"id": 1, "name": "Dale Gruber"},
        {"id": 2, "name": "Maria Garcia"},
        {"id": 3, "name": "Jose O"},
        {"id": 4, "name": "Eulogio Mendez"},
        {"id": 5, "name": "Isidro Pena"},
        {"id": 6, "name": "Pedro Martinez"},
        {"id": 7, "name": "Juan Hernandez"},
        {"id": 8, "name": "Roberto Silva"},
        {"id": 9, "name": "Carlos Lopez"},
        {"id": 10, "name": "Miguel Torres"},
    ],
    key=lambda p: p["name"].lower(),
)
# Pick Dale for downstream PIN / dashboard mocks (preview-only consumer).
dale = next(p for p in mock_people if p["name"] == "Dale Gruber")

mock_wcs = [
    {"name": "Repair 1", "bay": "Bay 1", "department": "Recycled"},
    {"name": "Repair 2", "bay": "Bay 1", "department": "Recycled"},
    {"name": "Repair 3", "bay": "Bay 1", "department": "Recycled"},
    {"name": "Dismantler 4", "bay": "Bay 2", "department": "Recycled"},
    {"name": "Dismantler 3", "bay": "Bay 2", "department": "Recycled"},
    {"name": "Dismantler 2", "bay": "Bay 3", "department": "Recycled"},
    {"name": "Dismantler 1", "bay": "Bay 3", "department": "Recycled"},
    {"name": "Trim Saw 1", "bay": "Bay 4", "department": "Recycled"},
    {"name": "Master Recycler", "bay": "Bay 4", "department": "Recycled"},
    {"name": "Repair 4", "bay": "Bay 5", "department": "Recycled"},
    {"name": "Repair 5", "bay": "Bay 5", "department": "Recycled"},
    {"name": "Hand Build #2", "bay": "Bay 5", "department": "New"},
    {"name": "Hand Build #1", "bay": "Bay 6", "department": "New"},
    {"name": "Chop/Notch", "bay": "Bay 14", "department": "New"},
    {"name": "Big Build #1", "bay": "Bay 14", "department": "New"},
    {"name": "Woodpecker #1", "bay": "Bay 16", "department": "New"},
    {"name": "Junior #1", "bay": "Bay 16", "department": "New"},
    {"name": "Junior #2", "bay": "Bay 17", "department": "New"},
    {"name": "Junior #3", "bay": "Bay 17", "department": "New"},
    {"name": "Loading/Jockeying", "bay": "Forklift", "department": "Supervisor"},
    {"name": "Tablets", "bay": "Forklift", "department": "Supervisor"},
    {"name": "Work Orders", "bay": "Maint.", "department": "Maintenance"},
]

mock_token = "1:1234567890:abcdef0123456789"

screens = [
    {
        "title": "1. Home — tap your name to start",
        "subtitle": "Alphabetical scrollable list with sticky search at top. Tap a name to go straight to your dashboard (no PIN).",
        "template": "kiosk_home.html",
        "context": {"people": mock_people},
    },
    {
        "title": "2. Dashboard — clocked OUT, today's schedule shown",
        "subtitle": "Big Confirm + smaller 'I'm somewhere else' override. Overrides log to kiosk_schedule_variances for supervisor review.",
        "template": "kiosk_dashboard.html",
        "context": {
            "person": dale,
            "token": mock_token,
            "is_clocked_in": False,
            "scheduled_wc": "Repair 1",
        },
    },
    {
        "title": "3. Dashboard — clocked OUT, not scheduled today",
        "subtitle": "Fallback when staffing has no assignment — pick any WC.",
        "template": "kiosk_dashboard.html",
        "context": {
            "person": dale,
            "token": mock_token,
            "is_clocked_in": False,
            "scheduled_wc": None,
        },
    },
    {
        "title": "4. Dashboard — clocked IN",
        "subtitle": "Shows where you're punched in. Big red Clock Out + Transfer button.",
        "template": "kiosk_dashboard.html",
        "context": {
            "person": dale,
            "token": mock_token,
            "is_clocked_in": True,
            "current_wc": "Repair 1",
            "check_in_display": "2:30 PM",
            "scheduled_wc": "Repair 1",
        },
    },
    {
        "title": "5. Pick Work Center — 22 WCs grouped by bay",
        "subtitle": "Used for both clock-in override and mid-shift transfer.",
        "template": "kiosk_pick_wc.html",
        "context": {
            "person": dale,
            "token": mock_token,
            "purpose": "transfer",
            "scheduled": "",
            "work_centers": mock_wcs,
        },
    },
    {
        "title": "6. Success — auto-returns home in 3s",
        "subtitle": "Always reassures the user. Sync warning appears if Odoo was unreachable.",
        "template": "kiosk_success.html",
        "context": {
            "person": dale,
            "message": "Clocked in to Repair 1",
            "time": "2:32 PM",
        },
    },
    {
        "title": "7. Dashboard — with persistent Odoo sync error",
        "subtitle": "Banner appears when there are person-specific punches that already failed to sync at least once. Pending-sync (not yet attempted) is intentionally not warned about.",
        "template": "kiosk_dashboard.html",
        "context": {
            "person": dale,
            "token": mock_token,
            "is_clocked_in": False,
            "scheduled_wc": "Repair 1",
            "sync_warning": {
                "count": 2,
                "latest_error": "401 Unauthorized — check ODOO_API_KEY",
                "latest_at_display": "5/26 2:14 PM",
            },
        },
    },
    {
        "title": "8. Success — with Odoo sync warning",
        "subtitle": "When Odoo was unreachable: punch saved locally, kiosk worker retries every 60s.",
        "template": "kiosk_success.html",
        "context": {
            "person": dale,
            "message": "Clocked out",
            "time": "5:01 PM",
            "sync_error": "Connection refused",
        },
    },
]


def render_screen(template_name: str, context: dict) -> str:
    """Render a template, then patch a couple of paths that work in the
    live FastAPI app but not in this bare-bones static preview server:
    (1) neutralize the idle/success auto-redirect to /kiosk, and
    (2) rewrite /static/gpi-logo.png to an inline data URL so the brand
    logo renders without a /static mount."""
    html = env.get_template(template_name).render(**context)
    html = html.replace(
        "location.href = '/kiosk'",
        "console.log('preview: would navigate to /kiosk')",
    )
    html = html.replace("/static/gpi-logo.png", _LOGO_DATA_URL)
    # Strip the htmx <script> tag — the static preview iframes don't
    # navigate, so htmx does nothing useful here and the /static path
    # isn't served by `python -m http.server` from the repo root.
    import re
    html = re.sub(
        r'<script src="/static/htmx-[^"]+"[^>]*></script>\s*',
        "",
        html,
    )
    return html


def build_combined() -> str:
    parts = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        "<title>Timeclock Preview — All Screens</title>",
        "<style>",
        "body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;",
        "  margin:0;background:#f1f5f9;color:#0f172a;}",
        ".hdr{padding:1.25rem 1.5rem;background:#0b1220;color:#f1f5f9;}",
        ".hdr h1{margin:0;font-size:1.5rem;}",
        ".hdr p{margin:0.4rem 0 0 0;color:#94a3b8;font-size:0.95rem;}",
        ".sep{padding:1rem 1.5rem;background:#0f172a;color:#fbbf24;",
        "  font-weight:600;font-size:1.05rem;border-top:3px solid #334155;}",
        ".sep small{display:block;color:#94a3b8;font-weight:400;",
        "  margin-top:0.25rem;font-size:0.9rem;}",
        "iframe{width:100%;height:780px;border:0;display:block;background:#0f172a;}",
        "</style></head><body>",
        "<div class='hdr'>",
        "<h1>Timeclock — All Screens</h1>",
        "<p>Static preview: forms and links don't navigate. Each iframe ",
        "renders exactly what FastAPI would serve at the live endpoint.</p>",
        "</div>",
    ]
    for s in screens:
        rendered = render_screen(s["template"], s["context"])
        escaped = html_module.escape(rendered, quote=True)
        parts.append(
            f"<div class='sep'>{html_module.escape(s['title'])}"
            f"<small>{html_module.escape(s['subtitle'])}</small></div>"
        )
        parts.append(f'<iframe srcdoc="{escaped}"></iframe>')
    parts.append("</body></html>")
    return "\n".join(parts)


if __name__ == "__main__":
    out = build_combined()
    out_path = Path("kiosk_preview.html")
    out_path.write_text(out, encoding="utf-8")
    print(f"Wrote {out_path} ({len(out):,} bytes, {len(screens)} screens)")
