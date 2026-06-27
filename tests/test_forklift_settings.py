"""forklift_settings load/save/cache + effective_throughput math.

The roundtrip/cache tests are Postgres-backed (DB-gated); the
effective_throughput property test runs everywhere (no DB).
"""
import os

import pytest

from zira_dashboard import forklift_settings as fs


def test_effective_throughput_default_math():
    # No DB needed — construct Settings directly.
    s = fs.Settings()
    assert s.calls_per_hour == 16.0
    assert s.target_utilization == 0.65
    assert s.effective_throughput == pytest.approx(10.4)


def test_effective_throughput_custom_and_floor():
    assert fs.Settings(calls_per_hour=20.0, target_utilization=0.5).effective_throughput == pytest.approx(10.0)
    # Never returns 0 even with degenerate inputs.
    assert fs.Settings(calls_per_hour=0.0, target_utilization=0.0).effective_throughput == pytest.approx(0.1)


pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pytestmark_db
class TestDbRoundtrip:
    @pytest.fixture(autouse=True)
    def _reset(self):
        from zira_dashboard import db
        db.bootstrap_schema()
        db.execute(
            "UPDATE forklift_settings SET enabled=TRUE, calls_per_hour=16, "
            "target_utilization=0.65, include_loading_jockeying=FALSE, "
            "history_samples=8, coldstart_calls_per_day=0 WHERE id=1")
        fs.reload()
        yield
        db.execute(
            "UPDATE forklift_settings SET enabled=TRUE, calls_per_hour=16, "
            "target_utilization=0.65, include_loading_jockeying=FALSE, "
            "history_samples=8, coldstart_calls_per_day=0 WHERE id=1")
        fs.reload()

    def test_defaults_when_seeded(self):
        s = fs.current()
        assert s.enabled is True
        assert s.calls_per_hour == 16.0
        assert s.target_utilization == 0.65
        assert s.include_loading_jockeying is False
        assert s.history_samples == 8
        assert s.coldstart_calls_per_day == 0.0
        assert s.effective_throughput == pytest.approx(10.4)

    def test_save_round_trip_and_cache_invalidation(self):
        from zira_dashboard import db
        fs.save(fs.Settings(
            enabled=False, calls_per_hour=20.0, target_utilization=0.5,
            include_loading_jockeying=True, history_samples=12,
            coldstart_calls_per_day=300.0))
        s = fs.current()
        assert s.enabled is False
        assert s.calls_per_hour == 20.0
        assert s.target_utilization == 0.5
        assert s.include_loading_jockeying is True
        assert s.history_samples == 12
        assert s.coldstart_calls_per_day == 300.0
        assert s.effective_throughput == pytest.approx(10.0)
        # A direct DB change is not seen until reload (proves caching).
        db.execute("UPDATE forklift_settings SET history_samples=4 WHERE id=1")
        assert fs.current().history_samples == 12
        assert fs.reload().history_samples == 4
