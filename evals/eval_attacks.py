"""Attack success rate: can a hostile (or just badly broken) endpoint hurt the monitor?

The monitor's job is to talk to servers that are failing. Some fail in ways
that attack the client: never finishing, sending gigabytes, looping, or
putting commands into text we'll show humans. Each attack runs in its own
process with a hard 25 s limit. A run that hits the limit counts as a success
for the attacker (the monitor was held hostage).

Then the ablation: switch off one defence at a time and re-run the attacks it
is meant to stop. If they don't start succeeding, the defence isn't doing
anything and the 0% headline means nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from common import save

WORKER = Path(__file__).with_name("attack_worker.py")
HARD_LIMIT_S = 25

ATTACKS = {
    "slowloris_body": "Body sent 1 byte every 0.2 s, forever",
    "hang": "Connection accepted, no reply ever",
    "header_drip_scheduler": "Headers sent 1 byte every 0.2 s, forever",
    "big_body": "50 MB response body",
    "gzip_bomb": "100 KB on the wire, 100 MB decompressed",
    "redirect_loop": "302 to itself, forever",
    "slack_mention": "Reason phrase with <!channel> and <@user>",
    "discord_mention": "Reason phrase with @everyone",
    "terminal_escape": "Reason phrase with ESC[2J (clear screen)",
}

DEFENCES = {
    "total_deadline": ("Total deadline while reading the body", ["slowloris_body"]),
    "socket_timeouts": ("Connect/read socket timeouts", ["hang"]),
    "watchdog": ("Scheduler watchdog", ["header_drip_scheduler"]),
    "body_cap": ("Body size cap (1 MB)", ["big_body", "gzip_bomb"]),
    "redirect_limit": ("Redirect limit (5)", ["redirect_loop"]),
    "sanitize_text": ("Sanitising server text before alerting",
                      ["slack_mention", "discord_mention", "terminal_escape"]),
}


def run(attack: str, off: str | None) -> dict:
    try:
        p = subprocess.run([sys.executable, str(WORKER), attack, off or "none"],
                           capture_output=True, text=True, timeout=HARD_LIMIT_S)
        return json.loads(p.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        return {"attack": attack, "disabled": off, "success": True, "note": f"hung > {HARD_LIMIT_S}s"}
    except (json.JSONDecodeError, IndexError):
        return {"attack": attack, "disabled": off, "success": True, "note": "worker crashed"}


def main() -> dict:
    baseline = []
    for a in ATTACKS:
        v = run(a, None)
        baseline.append(v)
        print(f"all defences on   {a:24} success={v['success']}  {v}")
    ablation = []
    for d, (label, attacks) in DEFENCES.items():
        vs = [run(a, d) for a in attacks]
        ablation.append({"defence": d, "label": label, "attacks": len(vs),
                         "succeeded": sum(v["success"] for v in vs), "detail": vs})
        print(f"{d:16} off -> {sum(v['success'] for v in vs)}/{len(vs)} attacks succeed")
    out = {"attacks": {a: d for a, d in ATTACKS.items()}, "baseline": baseline,
           "baseline_successes": sum(v["success"] for v in baseline), "baseline_total": len(baseline),
           "ablation": ablation}
    save("attacks", out)
    return out


if __name__ == "__main__":
    main()
