"""Render-tests for the _goat_badges.html Jinja macro.

We build a tiny Jinja Environment pointed at the templates directory,
import the macro, and render it with representative inputs. No FastAPI
app, no DB.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader


@pytest.fixture
def env():
    templates_dir = Path(__file__).resolve().parent.parent / "src" / "zira_dashboard" / "templates"
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )


def _render_macro(env, name, holders):
    tmpl = env.from_string(
        '{% from "_goat_badges.html" import goat_badges %}'
        '{{ goat_badges(name, holders) }}'
    )
    return tmpl.render(name=name, holders=holders).strip()


def test_no_goat_holdings_emits_nothing(env):
    out = _render_macro(env, "Alice", {})
    assert out == ""


def test_name_not_in_map_emits_nothing(env):
    out = _render_macro(env, "Alice", {"Bob": ["Repairs"]})
    assert out == ""


def test_none_holders_emits_nothing(env):
    out = _render_macro(env, "Alice", None)
    assert out == ""


def test_single_group_emits_one_badge(env):
    out = _render_macro(env, "Alice", {"Alice": ["Repairs"]})
    assert '<span class="goat-badges">' in out
    assert out.count('class="goat-badge"') == 1
    assert 'title="GOAT — Repairs"' in out
    assert "\U0001F410" in out


def test_multi_group_emits_multiple_badges(env):
    out = _render_macro(env, "Alice", {"Alice": ["Repairs", "Juniors"]})
    assert out.count('class="goat-badge"') == 2
    assert 'title="GOAT — Repairs"' in out
    assert 'title="GOAT — Juniors"' in out
    assert out.count("\U0001F410") == 2


def test_group_name_with_quotes_is_escaped(env):
    """Defensive: a group name with a `"` in it must not break the HTML."""
    out = _render_macro(env, "Alice", {"Alice": ['Re"pairs']})
    # Rendered title attribute must escape the quote.
    assert 'title="GOAT — Re&#34;pairs"' in out or 'title="GOAT — Re&quot;pairs"' in out


def test_css_macro_emits_class_rules(env):
    tmpl = env.from_string(
        '{% from "_goat_badges.html" import goat_badges_css %}'
        '{{ goat_badges_css() }}'
    )
    css = tmpl.render()
    assert ".goat-badges" in css
    assert ".goat-badge" in css
    assert "display: inline-flex" in css
