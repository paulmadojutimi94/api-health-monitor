import threading
import time

from api_health_monitor.checker import CheckResult, Reason, Status
from api_health_monitor.cli import main
from api_health_monitor.config import Config, AlertConfig
from api_health_monitor.incidents import EventKind
from api_health_monitor.monitor import Monitor
from api_health_monitor.scheduler import Scheduler
from api_health_monitor.storage import Store


def ok_check(ep):
    return CheckResult(ep.name, time.time(), Status.UP, Reason.OK, latency_ms=1.0)


def test_fixed_rate_no_drift(fast_ep):
    ep = fast_ep("http://x/", interval_s=0.1)
    seen = []
    sched = Scheduler([ep], ok_check, seen.append, jitter=0, seed=1)
    sched.run(duration_s=1.05)
    assert 10 <= len(seen) <= 12
    assert sorted(sched.stats.lateness_ms)[len(sched.stats.lateness_ms) // 2] < 20


def test_slow_check_does_not_delay_others(fast_ep):
    fast = fast_ep("http://fast/", name="fast", interval_s=0.1)
    slow = fast_ep("http://slow/", name="slow", interval_s=0.1, deadline_s=5)

    def check(ep):
        if ep.name == "slow":
            time.sleep(0.8)
        return ok_check(ep)

    seen = []
    Scheduler([fast, slow], check, seen.append, jitter=0).run(duration_s=1.0)
    assert sum(r.endpoint == "fast" for r in seen) >= 9
    assert sum(r.endpoint == "slow" for r in seen) <= 2      # no pile-up


def test_watchdog_rescues_stuck_check(fast_ep):
    ep = fast_ep("http://stuck/", interval_s=0.2, connect_timeout_s=0.1, deadline_s=0.3)
    release = threading.Event()

    def stuck(ep):
        release.wait(10)
        return ok_check(ep)

    seen = []
    sched = Scheduler([ep], stuck, seen.append, jitter=0)
    t = time.perf_counter()
    sched.run(duration_s=2.0)
    release.set()
    assert time.perf_counter() - t < 2.5
    assert sched.stats.watchdog_fired == 1
    assert seen and all(r.reason is Reason.DEADLINE_EXCEEDED for r in seen)
    assert len(seen) >= 4     # keeps reporting while stuck, without starting new threads
    assert threading.active_count() < 10


def test_crashing_check_and_callback_are_contained(fast_ep):
    ep = fast_ep("http://x/", interval_s=0.1)

    def boom(ep):
        raise RuntimeError("bug in check")

    calls = []

    def bad_callback(r):
        calls.append(r)
        raise ValueError("db locked")

    sched = Scheduler([ep], boom, bad_callback, jitter=0)
    sched.run(duration_s=0.5)
    assert calls and calls[0].reason is Reason.UNEXPECTED_ERROR
    assert sched.stats.callback_errors == len(calls)


def test_stop_event_ends_run_quickly(fast_ep):
    ep = fast_ep("http://x/", interval_s=10)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    t = time.perf_counter()
    Scheduler([ep], ok_check, lambda r: None).run(stop)
    assert time.perf_counter() - t < 1.0


def test_monitor_end_to_end_detects_outage(chaos, fast_ep):
    chaos.set_timeline("e2e", [(chaos.now() + 0.5, chaos.now() + 2.0, "503")])
    ep = fast_ep(chaos.base + "/scenario/e2e", name="e2e", interval_s=0.1,
                 failures_to_alert=3, successes_to_recover=2)
    cfg = Config(endpoints=(ep,), alerts=AlertConfig(console=False), db_path=":memory:", use_env_proxy=False)
    mon = Monitor(cfg, store=Store(), alerters=[])
    summary = mon.run(duration_s=3.0)
    kinds = [e.kind for e in summary.events]
    assert kinds == [EventKind.DOWN, EventKind.RECOVERED]
    (row,) = mon.store.uptime()
    assert 30 < row.uptime_pct < 70
    assert row.incidents == 1


def test_cli_check_exit_codes(chaos, tmp_path):
    cfg = tmp_path / "m.toml"
    cfg.write_text(f'''
[network]
use_env_proxy = false
[[endpoint]]
name = "ok"
url = "{chaos.base}/ok"
[[endpoint]]
name = "bad"
url = "{chaos.base}/status/500"
''')
    assert main(["check", "-c", str(cfg)]) == 1
    cfg.write_text(cfg.read_text().split("[[endpoint]]\nname = \"bad\"")[0])
    assert main(["check", "-c", str(cfg)]) == 0
    assert main(["check", "-c", str(tmp_path / "missing.toml")]) == 2
