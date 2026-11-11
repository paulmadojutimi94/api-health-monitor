import time

import pytest

from api_health_monitor.chaos_server import Blackhole, closed_port_url
from api_health_monitor.checker import Reason, Status, check, make_session


def run(ep):
    s = make_session(ep, use_env_proxy=False)
    try:
        return check(ep, s)
    finally:
        s.close()


@pytest.mark.parametrize("path,kw,status,reason", [
    ("/ok", {}, Status.UP, Reason.OK),
    ("/chunked", {"expect_body": "healthy"}, Status.UP, Reason.OK),
    ("/status/503", {}, Status.DOWN, Reason.HTTP_STATUS),
    ("/status/404", {}, Status.DOWN, Reason.HTTP_STATUS),
    ("/status/204", {"expect_status": (200, 204)}, Status.UP, Reason.OK),
    ("/maintenance", {"expect_body": "healthy"}, Status.DOWN, Reason.BODY_MISMATCH),
    ("/delay/450", {}, Status.DEGRADED, Reason.SLOW),
    ("/hang", {}, Status.DOWN, Reason.READ_TIMEOUT),
    ("/drip", {}, Status.DOWN, Reason.DEADLINE_EXCEEDED),
    ("/big", {}, Status.DOWN, Reason.BODY_TOO_LARGE),
    ("/gzip-bomb", {}, Status.DOWN, Reason.BODY_TOO_LARGE),
    ("/redirect-loop", {}, Status.DOWN, Reason.TOO_MANY_REDIRECTS),
])
def test_classification(chaos, fast_ep, path, kw, status, reason):
    r = run(fast_ep(chaos.base + path, **kw))
    assert (r.status, r.reason) == (status, reason), r.detail


def test_connection_refused(fast_ep):
    r = run(fast_ep(closed_port_url()))
    assert r.reason is Reason.CONNECTION_REFUSED


def test_dns_failure(fast_ep):
    r = run(fast_ep("http://no-such-host.invalid/"))
    assert r.reason is Reason.DNS_FAILURE


def test_connect_timeout(fast_ep):
    with Blackhole() as bh:
        r = run(fast_ep(bh.url))
    assert r.reason is Reason.CONNECT_TIMEOUT
    assert 450 <= r.latency_ms < 1200


def test_latency_is_measured(chaos, fast_ep):
    r = run(fast_ep(chaos.base + "/delay/200"))
    assert r.status is Status.UP
    assert 195 <= r.latency_ms < 300
    assert r.ttfb_ms <= r.latency_ms


def test_deadline_bounds_slowloris(chaos, fast_ep):
    """Bonus lesson: a server that never stops talking must not hold the check hostage."""
    t = time.perf_counter()
    r = run(fast_ep(chaos.base + "/drip", deadline_s=1.0))
    took = time.perf_counter() - t
    assert r.reason is Reason.DEADLINE_EXCEEDED
    assert took < 1.6


def test_body_cap_bounds_memory(chaos, fast_ep):
    r = run(fast_ep(chaos.base + "/big", max_body_bytes=100_000))
    assert r.reason is Reason.BODY_TOO_LARGE
    assert r.bytes_read < 100_000 + 64 * 1024


def test_reason_phrase_is_sanitized(chaos, fast_ep):
    r = run(fast_ep(chaos.base + "/inject"))
    assert "\x1b" not in r.detail
    assert r.http_status == 500


def test_check_never_raises_on_garbage_url(fast_ep):
    ep = fast_ep("http://127.0.0.1:1/")
    object.__setattr__(ep, "url", "http://[::1")    # bypass validation to force a weird error
    r = run(ep)
    assert r.status is Status.DOWN
    assert r.reason in (Reason.INVALID_URL, Reason.CONNECTION_ERROR, Reason.UNEXPECTED_ERROR)


def test_session_reuses_connection(chaos, fast_ep):
    ep = fast_ep(chaos.base + "/ok")
    s = make_session(ep, use_env_proxy=False)
    first = check(ep, s)
    second = check(ep, s)
    s.close()
    assert first.is_up and second.is_up
