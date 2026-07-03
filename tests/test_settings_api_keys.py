from zira_dashboard.deps import templates


def _extract_api_section() -> str:
    import re
    from pathlib import Path

    html = Path("src/zira_dashboard/templates/settings.html").read_text()
    match = re.search(r"<section class=\"panel\" id=\"api-panel\".*?</section>", html, re.DOTALL)
    assert match, "api-panel section missing from settings.html"
    return match.group(0)


def test_api_settings_section_renders_keys_and_create_form():
    rendered = templates.env.from_string(_extract_api_section()).render(
        active_section="api",
        api_keys_rows=[
            {
                "id": 1,
                "name": "CRM",
                "key_prefix": "gpi_live_abcd",
                "scopes": ["admin:*"],
                "allowed_ips": [],
                "created_at": None,
                "last_used_at": None,
                "revoked_at": None,
            }
        ],
        new_api_key="gpi_live_once",
    )
    assert "API Keys" in rendered
    assert "gpi_live_once" in rendered
    assert 'action="/settings/api-keys"' in rendered
    assert 'action="/settings/api-keys/1/revoke"' in rendered
    assert 'name="scope_admin"' in rendered


def test_api_scope_parser_defaults_to_read():
    from zira_dashboard.routes import settings

    assert settings._parse_api_key_scopes({}) == ["object:read"]
    assert settings._parse_api_key_scopes({"scope_admin": "on"}) == ["admin:*"]
    assert settings._parse_api_key_scopes(
        {"scope_read": "on", "scope_write": "on"}
    ) == ["object:read", "object:write"]
