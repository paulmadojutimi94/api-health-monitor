# LEARN: APIs going down, Requests, HTTP, scheduling and timeouts, from first principles

Read this top to bottom once, then use it as a reference. Every idea points at the line of code that implements it, and each part ends with exercises you can run in two minutes.

---

## Part 1: What actually happens when an API goes down

### 1.1 What a single API call really is

When your app calls `GET https://api.shop.com/orders/42`, six things happen in order. Each one can fail differently, and **each failure looks different from the outside**. That's the whole reason this monitor has 15 failure reasons, not one.

```
 your app                                                         the API
    │ 1. DNS: "what's the IP address of api.shop.com?" ──▶ DNS server
    │ 2. TCP handshake: SYN ─────────────────────────────────────▶ │
    │                   ◀──────────────────────────────── SYN-ACK  │
    │                   ACK ─────────────────────────────────────▶ │
    │ 3. TLS handshake: agree encryption, check certificate ────▶ │
    │ 4. Send request:  "GET /orders/42 HTTP/1.1 ..." ──────────▶ │
    │                                         5. app does the work │
    │ 6. Response:      ◀──── "HTTP/1.1 200 OK" + headers + body   │
```

Analogy: phoning a restaurant to place an order.
1. **DNS** = looking the number up in the phone book.
2. **TCP** = the phone ringing and someone picking up.
3. **TLS** = both sides confirming who they are ("This is Luigi's?" "Yes, and here's our licence.").
4. **Request** = you say your order.
5. **Work** = the kitchen cooks.
6. **Response** = the food arrives (status line = "here you go" / "we're out of that"; body = the food).

### 1.2 The failure catalogue: what "down" looks like at each step

| Step that breaks | Real-world cause | What the client experiences | Monitor reason |
|---|---|---|---|
| 1. DNS | Domain expired, record deleted, DNS provider outage | Instant error: "name or service not known" | `dns_failure` |
| 2. TCP, host up, nothing listening | App crashed, container restarting, wrong port | Instant **RST** packet: "connection refused" | `connection_refused` |
| 2. TCP, nothing answers | Server powered off, network partition, firewall *drops* packets, accept queue full | **Silence.** The SYN is retried with growing gaps; without a timeout, Linux gives up after ~2 minutes | `connect_timeout` |
| 3. TLS | Certificate expired, wrong hostname, broken chain | Handshake error | `tls_error` |
| 5. App frozen | Deadlock, thread pool exhausted, 30-second GC pause, stuck DB query | Connected... then silence. **Waits forever** without a read timeout | `read_timeout` |
| 5. App broken | Bug, dependency down, out of memory | `500 Internal Server Error` / `502 Bad Gateway` / `503 Service Unavailable` / `504 Gateway Timeout` | `http_status` |
| 5. App "up" but wrong | Maintenance page, empty JSON, error page with a 200 | **200 OK** with the wrong body: the *silent* outage | `body_mismatch` |
| 5. App overloaded | Traffic spike, slow DB | Works, slowly | `slow` (DEGRADED) |
| 6. Response never ends | Slow network, a bug streaming forever, a slowloris-style attacker | Bytes keep trickling; each read is "fast enough" so no timeout fires | `deadline_exceeded` |

Two things that surprise people:

- **"Refused" is good news compared to "timeout".** Refused comes back in 1 ms: you know immediately. A timeout means you sat there for the whole timeout, *and so did every user*.
- **The worst outages return 200.** A load balancer serving a "We'll be right back" page, or an API returning `{"items": []}` because its database connection failed and the code swallowed the error. Status-code-only monitoring shows 100% uptime throughout. That's why `expect_body` exists ([checker.py](../src/api_health_monitor/checker.py), the `BODY_MISMATCH` branch).

### 1.3 The knock-on effects: why one down API takes others with it

When an API goes down slowly (timeouts, not refusals), the damage spreads:

1. **Callers wait.** Every request that hits the dead API holds a thread/connection on the *caller* for the full timeout. With no timeout, forever.
2. **Callers run out of threads.** A web server with 200 worker threads, each stuck 30 s waiting on the dead API, can only serve ~7 requests a second, *including requests that never needed that API*. Now the caller is down too.
3. **Retries multiply load.** Every caller retries. When the API starts recovering, it's hit with 3x normal traffic and falls over again. This is the **retry storm** / **thundering herd**.
4. **Queues back up.** Messages pile up; when things recover, the backlog arrives all at once.
5. **Users retry too.** They hammer refresh, which is more load.

This is a **cascading failure**, and it's why the industry obsesses over **timeouts** (stop waiting), **circuit breakers** (stop calling something known to be dead for a while), **retries with exponential backoff and jitter** (spread retries out so they don't arrive together), and **bulkheads** (separate thread pools so one dead dependency can't eat all the threads).

The monitor applies those same lessons to *itself*: timeouts on every check, one thread per check so a dead API can't block the others (a bulkhead), no pile-up of checks on a stuck endpoint, and an alert queue so a dead Slack can't stall monitoring.

### 1.4 How long until someone knows? The detection timeline

```
outage starts ─▶ first failed check ─▶ Nth failed check ─▶ alert sent ─▶ human sees it ─▶ fixed ─▶ M good checks ─▶ "recovered"
               │◀ up to 1 interval ▶│◀ (N-1) intervals ▶│                                         │◀ (M-1) intervals ▶│
```

- **MTTD** (mean time to detect) ≈ ½ interval + (N−1) intervals + check duration. With a 30 s interval and N = 3: ~75 s (the eval measured p50 75 s, p95 88 s).
- **MTTR** (mean time to recovery) is measured from the first failed check to the recovered alert, which is what `storage.uptime()` reports.
- **Uptime %** turns into a budget: 99.9% = 43 minutes of downtime a month; 99.99% = 4.3 minutes. At 99.99%, a 75 s detection time alone uses 30% of the monthly budget, which is why serious teams check every 10 s from several regions.

### 1.5 Exercises
1. `make demo`. Which alert came first, and why does `search-api` show `http=200` while being DOWN?
2. In `cli.py` `demo_config`, change `failures_to_alert` to 1 and re-run. Count the `flaky-api` alerts. That's alert fatigue.
3. Run `curl -v http://127.0.0.1:1/` and then `curl -v --connect-timeout 3 http://10.255.255.1/` on your own machine. Feel the difference between refused and timeout.

---

## Part 2: HTTP in one sitting

HTTP is plain text over a TCP connection. A request:

```http
GET /health HTTP/1.1
Host: api.shop.com
User-Agent: api-health-monitor/0.1
Accept-Encoding: gzip

```
(the blank line means "headers finished")

A response:

```http
HTTP/1.1 503 Service Unavailable
Content-Type: text/plain
Content-Length: 20
Retry-After: 30

upstream unavailable
```

- **Status line:** version, **status code**, **reason phrase**. The code is for machines; the phrase is free text the server picks. It can say anything, including `<!channel>` (see `sanitize.py` and the `/inject` route in the chaos server).
- **Status code families:** `2xx` success, `3xx` go elsewhere (redirect), `4xx` *you* did something wrong (404 not found, 401 not logged in, 429 too many requests), `5xx` *the server* failed. For a health check, 5xx and timeouts are outages; 4xx usually means your monitor is misconfigured. That's why `expect_status` is explicit.
- **Headers:** `Content-Length` says how big the body is. `Transfer-Encoding: chunked` means "I don't know yet; I'll send pieces, each prefixed with its size" (`/chunked` route). `Content-Encoding: gzip` means the body is compressed, which is how a 100 KB response can unpack to 100 MB (`/gzip-bomb`). `Location` is where a redirect points.
- **Methods:** `GET` fetch, `HEAD` same but headers only (cheaper health checks), `POST` send data. Health checks should be **idempotent**: safe to repeat without side effects. Never point a monitor at a `POST /orders`.
- **Keep-alive:** HTTP/1.1 reuses the TCP (and TLS) connection for the next request. That skips steps 1–3 above, which over the internet is often 50–300 ms. Our checks reuse connections via one `Session` per endpoint ([monitor.py](../src/api_health_monitor/monitor.py)).

**Latency, precisely:** `ttfb_ms` (time to first byte) is from sending until the status line and headers arrive: "how long did the kitchen take?". `latency_ms` is until the last body byte: "until the food was on the table". A slow TTFB means slow server work; a big gap between TTFB and total means a big or slowly-sent body.

**p50 / p95 / p99:** sort all latencies; p95 is the value 95% are at or below. Averages lie: 99 requests at 10 ms and one at 10 s average 110 ms, which describes nobody. p95 tells you what your unluckiest regular users feel. Implemented as nearest-rank in `storage.percentile`.

### Exercises
1. `python -m api_health_monitor.chaos_server --port 8099`, then `curl -v localhost:8099/status/503`, `curl -v localhost:8099/redirect-loop`, `curl -v --compressed localhost:8099/chunked`. Read the raw status lines and headers.
2. `curl -sI localhost:8099/gzip-bomb` shows `Content-Length` (wire size). Why is the number so small?

---

## Part 3: The Requests library, the parts that matter

```python
import requests

s = requests.Session()                       # 1
s.trust_env = False                          # 2
r = s.get(url, timeout=(3, 5), stream=True)  # 3
r.status_code, r.reason, r.headers           # 4
chunk = r.raw.read1(16384, decode_content=True)  # 5
r.close()                                    # 6
```

1. **Session**: keeps a connection pool (keep-alive) and default headers. One per endpoint, reused. Without it, every check pays DNS + TCP + TLS again.
2. **trust_env**: when True (the default), Requests uses the `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` environment variables. A health check through a corporate proxy measures the proxy, not your API. Configurable via `[network] use_env_proxy`.
3. **timeout=(connect, read)**, the most important argument in the library:
   - **Default is no timeout at all.** `requests.get(url)` can hang forever. This is the #1 cause of "our service froze" postmortems.
   - **connect** = max wait for the TCP handshake.
   - **read** = max **silence between bytes**, *not* the total time. A server sending 1 byte every 4 s never trips a 5 s read timeout. See Part 5.
   - `stream=True` = return as soon as headers arrive; *we* decide how to read the body. Without it, Requests downloads the entire body into memory first, a 50 GB body included.
4. The status line and headers, available before any body is read.
5. **read1**: read *whatever has arrived*, up to the limit. Not `iter_content()`, which waits for a full chunk (POSTMORTEM #1). `decode_content=True` un-gzips as we go, so the size cap applies to the *unpacked* size, which is how the gzip bomb is stopped at 1 MB.
6. **close()**: return the connection to the pool, or drop it if we stopped mid-body. The `finally:` in `check()` guarantees this even when something raises.

**Exceptions** form a hierarchy, and the useful ones are wrapped several layers deep: `requests.ConnectionError` → urllib3 `MaxRetryError` → urllib3 `NewConnectionError` → Python `ConnectionRefusedError`. `checker._causes()` walks `.reason`, `__cause__`, `__context__` and `args` to find the real culprit. That's how "connection refused" and "DNS failure", both reported by Requests as a plain `ConnectionError`, get different reasons.

**Redirects:** followed automatically up to `Session.max_redirects` (default 30; we use 5). With the limit removed, one check against a redirect loop made **10,001 requests** and grew memory by up to 476 MB, because Requests keeps every intermediate response in `r.history`.

### Exercises
1. In a Python shell: `requests.get("http://127.0.0.1:8099/hang")` (no timeout). Count to 30, then Ctrl-C. Now add `timeout=(1, 2)`.
2. `r = requests.get("http://127.0.0.1:8099/big")` then `len(r.content)`. You just pulled 50 MB into memory. Now try with `stream=True` and `r.raw.read1(1000)`.

---

## Part 4: Scheduling: running things on time, forever

The naive version:

```python
while True:
    for ep in endpoints:
        check(ep)
    time.sleep(30)
```

Five problems, and how `scheduler.py` solves each:

| Problem | What goes wrong | Fix in scheduler.py |
|---|---|---|
| **Serial** | One hanging endpoint (10 s) delays every other check | Each check runs in its own daemon thread |
| **Drift (fixed delay)** | Interval becomes 30 s + check time, and keeps sliding | **Fixed rate:** `next_due = previous_due + interval` |
| **Catch-up bursts** | After a 5-minute laptop sleep, a fixed-rate loop fires 10 missed checks back to back | Missed slots are counted and **skipped** (`skipped_behind`) |
| **Thundering herd** | 100 endpoints all fire at t = 0, every time | **Jitter:** first check of each endpoint spread over the first 10% of its interval |
| **Wall-clock jumps** | NTP or daylight saving moves `time.time()`; checks fire all at once or not for an hour | `time.monotonic()`: only ever goes forward |

**The heap.** Endpoints have different intervals (10 s, 30 s, 5 min). A min-heap (`heapq`) keeps `(next_due, endpoint)` pairs with the earliest always at index 0. The loop: pop everything due, push each back with its next due time, launch checks, then sleep until the earliest of (next due, next watchdog deadline, a result arriving). Each pop/push is O(log n), so 10,000 endpoints cost nothing.

**Waiting without busy-looping.** `self._results.get(timeout=...)` sleeps until either a result arrives (wake immediately and handle it) or the timeout passes (time to launch the next check). The thread uses no CPU while waiting, which is why the whole monitor costs **0.53 ms of CPU per check**.

**No pile-up.** If an endpoint's previous check is still running when it's due again, we skip launching another. Otherwise a slow endpoint accumulates 1, 2, 3, ... parallel checks against itself, making it slower: the monitor would cause the outage it reports.

**One writer.** Worker threads only produce a `CheckResult` and put it on a queue. Storage and the incident tracker are touched only by the scheduler thread, so there are no locks and no races. This is the "share memory by communicating" pattern.

**Graceful shutdown.** `cli.py` turns `SIGINT` (Ctrl-C) and `SIGTERM` (what `docker stop`, Kubernetes and systemd send) into a `threading.Event`. The loop checks it every iteration (≤ 0.5 s), then drains queued alerts before exit.

### Exercises
1. Run `pytest tests/test_scheduler_monitor.py -v`. Read `test_fixed_rate_no_drift` and `test_slow_check_does_not_delay_others`.
2. Change `nxt = due + ep.interval_s` to `nxt = now + ep.interval_s` (fixed delay), add `print(now - start)` before `self._launch(i, now)`, and run a 60 s monitor. Compare how far the last launch is from an exact multiple of the interval with and without the change. Each loop's small lateness now adds up instead of cancelling out.

---

## Part 5 (bonus): Handling timeouts without crashing

The rule: **a monitor must never be the thing that's down.** Whatever the endpoint does, `check()` returns a `CheckResult` within a bounded time, and nothing escapes as an exception.

### 5.1 Why one timeout isn't enough

| Server behaviour | connect timeout | read timeout | total deadline | watchdog |
|---|---|---|---|---|
| Host gone, SYN dropped | ✅ catches | | | |
| Connected, then silence | | ✅ catches | | |
| Body: 1 byte every 0.2 s forever | ✗ | ✗ (each read is quick) | ✅ catches | |
| Headers: 1 byte every 0.2 s forever | ✗ | ✗ | ✗ (we never reach the body loop) | ✅ catches |
| 50 MB body, fast | ✗ | ✗ | maybe | body cap ✅ |

Analogy: you send someone to collect a parcel. "Give up if nobody answers the door in 3 minutes" (connect). "Give up if they go silent for 5 minutes" (read). But if the shopkeeper keeps saying "one moment..." every 4 minutes, neither rule ever fires. You also need "come home after 10 minutes total, whatever's happening" (deadline). And if your messenger is stuck in a conversation they can't leave, *you* need to stop waiting for them (watchdog).

### 5.2 Never crash: catch everything, but classify it

```python
try:
    ...
except Exception as exc:          # a monitor must survive anything
    reason = classify_exception(exc)
    return result(Status.DOWN, reason, detail=...)
finally:
    resp.close()
```

Catching `Exception` is usually a code smell. Here it's the point, with two rules that keep it honest: (1) every exception becomes a *classified* result, so nothing is silently swallowed (unknown ones are `unexpected_error`, visible in reports), and (2) `KeyboardInterrupt`/`SystemExit` are not `Exception` subclasses, so Ctrl-C still works.

The same idea is repeated at every boundary: the scheduler wraps the check function (`_worker`) and the result callback (`_deliver`); the alert dispatcher wraps every channel. A bug in one layer is recorded and counted, never fatal (`test_crashing_check_and_callback_are_contained`).

### 5.3 What you can't do in Python, and the workaround

Python cannot kill a thread. When the watchdog gives up on a stuck check, it records `deadline_exceeded`, marks the thread **abandoned**, and ignores its result if it ever arrives. The thread is a **daemon**, so it can't stop the program exiting. While it's stuck, the scheduler reports the endpoint as still down at each due time *without* starting another thread, so there's at most one stuck thread per endpoint. The stress eval ran 40 endpoints with 15 permanently stuck: **100%** of healthy checks stayed on schedule, p95 lateness 1.9 ms.

### 5.4 Choosing timeout values

- **connect:** a little over the worst normal round trip × 3 (SYN retries). 3 s is generous for most of the internet.
- **read:** longer than the slowest *legitimate* response from that endpoint, measured with p99 from this tool's own history.
- **deadline:** > connect + read. It's the promise you make to the scheduler.
- **interval:** ≥ deadline, or checks will be skipped while one is still running.

### Exercises
1. `python evals/attack_worker.py slowloris_body` then `python evals/attack_worker.py slowloris_body total_deadline`. The second hangs until you Ctrl-C it.
2. In `checker.py`, swap `read1` back to `for chunk in resp.iter_content(CHUNK_BYTES):` and run `pytest -k slowloris`. You've reproduced POSTMORTEM #1.

---

## Glossary
| Term | Plain English |
|---|---|
| Endpoint | One URL you can call (`/health`, `/orders/42`) |
| Latency | How long a request takes |
| SLO | Service level objective: the target you promise ("95% of requests under 300 ms") |
| Uptime / availability | Share of time (or checks) the service worked |
| MTTD / MTTR | Mean time to detect / to recover |
| Keep-alive | Reusing a connection for the next request |
| Idempotent | Safe to repeat: doing it twice = doing it once |
| Alert fatigue | So many false alarms that people stop reading alerts |
| Flapping | Rapidly alternating up/down, usually a borderline-healthy service |
| Slowloris | Holding a connection open by sending data very slowly |
| Thundering herd | Many clients acting at the same moment and overwhelming a server |
| Circuit breaker | Stop calling a dependency that's failing, try again later |
| Daemon thread | A background thread that doesn't stop the program from exiting |
| Monotonic clock | A clock that only moves forward; for measuring durations |
