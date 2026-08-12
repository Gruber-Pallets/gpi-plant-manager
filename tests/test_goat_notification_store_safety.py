"""Safety regression tests for GOAT notification-store database tests."""

from pathlib import Path


def test_notification_store_database_tests_require_a_safe_rollback_only_target():
    source = Path(__file__).with_name("test_goat_notification_store.py").read_text()

    assert "PAYROLL_GUARD_TEST_DATABASE" in source
    assert "_LOOPBACK_DATABASE_HOSTS" in source
    assert 'params.get("dbname", "").endswith("_test")' in source
    assert "with pytest.raises(_RollbackIntegrationData):" in source
    assert "DELETE FROM goat_alerts" not in source
