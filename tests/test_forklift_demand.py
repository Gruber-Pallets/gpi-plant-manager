from zira_dashboard import forklift_demand as fd


def _snap(total, by_hour):
    return {"total_calls": total, "by_hour": by_hour, "by_station": {}}


def test_predict_from_history_uses_median_and_peak():
    snaps = [
        _snap(400, {"8": {"calls": 30}, "9": {"calls": 70}}),
        _snap(420, {"8": {"calls": 40}, "9": {"calls": 60}}),
        _snap(440, {"8": {"calls": 50}, "9": {"calls": 80}}),
    ]
    f = fd.predict_from_history(snaps)
    assert f.total_calls == 420            # median of 400,420,440
    assert f.peak_hour == 9
    assert f.peak_calls == 70              # mean of 70,60,80
    assert f.basis == "history" and f.n_days == 3


def test_predict_from_history_empty_returns_zero_basis_none():
    f = fd.predict_from_history([])
    assert f.total_calls == 0 and f.basis == "none" and f.peak_hour is None


def test_bootstrap_from_trends_divides_week_by_operating_days():
    trends = {"weeks": [
        {"claimedCalls": 2000}, {"claimedCalls": 2100},
    ]}
    f = fd.bootstrap_from_trends(trends, operating_days=5)
    # mean weekly = 2050 -> per day = 410
    assert f.total_calls == 410 and f.basis == "bootstrap"


def test_forecast_from_total_and_shape_distributes_volume():
    slots = [{"slot": 8, "calls": 30}, {"slot": 9, "calls": 70}]
    f = fd.forecast_from_total_and_shape(450, slots)
    assert f.basis == "bootstrap"
    assert f.peak_hour == 9
    assert f.peak_calls == 315.0          # 450 * 70/100
    assert f.by_hour[8] == 135.0          # 450 * 30/100
    assert f.total_calls == 450


def test_forecast_from_total_and_shape_no_shape_is_shapeless():
    f = fd.forecast_from_total_and_shape(450, [])
    assert f.basis == "bootstrap" and f.peak_calls == 0.0 and f.total_calls == 450


def test_recommend_drivers_ceils_peak_over_throughput():
    assert fd.recommend_drivers(peak_calls=70, throughput_per_hour=30) == 3
    assert fd.recommend_drivers(peak_calls=0, throughput_per_hour=30) == 1  # floor of 1


def test_assess_coverage_ok_and_short():
    ok = fd.assess_coverage(recommended=3, scheduled=3, backups=3)
    assert ok.status == "ok" and ok.gap == 0 and ok.scheduled == 3
    short = fd.assess_coverage(recommended=4, scheduled=2, backups=3)
    assert short.status == "short" and short.gap == 2


def test_demand_at_percentile_busiest_typical_and_empty():
    by_hour = {8: 30.0, 9: 70.0, 10: 50.0}  # sorted by calls: 30(8),50(10),70(9)
    assert fd.demand_at_percentile(by_hour, 1.0) == (9, 70.0)   # busiest
    assert fd.demand_at_percentile(by_hour, 0.5) == (10, 50.0)  # median hour
    assert fd.demand_at_percentile(by_hour, 0.0) == (8, 30.0)   # quietest
    assert fd.demand_at_percentile({}, 1.0) == (None, 0.0)
