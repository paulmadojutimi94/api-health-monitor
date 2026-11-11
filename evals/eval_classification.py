"""Does a single check name the failure correctly? 15 fault types x 20 trials, each with a known answer."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from common import save

from api_health_monitor.chaos_server import Blackhole, ChaosServer, closed_port_url
from api_health_monitor.checker import Reason, check, make_session
from api_health_monitor.config import Endpoint

TRIALS = 20
TIMEOUTS = dict(connect_timeout_s=0.5, read_timeout_s=0.8, deadline_s=1.2, latency_slo_ms=300)


def cases(base: str, blackhole: str, refused: str):
    return [
        ("healthy 200", base + "/ok", {}, Reason.OK),
        ("healthy, chunked", base + "/chunked", {"expect_body": "healthy"}, Reason.OK),
        ("HTTP 503", base + "/status/503", {}, Reason.HTTP_STATUS),
        ("HTTP 404", base + "/status/404", {}, Reason.HTTP_STATUS),
        ("200 but maintenance page", base + "/maintenance", {"expect_body": "healthy"}, Reason.BODY_MISMATCH),
        ("slow (450 ms vs 300 ms SLO)", base + "/delay/450", {}, Reason.SLOW),
        ("server accepts, never answers", base + "/hang", {}, Reason.READ_TIMEOUT),
        ("slowloris body drip", base + "/drip", {}, Reason.DEADLINE_EXCEEDED),
        ("50 MB body", base + "/big", {}, Reason.BODY_TOO_LARGE),
        ("gzip bomb (100 MB unpacked)", base + "/gzip-bomb", {}, Reason.BODY_TOO_LARGE),
        ("redirect loop", base + "/redirect-loop", {}, Reason.TOO_MANY_REDIRECTS),
        ("nothing listening (refused)", refused, {}, Reason.CONNECTION_REFUSED),
        ("host unreachable (SYN dropped)", blackhole, {}, Reason.CONNECT_TIMEOUT),
        ("DNS name doesn't exist", "http://no-such-host.invalid/", {}, Reason.DNS_FAILURE),
        ("hostile reason phrase", base + "/inject", {}, Reason.HTTP_STATUS),
    ]


def main() -> dict:
    rows, confusion = [], Counter()
    with ChaosServer(seed=1) as srv, Blackhole() as bh:
        refused = closed_port_url()

        def one(spec):
            label, url, kw, expected = spec
            ep = Endpoint(name="c", url=url, **{**TIMEOUTS, **kw})
            s = make_session(ep, use_env_proxy=False)
            try:
                return label, expected, check(ep, s)
            finally:
                s.close()

        work = [c for c in cases(srv.base, bh.url, refused) for _ in range(TRIALS)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(one, work))

    by_label: dict[str, list] = {}
    for label, expected, r in results:
        by_label.setdefault(label, []).append((expected, r))
        confusion[(expected.value, r.reason.value)] += 1
    correct_total = 0
    for label, items in by_label.items():
        expected = items[0][0]
        correct = sum(r.reason is expected for _, r in items)
        correct_total += correct
        worst = max(r.latency_ms or 0 for _, r in items)
        rows.append({"case": label, "expected": expected.value, "correct": correct, "trials": len(items),
                     "max_check_ms": round(worst, 1)})
        print(f"{label:34} {correct:>2}/{len(items)}  expected={expected.value:<18} max {worst:7.1f} ms")
    total = len(results)
    out = {"trials_per_case": TRIALS, "cases": rows, "correct": correct_total, "total": total,
           "accuracy_pct": round(100 * correct_total / total, 2),
           "up_down_accuracy_pct": round(100 * sum((r.reason in (Reason.OK, Reason.SLOW)) ==
                                                   (e in (Reason.OK, Reason.SLOW)) for _, e, r in results) / total, 2),
           "max_check_ms": max(r["max_check_ms"] for r in rows),
           "confusion": {f"{a}->{b}": n for (a, b), n in sorted(confusion.items()) if a != b}}
    print(f"accuracy {out['accuracy_pct']}% ({correct_total}/{total}); slowest check {out['max_check_ms']} ms")
    save("classification", out)
    return out


if __name__ == "__main__":
    main()
