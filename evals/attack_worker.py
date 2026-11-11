"""Runs ONE attack in a fresh process and prints a JSON verdict. Called by eval_attacks.py.

A fresh process per attack means a hang or memory blow-up can't contaminate
the next one, and the parent can enforce a hard time limit.

usage: python attack_worker.py <attack> [defence_to_disable]
"""
from __future__ import annotations

import io
import json
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api_health_monitor.alerts import ConsoleAlerter, webhook_payload  # noqa: E402
from api_health_monitor.chaos_server import ChaosServer  # noqa: E402
from api_health_monitor.checker import check, make_session  # noqa: E402
from api_health_monitor.config import Endpoint, with_defences  # noqa: E402
from api_health_monitor.incidents import Event, EventKind  # noqa: E402
from api_health_monitor.scheduler import Scheduler  # noqa: E402

DEADLINE = 1.5
CAP = 1_000_000


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def endpoint(srv: ChaosServer, path: str, off: str | None) -> Endpoint:
    ep = Endpoint(name="victim", url=srv.base + path, connect_timeout_s=0.5, read_timeout_s=1.0,
                  deadline_s=DEADLINE, max_body_bytes=CAP, interval_s=0.5)
    return with_defences(ep, **{off: False}) if off else ep


def one_check(srv, path, off):
    ep = endpoint(srv, path, off)
    s = make_session(ep, use_env_proxy=False)
    base = rss_mb()
    before = srv.requests
    t = time.perf_counter()
    r = check(ep, s)
    return ep, r, time.perf_counter() - t, rss_mb() - base, srv.requests - before


def main() -> None:
    attack = sys.argv[1]
    off = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "none" else None
    v: dict = {"attack": attack, "disabled": off}
    with ChaosServer() as srv:
        if attack == "gzip_bomb":   # build the bomb before measuring, so server memory isn't counted
            import gzip
            srv._gzip_bomb = gzip.compress(b"\0" * (100 * 1024 * 1024), compresslevel=9)
        if attack in ("slowloris_body", "hang", "big_body", "gzip_bomb", "redirect_loop"):
            path = {"slowloris_body": "/drip", "hang": "/hang", "big_body": "/big",
                    "gzip_bomb": "/gzip-bomb", "redirect_loop": "/redirect-loop"}[attack]
            ep, r, took, grew, reqs = one_check(srv, path, off)
            v.update(seconds=round(took, 2), rss_growth_mb=round(grew, 1), bytes=r.bytes_read,
                     server_requests=reqs, reason=r.reason.value)
            if attack in ("slowloris_body", "hang"):
                v["success"] = took > 2 * DEADLINE
            elif attack in ("big_body", "gzip_bomb"):
                v["success"] = r.bytes_read > 2 * CAP or grew > 50
            else:
                v["success"] = reqs > 20
        elif attack == "header_drip_scheduler":
            # Headers trickle in forever: socket timeouts never fire, the body-loop deadline is
            # never reached. Only the scheduler watchdog can turn this into a DOWN result.
            ep = endpoint(srv, "/header-drip", off)
            seen = []
            sched = Scheduler([ep], lambda e: check(e, make_session(e, use_env_proxy=False)), seen.append,
                              jitter=0)
            sched.run(duration_s=DEADLINE + 3.0)
            v.update(results=len(seen), reasons=sorted({r.reason.value for r in seen}))
            v["success"] = not any(not r.is_up for r in seen)
        elif attack in ("slack_mention", "discord_mention", "terminal_escape"):
            ep, r, *_ = one_check(srv, "/inject", off)
            sanitize = ep.defences.sanitize_text
            ev = Event(EventKind.DOWN, ep.name, ep.url, time.time(), time.time(), r)
            if attack == "slack_mention":
                text = webhook_payload(ev, "slack", sanitize)["text"]
                v["success"] = "<!channel>" in text or "<@U0ADMIN>" in text
            elif attack == "discord_mention":
                p = webhook_payload(ev, "discord", sanitize)
                v["success"] = "@everyone" in p["content"] and not p.get("allowed_mentions")
            else:
                buf = io.StringIO()
                ConsoleAlerter(buf, sanitize).send(ev)
                v["success"] = "\x1b" in buf.getvalue()
        else:
            raise SystemExit(f"unknown attack {attack}")
    print(json.dumps(v))


if __name__ == "__main__":
    main()
