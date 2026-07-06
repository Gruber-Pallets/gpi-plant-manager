from pathlib import Path


def _template():
    return Path("src/zira_dashboard/templates/timeclock_time_off_request_details.html").read_text()


def _detail_template():
    return Path("src/zira_dashboard/templates/timeclock_time_off_mine_detail.html").read_text()


def _script():
    return Path("src/zira_dashboard/static/timeclock_time_off.js").read_text()


def _base_template():
    return Path("src/zira_dashboard/templates/timeclock_base.html").read_text()


def test_htmx_swaps_time_off_conflict_and_validation_rerenders():
    # The kiosk is hx-boost="true", so forms submit via htmx. htmx does NOT
    # swap 4xx/5xx responses by default — it fires htmx:responseError and
    # leaves the DOM untouched. That silently swallowed the time-off request/
    # edit form's 409 (overlap conflict modal) and 422 (invalid times) HTML
    # re-renders, so the green Submit button appeared to do nothing. A
    # beforeSwap handler must opt those two statuses back into the swap so the
    # returned screen (with its modal / error banner) replaces the form.
    base = _base_template()
    assert "htmx:beforeSwap" in base
    assert "=== 409" in base
    assert "=== 422" in base
    assert "shouldSwap = true" in base
    assert "isError = false" in base


def test_time_off_request_submit_exposes_busy_state():
    html = _template()
    js = _script()

    assert 'class="time-off-request-form"' in html
    assert 'id="submit-btn" class="k-btn success" aria-busy="false"' in html
    assert 'document.querySelector(".time-off-request-form")' in js
    assert "form.addEventListener(\"submit\"" in js
    assert "submitBtn.disabled = true;" in js
    assert "submitBtn.setAttribute(\"aria-busy\", \"true\");" in js


def test_time_off_cancel_submit_exposes_busy_state():
    html = _detail_template()

    assert 'class="time-off-cancel-form"' in html
    assert 'class="k-btn danger" aria-busy="false"' in html
    assert "document.querySelectorAll('.time-off-cancel-form').forEach" in html
    assert "form.addEventListener('submit'" in html
    assert "btn.disabled = true;" in html
    assert "btn.setAttribute('aria-busy', 'true');" in html
