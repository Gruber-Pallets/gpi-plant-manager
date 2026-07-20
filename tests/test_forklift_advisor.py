from datetime import date

from zira_dashboard import forklift_advisor


def test_build_advisor_uses_sla_recommender(monkeypatch):
    """build_advisor sizes the crew with the queue SLA recommender (smallest crew
    whose predicted time-to-claim stays under the target), not the capacity ratio."""
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    # Force a known forecast (busy hour 97 calls/hr), handling time, no calibration.
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=500, by_hour={9: 97.0, 10: 40.0}, peak_hour=9,
                            peak_calls=97.0, basis="history", n_days=8))
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])
    out = adv.build_advisor(date(2026, 6, 30), scheduled=3, backups=2)
    # target default 240s, k=1 (no samples) -> recommend ~5-6 under the target.
    assert out["available"] is True
    assert out["recommended"] in (5, 6)
    assert out["overloaded"] is False
    assert out["target_seconds"] == 240.0
    assert out["predicted_claim_seconds"] is not None
    assert out["predicted_claim_seconds"] <= 240.0
    assert out["backtest"]["uncalibrated"] is True
    # algorithm baseline (same calc at the DEFAULT 240s target) is carried too.
    assert out["algo_recommended"] in (5, 6)
    # coverage line preserved (sized to the SLA recommendation).
    assert out["coverage"] is not None


def test_build_advisor_predicts_time_to_claim_from_scheduled_drivers(monkeypatch):
    """The recommendation is still target-sized, but the displayed prediction is
    run against the actual scheduled driver count."""
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=500, by_hour={9: 97.0, 10: 40.0}, peak_hour=9,
                            peak_calls=97.0, basis="history", n_days=8))
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])

    out = adv.build_advisor(date(2026, 6, 30), scheduled=5, backups=0)

    assert out["recommended"] == 6
    assert out["predicted_claim_seconds"] <= out["target_seconds"]
    assert out["predicted_scheduled_claim_seconds"] > out["target_seconds"] * 1.5
    assert out["scheduled_prediction_overloaded"] is False
    assert out["scheduled_prediction_status"] == "danger"
    assert out["live_model"]["available"] is True
    assert out["live_model"]["recommended"] == 6
    assert out["live_model"]["lambda_per_hr"] == 97.0
    assert out["live_model"]["mean_handle_seconds"] == 180.0
    assert out["live_model"]["calibration_k"] == 1.0
    assert out["live_model"]["target_seconds"] == out["target_seconds"]


def test_build_advisor_marks_scheduled_prediction_overloaded(monkeypatch):
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=500, by_hour={9: 97.0, 10: 40.0}, peak_hour=9,
                            peak_calls=97.0, basis="history", n_days=8))
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])

    out = adv.build_advisor(date(2026, 6, 30), scheduled=4, backups=0)

    assert out["recommended"] == 6
    assert out["predicted_scheduled_claim_seconds"] is None
    assert out["scheduled_prediction_overloaded"] is True
    assert out["scheduled_prediction_status"] == "danger"


def test_build_advisor_overloaded_when_target_unreachable(monkeypatch):
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    # Extreme demand: even MAX_DRIVERS can't hold the target.
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=5000, by_hour={9: 900.0}, peak_hour=9,
                            peak_calls=900.0, basis="history", n_days=8))
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])
    out = adv.build_advisor(date(2026, 6, 30), scheduled=3, backups=2)
    assert out["available"] is True
    assert out["overloaded"] is True
    assert out["recommended"] is None


def test_build_advisor_falls_back_when_no_handle_time(monkeypatch):
    """No handling time yet -> degrade to the 'builds as history accrues' advisor,
    never raise into the request path."""
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=500, by_hour={9: 97.0}, peak_hour=9,
                            peak_calls=97.0, basis="history", n_days=8))
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: None)
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])
    out = adv.build_advisor(date(2026, 6, 30), scheduled=3, backups=2)
    # Degrades quietly: recommendation can't be computed, but no exception.
    assert out["available"] is True
    assert out["recommended"] is None


def test_build_advisor_with_history(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [
                            {"day": date(2026, 6, 19), "total_calls": 420,
                             "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                             "by_station": {}}])
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting",
                        lambda k: ["Louie", "Juan", "Luke"])
    # SLA model: 180s handle, no calibration samples (k=1).
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])

    adv = forklift_advisor.build_advisor(
        target_day=date(2026, 6, 26),   # Friday
        scheduled=7, backups=3,
    )
    assert adv["available"] is True
    assert adv["total_calls"] == 420
    # busiest hour ~70 calls/hr @ 180s, target 240s, k=1 -> 5 drivers under target
    assert adv["recommended"] == 5
    assert adv["predicted_claim_seconds"] <= adv["target_seconds"] == 240.0
    assert adv["coverage"].status == "ok"      # 5 needed, 7 scheduled
    assert adv["basis"] == "history"
    assert "9" in adv["peak_label"] or "9" in str(adv["peak_label"])


def test_cold_start_uses_today_shape_for_recommendation(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [])
    monkeypatch.setattr(forklift_advisor, "_weekly_trends_or_none",
                        lambda: {"weeks": [{"claimedCalls": 500}]})  # /5 = 100/day
    # The live dashboard reports 15-minute slots: hour 8 = slots 32-35,
    # hour 9 = slots 36-39. Folded, that is 30 calls in hour 8, 70 in hour 9.
    monkeypatch.setattr(forklift_advisor, "_today_hourly_shape_or_none",
                        lambda: [{"slot": 32, "calls": 30}, {"slot": 36, "calls": 70}])
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting", lambda k: [])
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=2, backups=1)
    assert adv["available"] is True and adv["basis"] == "bootstrap"
    assert adv["total_calls"] == 100
    # peak hour = 9 (a real clock hour, not slot 36); 100/day * 70/100 = 70
    # calls/hr -> SLA recommends 5 @ 240s, k=1
    assert adv["peak_label"].startswith("9:")
    assert adv["recommended"] == 5
    assert adv["coverage"].status == "short"   # 5 needed, 2 scheduled


def test_cold_start_shape_never_produces_impossible_hours(monkeypatch):
    """Regression: the raw dashboard slots (27-59) are 15-minute buckets. They
    must fold to clock hours 0-23, not leak through as bogus hours like 39."""
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [])
    monkeypatch.setattr(forklift_advisor, "_weekly_trends_or_none",
                        lambda: {"weeks": [{"claimedCalls": 500}]})
    monkeypatch.setattr(forklift_advisor, "_today_hourly_shape_or_none",
                        lambda: [{"slot": s, "calls": 10} for s in range(27, 60)])
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting", lambda k: [])
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=4, backups=0)
    # peak_label is "H:00–H+1:00"; the leading hour must be a real clock hour.
    peak_hour = int(adv["peak_label"].split(":")[0])
    assert 0 <= peak_hour <= 23


def test_build_advisor_suppresses_overloaded_when_calibration_maxed(monkeypatch):
    """Guard: when the fit slams the upper clamp (k == 5.0), the queue model
    under-predicts so badly that its 'no crew can meet the target' verdict is not
    trustworthy. build_advisor must NOT show 'Overloaded'; it falls back to the
    raw (uninflated) crew size instead."""
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=2000, by_hour={9: 210.0}, peak_hour=9,
                            peak_calls=210.0, basis="history", n_days=8))
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    # Samples whose actual waits dwarf the model prediction -> median ratio pinned
    # at the 5.0 clamp (calibrated, not uncalibrated).
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [
                            {"day": None, "avg_lambda": 50.0, "crew": 6,
                             "actual_wait_seconds": 600.0} for _ in range(6)])
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])
    out = adv.build_advisor(date(2026, 6, 30), scheduled=4, backups=0)
    assert out["available"] is True
    assert out["backtest"]["uncalibrated"] is False
    assert out["overloaded"] is False              # NOT the alarming verdict
    assert out["recommended"] is not None and 1 <= out["recommended"] <= 12


def test_cold_start_without_shape_suppresses_recommendation(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [])
    monkeypatch.setattr(forklift_advisor, "_weekly_trends_or_none",
                        lambda: {"weeks": [{"claimedCalls": 2250}]})
    monkeypatch.setattr(forklift_advisor, "_today_hourly_shape_or_none", lambda: None)
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting", lambda k: [])
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=0, backups=0)
    assert adv["available"] is True and adv["recommended"] is None and adv["coverage"] is None
    assert adv["total_calls"] == 450


def test_build_advisor_no_data_returns_unavailable(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [])
    monkeypatch.setattr(forklift_advisor, "_weekly_trends_or_none", lambda: None)
    adv = forklift_advisor.build_advisor(
        target_day=date(2026, 6, 26), scheduled=0, backups=0)
    assert adv["available"] is False


def test_build_advisor_unavailable_when_disabled(monkeypatch):
    """When settings.enabled is False the advisor short-circuits to
    available=False (and never touches the data source)."""
    from zira_dashboard import forklift_settings
    disabled = forklift_settings.Settings(enabled=False)
    monkeypatch.setattr(forklift_advisor.forklift_settings, "current",
                        lambda: disabled)
    # Even with real history present, disabled wins.
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [
                            {"day": date(2026, 6, 19), "total_calls": 420,
                             "by_hour": {"9": {"calls": 70}}, "by_station": {}}])
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=7, backups=3)
    assert adv == {"available": False}


def test_demand_summary_keys_and_recommendation(monkeypatch):
    """demand_summary returns the documented keys and the SAME SLA recommendation
    the scheduler card shows: smallest crew whose predicted time-to-claim stays
    under the target (NOT the old capacity ratio). The user-facing capacity number
    is gone."""
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [
                            {"day": date(2026, 6, 19), "total_calls": 420,
                             "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                             "by_station": {}}])
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    summary = forklift_advisor.demand_summary(date(2026, 6, 26))  # Friday
    assert set(summary) == {
        "total_calls", "peak_calls", "peak_hour", "peak_label", "basis",
        "n_days", "recommended", "enabled", "overloaded",
        "target_seconds", "predicted_claim_seconds", "backtest", "target_minutes",
        "algo_recommended", "algo_values", "resolved_values", "overrides",
        "hour_values", "ranges",
    }
    # The retired capacity-ratio number must not surface.
    assert "effective_throughput" not in summary
    assert summary["enabled"] is True
    assert summary["total_calls"] == 420
    assert summary["peak_calls"] == 70.0
    assert summary["peak_hour"] == 9
    assert summary["basis"] == "history"
    # SLA model: 70 calls/hr @ 180s handle, target 240s, k=1 -> 5 drivers.
    assert summary["recommended"] == 5
    assert summary["overloaded"] is False
    assert summary["target_seconds"] == 240.0
    assert summary["predicted_claim_seconds"] is not None
    assert summary["predicted_claim_seconds"] <= 240.0
    assert summary["backtest"]["uncalibrated"] is True


def test_demand_summary_no_signal_has_none_recommendation(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [])
    monkeypatch.setattr(forklift_advisor, "_weekly_trends_or_none", lambda: None)
    summary = forklift_advisor.demand_summary(date(2026, 6, 26))
    assert summary["recommended"] is None
    assert summary["peak_calls"] == 0.0
    assert summary["peak_label"] == "—"


def test_build_advisor_reports_algo_and_user_recommendations(monkeypatch):
    """The SLA recommendation tracks the user's TARGET; the algorithm baseline
    is the same calc at the default 240s target. A looser target -> fewer drivers
    than the baseline."""
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [{"day": date(2026, 6, 19), "total_calls": 420,
                                              "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                                              "by_station": {}}])
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting", lambda k: [])
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    # user loosens the target to 300s -> 4 drivers (c=4 predicts ~266s <= 300),
    # while the baseline at 240s needs 5.
    monkeypatch.setattr(forklift_advisor, "_cfg",
                        lambda: forklift_advisor.forklift_settings.Settings(target_claim_seconds=300.0))
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=2, backups=0)
    assert adv["available"] is True
    assert adv["target_seconds"] == 300.0
    assert adv["algo_recommended"] == 5      # default 240s target
    assert adv["recommended"] == 4           # looser 300s target
    assert adv["recommended"] < adv["algo_recommended"]


def test_demand_summary_carries_algo_and_overrides_and_hour_values(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [{"day": date(2026, 6, 19), "total_calls": 420,
                                              "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                                              "by_station": {}}])
    monkeypatch.setattr(forklift_advisor.forklift_store, "recent_driver_throughput",
                        lambda days=28: None)  # -> DEFAULT_THROUGHPUT 16
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    s = forklift_advisor.demand_summary(date(2026, 6, 26))
    # all-auto (default 240s target): user recommendation matches the baseline.
    assert s["recommended"] == s["algo_recommended"] == 5
    assert s["target_seconds"] == 240.0
    assert s["hour_values"] == [30.0, 70.0]          # sorted ascending for JS preview
    # plan-for + history sliders survive; their algorithm ticks are still carried.
    assert s["algo_values"]["percentile"] == 1.0
    assert s["algo_values"]["history_samples"] == 8
    # overrides for the surviving knobs all None when auto.
    assert s["overrides"]["plan_for"] is None
    assert s["overrides"]["history_samples"] is None
    # slider ranges present (capacity knobs retired but ranges dict keeps its keys
    # so the JS live-preview still resolves plan_for/history).
    assert {"plan_for", "history_samples"} <= set(s["ranges"])
