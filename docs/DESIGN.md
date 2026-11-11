# Design doc: api-health-monitor

**Author:** Paul Madojutimi · **Status:** v0.1 shipped · **Reviewers:** you, in the interview

## Goal
Know within a few check intervals when an API stops serving correct answers fast enough, say *why*, page a human only when it's real, and keep an honest uptime record. Non-goals for v0.1: multi-region quorum, distributed tracing, a hosted dashboard.

## Success measures (all measured; see README)
| Measure | Target | Result |
|---|---|---|
| Failure named correctly by one check | ≥ 99% | 100% (300/300) |
| Planted outages caught | 100% | 24/24 |
| False alarms on a 2%-flaky API | < 1 per week | 0 per day in 30 simulated days |
| Any check's wall time | ≤ deadline + one byte gap | 1.2 s deadline → ≤ 1.21 s |
| Hostile-endpoint attacks that succeed | 0 | 0/9, each defence proven by ablation |
| Cost per check | ~0 | €0 API, 0.53 ms CPU |

## Decisions and the alternatives rejected

### 1. When to alert: N-in-a-row state machine
- **Chosen:** alert after `failures_to_alert` (default 3) consecutive failures, recover after 2 consecutive successes. Alerts fire only on state changes.
- **Rejected: alert on every failure.** Simulated over 30 days, a 2%-flaky API produces **57 false alarms a day**. People mute the channel within a week, and then miss the real outage.
- **Rejected: retry inside the check (3 quick retries).** Hides the information: three retries in 1 s all hit the same 1-second blip, and the stored history no longer shows the flakiness. Consecutive checks spread over time are better evidence, and every attempt is recorded.
- **Rejected: failure rate over a sliding window (e.g. > 50% of last 10).** Better for flapping endpoints, but slower to fire on a clean outage and harder to explain at 3 a.m. Listed as a next step if flapping shows up in real data.
- **Trade-off accepted:** with N = 3 on a 30 s interval, outages shorter than ~90 s don't page. The threshold table makes that explicit; the fix is a shorter interval, not a lower N.

### 2. Concurrency: one daemon thread per due check
- **Chosen:** the scheduler starts a short-lived daemon thread per check; results come back on a `queue.Queue` and are handled on the scheduler thread only.
- **Rejected: `ThreadPoolExecutor`.** Its worker threads aren't daemons, and Python joins them at exit. One check stuck on a header drip would make `Ctrl-C` hang forever, and stuck workers silently shrink the pool for everyone else.
- **Rejected: `asyncio` + `httpx`/`aiohttp`.** Scales to thousands of endpoints in one thread and can cancel a stuck request outright. Rejected for v0.1 because the goal includes *learning Requests*, which is synchronous, and because a monitor of tens to hundreds of endpoints doesn't need it. It's the upgrade path past ~1,000 endpoints.
- **Rejected: process per check.** The only way to truly kill a stuck request (fixes the header-drip thread leak) but costs ~30 ms and ~10 MB per check. Kept as a next step, opt-in for known-bad endpoints.

### 3. Bounding a check: three clocks plus a watchdog
- **Chosen:** `timeout=(connect, read)` on every socket call, a total deadline checked between body reads, a 1 MB body cap, a redirect limit, and a scheduler watchdog at deadline + 1 s.
- **Rejected: `timeout=` alone.** `requests`' read timeout is *per socket read*, not per request. A server sending 1 byte every 0.2 s never trips a 5 s read timeout, ever. Proven by the ablation (check never returned).
- **Rejected: `signal.alarm()`.** Only works in the main thread, and we check in worker threads.
- **Rejected: urllib3 `Timeout(total=...)`.** Despite the name, it bounds the connect phase plus each individual read, not the whole response.

### 4. Scheduling: heap, fixed-rate, skip missed slots, monotonic clock
- **Chosen:** a min-heap of `(next_due, endpoint)`. Next due = previous due + interval (fixed rate). If a whole interval is missed, skip it rather than replaying a burst. `time.monotonic()` for timing.
- **Rejected: `time.sleep(interval)` loop per endpoint (fixed delay).** Drifts: a 30 s interval plus 1.4 s checks becomes a 31.4 s interval, and checks of different endpoints bunch together over time.
- **Rejected: cron / APScheduler / `schedule` library.** Cron's floor is 1 minute and forks a process per run. Libraries hide exactly the decisions this project exists to teach. `schedule` also wasn't installable in the build environment, which made the standard-library choice easy.
- **Rejected: `time.time()` for timing.** Wall clock can jump (NTP corrections, daylight saving), which would fire every check at once or none for an hour.

### 5. Storage: SQLite, single writer
- **Chosen:** one SQLite file with WAL mode; only the scheduler thread writes.
- **Rejected: CSV/JSONL only.** Fine for alerts (kept as a channel), but uptime-over-window and percentiles become full-file scans.
- **Rejected: Prometheus + Grafana.** The industry answer at scale, but it adds two services to run, and you can't answer "show me every check in that incident" without also keeping raw logs. The SQLite schema would map onto Prometheus metrics cleanly later.

### 6. Treat the monitored endpoint as hostile
- **Chosen:** sanitise every server-controlled string (reason phrase, error text) before it reaches a terminal or chat: strip control and invisible characters, truncate, escape `< > &` for Slack, and set `allowed_mentions: none` for Discord.
- **Why it matters:** the HTTP reason phrase is free text the server chooses. Without this, `HTTP/1.1 500 <!channel> log in at evil.example` pages your whole company with a phishing link, and it arrives from *your* trusted bot.

## Risks
| Risk | Mitigation |
|---|---|
| Monitor's own network fails → every endpoint "down" | Next step: multi-region quorum. Today: the alerts show *every* endpoint failing at once, an obvious tell |
| Alert channel down during an outage | Async dispatcher (never blocks checks), retry once, JSONL + SQLite always written |
| Monitoring traffic itself overloads a weak API | Jittered start, ≥ 1 interval per endpoint, no pile-up; HEAD supported |
| Webhook URL leaked in git | `monitor.toml` is git-ignored; example config uses placeholders |
