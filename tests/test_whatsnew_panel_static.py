from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "_footer.html"
CSS = ROOT / "src" / "zira_dashboard" / "static" / "footer.css"
JS = ROOT / "src" / "zira_dashboard" / "static" / "footer.js"


def test_footer_template_uses_panel_without_old_text_link():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "app-footer" not in html
    assert "changelog-open" not in html
    assert "changelog-feedback-toggle" in html
    assert "changelog-feedback-message" in html
    assert "changelog-markall" in html


def test_footer_css_has_whatsnew_trigger_and_card_styles():
    css = CSS.read_text(encoding="utf-8")

    assert ".app-footer" not in css
    assert ".changelog-deploy" not in css
    assert ".whatsnew-btn" in css
    assert ".whatsnew-dot" in css
    assert ".cl-entry" in css
    assert ".cl-badge" in css
    assert ".changelog-feedback" in css


def test_footer_js_injects_trigger_read_state_and_feedback_submit():
    js = JS.read_text(encoding="utf-8")

    assert "document.getElementById('changelog-open')" not in js
    assert "function injectButton()" in js
    assert "changelog_cutoff" in js
    assert "changelog_read" in js
    assert "function markAllRead()" in js
    assert "function submitFeedback" in js
    assert "window.gpiFetch('/feedback'" in js
    assert "function makeBadgeModal" in js


def test_footer_js_skips_tv_mode_documents():
    js = JS.read_text(encoding="utf-8")

    assert "function isTvMode()" in js
    assert "document.documentElement.dataset.tvTheme" in js
    assert "if (isTvMode()) return;" in js


def test_footer_js_uses_dedicated_header_slot_for_trigger():
    js = JS.read_text(encoding="utf-8")

    assert "slot.className = 'whatsnew-slot'" in js
    assert "header.appendChild(slot)" in js
    assert "header.children[header.children.length - 1].appendChild(btn)" not in js
