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
    monkeypatch.setattr(forklift_advisor, "_today_hourly_shape_or_none",
                        lambda: [{"slot": 8, "calls": 30}, {"slot": 9, "calls": 70}])
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting", lambda k: [])
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=2, backups=1)
    assert adv["available"] is True and adv["basis"] == "bootstrap"
    assert adv["total_calls"] == 100
    # peak hour = 100/day * 70/100 = 70 calls/hr -> SLA recommends 5 @ 240s, k=1
    assert adv["recommended"] == 5
    assert adv["coverage"].status == "short"   # 5 needed, 2 scheduled


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
    """demand_summary returns the documented keys with a sane recommended
    derived from the predicted peak and the effective throughput (16*0.65=10.4)."""
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [
                            {"day": date(2026, 6, 19), "total_calls": 420,
                             "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                             "by_station": {}}])
    summary = forklift_advisor.demand_summary(date(2026, 6, 26))  # Friday
    assert set(summary) == {
        "total_calls", "peak_calls", "peak_hour", "peak_label", "basis",
        "n_days", "effective_throughput", "recommended", "enabled",
        "algo_recommended", "algo_values", "resolved_values", "overrides",
        "hour_values", "ranges",
    }
    assert summary["enabled"] is True
    assert summary["total_calls"] == 420
    assert summary["peak_calls"] == 70.0
    assert summary["peak_hour"] == 9
    assert summary["basis"] == "history"
    assert summary["effective_throughput"] == 10.4
    assert summary["recommended"] == 7            # ceil(70 / 10.4) = 7


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
    s = forklift_advisor.demand_summary(date(2026, 6, 26))
    # all-auto: user recommendation matches the algorithm baseline.
    assert s["recommended"] == s["algo_recommended"] == 7
    assert s["hour_values"] == [30.0, 70.0]          # sorted ascending for JS preview
    assert s["algo_values"]["throughput"] == 16.0
    assert s["algo_values"]["utilization"] == 0.65
    assert s["algo_values"]["percentile"] == 1.0
    assert s["algo_values"]["history_samples"] == 8
    # overrides all None when auto.
    assert s["overrides"]["throughput"] is None
    assert s["overrides"]["utilization"] is None
    # slider ranges present.
    assert set(s["ranges"]) == {"throughput", "utilization_pct", "plan_for", "history_samples"}
