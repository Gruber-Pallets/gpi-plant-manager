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


def _page_with_lightbulb(page, setup_script=None):
    page.set_content(
        '<button id="feedback-opener">Open feedback</button>'
        + _render_enabled_chooser()
    )
    if setup_script:
        page.evaluate(setup_script)
    page.add_script_tag(content=FEEDBACK_JS.read_text(encoding="utf-8"))
    page.locator("#feedback-opener").evaluate(
        "button => button.addEventListener('click', () => window.gpiLightbulb.open(button))"
    )


def test_my_feedback_is_lazy_loaded_once_when_selected():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            _page_with_lightbulb(
                page,
                """
                window.mineRequests = 0;
                window.gpiFetch = function (url) {
                  if (url === '/api/feedback/mine') window.mineRequests += 1;
                  return Promise.resolve({json: function () {
                    return Promise.resolve({ok: true, items: []});
                  }});
                };
                """,
            )

            page.locator("#feedback-opener").click()
            assert page.evaluate("window.mineRequests") == 0

            page.locator("#lightbulb-tab-mine").click()
            page.get_by_text("You haven't sent any feedback yet.").wait_for()
            assert page.evaluate("window.mineRequests") == 1

            page.locator("#lightbulb-tab-send").click()
            page.locator("#lightbulb-tab-mine").click()
            assert page.evaluate("window.mineRequests") == 1
        finally:
            browser.close()


def test_successful_submit_switches_to_refreshed_my_feedback():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            _page_with_lightbulb(
                page,
                """
                window.mineRequests = 0;
                window.gpiFetch = function (url, options) {
                  if (options && options.method === 'POST') {
                    return Promise.resolve({json: function () {
                      return Promise.resolve({ok: true});
                    }});
                  }
                  if (url === '/api/feedback/mine') {
                    window.mineRequests += 1;
                    return Promise.resolve({json: function () {
                      return Promise.resolve({ok: true, items: [{
                        title: 'Count is wrong',
                        type_label: 'Bug',
                        created_at: '2026-09-04T12:00:00Z',
                        status: 'requested'
                      }]});
                    }});
                  }
                  return Promise.resolve({json: function () {
                    return Promise.resolve({ok: true, people: []});
                  }});
                };
                """,
            )

            page.locator("#feedback-opener").click()
            assert page.evaluate("window.mineRequests") == 0
            page.locator('[data-type="bug"]').click()
            page.locator("#fb-desc").fill("Count is wrong")
            page.locator("#fb-submit").click()

            page.locator("#lightbulb-tab-mine[aria-selected='true']").wait_for()
            assert page.locator("#fb-view-body").get_by_text("Count is wrong").is_visible()
            assert page.locator("#lightbulb-modal").get_attribute("hidden") is None
            assert page.evaluate("window.mineRequests") == 1
        finally:
            browser.close()


def test_failed_submit_keeps_form_and_draft():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            _page_with_lightbulb(
                page,
                """
                window.mineRequests = 0;
                window.gpiFetch = function (url, options) {
                  if (options && options.method === 'POST') {
                    return Promise.resolve({json: function () {
                      return Promise.resolve({ok: false, error: 'Try again'});
                    }});
                  }
                  if (url === '/api/feedback/mine') window.mineRequests += 1;
                  return Promise.resolve({json: function () {
                    return Promise.resolve({ok: true, people: []});
                  }});
                };
                """,
            )

            page.locator("#feedback-opener").click()
            page.locator('[data-type="bug"]').click()
            page.locator("#fb-desc").fill("Count is wrong")
            page.locator("#fb-submit").click()

            page.get_by_text("Failed: Try again").wait_for()
            assert page.locator("#lightbulb-tab-send").get_attribute("aria-selected") == "true"
            assert page.locator("#fb-desc").input_value() == "Count is wrong"
            assert page.evaluate("window.mineRequests") == 0
        finally:
            browser.close()


def test_my_feedback_failure_is_retryable_without_hiding_other_tabs():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            _page_with_lightbulb(
                page,
                """
                window.mineRequests = 0;
                window.gpiFetch = function (url) {
                  if (url !== '/api/feedback/mine') {
                    return Promise.resolve({json: function () {
                      return Promise.resolve({ok: true, people: []});
                    }});
                  }
                  window.mineRequests += 1;
                  if (window.mineRequests === 1) {
                    return Promise.reject(new Error('unavailable'));
                  }
                  return Promise.resolve({json: function () {
                    return Promise.resolve({ok: true, items: [{
                      title: 'Count is wrong',
                      type_label: 'Bug',
                      created_at: '2026-09-04T12:00:00Z',
                      status: 'requested'
                    }]});
                  }});
                };
                """,
            )

            page.locator("#feedback-opener").click()
            page.locator("#lightbulb-tab-mine").click()
            page.get_by_text("Could not load your feedback.").wait_for()
            page.locator("#fb-view-retry").click()

            page.get_by_text("Count is wrong").wait_for()
            assert page.locator("#lightbulb-tab-send").is_enabled()
            assert page.evaluate("window.mineRequests") == 2
        finally:
            browser.close()


def test_close_and_reopen_ignores_pending_my_feedback_from_previous_session():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            _page_with_lightbulb(
                page,
                """
                window.mineResolvers = [];
                window.gpiFetch = function (url) {
                  if (url === '/api/feedback/mine') {
                    return new Promise(function (resolve) {
                      window.mineResolvers.push(resolve);
                    });
                  }
                  return Promise.resolve({json: function () {
                    return Promise.resolve({ok: true, people: []});
                  }});
                };
                """,
            )

            page.locator("#feedback-opener").click()
            page.locator("#lightbulb-tab-mine").click()
            page.wait_for_function("window.mineResolvers.length === 1")
            page.locator("#lightbulb-close").click()

            page.locator("#feedback-opener").click()
            page.locator("#lightbulb-tab-mine").click()
            page.wait_for_function("window.mineResolvers.length === 2")
            page.evaluate(
                """
                window.mineResolvers[1]({json: function () {
                  return Promise.resolve({items: [{title: 'New session'}]});
                }});
                """
            )
            page.get_by_text("New session").wait_for()

            page.evaluate(
                """
                window.mineResolvers[0]({json: function () {
                  return Promise.resolve({items: [{title: 'Old session'}]});
                }});
                """
            )
            page.wait_for_timeout(50)

            assert page.get_by_text("New session").is_visible()
            assert page.get_by_text("Old session").count() == 0
        finally:
            browser.close()


def test_successful_submit_supersedes_an_in_flight_my_feedback_load():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            _page_with_lightbulb(
                page,
                """
                window.mineResolvers = [];
                window.gpiFetch = function (url, options) {
                  if (options && options.method === 'POST') {
                    return Promise.resolve({json: function () {
                      return Promise.resolve({ok: true});
                    }});
                  }
                  if (url === '/api/feedback/mine') {
                    return new Promise(function (resolve) {
                      window.mineResolvers.push(resolve);
                    });
                  }
                  return Promise.resolve({json: function () {
                    return Promise.resolve({ok: true, people: []});
                  }});
                };
                """,
            )

            page.locator("#feedback-opener").click()
            page.locator("#lightbulb-tab-mine").click()
            page.wait_for_function("window.mineResolvers.length === 1")
            page.locator("#lightbulb-tab-send").click()
            page.locator('[data-type="bug"]').click()
            page.locator("#fb-desc").fill("New request")
            page.locator("#fb-submit").click()

            page.wait_for_function("window.mineResolvers.length === 2", timeout=1000)
            page.evaluate(
                """
                window.mineResolvers[1]({json: function () {
                  return Promise.resolve({items: [{title: 'New request'}]});
                }});
                """
            )
            page.get_by_text("New request").wait_for()
            page.evaluate(
                """
                window.mineResolvers[0]({json: function () {
                  return Promise.resolve({items: [{title: 'Older result'}]});
                }});
                """
            )
            page.wait_for_timeout(50)

            assert page.get_by_text("New request").is_visible()
            assert page.get_by_text("Older result").count() == 0
        finally:
            browser.close()


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


def test_close_and_reopen_ignores_a_pending_submit_from_the_previous_session():
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
                window.resolvePendingFeedback = null;
                window.gpiFetch = function (url, options) {
                  if (options && options.method === 'POST') {
                    return new Promise(function (resolve) {
                      window.resolvePendingFeedback = resolve;
                    });
                  }
                  return Promise.resolve({json: function () {
                    return Promise.resolve({ok: true, people: []});
                  }});
                };
                """
            )
            page.add_script_tag(content=FEEDBACK_JS.read_text(encoding="utf-8"))
            page.locator("#feedback-opener").evaluate(
                "button => button.addEventListener('click', () => window.gpiLightbulb.open(button))"
            )

            page.locator("#feedback-opener").click()
            page.locator('[data-type="bug"]').click()
            page.locator("#fb-desc").fill("Old draft")
            page.locator("#fb-submit").click()
            assert page.locator("#fb-submit").is_disabled()

            page.locator("#lightbulb-close").click()
            page.locator("#feedback-opener").click()
            assert not page.locator("#fb-submit").is_disabled()
            page.locator('[data-type="bug"]').click()
            page.locator("#fb-desc").fill("New draft")

            page.evaluate(
                "window.resolvePendingFeedback({json: function () { return Promise.resolve({ok: true}); }})"
            )
            page.wait_for_timeout(1300)

            assert page.locator("#lightbulb-modal").get_attribute("hidden") is None
            assert page.locator("#fb-desc").input_value() == "New draft"
            assert page.locator("#fb-status").get_attribute("hidden") == ""
        finally:
            browser.close()


def test_successful_submit_leaves_no_close_timer_for_the_next_session():
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
                window.gpiFetch = function () {
                  return Promise.resolve({json: function () {
                    return Promise.resolve({ok: true, people: []});
                  }});
                };
                """
            )
            page.add_script_tag(content=FEEDBACK_JS.read_text(encoding="utf-8"))
            page.locator("#feedback-opener").evaluate(
                "button => button.addEventListener('click', () => window.gpiLightbulb.open(button))"
            )

            page.locator("#feedback-opener").click()
            page.locator('[data-type="bug"]').click()
            page.locator("#fb-desc").fill("Sent draft")
            page.locator("#fb-submit").click()
            page.locator("#lightbulb-tab-mine[aria-selected='true']").wait_for()

            page.locator("#lightbulb-close").click()
            page.locator("#feedback-opener").click()
            page.locator('[data-type="bug"]').click()
            page.locator("#fb-desc").fill("New draft")
            page.wait_for_timeout(1300)

            assert page.locator("#lightbulb-modal").get_attribute("hidden") is None
            assert page.locator("#fb-desc").input_value() == "New draft"
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
