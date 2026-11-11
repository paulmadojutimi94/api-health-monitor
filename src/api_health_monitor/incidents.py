"""Turn a stream of check results into alerts, without crying wolf.

One failed check is not an outage: networks drop packets, a deploy restarts a
pod, a garbage collector pauses. Alerting on every failure trains people to
ignore alerts. So each endpoint has a small state machine:

    UP ──(N failures in a row)──▶ DOWN ──(M successes in a row)──▶ UP
     │                                                              ▲
     └──(N slow checks in a row)──▶ DEGRADED ──(M fast checks)──────┘

N = failures_to_alert, M = successes_to_recover. Alerts fire only on
transitions, so a 2-hour outage is one "down" alert and one "recovered"
alert, not 240 pages. The incident's start time is the *first* failing check,
not the moment we became sure, so reported downtime isn't understated.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .checker import CheckResult, Status
from .config import Endpoint


class State(str, Enum):
    UNKNOWN = "unknown"
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class EventKind(str, Enum):
    DOWN = "down"
    STILL_DOWN = "still_down"      # optional reminder (realert_every_s)
    RECOVERED = "recovered"
    DEGRADED = "degraded"
    DEGRADED_RESOLVED = "degraded_resolved"


@dataclass(frozen=True)
class Event:
    kind: EventKind
    endpoint: str
    url: str
    at: float                     # when we decided (epoch seconds)
    incident_started_at: float    # first bad check of this incident
    result: CheckResult           # the check that triggered the event

    @property
    def duration_s(self) -> float:
        return max(0.0, self.at - self.incident_started_at)


@dataclass
class _Track:
    state: State = State.UNKNOWN
    bad_streak: int = 0
    good_streak: int = 0
    slow_streak: int = 0
    fast_streak: int = 0
    first_bad_at: float | None = None
    first_slow_at: float | None = None
    last_alert_at: float = 0.0


def _or(value: float | None, fallback: float) -> float:
    # Not `value or fallback`: a timestamp of 0.0 is falsy but perfectly valid.
    return fallback if value is None else value


class IncidentTracker:
    def __init__(self, endpoints: list[Endpoint] | tuple[Endpoint, ...]):
        self._eps = {e.name: e for e in endpoints}
        self._tracks = {e.name: _Track() for e in endpoints}

    def state(self, name: str) -> State:
        return self._tracks[name].state

    def observe(self, r: CheckResult) -> list[Event]:
        ep = self._eps[r.endpoint]
        t = self._tracks[r.endpoint]
        now = r.started_at
        events: list[Event] = []

        def emit(kind: EventKind, since: float) -> None:
            events.append(Event(kind, ep.name, ep.url, now, since, r))
            t.last_alert_at = now

        if r.status is Status.DOWN:
            t.good_streak = 0
            t.slow_streak = t.fast_streak = 0
            t.first_slow_at = None
            if t.bad_streak == 0 and t.state is not State.DOWN:
                t.first_bad_at = now   # mid-outage blip: keep the original start (POSTMORTEM #2)
            t.bad_streak += 1
            if t.state is not State.DOWN and t.bad_streak >= ep.failures_to_alert:
                t.state = State.DOWN
                emit(EventKind.DOWN, _or(t.first_bad_at, now))
            elif (t.state is State.DOWN and ep.realert_every_s > 0
                  and now - t.last_alert_at >= ep.realert_every_s):
                emit(EventKind.STILL_DOWN, _or(t.first_bad_at, now))
            return events

        # Reachable (UP or DEGRADED)
        t.bad_streak = 0
        if t.state is State.DOWN:
            t.good_streak += 1
            if t.good_streak >= ep.successes_to_recover:
                emit(EventKind.RECOVERED, _or(t.first_bad_at, now))
                t.state = State.UP
                t.good_streak = 0
                t.first_bad_at = None
            return events
        if t.state is State.UNKNOWN:
            t.state = State.UP   # first contact: no alert, nothing to recover from

        if r.status is Status.DEGRADED:
            t.fast_streak = 0
            if t.slow_streak == 0 and t.state is State.UP:
                t.first_slow_at = now   # in DEGRADED, keep the original start
            t.slow_streak += 1
            if t.state is State.UP and t.slow_streak >= ep.failures_to_alert:
                t.state = State.DEGRADED
                emit(EventKind.DEGRADED, _or(t.first_slow_at, now))
        else:
            t.slow_streak = 0
            if t.state is State.DEGRADED:
                t.fast_streak += 1
                if t.fast_streak >= ep.successes_to_recover:
                    emit(EventKind.DEGRADED_RESOLVED, _or(t.first_slow_at, now))
                    t.state = State.UP
                    t.fast_streak = 0
                    t.first_slow_at = None
        return events
