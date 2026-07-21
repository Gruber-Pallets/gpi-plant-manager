"""Read-only diagnostic: dump the inputs feeding the capacity-coverage forklift
recommendation. Run: railway run python -m scripts.diagnose_forklift_overload"""
from __future__ import annotations

from datetime import date

from zira_dashboard import (
    forklift_advisor, forklift_demand, forklift_settings,
)


def main() -> None:
    today = date.today()
    cfg = forklift_advisor._cfg()
    thr = forklift_advisor._algo_throughput()
    resolved = forklift_settings.resolve(cfg, algo_throughput=thr)
    forecast = forklift_advisor._forecast(
        today, resolved.history_samples, cfg.coldstart_calls_per_day)
    hour, lam = forklift_demand.demand_at_percentile(forecast.by_hour, resolved.percentile)

    print("throughput=%.2f  utilization=%.2f  effective=%.2f  percentile=%.2f"
          % (resolved.throughput, resolved.utilization,
             resolved.effective_throughput, resolved.percentile))
    print("forecast basis=%s n=%d peak=%s by_hour=%s"
          % (forecast.basis, forecast.n_days, forecast.peak_calls, forecast.by_hour))
    print("planned hour=%s lambda=%.1f" % (hour, lam or 0))
    if forecast.by_hour and (lam or 0) > 0:
        print("recommend_drivers = %d"
              % forklift_demand.recommend_drivers(lam, resolved.effective_throughput))
    print("observed_claim_seconds = %s" % forklift_advisor._observed_claim_or_none())

    rec = forklift_advisor.build_advisor(today, scheduled=4, backups=0)
    for k in ("available", "recommended", "observed_claim_seconds", "coverage"):
        print("  %s: %s" % (k, rec.get(k)))


if __name__ == "__main__":
    main()
