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


DEFAULT_TARGET_SECONDS = 240.0
MAX_DRIVERS = 12


@dataclass
class RecResult:
    drivers: int | None
    predicted_seconds: float | None
    overloaded: bool


def recommend_for_target(lambda_per_hr: float, mean_handle_seconds: float,
                         target_seconds: float = DEFAULT_TARGET_SECONDS,
                         k: float = 1.0, max_drivers: int = MAX_DRIVERS) -> RecResult:
    """Smallest crew whose calibrated predicted time-to-claim is <= target.
    Returns overloaded=True (drivers=None) if no crew up to max_drivers qualifies."""
    if lambda_per_hr <= 0 or mean_handle_seconds <= 0:
        return RecResult(drivers=1, predicted_seconds=0.0, overloaded=False)
    for c in range(1, max_drivers + 1):
        w = k * erlang_c_wait_seconds(c, lambda_per_hr, mean_handle_seconds)
        if w <= target_seconds:
            return RecResult(drivers=c, predicted_seconds=w, overloaded=False)
    return RecResult(drivers=None, predicted_seconds=None, overloaded=True)
