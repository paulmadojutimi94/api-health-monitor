"""End to end, in real time: plant outages on a live server and score the alerts.

8 endpoints, 2 rounds x 60 s, a check every 0.25 s, alert after 3 failures,
recover after 2 successes. Each scenario endpoint gets 2 random outages
(2-6 s, mode chosen from 503 / hang / maintenance page / slow) plus 3 one-check
blips that should NOT page anyone. Two more endpoints are healthy but flaky
(5% and 10% of checks fail): any alert there is a false alarm.

Scored against the planted timeline:
  recall      planted outages that produced the right alert
  precision   alerts that matched a planted outage
  TTD         alert time - outage start
  duration    |measured incident length - planted length|
"""
from __future__ import annotations

import random
import time

from common import percentile, save

from api_health_monitor.chaos_server import ChaosServer
from api_health_monitor.config import AlertConfig, Config, Endpoint
from api_health_monitor.incidents import EventKind
from api_health_monitor.monitor import Monitor
from api_health_monitor.storage import Store

INTERVAL = 0.25
ROUND_S = 60.0
ROUNDS = 2
N, M = 3, 2
MODES = ["503", "hang", "maintenance", "slow"]
SCENARIOS = 6


def plan(rng: random.Random):
    """Two outages per endpoint, non-overlapping, plus short blips."""
    tl, blips = [], []
    a = rng.uniform(4, 12)
    for _ in range(2):
        length = rng.uniform(2, 6)
        tl.append((a, a + length, rng.choice(MODES)))
        a = a + length + rng.uniform(8, 16)
    for _ in range(3):
        while True:
            b = rng.uniform(2, ROUND_S - 4)
            if all(not (s - 2 < b < e + 2) for s, e, _ in tl):
                break
        tl.append((b, b + INTERVAL * 0.8, "503"))     # shorter than one check gap
        blips.append(b)
    return sorted(tl), blips


def main() -> dict:
    rng = random.Random(11)
    planted = caught = 0
    alerts = matched = 0
    false_alarms_flaky = 0
    ttd, ttr, dur_err = [], [], []
    blip_alerts = blips_total = 0
    checks = 0
    for rnd in range(ROUNDS):
        with ChaosServer(seed=rnd, slow_ms=500) as srv:
            plans = {}
            eps = []
            common = dict(interval_s=INTERVAL, connect_timeout_s=0.4, read_timeout_s=0.6, deadline_s=0.9,
                          latency_slo_ms=300, failures_to_alert=N, successes_to_recover=M)
            for i in range(SCENARIOS):
                tl, blips = plan(rng)
                plans[f"s{i}"] = (tl, blips)
                srv.set_timeline(f"s{i}", tl)
                eps.append(Endpoint(name=f"s{i}", url=f"{srv.base}/scenario/s{i}", expect_body="healthy", **common))
            eps.append(Endpoint(name="flaky5", url=f"{srv.base}/flaky/5", **common))
            eps.append(Endpoint(name="flaky10", url=f"{srv.base}/flaky/10", **common))
            cfg = Config(endpoints=tuple(eps), alerts=AlertConfig(console=False), db_path=":memory:",
                         use_env_proxy=False)
            mon = Monitor(cfg, store=Store(), alerters=[], seed=rnd)
            wall0 = time.time() - srv.now()          # server t=0 in wall-clock seconds
            summary = mon.run(duration_s=ROUND_S)
            checks += summary.stats.completed + summary.stats.watchdog_fired

        events = summary.events
        for name, (tl, blips) in plans.items():
            outages = [(s, e, m) for s, e, m in tl if e - s > 1]
            evs = [e for e in events if e.endpoint == name]
            used = set()
            for s, e, mode in outages:
                if e > ROUND_S - 2:
                    continue
                planted += 1
                want = EventKind.DEGRADED if mode == "slow" else EventKind.DOWN
                done = EventKind.DEGRADED_RESOLVED if mode == "slow" else EventKind.RECOVERED
                hit = next((x for x in evs if x.kind is want and s - 0.3 <= x.at - wall0 <= e + 1.0), None)
                if hit:
                    caught += 1
                    used.add(id(hit))
                    ttd.append(hit.at - wall0 - s)
                    rec = next((x for x in evs if x.kind is done and x.at - wall0 > e - 0.3), None)
                    if rec:
                        ttr.append(rec.at - wall0 - e)
                        dur_err.append(abs((rec.at - rec.incident_started_at) - (e - s)))
            for b in blips:
                blips_total += 1
                if any(x.kind is EventKind.DOWN and b <= x.at - wall0 <= b + 1.5 for x in evs):
                    blip_alerts += 1
            opening = [x for x in evs if x.kind in (EventKind.DOWN, EventKind.DEGRADED)]
            alerts += len(opening)
            matched += sum(id(x) in used for x in opening)
        flaky = [e for e in events if e.endpoint.startswith("flaky") and e.kind is EventKind.DOWN]
        false_alarms_flaky += len(flaky)
        alerts += len(flaky)
        print(f"round {rnd}: planted so far {planted}, caught {caught}, alerts {alerts}, matched {matched}")

    flaky_checks = ROUNDS * 2 * ROUND_S / INTERVAL
    out = {
        "rounds": ROUNDS, "round_s": ROUND_S, "interval_s": INTERVAL, "failures_to_alert": N,
        "successes_to_recover": M, "checks": checks,
        "planted_outages": planted, "caught": caught, "recall_pct": round(100 * caught / planted, 1),
        "opening_alerts": alerts, "matched_alerts": matched,
        "precision_pct": round(100 * matched / alerts, 1) if alerts else None,
        "false_alarms_on_flaky": false_alarms_flaky, "flaky_checks": int(flaky_checks),
        "blips": blips_total, "blip_alerts": blip_alerts,
        "ttd_p50_s": round(percentile(ttd, 50), 2), "ttd_p95_s": round(percentile(ttd, 95), 2),
        "ttd_p50_intervals": round(percentile(ttd, 50) / INTERVAL, 1),
        "ttd_p95_intervals": round(percentile(ttd, 95) / INTERVAL, 1),
        "recovery_p50_s": round(percentile(ttr, 50), 2) if ttr else None,
        "recovery_p95_s": round(percentile(ttr, 95), 2) if ttr else None,
        "duration_error_p50_s": round(percentile(dur_err, 50), 2) if dur_err else None,
        "duration_error_p95_s": round(percentile(dur_err, 95), 2) if dur_err else None,
    }
    print(out)
    save("detection", out)
    return out


if __name__ == "__main__":
    main()
