"""Pure forklift queueing model: Erlang-C wait, calibration against actual
recorded waits, and a crew recommendation for a time-to-claim target.
No DB, no templates."""
from __future__ import annotations

import math
from dataclasses import dataclass


def erlang_c_wait_seconds(c: int, lambda_per_hr: float, mean_handle_seconds: float) -> float:
    """Expected wait-in-queue (time-to-claim), seconds, for an M/M/c queue with
    `c` servers, arrival rate `lambda_per_hr` (calls/hr), service = handling time.
    Returns 0.0 for no/invalid load, math.inf when the queue is unstable (c<=a)."""
    if c < 1 or lambda_per_hr <= 0 or mean_handle_seconds <= 0:
        return 0.0
    mu = 3600.0 / mean_handle_seconds          # calls/hr per server
    a = lambda_per_hr / mu                      # offered load (Erlangs)
    if c <= a:
        return math.inf
    # Erlang-C probability of waiting
    summ = 0.0
    term = 1.0                                  # a^0 / 0!
    for k in range(c):
        if k > 0:
            term *= a / k
        summ += term                            # sum_{k=0}^{c-1} a^k/k!
    ac_over_cfact = term * (a / c)              # a^c / c!  (term is a^(c-1)/(c-1)!)
    top = ac_over_cfact * (c / (c - a))
    p_wait = top / (summ + top)
    wq_hours = p_wait / (c * mu - lambda_per_hr)
    return wq_hours * 3600.0
