# CLAUDE.md: guide for agents working in this repo

## What this is
An API health monitor: scheduled HTTP checks → classified results → incident state machine → alerts, all recorded in SQLite. Python 3.11+, only dependency `requests`. Everything is testable offline against `chaos_server.py`.

## Module map (src/api_health_monitor/)
| File | One job | Touch it when |
|---|---|---|
| `config.py` | Parse + validate TOML into frozen dataclasses (`Endpoint`, `Defences`, `Config`) | adding a setting |
| `checker.py` | `check(ep, session) -> CheckResult`. Never raises. Classifies failures into `Reason` | adding a failure type |
| `scheduler.py` | Heap-based fixed-rate loop, one daemon thread per check, watchdog | changing timing behaviour |
| `incidents.py` | Per-endpoint state machine: UP / DEGRADED / DOWN, emits `Event`s on transitions only | changing alert rules |
| `alerts.py` | Console / JSONL / webhook alerters behind a queue + background thread | adding an alert channel |
| `storage.py` | SQLite `checks` + `incidents`, uptime/percentile maths | adding a metric |
| `report.py` | Table, Markdown, HTML status page | changing output |
| `sanitize.py` | Scrub server-controlled text before printing/posting | never weaken |
| `monitor.py` | Wires the above together | rarely |
| `chaos_server.py` | Local server that fails on purpose (+ `Blackhole`, `closed_port_url`) | adding a failure to test |
| `cli.py` | `run`, `check`, `report`, `demo` | adding a command |

## Invariants (tests enforce these; don't break them)
1. `check()` never raises and always returns within roughly `deadline_s` (+ one byte gap).
2. Only the scheduler thread calls `on_result`; storage and `IncidentTracker` are not thread-safe on purpose.
3. At most one in-flight check per endpoint (no pile-up).
4. Alerts fire on state transitions only. Incident start = first failing check, never detection time.
5. Every piece of server-controlled text passes through `clean_text` before display, and `escape_chat` before Slack.
6. Every defence in `Defences` has an attack in `evals/eval_attacks.py` proving it matters. New defence → new attack + ablation row.
7. Read bodies with `resp.raw.read1()`, never `iter_content()` (see POSTMORTEM #1).

## Commands
```bash
make test        # ~20 s
make demo        # 30 s live demo
make eval        # ~5 min, rewrites README results between the markers
make readme      # rebuild README tables from evals/results only
python evals/run_all.py --check   # CI: README matches results
python scripts/check_invisible_chars.py
```

## Conventions
- Tests use `use_env_proxy=False` sessions: the sandbox/CI may set HTTP(S)_PROXY.
- Never hand-edit the README between `<!-- results:start -->` and `<!-- results:end -->`.
- Write `\u202e`-style escapes in code, never the raw invisible character (the guard will fail CI).
- After changing behaviour, update `docs/CHEATSHEET.md` line references for the touched module.
