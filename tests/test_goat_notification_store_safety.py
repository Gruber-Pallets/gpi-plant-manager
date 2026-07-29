"""Safety regression tests for GOAT notification-store database tests."""

from pathlib import Path


def test_notification_store_test_cleans_up_only_its_returned_alert():
    source = Path(__file__).with_name("test_goat_notification_store.py").read_text()

    assert "finally:" in source
    assert 'DELETE FROM goat_alerts WHERE id = %s' in source
    assert "SELECT id FROM goat_slack_deliveries WHERE goat_alert_id = %s" in source
    assert "DELETE FROM goat_notification_state" not in source
    assert "DELETE FROM goat_notification_days" not in source
    assert "WHERE achieved_day = %s" not in source
