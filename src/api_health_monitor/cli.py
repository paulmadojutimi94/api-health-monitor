"""Command line:  python -m api_health_monitor <command>

    run     monitor forever (Ctrl-C to stop) or for --duration seconds
    check   check every endpoint once, print results, exit 1 if any are down
    report  uptime table / Markdown / HTML from the database
    demo    start the chaos server and monitor it for 30 s: watch outages get caught
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from .checker import check, make_session
from .config import ConfigError, load_config, parse_config
from .monitor import Monitor
from .report import html_page, markdown, table
from .sanitize import clean_text
from .storage import Store

ICON = {"up": "UP  ", "degraded": "SLOW", "down": "DOWN"}


def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum, _frame):
        print(f"\nreceived {signal.Signals(signum).name}, finishing up...", file=sys.stderr)
        stop.set()
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)   # what `docker stop` / systemd send


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    stop = threading.Event()
    _install_signal_handlers(stop)
    mon = Monitor(cfg)
    names = ", ".join(e.name for e in cfg.endpoints)
    print(f"monitoring {len(cfg.endpoints)} endpoint(s): {names}  -> {cfg.db_path}")
    summary = mon.run(stop, args.duration)
    print(table(mon.store.uptime()))
    print(f"\n{summary.stats.completed} checks, {len(summary.events)} alert event(s), "
          f"{summary.stats.watchdog_fired} watchdog rescues")
    return 0


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    worst = 0
    for ep in cfg.endpoints:
        s = make_session(ep, cfg.use_env_proxy)
        r = check(ep, s)
        s.close()
        lat = "-" if r.latency_ms is None else f"{r.latency_ms:.0f} ms"
        print(f"{ICON[r.status.value]}  {ep.name:<20} {r.reason.value:<18} {str(r.http_status or '-'):<4} "
              f"{lat:>9}  {clean_text(r.detail, 80)}")
        worst = max(worst, 1 if r.status.value == "down" else 0)
    return worst


def cmd_report(args) -> int:
    store = Store(args.db)
    rows = store.uptime(window_s=args.window * 3600 if args.window else None)
    if not rows:
        print("no checks recorded yet", file=sys.stderr)
        return 1
    if args.format == "table":
        print(table(rows))
    else:
        text = markdown(rows) if args.format == "md" else html_page(rows, store)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            print(text)
    return 0


def demo_config(base: str, db: str, interval: float) -> dict:
    common = {"interval_s": interval, "connect_timeout_s": 0.5, "read_timeout_s": 1.0,
              "deadline_s": 1.5, "latency_slo_ms": 400, "failures_to_alert": 3,
              "successes_to_recover": 2}
    eps = [
        {"name": "payments-api", "url": f"{base}/scenario/payments"},
        {"name": "search-api", "url": f"{base}/scenario/search", "expect_body": "healthy"},
        {"name": "auth-api", "url": f"{base}/scenario/auth"},
        {"name": "flaky-api", "url": f"{base}/flaky/15"},
        {"name": "slowloris", "url": f"{base}/drip"},
    ]
    return {"defaults": common, "endpoint": eps, "storage": {"db_path": db},
            "network": {"use_env_proxy": False}, "alerts": {"console": True, "jsonl": "data/demo-alerts.jsonl"}}


def cmd_demo(args) -> int:
    from .chaos_server import ChaosServer

    with ChaosServer(seed=7) as srv:
        srv.set_timeline("payments", [(6, 14, "503")])          # hard outage
        srv.set_timeline("search", [(10, 18, "maintenance")])   # 200 OK but wrong body
        srv.set_timeline("auth", [(4, 12, "slow"), (18, 25, "hang")])
        db = args.db
        Path(db).unlink(missing_ok=True)
        cfg = parse_config(demo_config(srv.base, db, args.interval))
        stop = threading.Event()
        _install_signal_handlers(stop)
        print(f"chaos server on {srv.base}. Planted: payments 503 @6-14s, search maintenance @10-18s,"
              f" auth slow @4-12s then hang @18-25s, flaky-api fails 15%, slowloris drips forever.\n")
        t0 = time.time()
        mon = Monitor(cfg, seed=1)
        summary = mon.run(stop, args.duration)
        print(f"\n--- after {time.time() - t0:.0f}s ---")
        print(table(mon.store.uptime()))
        print(f"\n{summary.stats.completed} checks, {len(summary.events)} alert event(s), "
              f"{summary.stats.watchdog_fired} watchdog rescues, 0 crashes")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="api-health-monitor", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="monitor continuously")
    p.add_argument("-c", "--config", default="monitor.toml")
    p.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("check", help="check every endpoint once")
    p.add_argument("-c", "--config", default="monitor.toml")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("report", help="uptime report from the database")
    p.add_argument("--db", default="data/monitor.db")
    p.add_argument("--format", choices=("table", "md", "html"), default="table")
    p.add_argument("--window", type=float, default=None, help="last N hours only")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("demo", help="watch the monitor catch planted outages")
    p.add_argument("--duration", type=float, default=30)
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--db", default="data/demo.db")
    p.set_defaults(fn=cmd_demo)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
