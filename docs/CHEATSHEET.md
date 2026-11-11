# CHEATSHEET: every line explained, for technical interviews

How to use this: read the module's code and this page side by side. Line numbers refer to `src/api_health_monitor/<module>.py` at v0.1. Each section ends with the questions an interviewer is likely to ask and a crisp answer.

**The 30-second pitch:** "It's a synthetic monitor. A heap-based scheduler fires checks on fixed-rate intervals, each in its own thread. Every check uses `requests` with stream=True, a (connect, read) timeout and a total deadline, and classifies the result into one of 15 reasons. Results come back through a queue to a single thread, which writes them to SQLite and feeds a per-endpoint state machine that alerts only on transitions: 3 failures to go down, 2 successes to recover. Alerts go through their own queue and thread so a slow Slack can't stall checks. I evaluated it against a local chaos server: 100% classification, 24/24 outages caught, 0/9 attacks succeed, and an ablation proves each defence matters."

---

## Data flow in one picture

```
Scheduler.run()  ──launch──▶ thread: check(ep, session) ──CheckResult──▶ queue
      ▲                                                                    │
      └──────────────────────── get(timeout) ◀──────────────────────────────┘
      │
      └─▶ Monitor.handle_result(r) ─▶ Store.record_check(r)
                                   └▶ IncidentTracker.observe(r) ─▶ [Event] ─▶ Store.record_event
                                                                            └▶ AlertDispatcher.publish ─▶ queue ─▶ thread ─▶ alerters
```

---

## config.py: turning TOML into trusted values

| Lines | What it does | Why |
|---|---|---|
| 1–10 | Module docstring: the three config sections | Orientation for readers |
| 12 | `from __future__ import annotations` | Type hints become strings, so `str \| None` works on 3.9+ and costs nothing at runtime |
| 14 | `import tomllib` | TOML parser, standard library since 3.11. No PyYAML dependency |
| 15 | `dataclass, field, fields, replace` | `field(default_factory=...)` for mutable defaults; `fields()` to list valid keys; `replace()` to copy-with-changes |
| 18 | `urlparse` | Split a URL into scheme/host/path to validate it |
| 22–23 | `ConfigError(ValueError)` | One exception type the CLI catches to print a clean message and exit 2 |
| 26–40 | `Defences` dataclass, all `True` | Each safety feature is a switch so the attack eval can turn exactly one off (ablation). `frozen=True` = immutable after creation |
| 42–60 | `Endpoint` fields with defaults | Every per-endpoint setting in one typed place. `expect_status` is a tuple (immutable, hashable) not a list |
| 60 | `defences: Defences = field(default_factory=Defences)` | A dataclass default must not be a shared mutable object; `default_factory` builds a fresh one per endpoint |
| 62–84 | `__post_init__` validation | Runs after `__init__`. Collects *all* problems then raises once, so you fix the config in one go. Checks: name present, http(s) URL with a host, supported method, positive timeouts, `deadline ≥ connect`, thresholds ≥ 1, valid status codes |
| 86–96 | `AlertConfig` | Console on by default; JSONL path and webhook optional; format validated |
| 98–111 | `Config` | Rejects zero endpoints and duplicate names (names are database keys) |
| 114 | `_ENDPOINT_KEYS` | The set of valid setting names, derived from the dataclass, so a typo like `timout_s` is rejected instead of silently ignored |
| 117–129 | `_endpoint_from` | Merges `[defaults]` under the endpoint's own keys (`{**defaults, **raw}`: right side wins), converts lists to tuples, sorts headers for determinism, turns `TypeError` (missing name/url) into `ConfigError` |
| 132–147 | `parse_config` | Dict → `Config`. Separate from file loading so tests can pass dicts |
| 150–158 | `load_config` | Reads the file; turns missing-file and bad-TOML errors into `ConfigError`. `from None` hides the noisy internal traceback |
| 161–163 | `with_defences` | `replace(ep, defences=replace(ep.defences, body_cap=False))`: copy of a frozen object with one field changed. Used only by evals |

**Q: Why frozen dataclasses?** Config is read from many threads. Immutable objects can be shared with no locks and can't be changed by accident mid-run.
**Q: Why reject unknown keys?** `read_timout_s = 2` (typo) would otherwise leave the 5 s default in place, and you'd find out during an outage.

---

## checker.py: one check, never raises

| Lines | What it does | Why |
|---|---|---|
| 1–8 | Docstring: the contract | "never raises" is the module's promise |
| 10–16 | stdlib imports | `socket` / `ssl` for exception types in the cause chain; `perf_counter` for timing |
| 18–28 | `requests` + specific exception classes | Catch precise types to classify, not just `RequestException` |
| 32 | `USER_AGENT` | Identifies the monitor in the API's logs, with a link. Good citizenship: ops teams can see who's calling |
| 33 | `CHUNK_BYTES = 16 KB` | Max bytes per read; small enough to check deadline/cap often, big enough to be efficient |
| 36–39 | `Status(str, Enum)` UP / DEGRADED / DOWN | `str` mixin means `.value` is a plain string for SQLite/JSON, and enums can't be typos |
| 42–57 | `Reason` enum, 15 values | Each maps to a step in the request lifecycle (LEARN.md §1.2). Precise reasons tell the on-call *where* to look |
| 60–74 | `CheckResult` frozen dataclass | Immutable value passed between threads. `started_at` is wall-clock (for humans); `ttfb_ms` vs `latency_ms` separates server think-time from transfer time. `is_up` = not DOWN (DEGRADED counts as up for uptime) |
| 77–88 | `make_session` | `Session` = connection pool + default headers. `trust_env` controls proxy env vars. `max_redirects` set from config, or 10,000 when that defence is off (ablation) |
| 91–108 | `_causes()` | Iterative depth-first walk over `.reason`, `__cause__`, `__context__`, `args`, with a `seen` set to survive cycles. Needed because `requests.ConnectionError` wraps urllib3 `MaxRetryError` wraps `NewConnectionError` wraps `ConnectionRefusedError` |
| 111–137 | `classify_exception` | Most-specific first: redirects, bad URL, connect timeout, read timeout, TLS. Then walk causes for DNS (`gaierror` / `NameResolutionError`), refused, TLS, socket timeouts. Anything else from requests = `connection_error`; anything else at all = `unexpected_error` (a bug in us, still contained) |
| 140 | `def check(ep, session=None)` | Session optional so one-off calls work; the monitor passes a long-lived one per endpoint |
| 142–143 | `own_session` | If we created the session, we must close it (line 217) |
| 144–146 | `started_at`, `t0`, `deadline` | Wall clock for the record; `perf_counter` (monotonic, sub-µs) for durations; the absolute deadline computed once |
| 147 | `d = ep.defences` | Short alias, read many times |
| 149–150 | `elapsed_ms()` | Closure over `t0` |
| 152–157 | `result(...)` helper | One place that fills latency if missing and sanitises `detail`, so no return path can forget either |
| 159 | `timeout = (connect, read)` or `None` | The tuple form gives separate limits. `None` only in the ablation: that's Requests' dangerous default |
| 162–169 | `session.request(..., stream=True)` | Returns once headers arrive, *before* the body. That's what lets us cap and time the body ourselves. Redirects followed (bounded by `max_redirects`) |
| 170 | `ttfb` | Time until headers: server processing + one network round trip |
| 172–175 | Comment | Points to POSTMORTEM #1, the reason for the next lines |
| 176–177 | `bytearray()`, `too_large` | `bytearray +=` appends in place (cheaper than `bytes +` copying) |
| 178–181 | `resp.raw.read1(CHUNK_BYTES, decode_content=True)` | Returns *whatever has arrived* (≥ 1 byte) instead of waiting for a full chunk; `b""` means the body ended. `decode_content` un-gzips as we go, so the cap counts unpacked bytes (stops gzip bombs) |
| 182–185 | Body cap | Stop reading past `max_body_bytes`: bounded memory whatever the server sends |
| 186–191 | Total deadline | Checked after every read, so a 1-byte-per-0.2 s drip is cut off within one gap of the deadline |
| 192–193 | `latency`, `common` | Total time; shared fields for the remaining results |
| 195–197 | Too large → DOWN | A health endpoint returning > 1 MB is wrong (or hostile) |
| 198–200 | Status not expected → DOWN | `resp.reason` is server-chosen text, so `result()` sanitises it |
| 201–205 | Body check | Decode with the server's declared charset (`errors="replace"` never raises), look for the expected text. Catches 200-OK maintenance pages |
| 206–208 | Over SLO → DEGRADED | Still working, so it counts as up, but slow gets its own alert |
| 209 | All good → UP | |
| 211–213 | `except Exception` | The safety net: every failure becomes a classified result. `KeyboardInterrupt` isn't an `Exception`, so Ctrl-C still works |
| 214–218 | `finally` | Always release the connection; if we stopped mid-body, urllib3 discards that socket rather than reusing a half-read one |
| 221–226 | `deadline_result` | Built by the scheduler when it gives up on a stuck thread; same shape as any other result |

**Q: Difference between connect and read timeout?** Connect bounds the TCP handshake. Read bounds the silence *between* received bytes, not the total. Hence the separate total deadline.
**Q: Why `stream=True`?** Without it, Requests downloads the whole body into memory before returning: a 50 GB body and you're out of memory, a slow body and you can't enforce a deadline.
**Q: Why not `iter_content`?** It blocks until a full chunk arrives. That's POSTMORTEM #1.
**Q: Why catch bare `Exception`?** A monitor's job is to observe failure, not propagate it. It's safe here because every exception is classified and recorded, and BaseException-only signals (Ctrl-C, exit) still propagate.
**Q: How do you tell DNS failure from refused if Requests raises the same class?** Walk the cause chain: `socket.gaierror` / urllib3 `NameResolutionError` vs `ConnectionRefusedError`.

---

## scheduler.py: on time, forever, without pile-ups

| Lines | What it does | Why |
|---|---|---|
| 1–20 | Docstring: the six design rules | Heap, fixed rate, threads, watchdog, single writer, monotonic clock |
| 23–30 | imports | `heapq` priority queue; `queue` thread-safe channel; `random` for jitter |
| 37 | `WATCHDOG_GRACE_S = 1.0` | Give the check's own deadline a full second to fire before overruling it |
| 38 | `MAX_WAIT_S = 0.5` | Never sleep longer than this, so the stop event is noticed within 0.5 s |
| 41–51 | `SchedulerStats` | Counters that make behaviour observable. `watchdog_fired > 0` routinely = an inner bug (lesson of POSTMORTEM #1) |
| 53–58 | `_InFlight` | What's running per endpoint: monotonic start (for the watchdog), wall start (for the record), `abandoned` flag |
| 62–81 | `__init__` | `check_fn` and `on_result` are injected, so tests pass fakes. `clock` is injectable too. `_results` is the queue workers write to. `_inflight` keyed by endpoint index enforces one-at-a-time. `_run_id` tags each launch so a late result can be matched to its launch |
| 83–90 | `_worker` | Runs in the check thread. Belt-and-braces `try`: `check()` never raises, but a custom `check_fn` might. Only touches the queue, never shared state |
| 92–97 | `_deliver` | Calls `on_result` (storage + state machine) inside `try`, so a full disk or locked DB is logged and counted, not fatal |
| 99–106 | `_launch` | Records in-flight state *before* starting the thread (so a fast result can't arrive before we're tracking it). `daemon=True`: a stuck thread can't block process exit |
| 108–111 | `run` setup | Optional stop event and duration; `end = inf` means forever |
| 112–115 | Initial heap with jitter | Each endpoint's first run is at a random point in the first 10% of its interval, so 100 endpoints don't all fire at t = 0. `heapify` is O(n) |
| 117–120 | Main loop, exit conditions | Checked every iteration |
| 123 | `while heap[0][0] <= now` | Peek at the earliest due time, O(1); launch everything due |
| 124 | `heappop` | O(log n) |
| 126 | `nxt = due + interval` | **Fixed rate**: based on when it was *due*, not when it ran, so no drift |
| 127–130 | Fell behind | If even the next slot is past (laptop slept, CPU starved), skip ahead to the first future slot and count what was missed, rather than firing a burst |
| 131 | `heappush` | Re-schedule before deciding whether to launch, so the endpoint is never lost from the heap |
| 132–140 | Already in flight | Normal case: `continue` (no pile-up). If it's abandoned, re-emit a DOWN result so alerts and uptime still see it, without a second thread |
| 141 | `lateness_ms` | How late each launch was versus its due time: the scheduling-accuracy metric in the README |
| 142 | `_launch` | |
| 145–153 | Watchdog | Any in-flight check past `deadline + grace` → mark abandoned, emit `deadline_exceeded`. The thread keeps running (Python can't kill threads); its eventual result is ignored |
| 156–160 | Compute sleep | Wake at the earliest of: next due check, next watchdog deadline, end of run, 0.5 s cap |
| 161–164 | `queue.get(timeout)` | Blocks with zero CPU until a result arrives or it's time to do something. `queue.Empty` → loop again |
| 165–178 | Drain results | Handle the one we got, then everything else already waiting (`get_nowait`) in one go. Match `run_id`, remove from in-flight, ignore if abandoned, else deliver |
| 179 | return stats | For the CLI summary and evals |

**Q: Why a heap and not a sorted list?** Insert into a sorted list is O(n); the heap is O(log n), and we only ever need the minimum.
**Q: Fixed rate vs fixed delay?** Fixed delay (`sleep(interval)` after each run) adds the run time to every interval and drifts. Fixed rate schedules from the previous *due* time.
**Q: Why threads, not asyncio?** Requests is synchronous. Threads are fine for tens to hundreds of endpoints. Past ~1,000, I'd move to asyncio + httpx, which can also cancel stuck requests (design doc §2).
**Q: Isn't a thread per check expensive?** ~50 µs to start one; checks run every few seconds. Measured CPU: 0.53 ms per check total.
**Q: Why can't you kill the stuck thread?** CPython has no safe thread cancellation; a thread could hold a lock forever. So we abandon it and make sure it can't multiply (one per endpoint) or block exit (daemon).
**Q: How do you avoid race conditions?** Only the scheduler thread touches storage and the state machine; workers only `put` on a thread-safe queue.

---

## incidents.py: the alerting state machine

| Lines | What it does | Why |
|---|---|---|
| 1–16 | Docstring with the state diagram | The behaviour in one picture |
| 26–30 | `State` UNKNOWN / UP / DEGRADED / DOWN | UNKNOWN until first contact, so start-up doesn't fire a "recovered" |
| 33–38 | `EventKind` | The five alerts that can be sent |
| 41–53 | `Event` | Includes `incident_started_at` (first bad check), so `duration_s` is real outage length, not time since we noticed |
| 55–64 | `_Track` per endpoint | Current state, four streak counters, start times, last alert time (for reminders) |
| 67–69 | `_or(value, fallback)` | `is None` test. `x or y` treats `0.0` as missing (POSTMORTEM #2b) |
| 72–78 | Tracker setup | Dicts keyed by endpoint name |
| 80–87 | `observe` setup | `now = r.started_at`: decisions are timestamped by when the check started, so they're reproducible from stored history |
| 85–87 | `emit()` | Append event, remember alert time |
| 89–104 | DOWN branch | Reset good/slow streaks. Record start of a failure streak **only if not already DOWN** (line 94–95, POSTMORTEM #2a). On the Nth failure while not DOWN → DOWN event. While DOWN, optional reminder every `realert_every_s` |
| 106–114 | Reachable while DOWN | Count successes; on the Mth → RECOVERED with the original start, back to UP |
| 115–116 | First contact | UNKNOWN → UP silently |
| 118–125 | Slow result | Count slow streak; start time only set from UP (line 120–121); Nth slow → DEGRADED |
| 126–134 | Fast result | While DEGRADED, count fast checks; Mth → DEGRADED_RESOLVED |

**Q: Why 3 and 2?** From the threshold table: N = 3 is the smallest N with ~zero false alarms for an API failing up to 2% of checks at random; 2 to recover avoids announcing a recovery that relapses on the next check.
**Q: What's hysteresis?** Different thresholds to enter and leave a state (3 to go down, 2 to come up), so a borderline service doesn't flap between states every check.
**Q: How is this testable?** It's pure: no I/O, no clocks, just results in and events out. The threshold eval replays 30 days of checks through it in seconds.

---

## alerts.py: delivery that can't stall monitoring

| Lines | What it does | Why |
|---|---|---|
| 1–7 | Docstring | Alerting is a network call that can hang too |
| 29–35 | `ICONS` | Emoji + word: scannable on a phone lock screen |
| 38–46 | `_fmt_duration` | 45s / 3m 05s / 1h 20m |
| 49–65 | `format_text` | One line with reason, HTTP code, latency, detail, duration, URL. All server-controlled text through `clean_text` |
| 68–71 | `Alerter` Protocol | Structural typing: anything with `name` and `send(ev)` is an alerter, no inheritance needed |
| 74–83 | `ConsoleAlerter` | Timestamp + text, `flush=True` so it appears immediately in logs |
| 86–105 | `JsonlAlerter` | One JSON object per line, append mode: greppable and machine-readable. `ensure_ascii=False` keeps emoji readable |
| 108–116 | `webhook_payload` | Slack: escape `< > &` so `<!channel>` is plain text. Discord: defuse `@everyone` *and* `allowed_mentions: {"parse": []}` (two layers). Generic: structured fields |
| 119–146 | `WebhookAlerter.send` | POST with its own (connect, read) timeout. 2xx = done. 4xx (except 429) = our payload or URL is wrong, so don't retry. 5xx / 429 / network error = retry once after a short backoff, then raise (the dispatcher counts it) |
| 148–152 | `DispatchStats` | sent / failed / dropped |
| 155–163 | `AlertDispatcher.__init__` | Bounded queue (1,000) + daemon delivery thread |
| 165–170 | `publish` | `put_nowait`: never blocks the scheduler. If the queue is full, drop and log rather than freeze monitoring |
| 172–183 | `_run` | Loop: take event, send to every channel, one channel's exception never stops the others. `None` is the stop signal |
| 185–191 | `close` | Enqueue `None`, wait up to 10 s for the backlog to send |
| 194–202 | `build_alerters` | Config → channel objects |

**Q: What if Slack is down during your outage?** Checks keep running (separate thread), the webhook retries once, the failure is counted, and the alert is still in the console, the JSONL file and the incidents table.
**Q: What's the injection risk?** The HTTP reason phrase is chosen by the server. `HTTP/1.1 500 <!channel> log in at evil.example` would ping everyone with a phishing link, sent by your trusted bot.

---

## storage.py: SQLite history and uptime maths

| Lines | What it does | Why |
|---|---|---|
| 19–45 | Schema | `checks`: one row per check. `incidents`: one row per outage with `started_at`, `detected_at`, `resolved_at`. Index on `(endpoint, ts)` makes window queries fast. `IF NOT EXISTS` so restarts are safe |
| 47–58 | `UptimeRow` | Everything the report shows |
| 61–67 | `percentile` | Nearest-rank: sort, take element `ceil(p/100 × n) − 1`. Always returns a real observed value |
| 71–76 | `Store.__init__` | Creates parent folder; `check_same_thread=False` because the connection is made on one thread and used on the scheduler thread (still only one at a time). `PRAGMA journal_mode=WAL`: readers (a report) don't block the writer |
| 78–85 | `record_check` | Parameterised `?` placeholders: never string-format SQL (SQL injection) |
| 87–100 | `record_event` | DOWN/DEGRADED inserts an incident; RECOVERED/RESOLVED closes the most recent open one of that kind |
| 102–109 | `incidents()` | Rows as dicts for the HTML page |
| 111–140 | `uptime()` | Per endpoint in the window: total and reachable checks (`SUM(status != 'down')` counts trues), latencies of reachable checks for p50/p95, incidents overlapping the window, downtime clipped to the window (ongoing incidents count up to `now`), MTTR over closed incidents |

**Q: Uptime by checks or by time?** By checks: simple and standard for synthetic monitoring. The incidents table gives wall-clock downtime. Both are reported; README "Known failures" explains the difference.
**Q: Why not Prometheus?** At scale, yes. For one process, SQLite needs no server and keeps raw checks for investigation.

---

## sanitize.py

| Lines | What it does | Why |
|---|---|---|
| 19 | 200-char limit | An error message shouldn't fill a phone screen |
| 22 | `_CONTROL` regex | C0 (0x00–0x1F, includes ESC) and C1 (0x7F–0x9F) control characters: terminal escapes, bells, carriage returns that overwrite a line |
| 24, 30 | Unicode category `Cf` | "Format" characters: zero-width spaces, right-to-left overrides that visually reverse text |
| 31 | `" ".join(s.split())` | Collapse all whitespace runs into single spaces |
| 38–44 | `escape_chat` | `&` first (otherwise we'd double-escape the others), then `<`, `>` |
| 47–52 | `discord_safe` | Zero-width space after `@` breaks the mention |

---

## monitor.py, report.py, cli.py, chaos_server.py (briefly)

- **monitor.py** 24–36: builds store, tracker, dispatcher and one `Session` per endpoint (keep-alive, never shared by two threads at once because of no-pile-up). 41–47 `handle_result`: store → state machine → store events → publish alerts. 49–56 `run`: always closes dispatcher and sessions in `finally`.
- **report.py** 31–33 `_cells`: one row of formatted strings, endpoint name sanitised. 36–44 `table`: column widths from the longest cell. 47–55 `markdown`: escapes `|` in names. 58+ `html_page`: everything through `html.escape(quote=True)`, dark-mode CSS, no JavaScript.
- **cli.py** 29–34: SIGINT and SIGTERM set the stop event (graceful shutdown in Docker/systemd). 51–62 `check`: exit code 1 if anything is down, so it works as a CI or cron gate. 84–121 `demo`: starts the chaos server with a planted timeline and runs the real monitor against it. 153–161 `main`: `ConfigError` → exit 2.
- **chaos_server.py**: `ThreadingHTTPServer` with `daemon_threads` (each request in a thread; hanging routes can't block exit). Line 53 `disable_nagle_algorithm` (POSTMORTEM #3), line 71 `request_queue_size = 128` (POSTMORTEM #4), `handle_error` silenced because clients hanging up is the normal case. `route()` maps paths to behaviours; `/scenario/<name>` follows a time-scripted list of `(start, end, mode)`. `Blackhole` (209+) fills a `listen(0)` backlog so new SYNs are dropped, a real connect timeout on localhost. `closed_port_url` binds then closes a port, so nothing listens: instant refused.

---

## Testing and evals: what to say

- **57 tests** (`pytest -q`, ~18 s): every failure reason against the real chaos server, state-machine edge cases, webhook retries, a slow webhook not blocking `publish`, a crashing channel not stopping others, SQLite maths, config validation, scheduler drift / no pile-up / watchdog / crash containment / stop, and CLI exit codes.
- **Evals** are separate from tests: tests say "does this behave as specified", evals say "how well, in numbers". Every eval is scored against a planted answer key.
- **Ablation** is the credibility move: 0% attack success is only meaningful if switching each defence off makes its attacks succeed.
- **Honesty markers:** precision is 96%, not 100% (and we explain the one false alarm); the 1-minute outage row shows a real blind spot; known failures list the thread leak.

## Rapid-fire answers
| Question | Answer |
|---|---|
| What's p95? | The value 95% of samples are at or below; shows the slow tail that averages hide |
| What's TTFB? | Time to first byte: request sent → headers received |
| What's a 503 vs 504? | 503: server says it can't serve now. 504: a gateway timed out waiting for the server behind it |
| What does `Retry-After` mean? | Server's hint for when to retry (on 429/503); a good next feature for backoff |
| Why HEAD? | Headers only: cheaper, but can't verify the body |
| Idempotent? | Safe to repeat. Health checks must be, since they run forever |
| What's jitter for? | Spreading load so many clients don't act at the same instant |
| Daemon thread? | Doesn't keep the process alive at exit |
| Monotonic clock? | Only moves forward; immune to NTP and DST changes |
| WAL mode? | Write-ahead log: readers see a consistent snapshot while the writer appends |
