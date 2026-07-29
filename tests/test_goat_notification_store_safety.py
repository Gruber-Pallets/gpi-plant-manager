"""Safety regression tests for GOAT notification-store database tests."""

from pathlib import Path


def test_notification_store_tests_do_not_delete_shared_notification_rows():
    source = Path(__file__).with_name("test_goat_notification_store.py").read_text()

    assert "DELETE FROM goat_" not in source
