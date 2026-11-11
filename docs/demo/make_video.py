"""Rebuild docs/demo/api-health-monitor-demo.mp4 (~3 min, 1280x720) from real outputs.

Slides are drawn with Pillow from examples/demo-output.txt and evals/results/*.json,
so the video can never show numbers the code didn't produce. Narration: docs/DEMO.md.

    python docs/demo/make_video.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "demo" / "api-health-monitor-demo.mp4"
FRAMES = ROOT / "docs" / "demo" / "frames"
W, H = 1280, 720
FONTS = Path("/usr/share/fonts/truetype/dejavu")
SANS = ImageFont.truetype(str(FONTS / "DejaVuSans.ttf"), 30)
SANS_B = ImageFont.truetype(str(FONTS / "DejaVuSans-Bold.ttf"), 50)
SMALL = ImageFont.truetype(str(FONTS / "DejaVuSans.ttf"), 24)
MONO = ImageFont.truetype(str(FONTS / "DejaVuSansMono.ttf"), 17)
BG, FG, DIM, ACC, RED, GRN, YEL = "#0f1115", "#e8e8ea", "#9a9aa3", "#5ac8fa", "#ff6961", "#30d158", "#ffd60a"
EMOJI = {"🔴": "●", "✅": "●", "🟡": "●"}


def load(name):
    return json.loads((ROOT / "evals" / "results" / f"{name}.json").read_text())


def title_slide(title, lines, footer=""):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((80, 90), title, font=SANS_B, fill=ACC)
    y = 200
    for ln in lines:
        d.text((80, y), ln, font=SANS, fill=FG)
        y += 52
    if footer:
        d.text((80, H - 70), footer, font=SMALL, fill=DIM)
    return im


def term_slide(title, lines, shown):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((40, 24), title, font=SMALL, fill=ACC)
    d.rounded_rectangle((30, 70, W - 30, H - 30), 12, fill="#16181d", outline="#2a2d35")
    y = 90
    for ln in lines[:shown]:
        for e, word in EMOJI.items():
            ln = ln.replace(e, word)
        col = (RED if "● DOWN" in ln else GRN if ("RECOVERED" in ln or "FAST AGAIN" in ln)
               else YEL if "● SLOW" in ln else FG)
        d.text((50, y), ln[:112] + ("…" if len(ln) > 112 else ""), font=MONO, fill=col)
        y += 24
    return im


def main() -> None:
    c, det, t, p, a = (load(n) for n in ("classification", "detection", "thresholds", "performance", "attacks"))
    raw = (ROOT / "examples" / "demo-output.txt").read_text().splitlines()
    demo = [re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:PORT", ln) for ln in raw]
    demo = [re.sub(r" \| http://\S+$", "", ln) for ln in demo]

    slides: list[tuple[Image.Image, float]] = []
    slides.append((title_slide("API Health Monitor", [
        "Checks endpoints. Measures latency. Detects failures.",
        "Sends alerts. Records uptime.",
        "", "...and survives the APIs it watches."], "github.com/paulmadojutimi94/api-health-monitor"), 12))
    slides.append((title_slide("What does 'down' look like?", [
        "Refused: answer in 1 ms.        Timeout: silence.",
        "HTTP 503: an answer, the wrong one.",
        "200 OK + maintenance page: the silent outage.",
        "Bytes trickling forever: no timeout ever fires."]), 16))
    slides.append((title_slide("How it works", [
        "Scheduler: heap of due times, fixed rate, jitter",
        "Checker: requests, stream=True, (connect, read) timeout,",
        "              total deadline, 1 MB body cap",
        "State machine: 3 failures -> DOWN, 2 passes -> RECOVERED",
        "Alerts on their own thread  |  SQLite for uptime"]), 18))
    steps = 14
    for k in range(1, steps + 1):
        shown = max(3, int(len(demo) * k / steps))
        slides.append((term_slide("$ make demo   (planted outages on a local chaos server)", demo, shown), 40 / steps))
    slides.append((title_slide("Does it name the cause?", [
        f"{c['correct']}/{c['total']} checks classified correctly ({c['accuracy_pct']}%)",
        f"{len(c['cases'])} failure types: refused, DNS, connect timeout, hang,",
        "slowloris, 503, maintenance page, gzip bomb, redirect loop...",
        f"Slowest check ever: {c['max_check_ms']:.0f} ms (deadline 1,200 ms)"]), 16))
    r3 = next(r for r in t["rows"] if r["failures_to_alert"] == 3)
    r1 = next(r for r in t["rows"] if r["failures_to_alert"] == 1)
    slides.append((title_slide("Catching outages without crying wolf", [
        f"Planted outages caught: {det['caught']}/{det['planted_outages']}",
        f"One-check blips that paged anyone: {det['blip_alerts']}/{det['blips']}",
        f"Alert on 1 failure: {r1['false_alerts_per_day_p0.02']:.0f} false alarms/day (2% flaky API)",
        f"Alert on 3 in a row: {r3['false_alerts_per_day_p0.02']:.0f}/day, detect p50 {r3['ttd_p50_s_5min']:.0f} s"]), 18))
    ps = p["scheduler_stress"]
    slides.append((title_slide("Latency and cost", [
        f"Latency error: p50 {p['latency_error_ms']['p50']} ms, p95 {p['latency_error_ms']['p95']} ms",
        f"Check overhead p50/p95: {p['check_latency_ms']['keepalive_p50']} / {p['check_latency_ms']['keepalive_p95']} ms",
        f"{ps['endpoints']} endpoints, {ps['stuck_endpoints']} hanging: {ps['healthy_on_schedule_pct']}% on time",
        f"Cost: EUR 0 API fees, {p['cpu_ms_per_check']} ms CPU per check"]), 18))
    rl = next(x for x in a["ablation"] if x["defence"] == "redirect_limit")["detail"][0]
    slides.append((title_slide("Bonus: timeouts without crashing", [
        f"Attacks that succeed, all defences on: {a['baseline_successes']}/{a['baseline_total']}",
        "Turn the deadline off: slowloris hangs the check forever",
        "Turn timeouts off: a silent server hangs it forever",
        f"Turn the redirect limit off: {rl['server_requests']:,} requests for ONE check"]), 18))
    slides.append((title_slide("Postmortem: the deadline that never fired", [
        "iter_content(16384) waits for a FULL 16 KB chunk.",
        "At 5 bytes/s that's ~55 minutes, so the deadline",
        "check inside the loop never ran.",
        "Fix: raw.read1(): return whatever has arrived.  1.6 s now."]), 18))
    slides.append((title_slide("Try it", [
        "git clone github.com/paulmadojutimi94/api-health-monitor",
        "pip install requests",
        "make demo",
        "", "README  ·  DESIGN  ·  POSTMORTEM  ·  CHEATSHEET  ·  LEARN"]), 12))

    if FRAMES.exists():
        shutil.rmtree(FRAMES)
    FRAMES.mkdir(parents=True)
    concat = []
    for n, (im, dur) in enumerate(slides):
        f = FRAMES / f"{n:03d}.png"
        im.save(f)
        concat.append(f"file '{f.name}'\nduration {dur:.3f}")
    concat.append(f"file '{f.name}'")   # ffmpeg concat needs the last frame repeated
    (FRAMES / "list.txt").write_text("\n".join(concat) + "\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
                    "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    str(OUT)], cwd=FRAMES, check=True)
    total = sum(d for _, d in slides)
    print(f"wrote {OUT.relative_to(ROOT)} ({total:.0f} s, {OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
