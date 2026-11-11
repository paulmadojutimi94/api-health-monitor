"""One health check: send a request, time it, and say what happened.

`check()` never raises. Whatever the endpoint does (hangs, refuses, sends
garbage, sends 50 GB) it comes back as a CheckResult with a status and a
precise reason. That is the whole point of a monitor: the thing being
watched is allowed to break, the watcher is not.
"""

from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass
from enum import Enum
from collections.abc import Iterator

import requests
from requests.exceptions import (
    ConnectTimeout,
    InvalidURL,
    MissingSchema,
    InvalidSchema,
    ReadTimeout,
    SSLError,
    TooManyRedirects,
)

from .config import Endpoint
from .sanitize import clean_text

USER_AGENT = "api-health-monitor/0.1 (+https://github.com/paulmadojutimi94/api-health-monitor)"
CHUNK_BYTES = 16 * 1024


class Status(str, Enum):
    UP = "up"              # reachable, right status, right body, fast enough
    DEGRADED = "degraded"  # works, but slower than the latency SLO
    DOWN = "down"          # anything else


class Reason(str, Enum):
    OK = "ok"
    SLOW = "slow"
    HTTP_STATUS = "http_status"              # got an answer, wrong code (e.g. 503)
    BODY_MISMATCH = "body_mismatch"          # 200, but the expected text is missing
    CONNECT_TIMEOUT = "connect_timeout"      # no TCP handshake in time
    READ_TIMEOUT = "read_timeout"            # connected, then silence
    DEADLINE_EXCEEDED = "deadline_exceeded"  # data kept trickling, whole check too slow
    CONNECTION_REFUSED = "connection_refused"  # host up, nothing listening on the port
    DNS_FAILURE = "dns_failure"              # name doesn't resolve
    TLS_ERROR = "tls_error"                  # bad/expired certificate, handshake failed
    TOO_MANY_REDIRECTS = "too_many_redirects"
    BODY_TOO_LARGE = "body_too_large"
    CONNECTION_ERROR = "connection_error"    # reset, dropped, other network trouble
    INVALID_URL = "invalid_url"
    UNEXPECTED_ERROR = "unexpected_error"    # a bug in us, caught so we keep running


@dataclass(frozen=True)
class CheckResult:
    endpoint: str
    started_at: float           # wall-clock epoch seconds (for humans and storage)
    status: Status
    reason: Reason
    http_status: int | None = None
    latency_ms: float | None = None   # request start -> last body byte
    ttfb_ms: float | None = None      # request start -> response headers
    bytes_read: int = 0
    detail: str = ""

    @property
    def is_up(self) -> bool:
        return self.status is not Status.DOWN


def make_session(ep: Endpoint | None = None, use_env_proxy: bool = True) -> requests.Session:
    """A Session reuses TCP/TLS connections between checks (keep-alive).

    trust_env=False ignores HTTP(S)_PROXY variables, which matters when a
    corporate proxy would otherwise sit between you and the API you're testing.
    """
    s = requests.Session()
    s.trust_env = use_env_proxy
    s.headers["User-Agent"] = USER_AGENT
    if ep is not None:
        s.max_redirects = ep.max_redirects if ep.defences.redirect_limit else 10_000
    return s


def _causes(exc: BaseException) -> Iterator[BaseException]:
    """Walk an exception and everything it wraps.

    requests wraps urllib3 errors, which wrap socket errors, so "connection
    refused" is three layers down. We look through .reason, __cause__,
    __context__ and args to find the real culprit.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        for nxt in (getattr(e, "reason", None), e.__cause__, e.__context__, *getattr(e, "args", ())):
            if isinstance(nxt, BaseException):
                stack.append(nxt)


def classify_exception(exc: BaseException) -> Reason:
    # Order matters: most specific first.
    if isinstance(exc, TooManyRedirects):
        return Reason.TOO_MANY_REDIRECTS
    if isinstance(exc, (MissingSchema, InvalidSchema, InvalidURL)):
        return Reason.INVALID_URL
    if isinstance(exc, ConnectTimeout):
        return Reason.CONNECT_TIMEOUT
    if isinstance(exc, ReadTimeout):
        return Reason.READ_TIMEOUT
    if isinstance(exc, SSLError):
        return Reason.TLS_ERROR
    for cause in _causes(exc):
        name = type(cause).__name__
        if isinstance(cause, socket.gaierror) or name == "NameResolutionError":
            return Reason.DNS_FAILURE
        if isinstance(cause, ConnectionRefusedError):
            return Reason.CONNECTION_REFUSED
        if isinstance(cause, ssl.SSLError):
            return Reason.TLS_ERROR
        if isinstance(cause, (socket.timeout, TimeoutError)) or name in ("ReadTimeoutError",):
            return Reason.READ_TIMEOUT
        if name == "ConnectTimeoutError":
            return Reason.CONNECT_TIMEOUT
    if isinstance(exc, requests.RequestException):
        return Reason.CONNECTION_ERROR
    return Reason.UNEXPECTED_ERROR


def check(ep: Endpoint, session: requests.Session | None = None) -> CheckResult:
    """Run one check against `ep`. Never raises."""
    own_session = session is None
    session = session or make_session(ep)
    started_at = time.time()
    t0 = time.perf_counter()          # perf_counter: monotonic, high resolution
    deadline = t0 + ep.deadline_s
    d = ep.defences

    def elapsed_ms() -> float:
        return (time.perf_counter() - t0) * 1000

    def result(status: Status, reason: Reason, **kw) -> CheckResult:
        if "latency_ms" not in kw:
            kw["latency_ms"] = round(elapsed_ms(), 2)
        if "detail" in kw:
            kw["detail"] = clean_text(kw["detail"]) if d.sanitize_text else str(kw["detail"])
        return CheckResult(ep.name, started_at, status, reason, **kw)

    timeout = (ep.connect_timeout_s, ep.read_timeout_s) if d.socket_timeouts else None
    resp = None
    try:
        resp = session.request(
            ep.method,
            ep.url,
            timeout=timeout,          # (connect, read) - see LEARN.md "Timeouts"
            stream=True,              # don't download the body yet: we read it ourselves
            headers=dict(ep.headers),
            allow_redirects=True,
        )
        ttfb = round(elapsed_ms(), 2)

        # Read the body ourselves so we can stop on size or time.
        # read1() returns as soon as *any* bytes arrive. iter_content() waits for a
        # full chunk, so a server dripping 1 byte every 0.2 s never let our deadline
        # check run (see docs/POSTMORTEM.md, incident 1).
        body = bytearray()
        too_large = False
        while True:
            chunk = resp.raw.read1(CHUNK_BYTES, decode_content=True)
            if not chunk:
                break
            body += chunk
            if d.body_cap and len(body) > ep.max_body_bytes:
                too_large = True
                break
            if d.total_deadline and time.perf_counter() > deadline:
                return result(
                    Status.DOWN, Reason.DEADLINE_EXCEEDED,
                    http_status=resp.status_code, ttfb_ms=ttfb, bytes_read=len(body),
                    detail=f"still receiving after {ep.deadline_s:g}s ({len(body)} bytes so far)",
                )
        latency = round(elapsed_ms(), 2)
        common = dict(http_status=resp.status_code, ttfb_ms=ttfb, latency_ms=latency, bytes_read=len(body))

        if too_large:
            return result(Status.DOWN, Reason.BODY_TOO_LARGE,
                          detail=f"body exceeded {ep.max_body_bytes} bytes", **common)
        if resp.status_code not in ep.expect_status:
            return result(Status.DOWN, Reason.HTTP_STATUS,
                          detail=f"HTTP {resp.status_code} {resp.reason or ''}", **common)
        if ep.expect_body is not None:
            text = body.decode(resp.encoding or "utf-8", errors="replace")
            if ep.expect_body not in text:
                return result(Status.DOWN, Reason.BODY_MISMATCH,
                              detail=f"expected text {ep.expect_body!r} not in body", **common)
        if latency > ep.latency_slo_ms:
            return result(Status.DEGRADED, Reason.SLOW,
                          detail=f"{latency:.0f} ms > SLO {ep.latency_slo_ms:.0f} ms", **common)
        return result(Status.UP, Reason.OK, **common)

    except Exception as exc:  # noqa: BLE001 - a monitor must survive anything
        reason = classify_exception(exc)
        return result(Status.DOWN, reason, detail=f"{type(exc).__name__}: {exc}")
    finally:
        if resp is not None:
            resp.close()       # give the socket back (or drop it if we stopped mid-body)
        if own_session:
            session.close()


def deadline_result(ep: Endpoint, started_at: float, note: str) -> CheckResult:
    """Result the scheduler records when it gives up waiting on a stuck check."""
    return CheckResult(
        ep.name, started_at, Status.DOWN, Reason.DEADLINE_EXCEEDED,
        latency_ms=None, detail=clean_text(note),
    )
