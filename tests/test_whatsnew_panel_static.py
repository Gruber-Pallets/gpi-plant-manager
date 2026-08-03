import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "_footer.html"
CSS = ROOT / "src" / "zira_dashboard" / "static" / "footer.css"
JS = ROOT / "src" / "zira_dashboard" / "static" / "footer.js"
FEEDBACK_TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "_feedback.html"
FEEDBACK_CSS = ROOT / "src" / "zira_dashboard" / "static" / "feedback.css"
FEEDBACK_JS = ROOT / "src" / "zira_dashboard" / "static" / "feedback.js"


def _rule_zindex(css, selector):
    """z-index of the first rule block for an exact selector (skips `sel[hidden]`)."""
    m = re.search(re.escape(selector) + r"\s*\{[^}]*?z-index:\s*(\d+)", css)
    return int(m.group(1)) if m else None


def test_feedback_modal_stacks_above_whatsnew_panel():
    # The Send/View feedback modals open from inside the What's New panel, so
    # their z-index must sit ABOVE the panel's or they render behind it.
    feedback_css = FEEDBACK_CSS.read_text(encoding="utf-8")
    footer_css = CSS.read_text(encoding="utf-8")
    fb = _rule_zindex(feedback_css, ".fb-modal")
    panel = _rule_zindex(footer_css, ".changelog-modal")
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
    # Old inline feedback form is gone; shared feedback buttons remain.
    assert "changelog-feedback-toggle" not in html
    assert 'id="fb-open"' in html
    assert 'id="fb-view-open"' in html


def test_footer_includes_shared_feedback_component():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "{% include '_feedback.html' %}" in html
    assert 'id="fb-open"' in html
    assert 'id="fb-view-open"' in html


def test_shared_feedback_component_keeps_modal_contract():
    html = FEEDBACK_TEMPLATE.read_text(encoding="utf-8")

    assert 'id="fb-modal"' in html
    assert 'id="fb-view-modal"' in html
    assert 'id="fb-desc"' in html
    assert 'data-type="bug"' in html
    assert 'data-type="feature"' in html
    assert 'id="fb-file-input"' in html
    assert 'id="fb-status" class="fb-status" role="status" aria-live="polite"' in html
    assert "/static/feedback.css" in html
    assert "/static/feedback.js" in html


def test_footer_css_has_whatsnew_trigger_and_card_styles():
    css = CSS.read_text(encoding="utf-8")

    assert ".app-footer" not in css
    assert ".changelog-deploy" not in css
    assert ".whatsnew-btn" in css
    assert ".whatsnew-dot" in css
    assert ".cl-entry" in css
    assert ".cl-badge" in css


def test_footer_js_injects_trigger_read_state_and_feedback_submit():
    js = JS.read_text(encoding="utf-8")

    assert "document.getElementById('changelog-open')" not in js
    assert "function injectButton()" in js
    assert "changelog_cutoff" in js
    assert "changelog_read" in js
    assert "function markAllRead()" in js
    assert "function makeBadgeModal" in js


def test_shared_feedback_assets_keep_submit_and_screenshot_support():
    css = FEEDBACK_CSS.read_text(encoding="utf-8")
    js = FEEDBACK_JS.read_text(encoding="utf-8")

    assert ".fb-modal" in css
    assert ".fb-card" in css
    assert ".fb-type-btn" in css
    assert ".fb-submit" in css
    assert ".fb-attachment-chip" in css
    assert ".fb-status-pill" in css
    assert "function submitFeedback" in js
    assert "FormData" in js
    assert "window.gpiFetch('/feedback'" in js
    assert "/api/feedback/mine" in js
    assert "function renderMyFeedback" in js
    assert "'paste'" in js
    assert "window.location.href" in js
    assert "[data-feedback-open]" in js
    assert "activeOpener" in js
    assert "function trapFocus" in js
    assert "event.key !== 'Tab'" in js
    assert "focusable.indexOf(current)" in js
    assert "currentIndex === -1" in js
    assert "opener.focus()" in js
    assert "gpi:feedback-opened" in js
    assert "gpi:feedback-closed" in js


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


def test_whatsnew_uses_lucide_lightbulb_icon_button():
    css = CSS.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")

    # The trigger uses the accessible, outline-only Lucide lightbulb.
    assert "btn.setAttribute('aria-label', \"What's new\")" in js
    assert 'class="whatsnew-lightbulb"' in js
    assert 'width="24" height="24"' in js
    assert 'stroke-width="2"' in js
    lightbulb_path = (
        'M15 14c.2-1 .7-1.7 1.5-2.5C17.5 10.5 18 9.2 18 8A6 6 0 0 0 6 8'
        'c0 1.2.5 2.5 1.5 3.5.7.7 1.3 1.5 1.5 2.5'
    )
    assert lightbulb_path in js

    # The button chrome is a quiet 44px card with an 8px corner radius.
    assert ".whatsnew-btn" in css
    assert "width: 44px" in css
    assert "height: 44px" in css
    assert "border-radius: 8px" in css
