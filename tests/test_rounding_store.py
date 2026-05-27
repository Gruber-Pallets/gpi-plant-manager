"""Tests for rounding_store load/save/cache behavior. Postgres-backed."""

import os

import pytest

from zira_dashboard import db, rounding_store
from zira_dashboard.rounding import RoundingSettings


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset to ship-disabled defaults around each test, AND clear the cache."""
    db.execute(
        "UPDATE rounding_settings SET in_before_min = 0, in_after_min = 0, "
        "out_before_min = 0, out_after_min = 0 WHERE id = 1"
    )
    rounding_store.reload()
    yield
    db.execute(
        "UPDATE rounding_settings SET in_before_min = 0, in_after_min = 0, "
        "out_before_min = 0, out_after_min = 0 WHERE id = 1"
    )
    rounding_store.reload()


def test_default_settings_all_zero():
    s = rounding_store.current()
    assert s == RoundingSettings(0, 0, 0, 0)


def test_save_persists_and_invalidates_cache():
    rounding_store.save(RoundingSettings(20, 0, 0, 20))
    # Reload from DB to confirm persistence.
    rounding_store.reload()
    s = rounding_store.current()
    assert s == RoundingSettings(20, 0, 0, 20)


def test_save_returns_settings_via_current_immediately():
    """After save(), current() should reflect the new values without
    requiring an explicit reload."""
    rounding_store.save(RoundingSettings(15, 5, 5, 15))
    assert rounding_store.current() == RoundingSettings(15, 5, 5, 15)


def test_current_is_cached():
    """Mutating the DB directly without calling save() should NOT show up
    in current() until reload() is called — proves the cache works."""
    rounding_store.current()  # prime the cache
    db.execute("UPDATE rounding_settings SET in_before_min = 99 WHERE id = 1")
    assert rounding_store.current().in_before_min == 0  # stale cache
    rounding_store.reload()
    assert rounding_store.current().in_before_min == 99
