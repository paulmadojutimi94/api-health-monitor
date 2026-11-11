# Postmortem: what went wrong while building this, and how each was found

Blameless format: what happened, how we found out, root cause, impact, fix, and the guard that stops it coming back. Five incidents, in the order they happened.

---

## Incident 1: The deadline that couldn't fire (slowloris)

**What happened.** The first demo run included a "slowloris" endpoint that sends a response body 1 byte every 0.2 s, forever. Its deadline was 1.5 s. It was reported DOWN after **3.5 s**, and the alert text was `previous check still stuck after 3.5s`.

**How it was found.** That wording belongs to the *scheduler watchdog*, the last-resort safety net, not to the checker. A body drip is exactly what the checker's own deadline exists for, so the watchdog should never have been needed. A one-off script confirmed it: reading the drip with `iter_content(16384)` produced **zero chunks in 60 s** (the script itself had to be killed).

**Root cause.** The body loop was:
```python
for chunk in resp.iter_content(CHUNK_BYTES):
    body += chunk
    if time.perf_counter() > deadline: ...
```
`iter_content(16384)` doesn't return "whatever has arrived". It keeps reading until it has a **full** 16,384 bytes (or the stream ends). At 5 bytes/s that's ~55 minutes per chunk. Each individual socket read got a byte within 0.2 s, so the 1 s read timeout never fired either. The deadline check was correct code that never ran.

**Impact.** Without the watchdog, one hostile or broken endpoint would hold a thread forever and never be reported down. With it, detection was ~2 s late and a thread was stranded until the server gave up.

**Fix.** Read with `resp.raw.read1(CHUNK_BYTES, decode_content=True)`: `read1` returns as soon as *any* bytes are available (up to the limit). The deadline is now checked after every byte that trickles in.

**Guards.** `test_deadline_bounds_slowloris` (must finish < 1.6 s with a 1 s deadline); the attack eval re-runs slowloris with `total_deadline` switched off and shows it hanging past the 25 s kill switch; CLAUDE.md invariant 7. Demo now shows `deadline_exceeded ... latency=1603ms` and **0 watchdog rescues**.

**Lesson.** Layered defences hide bugs in the inner layers: the system "worked" because the outer layer caught it. Count how often each layer fires; a safety net that's catching things routinely means something inside is broken.

---

## Incident 2: A 6-second outage reported as 2 seconds

**What happened.** `test_recovery_needs_two_successes` fed the state machine `DOWN, DOWN, DOWN, UP, DOWN, UP, UP` and expected the incident to start at check 0. It started at check 4.

**How it was found.** The unit test, written from the spec before the code was trusted.

**Root cause, part 1.** The tracker records `first_bad_at` when a failure streak begins (`bad_streak == 0`). During an outage, a single successful check resets the streak to 0, so the next failure looked like the *start of a new incident* and overwrote the real start time. Real outages often have one lucky check in the middle (a retry that hits the one healthy pod).

**Root cause, part 2.** After fixing that, the test still failed: start = 6. The recovery code used `t.first_bad_at or now`. The test's first check was at time `0.0`, which Python treats as false, so it fell back to `now`. Real epoch timestamps are never 0, so this would never have shown up in production, but `x or default` on a number is a bug waiting for the right input.

**Impact.** Downtime and MTTR (mean time to recovery) understated, here by 3x, and in exactly the messy outages where people most need accurate numbers for the incident review.

**Fix.** Only set `first_bad_at` when not already DOWN; the same fix for the DEGRADED start. Replaced every `x or now` with `_or(x, now)`, which tests `is None`.

**Guards.** The test above plus `test_degraded_cycle`; end-to-end, the detection eval now measures incident-length error: **p50 0.32 s, p95 0.77 s**.

---

## Incident 3: The 44 ms "regression" that was really the kernel

**What happened.** The performance eval reported that checks over a **reused** (keep-alive) connection took **44 ms**, while checks that opened a **new** connection each time took **1.1 ms**. Latency measurement error was also ~44 ms at every injected delay. Reusing connections is supposed to be the *fast* path.

**How it was found.** The numbers were backwards, and the error was the same ~42–44 ms regardless of the injected delay (0, 50, 100, 250, 500 ms). A constant offset that ignores the workload points below the application, at the network stack.

**Root cause.** A classic: **Nagle's algorithm + delayed ACK.** Python's `http.server` writes the headers and the body as two separate small `send()`s. Nagle's algorithm holds back a small packet while an earlier one is unacknowledged; the client's TCP stack deliberately delays its ACK (up to ~40 ms on Linux) hoping to piggyback it on a reply. Each side waits for the other. A brand-new connection starts in "quick ACK" mode, which is why it was fast.

**Impact.** Only the test server was affected, but every latency number in the evals was wrong by ~44 ms. The monitor measured correctly: that server really was 44 ms slow. The same bug exists in real services, and is a good example of what a latency monitor is *for*.

**Fix.** `disable_nagle_algorithm = True` on the chaos server's handler (sets `TCP_NODELAY`). Keep-alive checks: 44 ms → **0.5 ms**; measurement error: 44 ms → **p95 2.1 ms**.

**Lesson.** When an effect is constant and independent of your input, look a layer down.

---

## Incident 4: The eval's own server looked like an outage

**What happened.** Classification eval: 298/300. The two misses were *healthy* endpoints reported as `connect_timeout` at exactly 500 ms (the connect timeout).

**How it was found.** Looking at *which* trials failed rather than accepting 99.3%: both were on `/ok`, early in the run, when 8 checks run in parallel.

**Root cause.** `ThreadingHTTPServer`'s default accept queue (`request_queue_size`) is **5**. Eight simultaneous connection attempts overflowed it and Linux silently dropped the extra handshake packets (SYN), exactly what a firewall or a dead host does. The client waited for the full connect timeout.

**Impact.** Eval accuracy understated. More interestingly, it's a faithful reproduction of a real failure: an overloaded API whose accept queue is full *looks unreachable* from outside, not slow. A monitor reporting `connect_timeout` on a host you know is up should make you check its connection backlog.

**Fix.** `request_queue_size = 128` on the chaos server. Classification: **300/300**.

---

## Incident 5: Invisible characters in our own tests

**What happened.** The first run of `scripts/check_invisible_chars.py` failed on `tests/test_alerts_storage_config.py` lines 28–29: two raw U+202E (RIGHT-TO-LEFT OVERRIDE) characters.

**Root cause.** The test checks that `clean_text` strips U+202E, and the character had been written into the source *as itself* rather than as the escape `\u202e`. Same story earlier in `sanitize.py` with a zero-width space, caught by a grep before the script existed. A raw U+202E reverses the display of everything after it on the line, so code can look different from what runs ("Trojan Source", CVE-2021-42574).

**Fix.** Escapes, not raw characters. The script now runs in CI on every `.py`, `.md`, `.toml` and `.yml` file.

**Lesson.** Code that defends against hostile text must itself be checked for hostile text.

---

## What went well
- Every incident was found by a test, an eval or a demo, not in production.
- Building the chaos server first meant every failure mode had a reproducible switch.
- The ablation eval turned "we have defences" into "each defence stops these specific attacks".

## Action items
| Item | Status |
|---|---|
| Body reads via `read1`; CLAUDE.md invariant | Done |
| No `x or default` for numbers in incidents.py | Done |
| Count watchdog rescues in every run summary | Done (demo prints it) |
| Invisible-character check in CI | Done |
| Process-per-check option to close the header-drip thread leak | Next step |
