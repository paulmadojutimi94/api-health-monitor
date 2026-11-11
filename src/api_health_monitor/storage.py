"""Record every check and every incident in SQLite, and compute uptime from them.

SQLite is a whole SQL database in one file, built into Python. No server to
run, survives restarts, and you can open it with any SQL tool to ask your own
questions. Only the scheduler's main thread writes, so no locking is needed.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .checker import CheckResult
from .incidents import Event, EventKind

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id          INTEGER PRIMARY KEY,
    endpoint    TEXT    NOT NULL,
    ts          REAL    NOT NULL,     -- epoch seconds
    status      TEXT    NOT NULL,     -- up | degraded | down
    reason      TEXT    NOT NULL,
    http_status INTEGER,
    latency_ms  REAL,
    ttfb_ms     REAL,
    bytes       INTEGER,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS ix_checks_endpoint_ts ON checks(endpoint, ts);

CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY,
    endpoint    TEXT NOT NULL,
    kind        TEXT NOT NULL,        -- down | degraded
    started_at  REAL NOT NULL,        -- first bad check
    detected_at REAL NOT NULL,        -- when the alert fired
    resolved_at REAL,                 -- NULL while ongoing
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS ix_incidents_endpoint ON incidents(endpoint, started_at);
"""


@dataclass(frozen=True)
class UptimeRow:
    endpoint: str
    checks: int
    up_checks: int
    uptime_pct: float | None
    p50_ms: float | None
    p95_ms: float | None
    incidents: int
    downtime_s: float
    mttr_s: float | None          # mean time to recovery, closed incidents only
    last_status: str | None


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: the value that p% of samples are at or below."""
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))
    return s[k]


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")   # readers don't block the writer
        self.db.executescript(SCHEMA)

    def record_check(self, r: CheckResult) -> None:
        self.db.execute(
            "INSERT INTO checks(endpoint, ts, status, reason, http_status, latency_ms, ttfb_ms, bytes, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (r.endpoint, r.started_at, r.status.value, r.reason.value, r.http_status,
             r.latency_ms, r.ttfb_ms, r.bytes_read, r.detail),
        )
        self.db.commit()

    def record_event(self, ev: Event) -> None:
        if ev.kind in (EventKind.DOWN, EventKind.DEGRADED):
            self.db.execute(
                "INSERT INTO incidents(endpoint, kind, started_at, detected_at, reason) VALUES (?,?,?,?,?)",
                (ev.endpoint, ev.kind.value, ev.incident_started_at, ev.at, ev.result.reason.value),
            )
        elif ev.kind in (EventKind.RECOVERED, EventKind.DEGRADED_RESOLVED):
            kind = "down" if ev.kind is EventKind.RECOVERED else "degraded"
            self.db.execute(
                "UPDATE incidents SET resolved_at=? WHERE id = (SELECT id FROM incidents"
                " WHERE endpoint=? AND kind=? AND resolved_at IS NULL ORDER BY started_at DESC LIMIT 1)",
                (ev.at, ev.endpoint, kind),
            )
        self.db.commit()

    def incidents(self, endpoint: str | None = None) -> list[dict]:
        q = "SELECT endpoint, kind, started_at, detected_at, resolved_at, reason FROM incidents"
        args: tuple = ()
        if endpoint:
            q += " WHERE endpoint=?"
            args = (endpoint,)
        cols = ("endpoint", "kind", "started_at", "detected_at", "resolved_at", "reason")
        return [dict(zip(cols, row)) for row in self.db.execute(q + " ORDER BY started_at", args)]

    def uptime(self, window_s: float | None = None, now: float | None = None) -> list[UptimeRow]:
        """Uptime per endpoint = share of checks that were reachable (UP or DEGRADED)."""
        now = now or time.time()
        since = now - window_s if window_s else 0.0
        rows: list[UptimeRow] = []
        names = [n for (n,) in self.db.execute(
            "SELECT DISTINCT endpoint FROM checks WHERE ts >= ? ORDER BY endpoint", (since,))]
        for name in names:
            total, up = self.db.execute(
                "SELECT COUNT(*), SUM(status != 'down') FROM checks WHERE endpoint=? AND ts>=?",
                (name, since)).fetchone()
            lats = [v for (v,) in self.db.execute(
                "SELECT latency_ms FROM checks WHERE endpoint=? AND ts>=? AND status!='down'"
                " AND latency_ms IS NOT NULL", (name, since))]
            incs = self.db.execute(
                "SELECT started_at, resolved_at FROM incidents WHERE endpoint=? AND kind='down'"
                " AND COALESCE(resolved_at, ?) >= ?", (name, now, since)).fetchall()
            downtime = sum((r or now) - max(s, since) for s, r in incs)
            closed = [r - s for s, r in incs if r is not None]
            last = self.db.execute(
                "SELECT status FROM checks WHERE endpoint=? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
            rows.append(UptimeRow(
                endpoint=name, checks=total, up_checks=up or 0,
                uptime_pct=round(100 * (up or 0) / total, 3) if total else None,
                p50_ms=percentile(lats, 50), p95_ms=percentile(lats, 95),
                incidents=len(incs), downtime_s=round(downtime, 1),
                mttr_s=round(sum(closed) / len(closed), 1) if closed else None,
                last_status=last[0] if last else None,
            ))
        return rows

    def close(self) -> None:
        self.db.close()
