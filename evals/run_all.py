"""Run every eval, then rewrite the results tables in README.md between the markers.

    python evals/run_all.py            # run all evals (~5 min) and update README
    python evals/run_all.py --readme   # only rebuild README tables from evals/results/
    python evals/run_all.py --check    # exit 1 if README doesn't match evals/results/ (CI)
"""
from __future__ import annotations

import json
import sys

from common import RESULTS, ROOT

START, END = "<!-- results:start -->", "<!-- results:end -->"


def load(name: str) -> dict:
    return json.loads((RESULTS / f"{name}.json").read_text())


def tables() -> str:
    c, d, t, p, a = (load(n) for n in ("classification", "detection", "thresholds", "performance", "attacks"))
    ps = p["scheduler_stress"]
    lines = [
        "| What | Result | How it was measured |",
        "|---|---|---|",
        f"| **Failure classification** (single check names the right cause) | **{c['accuracy_pct']}%** "
        f"({c['correct']}/{c['total']}) | {len(c['cases'])} failure types x {c['trials_per_case']} trials "
        "against a local server that fails on purpose |",
        f"| Up/down verdict correct | **{c['up_down_accuracy_pct']}%** | Same {c['total']} checks |",
        f"| **Outages detected** (recall) | **{d['caught']} / {d['planted_outages']}** ({d['recall_pct']}%) "
        f"| {d['rounds']} x {d['round_s']:.0f} s live runs, 6 endpoints with random planted outages, "
        f"{d['checks']:,} real checks |",
        f"| Alerts that were real (precision) | **{d['precision_pct']}%** ({d['matched_alerts']}/{d['opening_alerts']})"
        f" | The one false alarm came from an endpoint failing 10% of checks at random |",
        f"| One-check blips that paged someone | **{d['blip_alerts']} / {d['blips']}** | "
        f"Failures shorter than one check interval, which should never page |",
        f"| **Time to detect** | **p50 {d['ttd_p50_intervals']}, p95 {d['ttd_p95_intervals']} check intervals** "
        f"| Alert time minus planted start; rule is {d['failures_to_alert']} failures in a row |",
        f"| Incident length error | p50 {d['duration_error_p50_s']} s, p95 {d['duration_error_p95_s']} s | "
        "Reported duration vs planted duration |",
        f"| **Latency measurement error** | **p50 {p['latency_error_ms']['p50']} ms, "
        f"p95 {p['latency_error_ms']['p95']} ms** | Server sleeps 0-500 ms; measured minus injected, 150 checks |",
        f"| **Check latency (p50/p95)**, healthy endpoint | **{p['check_latency_ms']['keepalive_p50']} / "
        f"{p['check_latency_ms']['keepalive_p95']} ms** reused connection; "
        f"{p['check_latency_ms']['new_connection_p50']} / {p['check_latency_ms']['new_connection_p95']} ms new "
        "| Localhost, so this is the monitor's own overhead |",
        f"| Slowest any check ever took | **{c['max_check_ms']:.0f} ms** with a 1,200 ms deadline | "
        "Includes hangs, slowloris, 50 MB bodies, gzip bombs |",
        f"| Scheduling under stress | **{ps['healthy_on_schedule_pct']}%** of healthy checks on time, "
        f"lateness p95 {ps['lateness_p95_ms']} ms | {ps['endpoints']} endpoints every 0.5 s, "
        f"{ps['stuck_endpoints']} of them hanging forever, peak memory {ps['peak_rss_mb']} MB |",
        f"| **Cost per check** | **€0.00** in API fees; {p['cpu_ms_per_check']} ms CPU | "
        f"{p['cpu_hours_per_million_checks']} CPU-hours per million checks |",
        f"| **Attack success rate**, all defences on | **{a['baseline_successes']} / {a['baseline_total']}** "
        f"({100 * a['baseline_successes'] / a['baseline_total']:.0f}%) | 9 hostile-endpoint attacks, "
        "each in its own process with a 25 s kill switch |",
        "",
        "| Defence switched off | Attacks that then succeed | What happened |",
        "|---|---|---|",
    ]
    for ab in a["ablation"]:
        notes = []
        for v in ab["detail"]:
            if v.get("note"):
                notes.append(f"{v['attack']}: {v['note']}")
            elif "server_requests" in v and v["server_requests"] > 20:
                notes.append(f"{v['server_requests']:,} requests, +{v['rss_growth_mb']:.0f} MB memory")
            elif v.get("bytes", 0) > 2_000_000:
                notes.append(f"{v['attack']}: read {v['bytes'] / 1e6:.0f} MB")
            elif "reasons" in v and v["success"]:
                notes.append("stuck check never reported DOWN")
            elif v["attack"] == "discord_mention" and not v["success"]:
                notes.append("@everyone still blocked by allowed_mentions (2nd layer)")
            elif v["success"]:
                notes.append(f"{v['attack']} got through")
        lines.append(f"| {ab['label']} | {ab['succeeded']} / {ab['attacks']} | {'; '.join(notes)} |")

    lines += ["", "**Alert threshold trade-off** (simulated: 30 days of 30-second checks per row, "
              f"{t['outage_trials']} planted outages per length, using the real state machine):", "",
              "| Alert after N failures | False alarms/day at 0.5% / 2% / 5% random failure | "
              "1-min outage caught | 5-min outage caught | Time to detect, p50 / p95 |",
              "|---|---|---|---|---|"]
    for r in t["rows"]:
        fa = " / ".join(f"{r[f'false_alerts_per_day_p{x}']:.1f}" for x in (0.005, 0.02, 0.05))
        ttd = f"{r['ttd_p50_s_5min']:.0f} s / {r['ttd_p95_s_5min']:.0f} s"
        mark = " **(default)**" if r["failures_to_alert"] == 3 else ""
        lines.append(f"| {r['failures_to_alert']}{mark} | {fa} | {r['caught_1min_pct']}% | "
                     f"{r['caught_5min_pct']}% | {ttd} |")
    lines += ["", f"Measured on {p['machine']}."]
    return "\n".join(lines)


def render_readme() -> tuple[str, str]:
    readme = (ROOT / "README.md").read_text()
    head, rest = readme.split(START, 1)
    _, tail = rest.split(END, 1)
    return readme, head + START + "\n" + tables() + "\n" + END + tail


def main() -> int:
    if "--check" in sys.argv:
        old, new = render_readme()
        if old != new:
            print("README results are stale: run python evals/run_all.py --readme")
            return 1
        print("README results match evals/results/")
        return 0
    if "--readme" not in sys.argv:
        import subprocess
        # Each eval in its own process, so one eval's memory peak can't leak into another's numbers.
        for name in ("eval_classification", "eval_thresholds", "eval_detection", "eval_performance",
                     "eval_attacks"):
            print(f"\n=== {name} ===", flush=True)
            subprocess.run([sys.executable, str(ROOT / "evals" / f"{name}.py")], check=True)
    _, new = render_readme()
    (ROOT / "README.md").write_text(new)
    print("README.md results updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
