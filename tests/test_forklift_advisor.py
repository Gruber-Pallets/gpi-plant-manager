from datetime import date

from zira_dashboard import forklift_advisor


def test_build_advisor_recommends_capacity_coverage(monkeypatch):
    """Recommendation = ceil(busiest-hour lambda / (throughput * utilization))."""
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=500, by_hour={9: 78.0, 10: 40.0}, peak_hour=9,
                            peak_calls=78.0, basis="history", n_days=8))
    monkeypatch.setattr(adv, "_algo_throughput", lambda: 19.0)     # data-derived
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])
    out = adv.build_advisor(date(2026, 6, 30), scheduled=5, backups=0)
    # effective = 19 * 0.75 = 14.25 ; ceil(78/14.25) = 6
    assert out["available"] is True
    assert out["recommended"] == 6
    assert out["observed_claim_seconds"] == 250.0
    assert "overloaded" not in out
    assert "predicted_claim_seconds" not in out
    assert out["coverage"].status == "short"      # 6 needed, 5 scheduled
    assert out["live_model"]["available"] is True
    assert out["live_model"]["recommended"] == 6
    assert round(out["live_model"]["effective_throughput"], 2) == 14.25
    assert out["live_model"]["lambda_per_hr"] == 78.0


def test_build_advisor_no_observed_claim_is_none(monkeypatch):
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=500, by_hour={9: 78.0}, peak_hour=9,
                            peak_calls=78.0, basis="history", n_days=8))
    monkeypatch.setattr(adv, "_algo_throughput", lambda: 19.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: None)
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])
    out = adv.build_advisor(date(2026, 6, 30), scheduled=6, backups=0)
    assert out["recommended"] == 6
    assert out["observed_claim_seconds"] is None
    assert out["coverage"].status == "ok"


def test_build_advisor_no_hourly_shape_suppresses_recommendation(monkeypatch):
    """A forecast with volume but no hourly shape yields no coverage number."""
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=500, by_hour={}, peak_hour=None,
                            peak_calls=0.0, basis="bootstrap", n_days=0))
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])
    out = adv.build_advisor(date(2026, 6, 30), scheduled=3, backups=2)
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
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_driver_throughput",
                        lambda days=28: None)   # -> DEFAULT_THROUGHPUT 16
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)

    adv = forklift_advisor.build_advisor(
        target_day=date(2026, 6, 26),   # Friday
        scheduled=7, backups=3,
    )
    assert adv["available"] is True
    assert adv["total_calls"] == 420
    # busiest hour 70/hr, effective 16*0.75=12 -> ceil(70/12) = 6
    assert adv["recommended"] == 6
    assert adv["observed_claim_seconds"] == 250.0
    assert adv["coverage"].status == "ok"      # 6 needed, 7 scheduled
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
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=2, backups=1)
    assert adv["available"] is True and adv["basis"] == "bootstrap"
    assert adv["total_calls"] == 100
    # peak hour = 9 (a real clock hour, not slot 36); 100/day * 70/100 = 70
    # calls/hr -> SLA recommends 5 @ 240s, k=1
    assert adv["peak_label"].startswith("9:")
    assert adv["recommended"] == 6
    assert adv["coverage"].status == "short"   # 6 needed, 2 scheduled


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
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=4, backups=0)
    # peak_label is "H:00–H+1:00"; the leading hour must be a real clock hour.
    peak_hour = int(adv["peak_label"].split(":")[0])
    assert 0 <= peak_hour <= 23


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
    """demand_summary returns the documented keys and the SAME capacity-coverage
    recommendation the scheduler card shows: ceil(planned-hour lambda /
    (throughput * utilization)). The retired SLA keys are gone."""
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [
                            {"day": date(2026, 6, 19), "total_calls": 420,
                             "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                             "by_station": {}}])
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_driver_throughput",
                        lambda days=28: None)   # -> DEFAULT_THROUGHPUT 16
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)
    summary = forklift_advisor.demand_summary(date(2026, 6, 26))  # Friday
    assert set(summary) == {
        "total_calls", "peak_calls", "peak_hour", "peak_label", "basis",
        "n_days", "recommended", "enabled", "observed_claim_seconds",
        "algo_recommended", "algo_values", "resolved_values", "overrides",
        "hour_values", "ranges",
    }
    assert "overloaded" not in summary
    assert "target_seconds" not in summary
    assert "predicted_claim_seconds" not in summary
    assert summary["enabled"] is True
    assert summary["total_calls"] == 420
    assert summary["peak_calls"] == 70.0
    assert summary["peak_hour"] == 9
    assert summary["basis"] == "history"
    assert summary["recommended"] == 6
    assert summary["observed_claim_seconds"] == 250.0


def test_demand_summary_no_signal_has_none_recommendation(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [])
    monkeypatch.setattr(forklift_advisor, "_weekly_trends_or_none", lambda: None)
    summary = forklift_advisor.demand_summary(date(2026, 6, 26))
    assert summary["recommended"] is None
    assert summary["peak_calls"] == 0.0
    assert summary["peak_label"] == "—"


def test_demand_summary_carries_algo_and_overrides_and_hour_values(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [{"day": date(2026, 6, 19), "total_calls": 420,
                                              "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                                              "by_station": {}}])
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_driver_throughput",
                        lambda days=28: None)   # -> DEFAULT_THROUGHPUT 16
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)
    s = forklift_advisor.demand_summary(date(2026, 6, 26))
    # all-auto (default 240s target): user recommendation matches the baseline.
    assert s["recommended"] == s["algo_recommended"] == 6
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


def test_demand_summary_user_and_algo_recommendations_can_diverge(monkeypatch):
    # by_hour has a clear busiest (70) vs quietest (30) hour. The algorithm plans
    # for the busiest hour (percentile 1.0); a plan_for override of 0.0 makes the
    # USER recommendation size to the quietest hour -> the two must differ.
    monkeypatch.setattr(forklift_advisor, "_cfg",
                        lambda: forklift_advisor.forklift_settings.Settings(
                            plan_for_percentile_override=0.0))
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [{"day": date(2026, 6, 19), "total_calls": 420,
                                              "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                                              "by_station": {}}])
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_driver_throughput",
                        lambda days=28: None)   # -> DEFAULT_THROUGHPUT 16, effective 12
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)
    s = forklift_advisor.demand_summary(date(2026, 6, 26))
    # algo plans busiest hour 70 -> ceil(70/12)=6 ; user plans quietest 30 -> ceil(30/12)=3
    assert s["algo_recommended"] == 6
    assert s["recommended"] == 3
