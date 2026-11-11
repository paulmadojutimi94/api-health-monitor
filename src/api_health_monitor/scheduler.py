"""Run every endpoint's check on its own interval, forever, without drifting or piling up.

Built from the standard library only, so every scheduling decision is visible:

* A heap (priority queue) holds (next_due_time, endpoint). The loop always
  looks at the earliest one. With 500 endpoints that's still one cheap peek.
* Fixed-rate, not fixed-delay. The next run is due at `previous_due + interval`,
  not `finished + interval`, so a 30 s check doesn't creep to every 31.4 s.
  If we fall a whole interval behind, missed slots are skipped, not replayed
  in a burst.
* Each check runs in its own daemon thread so one slow endpoint can't delay
  the others. At most one check per endpoint is in flight; no pile-up.
* Watchdog: if a check runs past deadline + grace, we stop waiting, record
  DEADLINE_EXCEEDED, and keep going. Python can't kill a thread, so the stuck
  one is abandoned (daemon, so it can't block exit) and its late result ignored.
* All results are handled on this one thread: storage and state need no locks.
* time.monotonic() for scheduling (never jumps when the wall clock is
  adjusted); time.time() only for timestamps humans read.
"""

from __future__ import annotations

import heapq
import logging
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .checker import CheckResult, deadline_result
from .config import Endpoint

log = logging.getLogger(__name__)

WATCHDOG_GRACE_S = 1.0
MAX_WAIT_S = 0.5


@dataclass
class SchedulerStats:
    launched: int = 0
    completed: int = 0
    watchdog_fired: int = 0       # checks we gave up waiting for
    skipped_stuck: int = 0        # slots where the previous check was still stuck
    skipped_behind: int = 0       # slots skipped because we fell a whole interval behind
    late_results_ignored: int = 0
    callback_errors: int = 0
    lateness_ms: list[float] = field(default_factory=list)   # actual start - due time


@dataclass
class _InFlight:
    ep: Endpoint
    started_mono: float
    started_wall: float
    abandoned: bool = False


class Scheduler:
    def __init__(
        self,
        endpoints: list[Endpoint] | tuple[Endpoint, ...],
        check_fn: Callable[[Endpoint], CheckResult],
        on_result: Callable[[CheckResult], None],
        jitter: float = 0.1,
        seed: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.endpoints = list(endpoints)
        self.check_fn = check_fn
        self.on_result = on_result
        self.jitter = jitter
        self.clock = clock
        self.rng = random.Random(seed)
        self.stats = SchedulerStats()
        self._results: queue.Queue[tuple[int, int, CheckResult]] = queue.Queue()
        self._inflight: dict[int, _InFlight] = {}   # endpoint index -> running check
        self._run_id = 0
        self._current_run: dict[int, int] = {}       # endpoint index -> run id we're waiting on

    def _worker(self, i: int, run_id: int, ep: Endpoint) -> None:
        try:
            r = self.check_fn(ep)
        except Exception as exc:  # noqa: BLE001 - check() never raises, but be sure
            from .checker import Reason, Status
            r = CheckResult(ep.name, time.time(), Status.DOWN, Reason.UNEXPECTED_ERROR,
                            detail=f"{type(exc).__name__}: {exc}")
        self._results.put((i, run_id, r))

    def _deliver(self, r: CheckResult) -> None:
        try:
            self.on_result(r)
        except Exception:  # noqa: BLE001 - a storage hiccup must not stop monitoring
            self.stats.callback_errors += 1
            log.exception("on_result failed for %s", r.endpoint)

    def _launch(self, i: int, now: float) -> None:
        ep = self.endpoints[i]
        self._run_id += 1
        self._current_run[i] = self._run_id
        self._inflight[i] = _InFlight(ep, now, time.time())
        threading.Thread(target=self._worker, args=(i, self._run_id, ep),
                         name=f"check-{ep.name}", daemon=True).start()
        self.stats.launched += 1

    def run(self, stop: threading.Event | None = None, duration_s: float | None = None) -> SchedulerStats:
        stop = stop or threading.Event()
        start = self.clock()
        end = start + duration_s if duration_s else float("inf")
        # Spread first checks over the first interval so 100 endpoints don't all fire at t=0.
        heap = [(start + self.rng.uniform(0, ep.interval_s * self.jitter), i)
                for i, ep in enumerate(self.endpoints)]
        heapq.heapify(heap)

        while not stop.is_set():
            now = self.clock()
            if now >= end:
                break

            # 1) Launch everything that is due.
            while heap and heap[0][0] <= now:
                due, i = heapq.heappop(heap)
                ep = self.endpoints[i]
                nxt = due + ep.interval_s
                if nxt <= now:                      # fell behind: skip missed slots
                    missed = int((now - due) // ep.interval_s)
                    self.stats.skipped_behind += missed
                    nxt = due + (missed + 1) * ep.interval_s
                heapq.heappush(heap, (nxt, i))
                fl = self._inflight.get(i)
                if fl is not None:
                    if fl.abandoned:
                        # Previous check is still stuck: report it again rather than start a 2nd thread.
                        self.stats.skipped_stuck += 1
                        self._deliver(deadline_result(
                            ep, time.time(),
                            f"previous check still stuck after {now - fl.started_mono:.1f}s"))
                    continue                        # normal one still running: don't pile up
                self.stats.lateness_ms.append((now - due) * 1000)
                self._launch(i, now)

            # 2) Watchdog: give up on checks that blew far past their deadline.
            for i, fl in self._inflight.items():
                ep = fl.ep
                if (ep.defences.watchdog and not fl.abandoned
                        and now - fl.started_mono > ep.deadline_s + WATCHDOG_GRACE_S):
                    fl.abandoned = True
                    self.stats.watchdog_fired += 1
                    self._deliver(deadline_result(
                        ep, fl.started_wall,
                        f"no result after {ep.deadline_s + WATCHDOG_GRACE_S:g}s; check abandoned"))

            # 3) Sleep until the next thing to do, waking early if a result arrives.
            wake = heap[0][0] if heap else now + MAX_WAIT_S
            for fl in self._inflight.values():
                if fl.ep.defences.watchdog and not fl.abandoned:
                    wake = min(wake, fl.started_mono + fl.ep.deadline_s + WATCHDOG_GRACE_S)
            timeout = max(0.0, min(wake - now, end - now, MAX_WAIT_S))
            try:
                item = self._results.get(timeout=timeout)
            except queue.Empty:
                continue
            while True:
                i, run_id, r = item
                fl = self._inflight.get(i)
                if fl is not None and self._current_run.get(i) == run_id:
                    del self._inflight[i]
                    if fl.abandoned:
                        self.stats.late_results_ignored += 1
                    else:
                        self.stats.completed += 1
                        self._deliver(r)
                try:
                    item = self._results.get_nowait()
                except queue.Empty:
                    break
        return self.stats
