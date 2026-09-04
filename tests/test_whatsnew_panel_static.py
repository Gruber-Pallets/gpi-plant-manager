import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "_footer.html"
CSS = ROOT / "src" / "zira_dashboard" / "static" / "footer.css"
JS = ROOT / "src" / "zira_dashboard" / "static" / "footer.js"
FEEDBACK_TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "_feedback.html"
FEEDBACK_CSS = ROOT / "src" / "zira_dashboard" / "static" / "feedback.css"
FEEDBACK_JS = ROOT / "src" / "zira_dashboard" / "static" / "feedback.js"


def test_footer_has_no_second_lightbulb_modal():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="changelog-modal"' not in html
    assert 'id="fb-open"' not in html
    assert 'id="fb-view-open"' not in html
    assert html.count("{% include '_feedback.html' %}") == 1


def _rule_zindex(css, selector):
    """z-index of the first rule block for an exact selector (skips `sel[hidden]`)."""
    m = re.search(re.escape(selector) + r"\s*\{[^}]*?z-index:\s*(\d+)", css)
    return int(m.group(1)) if m else None


def test_lightbulb_uses_the_feedback_modal_layer_without_changelog_layer():
    feedback_css = FEEDBACK_CSS.read_text(encoding="utf-8")
    footer_css = CSS.read_text(encoding="utf-8")
    fb = _rule_zindex(feedback_css, ".fb-modal")
    assert fb is not None, ".fb-modal z-index not found"
    assert ".changelog-modal" not in footer_css
    assert ".changelog-backdrop" not in footer_css
    assert ".changelog-card" not in footer_css


def test_footer_template_uses_shared_lightbulb_without_old_text_link():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "app-footer" not in html
    assert "changelog-open" not in html
    assert "changelog-markall" not in html
    assert "changelog-feedback-toggle" not in html
    assert 'id="fb-open"' not in html
    assert 'id="fb-view-open"' not in html


def test_footer_includes_shared_feedback_component():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert html.count("{% include '_feedback.html' %}") == 1
    assert html.count("/static/footer.css") == 1
    assert html.count("/static/footer.js") == 1


def test_shared_feedback_component_keeps_modal_contract():
    html = FEEDBACK_TEMPLATE.read_text(encoding="utf-8")

    assert 'id="lightbulb-modal"' in html
    assert 'id="lightbulb-panel-send"' in html
    assert 'id="lightbulb-panel-mine"' in html
    assert 'id="lightbulb-panel-news"' in html
    assert 'id="fb-view-modal"' not in html
    assert 'id="fb-desc"' in html
    assert 'data-type="{{ item.value }}"' in html
    assert "feedback_types_for_chooser()" in html
    assert 'id="fb-file-input"' in html
    assert 'id="fb-status" class="fb-status" role="status" aria-live="polite"' in html
    assert 'id="lightbulb-title"' in html
    assert "/static/feedback.css" in html
    assert "/static/feedback.js" in html


def test_feedback_panel_renders_catalog_types_in_two_steps():
    html = FEEDBACK_TEMPLATE.read_text(encoding="utf-8")

    assert "for item in group_types" in html
    assert "{{ item.label }}" in html
    assert "{{ item.description }}" in html
    assert ">Send feedback<" in html
    assert "Ready to create work — straight to the floor team" in html
    assert "opens GPI Maintenance" in html
    assert (
        "App bugs and ideas become a coding task. Floor reports and 2S "
        "improvements go to Dale to review first. A repair is filed in GPI Maintenance."
    ) in html
    assert 'id="fb-type-step"' in html
    assert 'id="fb-detail-step"' in html
    assert 'id="fb-back"' in html


def test_feedback_assets_use_rendered_choice_metadata_and_step_styles():
    css = FEEDBACK_CSS.read_text(encoding="utf-8")
    js = FEEDBACK_JS.read_text(encoding="utf-8")

    assert "ALLOWED_TYPES" not in js
    assert "PLACEHOLDERS" not in js
    assert "getAttribute('data-type')" in js
    assert "getAttribute('data-behavior')" in js
    assert "typeStep.hidden = true;" in js
    assert "detailStep.hidden = false;" in js
    assert "desc.focus();" in js
    assert "back.addEventListener('click'" in js
    assert "typeStep.hidden = false;" in js
    assert "detailStep.hidden = true;" in js
    assert "chosen.focus();" in js
    assert "setType(defaultTypeButton.getAttribute('data-type'));" in js
    assert "showTypeStep(false);" in js
    assert "it.type_label || 'Unknown'" in js
    assert "it.type === 'feature'" not in js

    for selector in (
        ".fb-type-card",
        ".fb-type-icon",
        ".fb-type-subtitle",
        ".fb-type-btn.is-active",
        ".fb-type-btn:focus-visible",
        ".fb-type-divider",
        ".fb-type-repair",
        ".fb-type-improve",
        ".fb-type-footnote",
    ):
        assert selector in css
    assert "grid-template-columns: repeat(2" not in css


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
    assert "window.gpiFetch(submitUrl" in js
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


def test_shared_feedback_holds_one_image_screenshot_and_local_statuses():
    html = FEEDBACK_TEMPLATE.read_text(encoding="utf-8")
    js = FEEDBACK_JS.read_text(encoding="utf-8")

    assert 'id="fb-upload-btn" class="fb-upload">Add screenshot</button>' in html
    assert 'accept="image/jpeg,image/png,image/webp"' in html
    assert "multiple" not in html
    assert "application/pdf" not in html
    assert "var screenshot = null" in js
    assert "var attachments = []" not in js
    assert "form.append('screenshot', screenshot.file, screenshot.name)" in js
    assert "form.append('files'" not in js
    for status, label in (
        ("requested", "Requested"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
        ("declined", "Declined"),
    ):
        assert f"{status}: '{label}'" in js


def test_footer_js_skips_tv_mode_documents():
    js = JS.read_text(encoding="utf-8")

    assert "function isTvMode()" in js
    assert "document.documentElement.dataset.tvTheme" in js
    assert "if (isTvMode()) return;" in js


def test_footer_js_uses_dedicated_header_slot_for_trigger():
    js = JS.read_text(encoding="utf-8")

    assert "slot.className = 'whatsnew-slot'" in js
    assert "var slotParent = header.querySelector('.k-header-actions') || header;" in js
    assert "slotParent.appendChild(slot)" in js
    assert "header.children[header.children.length - 1].appendChild(btn)" not in js


def test_footer_js_mounts_timeclock_trigger_after_htmx_swaps():
    js = JS.read_text(encoding="utf-8")

    assert "document.querySelector('header, .k-header')" in js
    assert "document.body.addEventListener('htmx:afterSwap'" in js
    assert "event.target.id !== 'timeclock-screen'" in js
    assert "injectButton();" in js
    assert "refreshDot();" in js


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
