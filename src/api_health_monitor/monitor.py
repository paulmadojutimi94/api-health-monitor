"""Wire the parts together: scheduler -> checker -> store + incident tracker -> alerts."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .alerts import AlertDispatcher, Alerter, build_alerters
from .checker import CheckResult, check, make_session
from .config import Config, Endpoint
from .incidents import Event, IncidentTracker
from .scheduler import Scheduler, SchedulerStats
from .storage import Store


@dataclass
class RunSummary:
    stats: SchedulerStats
    events: list[Event]
    alerts_sent: int
    alerts_failed: int


class Monitor:
    def __init__(self, cfg: Config, store: Store | None = None,
                 alerters: list[Alerter] | None = None, seed: int | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.tracker = IncidentTracker(cfg.endpoints)
        self.dispatcher = AlertDispatcher(alerters if alerters is not None else build_alerters(cfg.alerts))
        # One Session per endpoint: keep-alive between its checks, and never shared
        # between two running threads (the scheduler allows one check per endpoint).
        self._sessions = {e.name: make_session(e, cfg.use_env_proxy) for e in cfg.endpoints}
        self.events: list[Event] = []
        self.results: list[CheckResult] = []
        self.scheduler = Scheduler(cfg.endpoints, self._check, self.handle_result, seed=seed)

    def _check(self, ep: Endpoint) -> CheckResult:
        return check(ep, self._sessions[ep.name])

    def handle_result(self, r: CheckResult) -> None:
        self.results.append(r)
        self.store.record_check(r)
        for ev in self.tracker.observe(r):
            self.events.append(ev)
            self.store.record_event(ev)
            self.dispatcher.publish(ev)

    def run(self, stop: threading.Event | None = None, duration_s: float | None = None) -> RunSummary:
        try:
            stats = self.scheduler.run(stop, duration_s)
        finally:
            self.dispatcher.close()
            for s in self._sessions.values():
                s.close()
        return RunSummary(stats, self.events, self.dispatcher.stats.sent, self.dispatcher.stats.failed)
