# Demo: 3-minute walkthrough

**Video:** [demo/api-health-monitor-demo.mp4](demo/api-health-monitor-demo.mp4) (3:06, silent slides; read the narration below over it, or record your own voice-over).
**Rebuild it:** `make video`. Every number on screen is read from `examples/demo-output.txt` and `evals/results/*.json`, so the video can't drift from the code.

## Timed narration script

| Time | On screen | Say |
|---|---|---|
| 0:00–0:12 | Title | "This is an API health monitor I built in Python. It checks endpoints, measures latency, detects failures, sends alerts and records uptime. And it survives the APIs it's watching, which turned out to be the interesting part." |
| 0:12–0:28 | What does "down" look like? | "'Down' isn't one thing. A crashed server refuses you in a millisecond. A dead host gives you silence. A broken app gives you a 503. The nasty ones are a 200 OK that's actually a maintenance page, and a server trickling bytes so slowly that no timeout ever fires." |
| 0:28–0:46 | How it works | "A scheduler keeps a heap of when each endpoint is next due: fixed rate, so it doesn't drift, with jitter. Each check runs in its own thread using Requests with a connect timeout, a read timeout, a total deadline and a 1 MB body cap. Results feed a state machine: three failures in a row means DOWN, two successes means RECOVERED. Alerts go out on their own thread, and everything lands in SQLite." |
| 0:46–1:26 | `make demo` output scrolling | "Here's a live run against a local server I built to fail on purpose. The slowloris endpoint is cut off at 1.6 seconds. Auth gets slow, so we get a SLOW alert, not a DOWN. Payments returns 503. Search returns 200 OK, and we still catch it, because the body says maintenance. Each one recovers and reports how long it lasted. Notice flaky-api: it fails 15% of the time at random and never pages anyone." |
| 1:26–1:42 | Classification | "Does a single check name the right cause? 300 out of 300, across 15 failure types: refused, DNS, connect timeout, hang, slowloris, gzip bomb, redirect loop. And no check ever ran meaningfully past its deadline." |
| 1:42–2:00 | Catching outages | "Twenty-four planted outages, twenty-four caught. Zero one-check blips paged anyone. This is the key trade-off: alert on a single failure and a 2%-flaky API pages you 57 times a day. Wait for three in a row and it's zero, for about 75 seconds of detection time on a 30-second interval." |
| 2:00–2:18 | Latency and cost | "Latency is measured to within about 2 milliseconds. With 40 endpoints and 15 of them hanging forever, every healthy check still ran on time. Cost: no API fees, about half a millisecond of CPU per check." |
| 2:18–2:36 | Bonus: timeouts | "Nine hostile-endpoint attacks, zero succeed. Then I switch each defence off to prove it matters: without the deadline, slowloris holds the check forever. Without the redirect limit, one check made ten thousand requests." |
| 2:36–2:54 | Postmortem | "My favourite bug: the deadline existed but never fired. iter_content waits for a full 16 KB chunk, and at 5 bytes a second that's about 55 minutes. The watchdog was quietly saving me. Switching to read1, which returns whatever has arrived, fixed it." |
| 2:54–3:06 | Try it | "Clone it, pip install requests, make demo. The README has the numbers, the design doc has the alternatives I rejected, and the cheatsheet explains every line." |

## Recording tips
- Run `make demo` live in a terminal for the 0:46 segment if you'd rather show the real thing than the slide. It takes 30 s and the output matches.
- Open `examples/status.html` to show the HTML status page.
