"""The core alerting trade-off: how many failures in a row before we page someone?

Uses the real IncidentTracker on simulated check results (no network), so we
can replay 30 days of checks in seconds:

* A healthy-but-imperfect API: each check fails independently with probability
  p (packet loss, a pod restart, a GC pause). Any alert here is a FALSE alarm.
* A real outage: every check fails for D minutes. We measure whether it's caught
  and how long after it started (time to detect).

Interval 30 s -> 2,880 checks per endpoint per day.
"""
from __future__ import annotations

import random

from common import percentile, save

from api_health_monitor.checker import CheckResult, Reason, Status
from api_health_monitor.config import Endpoint
from api_health_monitor.incidents import EventKind, IncidentTracker

INTERVAL = 30.0
DAYS = 30
CHECKS = int(DAYS * 86400 / INTERVAL)
FLAKE_RATES = [0.005, 0.02, 0.05]
THRESHOLDS = [1, 2, 3, 4, 5]
OUTAGE_MINUTES = [1, 2, 5]
OUTAGE_TRIALS = 400


def run(ep: Endpoint, fails: list[bool]) -> list:
    tr = IncidentTracker([ep])
    ev = []
    for i, f in enumerate(fails):
        st, rs = (Status.DOWN, Reason.HTTP_STATUS) if f else (Status.UP, Reason.OK)
        ev += tr.observe(CheckResult(ep.name, i * INTERVAL, st, rs))
    return ev


def main() -> dict:
    rng = random.Random(2026)
    rows = []
    for n in THRESHOLDS:
        ep = Endpoint(name="sim", url="http://sim/", interval_s=INTERVAL,
                      failures_to_alert=n, successes_to_recover=2)
        row = {"failures_to_alert": n}
        for p in FLAKE_RATES:
            fails = [rng.random() < p for _ in range(CHECKS)]
            downs = sum(e.kind is EventKind.DOWN for e in run(ep, fails))
            row[f"false_alerts_per_day_p{p}"] = round(downs / DAYS, 3)
        for minutes in OUTAGE_MINUTES:
            ttd, caught = [], 0
            for _ in range(OUTAGE_TRIALS):
                # outage starts at a random moment between two checks
                start = 3600 + rng.uniform(0, INTERVAL)
                end = start + minutes * 60
                fails = [(start <= i * INTERVAL < end) or (rng.random() < 0.005) for i in range(240)]
                downs = [e for e in run(ep, fails) if e.kind is EventKind.DOWN and e.at >= start - 1]
                if downs and downs[0].at < end + INTERVAL:
                    caught += 1
                    ttd.append(downs[0].at - start)
            row[f"caught_{minutes}min_pct"] = round(100 * caught / OUTAGE_TRIALS, 1)
            row[f"ttd_p50_s_{minutes}min"] = round(percentile(ttd, 50), 1) if ttd else None
            row[f"ttd_p95_s_{minutes}min"] = round(percentile(ttd, 95), 1) if ttd else None
        rows.append(row)
        print(row)
    out = {"interval_s": INTERVAL, "days_simulated": DAYS, "outage_trials": OUTAGE_TRIALS, "rows": rows}
    save("thresholds", out)
    return out


if __name__ == "__main__":
    main()
