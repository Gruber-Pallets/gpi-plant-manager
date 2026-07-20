"""Read-only diagnostic: dump the exact inputs feeding the forklift SLA
recommender and show why it reports 'Overloaded / Target missed'.

Run: railway run python -m scripts.diagnose_forklift_overload
"""
from __future__ import annotations

from datetime import date

from zira_dashboard import (
    forklift_advisor,
    forklift_demand,
    forklift_queue,
    forklift_settings,
)


def main() -> None:
    today = date.today()
    cfg = forklift_advisor._cfg()
    algo_throughput = forklift_advisor._algo_throughput()
    resolved = forklift_settings.resolve(cfg, algo_throughput=algo_throughput)

    forecast = forklift_advisor._forecast(
        today, resolved.history_samples, cfg.coldstart_calls_per_day)
    mean_handle = forklift_advisor._mean_handle_or_none()

    print("=== settings / resolved ===")
    print(f"enabled              : {cfg.enabled}")
    print(f"target_claim_seconds : {resolved.target_claim_seconds}")
    print(f"percentile (plan_for): {resolved.percentile}")
    print(f"history_samples      : {resolved.history_samples}")

    print("\n=== forecast ===")
    print(f"basis      : {forecast.basis}  n_days={forecast.n_days}")
    print(f"total_calls: {forecast.total_calls}")
    print(f"peak_hour  : {forecast.peak_hour}  peak_calls/hr={forecast.peak_calls}")
    print(f"by_hour    : {forecast.by_hour}")

    hour, lam = forklift_demand.demand_at_percentile(
        forecast.by_hour, resolved.percentile)
    print(f"\nsized-to hour={hour}  lambda_per_hr={lam}")

    print("\n=== service time (mean_handle_seconds) ===")
    print(f"mean_handle_seconds  : {mean_handle}")
    if mean_handle:
        print(f"   -> minutes/call   : {mean_handle / 60.0:.2f}")

    # raw store aggregates behind mean_handle
    try:
        from zira_dashboard import db
        rows = db.query(
            "SELECT COALESCE(SUM(on_call_ms),0) AS ms, COALESCE(SUM(calls),0) AS calls "
            "FROM forklift_driver_daily WHERE day >= (CURRENT_DATE - %s::int)",
            (forklift_advisor._CALIB_WINDOW_DAYS,),
        )
        print(f"   sum(on_call_ms)={rows[0]['ms']}  sum(calls)={rows[0]['calls']}")
        sample = db.query(
            "SELECT day, driver_id, calls, on_call_ms, avg_ms "
            "FROM forklift_driver_daily WHERE calls > 0 "
            "ORDER BY day DESC LIMIT 12")
        print("   recent driver-day rows (calls, on_call_ms, per-call min, avg_ms):")
        for r in sample:
            per_call = (r["on_call_ms"] / r["calls"] / 1000.0 / 60.0) if r["calls"] else 0
            print(f"     {r['day']} {str(r['driver_id'])[:8]:8} calls={r['calls']:>4} "
                  f"on_call_ms={r['on_call_ms']:>12} per-call={per_call:6.2f}min "
                  f"avg_ms={r['avg_ms']}")
    except Exception as e:
        print(f"   (raw store dump failed: {e})")

    if mean_handle and lam:
        mu = 3600.0 / mean_handle
        a = lam / mu
        print("\n=== offered load ===")
        print(f"mu (calls/hr/server) = {mu:.3f}")
        print(f"a  (Erlangs)         = {a:.3f}   "
              f"(queue needs c > a just to be STABLE)")

        calib = forklift_advisor._fit_calibration(mean_handle)
        print(f"\ncalibration k = {calib.k}  (n={calib.n_samples}, "
              f"uncalibrated={calib.uncalibrated})")

        print("\n=== per-crew predicted time-to-claim vs target ===")
        target = resolved.target_claim_seconds
        for c in range(1, forklift_queue.MAX_DRIVERS + 1):
            w = forklift_queue.erlang_c_wait_seconds(c, lam, mean_handle)
            cw = calib.k * w
            ok = cw <= target
            wtxt = "inf" if w == float("inf") else f"{cw/60.0:7.2f}min"
            print(f"  c={c:>2}: predicted={wtxt}  {'<= target OK' if ok else ''}")

    rec = forklift_advisor.build_advisor(today, scheduled=4, backups=0)
    print("\n=== build_advisor result ===")
    for key in ("available", "recommended", "overloaded", "predicted_claim_seconds",
                "target_seconds"):
        print(f"  {key}: {rec.get(key)}")


if __name__ == "__main__":
    main()
