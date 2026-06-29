import math

from zira_dashboard import forklift_score as fs


def _row(calls, on_time, late, avg_ms, util):
    return {"calls": calls, "on_time": on_time, "late": late,
            "avg_ms": avg_ms, "utilization_pct": util}


def test_subscores_hit_targets():
    cfg = fs.DEFAULT_SCORE_CONFIG
    # 25 calls -> calls sub = 100; 30s avg -> speed = 100; 100% on-time -> 100; util passthrough
    b = fs.daily_score(_row(25, 100, 0, 30000, 80), cfg)
    c = b.components
    assert round(c["calls"]["sub"]) == 100
    assert round(c["speed"]["sub"]) == 100
    assert round(c["ontime"]["sub"]) == 100
    assert round(c["util"]["sub"]) == 80


def test_calls_subscore_caps_at_100():
    b = fs.daily_score(_row(50, 50, 0, 60000, 50), fs.DEFAULT_SCORE_CONFIG)
    assert b.components["calls"]["sub"] == 100  # 50/25 capped


def test_speed_floor_and_ceiling():
    cfg = fs.DEFAULT_SCORE_CONFIG
    fast = fs.daily_score(_row(20, 20, 0, 30000, 50), cfg).components["speed"]["sub"]
    slow = fs.daily_score(_row(20, 20, 0, 180000, 50), cfg).components["speed"]["sub"]
    assert round(fast) == 100 and round(slow) == 0


def test_ontime_floor_spreads_range():
    cfg = fs.DEFAULT_SCORE_CONFIG  # floor 80
    # 90% on-time -> (90-80)/(100-80)*100 = 50
    b = fs.daily_score(_row(20, 18, 2, 60000, 50), cfg)
    assert round(b.components["ontime"]["sub"]) == 50


def test_gate_returns_none_below_min_calls():
    assert fs.daily_score(_row(7, 7, 0, 30000, 100), fs.DEFAULT_SCORE_CONFIG) is None


def test_weighted_total_matches_hand_calc():
    cfg = fs.DEFAULT_SCORE_CONFIG  # 40/30/20/10
    # subs: calls 100, ontime (97-80)/20*100=85, speed (180-40)/150*100=93.33, util 22
    b = fs.daily_score(_row(31, 97, 3, 40000, 22), cfg)
    expected = 0.4*100 + 0.3*85 + 0.2*(140/150*100) + 0.1*22
    assert math.isclose(b.score, expected, rel_tol=1e-6)


def test_zero_weights_fall_back_to_equal():
    cfg = fs.ScoreConfig(weights={"calls": 0, "ontime": 0, "speed": 0, "util": 0})
    b = fs.daily_score(_row(25, 100, 0, 30000, 100), cfg)
    assert round(b.score) == 100  # equal weights, all subs 100


def test_no_calls_ontime_is_zero_not_crash():
    # gate is 8; use exactly min with on_time+late=0 guard via direct subscore call
    b = fs.daily_score(_row(8, 0, 0, 60000, 0), fs.DEFAULT_SCORE_CONFIG)
    assert b.components["ontime"]["sub"] == 0
