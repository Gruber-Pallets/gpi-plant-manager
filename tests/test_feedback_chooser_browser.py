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


def _page_with_lightbulb(page):
    page.set_content(
        '<button id="feedback-opener">Open feedback</button>'
        + _render_enabled_chooser()
    )
    page.add_script_tag(content=FEEDBACK_JS.read_text(encoding="utf-8"))
    page.locator("#feedback-opener").evaluate(
        "button => button.addEventListener('click', () => window.gpiLightbulb.open(button))"
    )


def test_lightbulb_opens_send_first_and_switching_tabs_keeps_draft_and_screenshot():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            _page_with_lightbulb(page)

            page.locator("#feedback-opener").click()
            assert page.locator("#lightbulb-tab-send").get_attribute("aria-selected") == "true"
            page.locator('[data-type="bug"]').click()
            page.locator("#fb-desc").fill("The count is wrong")
            page.locator("#fb-file-input").set_input_files(
                {"name": "count.png", "mimeType": "image/png", "buffer": b"png"}
            )
            page.locator("#lightbulb-tab-mine").click()
            page.locator("#lightbulb-tab-send").click()

            assert page.locator("#fb-desc").input_value() == "The count is wrong"
            assert page.locator("#fb-attachments").get_by_text("count.png").count() == 1
            assert page.evaluate("document.activeElement.id") == "fb-back"
        finally:
            browser.close()


def test_close_clears_draft_and_reopen_returns_to_send():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            _page_with_lightbulb(page)

            page.locator("#feedback-opener").click()
            page.locator('[data-type="bug"]').click()
            page.locator("#fb-desc").fill("Unsaved")
            page.locator("#fb-file-input").set_input_files(
                {"name": "unsaved.png", "mimeType": "image/png", "buffer": b"png"}
            )
            page.locator("#lightbulb-tab-mine").click()
            page.locator("#lightbulb-close").click()
            assert page.evaluate("document.activeElement.id") == "feedback-opener"

            page.locator("#feedback-opener").click()
            assert page.locator("#lightbulb-tab-send").get_attribute("aria-selected") == "true"
            page.locator('[data-type="bug"]').click()
            assert page.locator("#fb-desc").input_value() == ""
            assert page.locator("#fb-attachments").get_by_text("unsaved.png").count() == 0
        finally:
            browser.close()


def test_tab_arrow_keys_wrap_and_escape_returns_focus_to_opener():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            _page_with_lightbulb(page)

            page.locator("#feedback-opener").click()
            page.keyboard.press("ArrowRight")
            assert page.locator("#lightbulb-tab-mine").get_attribute("aria-selected") == "true"
            page.locator("#lightbulb-tab-mine").focus()
            page.keyboard.press("ArrowRight")
            assert page.locator("#lightbulb-tab-news").get_attribute("aria-selected") == "true"
            page.locator("#lightbulb-tab-news").focus()
            page.keyboard.press("ArrowRight")
            assert page.locator("#lightbulb-tab-send").get_attribute("aria-selected") == "true"
            page.keyboard.press("ArrowLeft")
            assert page.locator("#lightbulb-tab-news").get_attribute("aria-selected") == "true"
            page.keyboard.press("Escape")

            assert page.locator("#lightbulb-modal").get_attribute("hidden") == ""
            assert page.evaluate("document.activeElement.id") == "feedback-opener"
        finally:
            browser.close()


def test_focus_trap_skips_controls_inside_hidden_ancestors():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            _page_with_lightbulb(page)
            page.locator("body").evaluate(
                "body => body.insertAdjacentHTML('beforeend', '<button id=\"outside-after\">Outside after modal</button>')"
            )

            page.locator("#feedback-opener").click()
            page.locator("#fb-type-group-ready").evaluate(
                "title => { title.parentElement.hidden = true; }"
            )
            page.locator('[data-type="floor_suggestion"]').focus()
            page.keyboard.press("Tab")

            assert page.evaluate("document.activeElement.id") == "lightbulb-close"
        finally:
            browser.close()


def test_repair_link_closes_modal_resumes_idle_and_never_posts_feedback():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(
                '<button id="feedback-opener">Open feedback</button>'
                + _render_enabled_chooser()
            )
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
            page.locator("#feedback-opener").evaluate(
                "button => button.addEventListener('click', () => window.gpiLightbulb.open(button))"
            )
            page.locator('[data-type="repair"]').evaluate(
                "link => { link.href = 'about:blank'; }"
            )

            page.locator("#feedback-opener").click()
            with page.expect_popup() as popup_info:
                page.locator('[data-type="repair"]').click()
            popup_info.value.close()

            assert page.locator("#lightbulb-modal").get_attribute("hidden") == ""
            assert page.evaluate("window.feedbackClosedEvents") == 1
            assert page.evaluate("window.feedbackPosts") == []
        finally:
            browser.close()
