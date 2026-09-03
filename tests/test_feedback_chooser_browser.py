from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from zira_dashboard.feedback_types import FEEDBACK_TYPES


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "zira_dashboard" / "templates"
FEEDBACK_JS = ROOT / "src" / "zira_dashboard" / "static" / "feedback.js"


def _render_enabled_chooser() -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(("html",)),
    )
    environment.globals["static_v"] = lambda _filename: "test"
    environment.globals["feedback_types_for_chooser"] = lambda: FEEDBACK_TYPES
    return environment.get_template("_feedback.html").render()


def test_focus_trap_skips_controls_inside_hidden_ancestors():
    document = (
        '<button id="feedback-opener" data-feedback-open>Open feedback</button>'
        + _render_enabled_chooser()
        + '<button id="outside-after">Outside after modal</button>'
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(document)
            page.add_script_tag(content=FEEDBACK_JS.read_text(encoding="utf-8"))

            page.locator("#feedback-opener").click()
            page.locator("#fb-type-group-ready").evaluate(
                "title => { title.parentElement.hidden = true; }"
            )
            page.locator('[data-type="floor_suggestion"]').focus()
            page.keyboard.press("Tab")

            assert page.evaluate("document.activeElement.id") == "fb-close"
        finally:
            browser.close()


def test_repair_link_closes_modal_resumes_idle_and_never_posts_feedback():
    document = (
        '<button id="feedback-opener" data-feedback-open>Open feedback</button>'
        + _render_enabled_chooser()
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(document)
            page.evaluate(
                """
                window.feedbackPosts = [];
                window.feedbackClosedEvents = 0;
                window.gpiFetch = function (url, options) {
                  if (options && options.method === 'POST') {
                    window.feedbackPosts.push(url);
                  }
                  return Promise.resolve({json: function () {
                    return Promise.resolve({ok: true, people: []});
                  }});
                };
                document.addEventListener('gpi:feedback-closed', function () {
                  window.feedbackClosedEvents += 1;
                });
                """
            )
            page.add_script_tag(content=FEEDBACK_JS.read_text(encoding="utf-8"))
            page.locator('[data-type="repair"]').evaluate(
                "link => { link.href = 'about:blank'; }"
            )

            page.locator("#feedback-opener").click()
            with page.expect_popup() as popup_info:
                page.locator('[data-type="repair"]').click()
            popup_info.value.close()

            assert page.locator("#fb-modal").get_attribute("hidden") == ""
            assert page.evaluate("window.feedbackClosedEvents") == 1
            assert page.evaluate("window.feedbackPosts") == []
        finally:
            browser.close()
