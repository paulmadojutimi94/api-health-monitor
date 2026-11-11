import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from api_health_monitor.alerts import (AlertDispatcher, ConsoleAlerter, JsonlAlerter, WebhookAlerter,
                                       format_text, webhook_payload)
from api_health_monitor.chaos_server import INJECT_REASON
from api_health_monitor.checker import CheckResult, Reason, Status
from api_health_monitor.config import ConfigError, Endpoint, parse_config
from api_health_monitor.incidents import Event, EventKind
from api_health_monitor.report import html_page, markdown, table
from api_health_monitor.sanitize import clean_text
from api_health_monitor.storage import Store, percentile


def event(kind=EventKind.DOWN, detail="HTTP 503", at=10.0, start=4.0, name="api"):
    r = CheckResult(name, at, Status.DOWN, Reason.HTTP_STATUS, http_status=503, latency_ms=12.0, detail=detail)
    return Event(kind, name, "http://x/", at, start, r)


# ---------- sanitize / alert text ----------

def test_clean_text_strips_controls_and_truncates():
    s = clean_text("a\x1b[2Jb\u202ec" + "z" * 500)
    assert "\x1b" not in s and "\u202e" not in s
    assert len(s) <= 200


def test_slack_payload_neutralises_mentions():
    p = webhook_payload(event(detail=f"HTTP 500 {INJECT_REASON}"), "slack")
    assert "<!channel>" not in p["text"] and "&lt;!channel&gt;" in p["text"]
    assert "\x1b" not in p["text"]


def test_discord_payload_blocks_pings():
    p = webhook_payload(event(detail="@everyone look"), "discord")
    assert p["allowed_mentions"] == {"parse": []}
    assert "@everyone" not in p["content"]


def test_format_text_recovered_has_duration():
    assert "after 6s" in format_text(event(EventKind.RECOVERED))


def test_console_and_jsonl(tmp_path):
    buf = io.StringIO()
    ConsoleAlerter(buf).send(event())
    assert "DOWN api" in buf.getvalue()
    path = tmp_path / "a.jsonl"
    JsonlAlerter(path).send(event())
    rec = json.loads(path.read_text())
    assert rec["kind"] == "down" and rec["duration_s"] == 6.0


class _Hook(BaseHTTPRequestHandler):
    received: list = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if _Hook.mode == "hang":
            time.sleep(3)
        _Hook.received.append(json.loads(body))
        code = 500 if _Hook.mode == "500" else 200
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def hook():
    _Hook.received, _Hook.mode = [], "ok"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def _no_proxy_session():
    import requests
    s = requests.Session()
    s.trust_env = False
    return s


def test_webhook_delivers(hook):
    WebhookAlerter(hook, "slack", session=_no_proxy_session()).send(event())
    assert _Hook.received and "DOWN api" in _Hook.received[0]["text"]


def test_webhook_retries_then_raises(hook):
    _Hook.mode = "500"
    with pytest.raises(RuntimeError):
        WebhookAlerter(hook, "slack", retries=1, session=_no_proxy_session()).send(event())
    assert len(_Hook.received) == 2


def test_dispatcher_publish_never_blocks_on_slow_webhook(hook):
    _Hook.mode = "hang"
    slow = WebhookAlerter(hook, "slack", timeout=(0.5, 0.5), retries=0, session=_no_proxy_session())
    d = AlertDispatcher([slow])
    t = time.perf_counter()
    for _ in range(5):
        d.publish(event())
    assert time.perf_counter() - t < 0.05
    d.close(timeout=5)
    assert d.stats.failed >= 1


def test_one_broken_channel_does_not_stop_others():
    class Boom:
        name = "boom"

        def send(self, ev):
            raise OSError("disk full")

    buf = io.StringIO()
    d = AlertDispatcher([Boom(), ConsoleAlerter(buf)])
    d.publish(event())
    d.close()
    assert d.stats.failed == 1 and d.stats.sent == 1 and "DOWN" in buf.getvalue()


# ---------- storage ----------

def test_percentile_nearest_rank():
    vals = list(range(1, 101))
    assert percentile(vals, 50) == 50
    assert percentile(vals, 95) == 95
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 50) is None


def test_store_uptime_and_incidents():
    st = Store()
    for t in range(10):
        status = Status.DOWN if 3 <= t <= 5 else Status.UP
        st.record_check(CheckResult("api", 1000.0 + t, status,
                                    Reason.OK if status is Status.UP else Reason.HTTP_STATUS, latency_ms=10.0 + t))
    st.record_event(event(EventKind.DOWN, at=1005.0, start=1003.0))
    st.record_event(event(EventKind.RECOVERED, at=1007.0, start=1003.0))
    (row,) = st.uptime(now=1010.0)
    assert row.checks == 10 and row.up_checks == 7 and row.uptime_pct == 70.0
    assert row.incidents == 1 and row.downtime_s == 4.0 and row.mttr_s == 4.0
    assert "api" in table([row]) and "| api |" in markdown([row])
    page = html_page([row], st)
    assert "<script" not in page and "Recent incidents" in page


def test_html_report_escapes_names():
    st = Store()
    st.record_check(CheckResult("<script>x</script>", 1.0, Status.UP, Reason.OK, latency_ms=1.0))
    assert "<script>x" not in html_page(st.uptime(now=2.0), st)


# ---------- config ----------

def test_config_defaults_inherit_and_validate():
    cfg = parse_config({"defaults": {"interval_s": 10, "failures_to_alert": 2},
                        "endpoint": [{"name": "a", "url": "https://a.example/health"},
                                     {"name": "b", "url": "https://b.example", "interval_s": 5}]})
    assert [e.interval_s for e in cfg.endpoints] == [10, 5]
    assert all(e.failures_to_alert == 2 for e in cfg.endpoints)


@pytest.mark.parametrize("bad", [
    {"endpoint": []},
    {"endpoint": [{"name": "a", "url": "ftp://x"}]},
    {"endpoint": [{"name": "a", "url": "http://x", "read_timeout_s": 0}]},
    {"endpoint": [{"name": "a", "url": "http://x", "typo_setting": 1}]},
    {"endpoint": [{"name": "a", "url": "http://x"}, {"name": "a", "url": "http://y"}]},
    {"endpoint": [{"name": "a", "url": "http://x", "expect_status": [999]}]},
    {"endpoint": [{"url": "http://x"}]},
])
def test_config_rejects_bad_input(bad):
    with pytest.raises(ConfigError):
        parse_config(bad)


def test_example_config_loads():
    from pathlib import Path

    from api_health_monitor.config import load_config
    cfg = load_config(Path(__file__).resolve().parents[1] / "monitor.example.toml")
    assert len(cfg.endpoints) >= 3
    assert isinstance(cfg.endpoints[0], Endpoint)
