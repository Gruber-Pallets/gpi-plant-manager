"""forklift_settings nullable-override model: resolver (auto vs override),
algorithm baseline, effective throughput, and the DB-gated load/save/cache
round-trip. The resolver tests run everywhere (no DB)."""
import os

import pytest

from zira_dashboard import forklift_settings as fs


def test_resolve_uses_algorithm_values_when_overrides_none():
    s = fs.Settings()  # all overrides None
    r = fs.resolve(s, algo_throughput=18.0)
    assert r.throughput == 18.0
    assert r.utilization == fs.DEFAULT_UTILIZATION == 0.75
    assert r.percentile == fs.DEFAULT_PLAN_FOR_PERCENTILE == 1.0
    assert r.history_samples == fs.DEFAULT_HISTORY_SAMPLES == 8
    assert round(r.effective_throughput, 2) == round(18.0 * 0.75, 2)


def test_resolve_has_no_target_claim_field():
    from zira_dashboard import forklift_settings as fs
    r = fs.resolve(fs.Settings(), algo_throughput=19.0)
    assert not hasattr(r, "target_claim_seconds")
    # utilization headroom lever is present
    assert 0.0 < r.utilization <= 1.0
    assert r.effective_throughput > 0


def test_resolve_prefers_overrides():
    s = fs.Settings(throughput_override=24.0, utilization_override=0.8,
                    plan_for_percentile_override=0.5, history_samples_override=4)
    r = fs.resolve(s, algo_throughput=18.0)
    assert (r.throughput, r.utilization, r.percentile, r.history_samples) == (24.0, 0.8, 0.5, 4)


def test_algorithm_values_ignores_overrides():
    s = fs.Settings(throughput_override=24.0, utilization_override=0.9)
    a = fs.algorithm_values(s, algo_throughput=18.0)
    assert a.throughput == 18.0 and a.utilization == 0.75 and a.percentile == 1.0


def test_effective_throughput_floor():
    # Never returns 0 even with degenerate inputs.
    r = fs.Resolved(throughput=0.0, utilization=0.0, percentile=1.0, history_samples=8)
    assert r.effective_throughput == pytest.approx(0.1)


def test_score_config_defaults_when_unset():
    from zira_dashboard import forklift_score as fscore
    s = fs.Settings()  # all overrides None
    cfg = fs.resolve(s, algo_throughput=16).score_config()
    assert isinstance(cfg, fscore.ScoreConfig)
    assert cfg.weights == {"calls": 40.0, "ontime": 30.0, "speed": 20.0, "util": 10.0}
    assert cfg.min_calls == 8 and cfg.target_calls == 25.0
    assert cfg.ontime_floor == 80.0 and cfg.fast_secs == 30.0 and cfg.slow_secs == 180.0


def test_score_config_applies_overrides():
    s = fs.Settings(score_w_calls=50, score_w_ontime=20, score_w_speed=20,
                    score_w_util=10, score_min_calls=12, score_target_calls=30)
    cfg = fs.resolve(s, algo_throughput=16).score_config()
    assert cfg.weights["calls"] == 50.0 and cfg.min_calls == 12 and cfg.target_calls == 30.0
    assert cfg.weights["ontime"] == 20.0 and cfg.weights["util"] == 10.0


pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pytestmark_db
class TestDbRoundtrip:
    @pytest.fixture(autouse=True)
    def _reset(self):
        from zira_dashboard import db
        db.bootstrap_schema()
        db.execute(
            "UPDATE forklift_settings SET enabled=TRUE, throughput_override=NULL, "
            "utilization_override=NULL, plan_for_percentile_override=NULL, "
            "history_samples_override=NULL, include_loading_jockeying=FALSE, "
            "coldstart_calls_per_day=0 WHERE id=1")
        fs.reload()
        yield
        db.execute(
            "UPDATE forklift_settings SET enabled=TRUE, throughput_override=NULL, "
            "utilization_override=NULL, plan_for_percentile_override=NULL, "
            "history_samples_override=NULL, include_loading_jockeying=FALSE, "
            "coldstart_calls_per_day=0 WHERE id=1")
        fs.reload()

    def test_defaults_when_seeded_are_auto(self):
        s = fs.current()
        assert s.enabled is True
        assert s.throughput_override is None
        assert s.utilization_override is None
        assert s.plan_for_percentile_override is None
        assert s.history_samples_override is None
        assert s.include_loading_jockeying is False
        assert s.coldstart_calls_per_day == 0.0

    def test_save_round_trip_overrides_and_auto(self):
        from zira_dashboard import db
        # Save a mix of set overrides and auto (None).
        fs.save(fs.Settings(
            enabled=False, throughput_override=24.0, utilization_override=0.8,
            plan_for_percentile_override=None, history_samples_override=12,
            include_loading_jockeying=True, coldstart_calls_per_day=300.0))
        s = fs.current()
        assert s.enabled is False
        assert s.throughput_override == 24.0
        assert s.utilization_override == 0.8
        assert s.plan_for_percentile_override is None      # auto round-trips as None
        assert s.history_samples_override == 12
        assert s.include_loading_jockeying is True
        assert s.coldstart_calls_per_day == 300.0
        # A direct DB change is not seen until reload (proves caching).
        db.execute("UPDATE forklift_settings SET history_samples_override=4 WHERE id=1")
        assert fs.current().history_samples_override == 12
        assert fs.reload().history_samples_override == 4
