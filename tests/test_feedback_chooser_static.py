from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader, select_autoescape

from zira_dashboard import feedback_types
from zira_dashboard.deps import templates
from zira_dashboard.feedback_types import FEEDBACK_TYPES


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "zira_dashboard" / "templates"
FEEDBACK_JS = ROOT / "src" / "zira_dashboard" / "static" / "feedback.js"
FEEDBACK_TEMPLATE = TEMPLATES / "_feedback.html"


def test_lightbulb_is_one_send_first_tabbed_modal():
    html = FEEDBACK_TEMPLATE.read_text(encoding="utf-8")
    assert html.count('role="dialog"') == 1
    assert 'id="lightbulb-modal"' in html
    assert 'role="tablist" aria-label="Light bulb sections"' in html
    assert 'id="lightbulb-tab-send"' in html
    assert '>Send feedback<' in html
    assert 'id="lightbulb-tab-mine"' in html
    assert '>My feedback<' in html
    assert 'id="lightbulb-tab-news"' in html
    assert '>What’s new<' in html
    assert 'id="lightbulb-panel-send"' in html
    assert 'id="lightbulb-panel-mine"' in html
    assert 'id="lightbulb-panel-news"' in html
    assert 'id="fb-view-modal"' not in html


def _render_chooser(catalog=FEEDBACK_TYPES) -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(("html",)),
    )
    environment.globals["static_v"] = lambda _filename: "test"
    environment.globals["feedback_types_for_chooser"] = lambda: catalog
    return environment.get_template("_feedback.html").render()


def test_shared_partial_still_renders_in_a_bare_template_environment():
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(("html",)),
    )
    environment.globals["static_v"] = lambda _filename: "test"

    html = environment.get_template("_feedback.html").render()

    assert 'id="lightbulb-modal"' in html
    assert 'class="fb-type-btn' not in html


def test_lightbulb_tabs_have_accessible_panel_links_and_responsive_styles():
    html = FEEDBACK_TEMPLATE.read_text(encoding="utf-8")
    css = (ROOT / "src" / "zira_dashboard" / "static" / "feedback.css").read_text(
        encoding="utf-8"
    )

    for section in ("send", "mine", "news"):
        assert f'aria-controls="lightbulb-panel-{section}"' in html
        assert f'aria-labelledby="lightbulb-tab-{section}"' in html
    assert html.count('role="tabpanel"') == 3
    assert '.fb-tabs { display: grid; grid-template-columns: repeat(3' in css
    assert ".fb-tab:focus-visible" in css
    assert ".fb-panel[hidden]" in css
    assert "@media (max-width: 520px)" in css


def test_chooser_shows_all_six_choices_with_review_workflow_enabled():
    selector = getattr(feedback_types, "feedback_types_for_chooser", lambda: ())

    assert feedback_types.REVIEW_WORKFLOW_ENABLED is True
    assert [item.label for item in selector()] == [
        "Bug",
        "New Feature",
        "Floor Issue",
        "Floor Suggestion",
        "Repair",
        "2s Improvement",
    ]


def test_shared_jinja_environment_uses_the_python_chooser_catalog():
    selector = getattr(feedback_types, "feedback_types_for_chooser", None)

    assert selector is not None
    assert templates.env.globals.get("feedback_types_for_chooser") is selector


def test_chooser_renders_exact_groups_and_buttons_when_enabled():
    html = _render_chooser()

    reporting = html.index("Reporting — the 2s board triages it")
    ready = html.index("Ready to create work — straight to the floor team")
    assert reporting < ready
    for label in (
        "Bug",
        "New Feature",
        "Floor Issue",
        "Floor Suggestion",
        "Repair",
        "2s Improvement",
    ):
        assert html.count(f">{label}<") == 1


def test_python_catalog_drives_each_button_behavior_and_repair_url():
    html = _render_chooser()

    for item in FEEDBACK_TYPES:
        assert html.count(f'data-type="{item.value}"') == 1
        assert html.count(
            f'data-type="{item.value}" data-behavior="{item.behavior}"'
        ) == 1
    assert 'data-type="repair" data-behavior="external"' in html
    assert (
        'data-url="https://www.gpimaintenance.com/request"'
        in html
    )


def test_repair_is_an_ordinary_protected_anchor_primary_action():
    html = _render_chooser()

    repair = re.search(r'<a\b(?=[^>]*data-type="repair")(?P<attrs>[^>]*)>', html)
    assert repair is not None
    attrs = repair.group("attrs")
    assert "fb-type-btn" in attrs
    assert "fb-type-card" in attrs
    assert 'data-behavior="external"' in attrs
    assert 'href="https://www.gpimaintenance.com/request"' in attrs
    assert 'target="_blank"' in attrs
    assert 'rel="noopener"' in attrs
    assert 'id="fb-external-fallback"' not in html


def test_javascript_uses_button_metadata_without_a_second_type_catalog():
    js = FEEDBACK_JS.read_text(encoding="utf-8")

    assert "ALLOWED_TYPES" not in js
    assert "PLACEHOLDERS" not in js
    assert "getAttribute('data-behavior')" in js
    assert "window.open(" not in js
    assert "opened === null" not in js
    assert "fb-external-fallback" not in js
    assert "window.gpiFetch(submitUrl" in js


def test_javascript_exposes_one_send_first_lightbulb_controller():
    js = FEEDBACK_JS.read_text(encoding="utf-8")

    assert "window.gpiLightbulb = { open: openLightbulb }" in js
    assert "function selectTab(name, options)" in js
    assert "selectTab('send', { focus: false })" in js
    assert "$('lightbulb-modal')" in js
    assert "$('fb-modal')" not in js
    assert "$('fb-view-modal')" not in js
    assert "function openView" not in js


def test_detail_step_keeps_description_screenshot_and_accessibility_contracts():
    html = _render_chooser()
    js = FEEDBACK_JS.read_text(encoding="utf-8")

    assert '<label class="fb-label" for="fb-desc">Description</label>' in html
    assert 'id="fb-upload-btn" class="fb-upload">Add screenshot</button>' in html
    assert 'accept="image/jpeg,image/png,image/webp"' in html
    assert 'aria-pressed="false"' in html
    assert "function trapFocus" in js
    assert "[href]:not([hidden])" in js
    assert "event.key !== 'Escape'" in js
    assert "opener.focus()" in js
    assert "gpi:feedback-opened" in js
    assert "gpi:feedback-closed" in js
    assert "'paste'" in js
    assert "revokeScreenshotUrl" in js
