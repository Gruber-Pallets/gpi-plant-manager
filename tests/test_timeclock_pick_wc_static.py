from pathlib import Path


def _template():
    return Path("src/zira_dashboard/templates/timeclock_pick_wc.html").read_text()


def test_timeclock_work_center_picker_forms_guard_against_double_taps():
    html = _template()

    assert 'class="k-punch-form"' in html
    assert 'class="k-btn secondary" aria-busy="false"' in html
    assert "document.querySelectorAll('.k-punch-form').forEach" in html
    assert "form.addEventListener('submit'" in html
    assert "btn.disabled = true;" in html
    assert "btn.setAttribute('aria-busy', 'true');" in html
