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
