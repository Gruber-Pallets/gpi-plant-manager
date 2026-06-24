from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from datetime import datetime

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path("src/zira_dashboard/templates")


def _env():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    env.globals["static_v"] = lambda _f: "test"
    return env


def test_admin_device_token_fields_have_accessible_names():
    html = _env().get_template("admin_devices.html").render(
        active="settings",
        tokens=[],
        just_minted=SimpleNamespace(name="Bay 3 TV", signed="signed-token"),
        host="plant.example",
    )

    assert 'name="name"' in html
    assert 'aria-label="Device display name"' in html
    assert "https://plant.example/tv/recycling?device=signed-token" in html
    assert 'aria-label="New device token URL"' in html


def test_admin_device_revoke_button_names_device():
    token = SimpleNamespace(
        id=12,
        name="Bay 3 TV",
        created_at=datetime(2026, 6, 24, 8, 30),
        created_by="Dale",
        last_used_at=None,
        revoked_at=None,
    )

    html = _env().get_template("admin_devices.html").render(
        active="settings",
        tokens=[token],
        just_minted=None,
        host="plant.example",
    )

    assert 'action="/admin/devices/12/revoke"' in html
    assert 'aria-label="Revoke Bay 3 TV"' in html
