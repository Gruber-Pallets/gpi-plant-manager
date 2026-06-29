import math

from zira_dashboard import forklift_queue as q


def test_mm1_matches_closed_form():
    # c=1 reduces to M/M/1: W_q = rho/(mu - lambda). lambda=10/hr, handle=180s -> mu=20/hr.
    # rho=0.5 -> W_q = 0.5/(20-10) hr = 0.05 hr = 180 s.
    assert math.isclose(q.erlang_c_wait_seconds(1, 10, 180), 180.0, rel_tol=1e-6)


def test_unstable_returns_inf():
    # lambda=25/hr, handle=180s -> mu=20/hr; one server can't keep up (a=1.25 >= c=1).
    assert q.erlang_c_wait_seconds(1, 25, 180) == math.inf


def test_more_servers_strictly_reduce_wait():
    w5 = q.erlang_c_wait_seconds(5, 97, 180)
    w6 = q.erlang_c_wait_seconds(6, 97, 180)
    w7 = q.erlang_c_wait_seconds(7, 97, 180)
    assert w5 > w6 > w7 >= 0


def test_zero_or_invalid_load_is_zero_wait():
    assert q.erlang_c_wait_seconds(3, 0, 180) == 0.0
    assert q.erlang_c_wait_seconds(3, 50, 0) == 0.0
    assert q.erlang_c_wait_seconds(0, 50, 180) == 0.0


def test_recommend_picks_smallest_crew_under_target():
    # busy hour ~97 calls/hr, 3-min handle, calibration 1.8x, target 240s.
    r = q.recommend_for_target(97, 180, target_seconds=240, k=1.8)
    assert r.drivers == 6 and not r.overloaded
    assert r.predicted_seconds is not None and r.predicted_seconds <= 240


def test_tighter_target_needs_more_drivers():
    loose = q.recommend_for_target(97, 180, target_seconds=300, k=1.8).drivers
    tight = q.recommend_for_target(97, 180, target_seconds=90, k=1.8).drivers
    assert tight > loose


def test_overload_when_cap_too_low():
    r = q.recommend_for_target(97, 180, target_seconds=60, k=1.8, max_drivers=5)
    assert r.drivers is None and r.overloaded is True


def test_near_zero_demand_recommends_one():
    r = q.recommend_for_target(0.0, 180, target_seconds=240, k=1.0)
    assert r.drivers == 1 and not r.overloaded


def test_calibration_is_median_ratio_of_actual_to_predicted():
    # Build samples whose actual wait is ~2x the model's prediction at their (crew, lambda).
    handle = 180
    samples = []
    for lam, crew in [(60, 5), (70, 6), (80, 6), (50, 5), (65, 6)]:
        pred = q.erlang_c_wait_seconds(crew, lam, handle)
        samples.append({"avg_lambda": lam, "crew": crew, "actual_wait_seconds": 2.0 * pred})
    res = q.fit_calibration(samples, handle)
    assert abs(res.k - 2.0) < 1e-6 and res.uncalibrated is False
    assert res.n_samples == 5


def test_calibration_clamps_extremes():
    handle = 180
    s = [{"avg_lambda": 60, "crew": 5,
          "actual_wait_seconds": 999 * q.erlang_c_wait_seconds(5, 60, handle)}] * 6
    assert q.fit_calibration(s, handle).k == 5.0  # clamped to max


def test_too_few_samples_is_uncalibrated():
    handle = 180
    s = [{"avg_lambda": 60, "crew": 5,
          "actual_wait_seconds": 2 * q.erlang_c_wait_seconds(5, 60, handle)}] * 3
    res = q.fit_calibration(s, handle)
    assert res.k == 1.0 and res.uncalibrated is True


def test_unstable_samples_are_skipped():
    handle = 180
    # crew below offered load -> inf prediction -> skipped, leaving too few -> uncalibrated
    s = [{"avg_lambda": 500, "crew": 1, "actual_wait_seconds": 100.0}] * 8
    assert q.fit_calibration(s, handle).uncalibrated is True


def test_malformed_sample_is_skipped_not_fatal():
    # One malformed row (missing/garbage fields) among good ones is skipped,
    # leaving enough good samples to calibrate -> no raise.
    handle = 180
    good = []
    for lam, crew in [(60, 5), (70, 6), (80, 6), (50, 5), (65, 6)]:
        pred = q.erlang_c_wait_seconds(crew, lam, handle)
        good.append({"avg_lambda": lam, "crew": crew, "actual_wait_seconds": 2.0 * pred})
    bad = {"avg_lambda": "not-a-number", "crew": None}  # missing actual_wait_seconds too
    res = q.fit_calibration(good + [bad], handle)
    assert res.n_samples == 5 and res.uncalibrated is False
    assert abs(res.k - 2.0) < 1e-6


def test_only_malformed_samples_is_uncalibrated_not_fatal():
    handle = 180
    s = [{"avg_lambda": "x", "crew": "y", "actual_wait_seconds": "z"}] * 8
    res = q.fit_calibration(s, handle)
    assert res.uncalibrated is True and res.n_samples == 0
