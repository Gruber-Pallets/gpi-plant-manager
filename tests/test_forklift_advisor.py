from datetime import date

from zira_dashboard import forklift_advisor


def test_build_advisor_with_history(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [
                            {"day": date(2026, 6, 19), "total_calls": 420,
                             "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                             "by_station": {}}])
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting",
                        lambda k: ["Louie", "Juan", "Luke"])

    adv = forklift_advisor.build_advisor(
        target_day=date(2026, 6, 26),   # Friday
        scheduled=7, backups=3,
    )
    assert adv["available"] is True
    assert adv["total_calls"] == 420
    assert adv["recommended"] == 7            # ceil(70/10)
    assert adv["coverage"].status == "ok"
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
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=2, backups=1)
    assert adv["available"] is True and adv["basis"] == "bootstrap"
    assert adv["total_calls"] == 100
    assert adv["recommended"] == 7            # ceil(70/10): 100/day * 70/100 peak = 70
    assert adv["coverage"].status == "short"


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
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [{"day": date(2026, 6, 19), "total_calls": 420,
                                              "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                                              "by_station": {}}])
    monkeypatch.setattr(forklift_advisor.forklift_store, "recent_driver_throughput",
                        lambda days=28: None)  # -> DEFAULT_THROUGHPUT 16
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting", lambda k: [])
    # user overrides driver speed way up -> their recommendation drops below algo's
    monkeypatch.setattr(forklift_advisor, "_cfg",
                        lambda: forklift_advisor.forklift_settings.Settings(throughput_override=70.0))
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=2, backups=0)
    assert adv["available"] is True
    assert adv["algo_recommended"] == 7      # ceil(70 / (16*0.65=10.4))
    assert adv["recommended"] == 2           # ceil(70 / (70*0.65=45.5)) = 2
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
