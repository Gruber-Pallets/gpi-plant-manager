import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "_footer.html"
CSS = ROOT / "src" / "zira_dashboard" / "static" / "footer.css"
JS = ROOT / "src" / "zira_dashboard" / "static" / "footer.js"


def _rule_zindex(css, selector):
    """z-index of the first rule block for an exact selector (skips `sel[hidden]`)."""
    m = re.search(re.escape(selector) + r"\s*\{[^}]*?z-index:\s*(\d+)", css)
    return int(m.group(1)) if m else None


def test_feedback_modal_stacks_above_whatsnew_panel():
    # The Send/View feedback modals open from inside the What's New panel, so
    # their z-index must sit ABOVE the panel's or they render behind it.
    css = CSS.read_text(encoding="utf-8")
    fb = _rule_zindex(css, ".fb-modal")
    panel = _rule_zindex(css, ".changelog-modal")
    assert fb is not None, ".fb-modal z-index not found"
    assert panel is not None, ".changelog-modal z-index not found"
    assert fb >= panel, (
        f".fb-modal z-index ({fb}) must be >= .changelog-modal ({panel}) — "
        "it opens from within the What's New panel"
    )


def test_footer_template_uses_panel_without_old_text_link():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "app-footer" not in html
    assert "changelog-open" not in html
    assert "changelog-markall" in html
    # Old inline feedback form is gone; new modals + buttons present.
    assert "changelog-feedback-toggle" not in html
    assert 'id="fb-open"' in html
    assert 'id="fb-view-open"' in html
    assert 'id="fb-modal"' in html
    assert 'id="fb-view-modal"' in html
    assert 'id="fb-desc"' in html
    assert 'data-type="bug"' in html
    assert 'data-type="feature"' in html
    assert 'id="fb-file-input"' in html


def test_footer_css_has_whatsnew_trigger_and_card_styles():
    css = CSS.read_text(encoding="utf-8")

    assert ".app-footer" not in css
    assert ".changelog-deploy" not in css
    assert ".whatsnew-btn" in css
    assert ".whatsnew-dot" in css
    assert ".cl-entry" in css
    assert ".cl-badge" in css
    # New feedback modal styles.
    assert ".fb-modal" in css
    assert ".fb-card" in css
    assert ".fb-type-btn" in css
    assert ".fb-submit" in css
    assert ".fb-attachment-chip" in css
    assert ".fb-status-pill" in css


def test_footer_js_injects_trigger_read_state_and_feedback_submit():
    js = JS.read_text(encoding="utf-8")

    assert "document.getElementById('changelog-open')" not in js
    assert "function injectButton()" in js
    assert "changelog_cutoff" in js
    assert "changelog_read" in js
    assert "function markAllRead()" in js
    assert "function makeBadgeModal" in js
    # New feedback modal wiring.
    assert "function submitFeedback" in js
    assert "FormData" in js
    assert "window.gpiFetch('/feedback'" in js
    assert "/api/feedback/mine" in js
    assert "function renderMyFeedback" in js
    assert "'paste'" in js


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
