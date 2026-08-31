"""String-membership tests against the JS source, mirroring the existing
test_exceptions_js_refreshes_shared_badges_after_inline_resolution style."""
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "zira_dashboard" / "static"


def test_exceptions_js_has_breakdown_transfer_handler():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")
    assert "js-breakdown-transfer" in js
    assert "/api/exceptions/breakdown/transfer" in js


def test_exceptions_js_has_breakdown_snooze_handler():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")
    assert "js-breakdown-snooze" in js
    assert "/api/exceptions/breakdown/snooze" in js
    assert "employee_odoo_id: breakdownEmployeeId" in js


def test_exceptions_js_has_breakdown_dismiss_handler():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")
    assert "js-breakdown-dismiss" in js
    assert "/api/exceptions/breakdown/dismiss" in js


def test_exceptions_js_refreshes_breakdown_badge():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")
    assert "'breakdown'" in js


def test_exceptions_js_has_running_late_handler():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")
    assert "js-running-late" in js
    assert "js-running-late-open" not in js
    assert "js-running-late-save" not in js
    assert "/api/late-report/running-late" in js
