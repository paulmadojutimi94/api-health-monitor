"""Deliver alerts to people, without letting a slow alert channel stall monitoring.

Alerting is itself a network call, and Slack can be down too. If we posted
webhooks inline, a hanging Slack would freeze every health check. So alerts
go on a queue and a separate background thread delivers them. Each channel
gets a timeout, one retry, and its failures are counted, never raised.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

import requests

from .config import AlertConfig
from .incidents import Event, EventKind
from .sanitize import clean_text, discord_safe, escape_chat

log = logging.getLogger(__name__)

ICONS = {
    EventKind.DOWN: "🔴 DOWN",
    EventKind.STILL_DOWN: "🔴 STILL DOWN",
    EventKind.RECOVERED: "✅ RECOVERED",
    EventKind.DEGRADED: "🟡 SLOW",
    EventKind.DEGRADED_RESOLVED: "✅ FAST AGAIN",
}


def _fmt_duration(s: float) -> str:
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def format_text(ev: Event, sanitize: bool = True) -> str:
    """One human-readable line. Everything server-controlled goes through clean_text."""
    r = ev.result
    clean = clean_text if sanitize else (lambda x, limit=0: str(x))
    parts = [f"{ICONS[ev.kind]} {ev.endpoint}"]
    if ev.kind in (EventKind.DOWN, EventKind.STILL_DOWN, EventKind.DEGRADED):
        parts.append(f"reason={r.reason.value}")
        if r.http_status:
            parts.append(f"http={r.http_status}")
        if r.latency_ms is not None:
            parts.append(f"latency={r.latency_ms:.0f}ms")
        if r.detail:
            parts.append(f"detail={clean(r.detail)}")
    if ev.kind in (EventKind.RECOVERED, EventKind.DEGRADED_RESOLVED, EventKind.STILL_DOWN):
        parts.append(f"after {_fmt_duration(ev.duration_s)}")
    parts.append(clean(ev.url))
    return " | ".join(parts)


class Alerter(Protocol):
    name: str

    def send(self, ev: Event) -> None: ...


class ConsoleAlerter:
    name = "console"

    def __init__(self, stream: TextIO | None = None, sanitize: bool = True):
        self.stream = stream or sys.stdout
        self.sanitize = sanitize

    def send(self, ev: Event) -> None:
        ts = time.strftime("%H:%M:%S", time.localtime(ev.at))
        print(f"[{ts}] ALERT {format_text(ev, self.sanitize)}", file=self.stream, flush=True)


class JsonlAlerter:
    """Append-only alert log: easy to grep, easy to feed into anything else."""

    name = "jsonl"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, ev: Event) -> None:
        r = ev.result
        rec = {
            "kind": ev.kind.value, "endpoint": ev.endpoint, "url": ev.url,
            "at": ev.at, "incident_started_at": ev.incident_started_at,
            "duration_s": round(ev.duration_s, 3), "reason": r.reason.value,
            "http_status": r.http_status, "latency_ms": r.latency_ms,
            "detail": clean_text(r.detail),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def webhook_payload(ev: Event, fmt: str, sanitize: bool = True) -> dict:
    text = format_text(ev, sanitize)
    if fmt == "slack":
        return {"text": escape_chat(text) if sanitize else text}
    if fmt == "discord":
        return {"content": discord_safe(text) if sanitize else text,
                "allowed_mentions": {"parse": []}}
    return {"text": text, "kind": ev.kind.value, "endpoint": ev.endpoint,
            "reason": ev.result.reason.value, "at": ev.at}


class WebhookAlerter:
    """POST JSON to Slack / Discord / anything that accepts a webhook."""

    name = "webhook"

    def __init__(self, url: str, fmt: str = "slack", timeout=(3.0, 5.0), retries: int = 1,
                 session: requests.Session | None = None, sanitize: bool = True):
        self.url, self.fmt, self.timeout, self.retries = url, fmt, timeout, retries
        self.session = session or requests.Session()
        self.sanitize = sanitize

    def send(self, ev: Event) -> None:
        payload = webhook_payload(ev, self.fmt, self.sanitize)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.session.post(self.url, json=payload, timeout=self.timeout)
                if r.status_code < 300:
                    return
                if r.status_code < 500 and r.status_code != 429:
                    raise RuntimeError(f"webhook rejected alert: HTTP {r.status_code}")
                last = RuntimeError(f"webhook HTTP {r.status_code}")
            except requests.RequestException as exc:
                last = exc
            if attempt < self.retries:
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"webhook failed after {self.retries + 1} attempts: {last}")


@dataclass
class DispatchStats:
    sent: int = 0
    failed: int = 0
    dropped: int = 0


class AlertDispatcher:
    """Queue + background thread. `publish()` returns immediately, always."""

    def __init__(self, alerters: list[Alerter], max_queue: int = 1000):
        self.alerters = alerters
        self.stats = DispatchStats()
        self._q: queue.Queue[Event | None] = queue.Queue(maxsize=max_queue)
        self._thread = threading.Thread(target=self._run, name="alert-dispatcher", daemon=True)
        self._thread.start()

    def publish(self, ev: Event) -> None:
        try:
            self._q.put_nowait(ev)
        except queue.Full:
            self.stats.dropped += 1
            log.error("alert queue full; dropped %s alert for %s", ev.kind.value, ev.endpoint)

    def _run(self) -> None:
        while True:
            ev = self._q.get()
            if ev is None:
                return
            for a in self.alerters:
                try:
                    a.send(ev)
                    self.stats.sent += 1
                except Exception as exc:  # noqa: BLE001 - one bad channel must not stop the others
                    self.stats.failed += 1
                    log.error("alert channel %s failed: %s", a.name, clean_text(exc))

    def close(self, timeout: float = 10.0) -> None:
        """Deliver what's queued (up to `timeout` seconds), then stop."""
        try:
            self._q.put(None, timeout=1)
        except queue.Full:
            pass
        self._thread.join(timeout)


def build_alerters(cfg: AlertConfig) -> list[Alerter]:
    out: list[Alerter] = []
    if cfg.console:
        out.append(ConsoleAlerter())
    if cfg.jsonl_path:
        out.append(JsonlAlerter(cfg.jsonl_path))
    if cfg.webhook_url:
        out.append(WebhookAlerter(cfg.webhook_url, cfg.webhook_format))
    return out
