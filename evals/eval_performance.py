"""How accurate is the latency we report, what does a check cost, and does scheduling hold up under load?

1. Measurement accuracy: server sleeps a known time; measured - injected = our error.
2. Check overhead on a healthy endpoint, with and without connection reuse (keep-alive).
3. Cost: CPU time per check (there are no paid API calls).
4. Scheduler under stress: 40 endpoints every 0.5 s for 20 s, 15 of them hanging
   or dripping forever. Are healthy checks still on time?
"""
from __future__ import annotations

import resource
import time

from common import percentile, save

from api_health_monitor.chaos_server import ChaosServer
from api_health_monitor.checker import check, make_session
from api_health_monitor.config import AlertConfig, Config, Endpoint
from api_health_monitor.monitor import Monitor
from api_health_monitor.storage import Store

DELAYS_MS = [0, 50, 100, 250, 500]
REPS = 30


def cpu_s() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime


def main() -> dict:
    out: dict = {}
    with ChaosServer() as srv:
        # 1. accuracy
        errs, by_delay = [], {}
        for d in DELAYS_MS:
            ep = Endpoint(name="d", url=f"{srv.base}/delay/{d}", latency_slo_ms=10_000)
            s = make_session(ep, use_env_proxy=False)
            e = [check(ep, s).latency_ms - d for _ in range(REPS)]
            s.close()
            errs += e
            by_delay[d] = round(percentile(e, 50), 2)
        out["latency_error_ms"] = {"p50": round(percentile(errs, 50), 2), "p95": round(percentile(errs, 95), 2),
                                   "max": round(max(errs), 2), "p50_by_injected_delay": by_delay}

        # 2 + 3. overhead and CPU cost
        ep = Endpoint(name="ok", url=f"{srv.base}/ok")
        s = make_session(ep, use_env_proxy=False)
        check(ep, s)                                  # warm up
        c0 = cpu_s()
        warm = [check(ep, s).latency_ms for _ in range(300)]
        cpu_per_check_ms = (cpu_s() - c0) / 300 * 1000
        s.close()
        cold = []
        for _ in range(100):
            s = make_session(ep, use_env_proxy=False)
            cold.append(check(ep, s).latency_ms)
            s.close()
        out["check_latency_ms"] = {
            "keepalive_p50": round(percentile(warm, 50), 2), "keepalive_p95": round(percentile(warm, 95), 2),
            "new_connection_p50": round(percentile(cold, 50), 2), "new_connection_p95": round(percentile(cold, 95), 2)}
        out["cpu_ms_per_check"] = round(cpu_per_check_ms, 3)
        out["cpu_hours_per_million_checks"] = round(cpu_per_check_ms * 1e6 / 1000 / 3600, 3)

        # 4. scheduler under stress
        common = dict(interval_s=0.5, connect_timeout_s=0.5, read_timeout_s=1.0, deadline_s=1.5)
        eps = [Endpoint(name=f"ok{i}", url=f"{srv.base}/ok", **common) for i in range(25)]
        eps += [Endpoint(name=f"hang{i}", url=f"{srv.base}/hang", **common) for i in range(8)]
        eps += [Endpoint(name=f"drip{i}", url=f"{srv.base}/header-drip", **common) for i in range(7)]
        cfg = Config(endpoints=tuple(eps), alerts=AlertConfig(console=False), db_path=":memory:",
                     use_env_proxy=False)
        mon = Monitor(cfg, store=Store(), alerters=[], seed=3)
        dur = 20.0
        t0 = time.perf_counter()
        summary = mon.run(duration_s=dur)
        wall = time.perf_counter() - t0
        ok_checks = sum(r.endpoint.startswith("ok") for r in mon.results)
        expected_ok = 25 * dur / 0.5
        lat = summary.stats.lateness_ms
        out["scheduler_stress"] = {
            "endpoints": len(eps), "stuck_endpoints": 15, "duration_s": dur, "wall_s": round(wall, 2),
            "healthy_checks_done": ok_checks, "healthy_checks_expected": int(expected_ok),
            "healthy_on_schedule_pct": round(100 * ok_checks / expected_ok, 1),
            "lateness_p50_ms": round(percentile(lat, 50), 2), "lateness_p95_ms": round(percentile(lat, 95), 2),
            "watchdog_fired": summary.stats.watchdog_fired,
            "header_drip_reported_down": all(any(r.endpoint == f"drip{i}" and not r.is_up for r in mon.results)
                                             for i in range(7)),
            "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        }
    print(out)
    save("performance", out)
    return out


if __name__ == "__main__":
    main()
