"""A local HTTP server that misbehaves on purpose, so every failure mode can be tested offline.

    /ok                    200 "status: healthy"
    /status/<code>         that status code
    /delay/<ms>            wait, then 200
    /hang                  accept, then say nothing (until the server stops)
    /drip                  send headers, then 1 byte every 0.2 s, forever  (slowloris)
    /header-drip           send the status line, then headers 1 byte every 0.2 s
    /big                   200 with a 50 MB body
    /gzip-bomb             ~100 KB on the wire, 100 MB once decompressed
    /redirect-loop         302 to itself, forever
    /chunked               200 "status: healthy" sent with chunked transfer-encoding
    /maintenance           200, but the body says "down for maintenance"
    /inject                500 whose reason phrase tries Slack mentions and terminal escapes
    /flaky/<pct>           fails pct% of requests (seeded, reproducible)
    /scenario/<name>       follows a scripted timeline set with set_timeline()

Run it alone with:  python -m api_health_monitor.chaos_server --port 8099
"""

from __future__ import annotations

import argparse
import gzip
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INJECT_REASON = "Oops <!channel> <@U0ADMIN> @everyone \x1b[2J\x1b[31mlog in at http://evil.example"  # latin-1 only: it goes in the status line

# A timeline is a list of (start_s, end_s, mode) relative to server start.
# mode: "ok" | "500" | "503" | "hang" | "slow" | "maintenance"
Timeline = list[tuple[float, float, str]]


class ChaosServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, seed: int = 0, slow_ms: int = 600):
        self.timelines: dict[str, Timeline] = {}
        self.t0 = time.monotonic()
        self.stopping = threading.Event()
        self.rng = random.Random(seed)
        self.rng_lock = threading.Lock()
        self.slow_ms = slow_ms
        self._gzip_bomb: bytes | None = None
        self.requests = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # Send small writes immediately. Without this, keep-alive responses sat ~40 ms
            # in the kernel (Nagle's algorithm vs. delayed ACK): see docs/POSTMORTEM.md #3.
            disable_nagle_algorithm = True

            def log_message(self, *a):  # keep test output quiet
                pass

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                server.requests += 1
                try:
                    server.route(self)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass   # the client hung up on us: exactly what timeouts are for

        class QuietServer(ThreadingHTTPServer):
            # Default accept queue is 5. Parallel evals overflowed it, and the kernel
            # silently dropped new connections: looked exactly like an API outage.
            request_queue_size = 128

            def handle_error(self, request, client_address):
                pass   # clients hanging up mid-request is the normal case here

        self.httpd = QuietServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = f"http://{host}:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "ChaosServer":
        self.t0 = time.monotonic()
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def set_timeline(self, name: str, timeline: Timeline) -> None:
        self.timelines[name] = timeline

    def now(self) -> float:
        return time.monotonic() - self.t0

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _send(h, code: int, body: bytes = b"", reason: str | None = None,
              headers: dict[str, str] | None = None) -> None:
        h.send_response(code, reason)
        h.send_header("Content-Type", "text/plain; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)

    def _hang(self) -> None:
        self.stopping.wait(120)

    def _mode(self, h, mode: str) -> None:
        if mode == "ok":
            self._send(h, 200, b"status: healthy")
        elif mode in ("500", "502", "503", "504"):
            self._send(h, int(mode), b"upstream unavailable")
        elif mode == "hang":
            self._hang()
        elif mode == "slow":
            time.sleep(self.slow_ms / 1000)
            self._send(h, 200, b"status: healthy")
        elif mode == "maintenance":
            self._send(h, 200, b"down for maintenance")
        else:
            self._send(h, 500, f"unknown mode {mode}".encode())

    # -- routes ----------------------------------------------------------
    def route(self, h) -> None:
        parts = [p for p in h.path.split("?")[0].split("/") if p]
        head = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else ""

        if head == "ok":
            self._send(h, 200, b"status: healthy")
        elif head == "status":
            self._send(h, int(arg or 500), b"status page says no")
        elif head == "delay":
            time.sleep(int(arg or 0) / 1000)
            self._send(h, 200, b"status: healthy")
        elif head == "hang":
            self._hang()
        elif head == "drip":
            h.send_response(200)
            h.send_header("Content-Length", str(10_000_000))
            h.end_headers()
            while not self.stopping.is_set():
                h.wfile.write(b"x")
                h.wfile.flush()
                time.sleep(0.2)
        elif head == "header-drip":
            h.wfile.write(b"HTTP/1.1 200 OK\r\n")
            h.wfile.flush()
            while not self.stopping.is_set():
                h.wfile.write(b"X-Slow: " + b"a" * 1 + b"\r\n")
                h.wfile.flush()
                time.sleep(0.2)
        elif head == "big":
            size = 50 * 1024 * 1024
            h.send_response(200)
            h.send_header("Content-Length", str(size))
            h.end_headers()
            block = b"0" * 65536
            for _ in range(size // len(block)):
                if self.stopping.is_set():
                    break
                h.wfile.write(block)
        elif head == "gzip-bomb":
            if self._gzip_bomb is None:
                self._gzip_bomb = gzip.compress(b"\0" * (100 * 1024 * 1024), compresslevel=9)
            self._send(h, 200, self._gzip_bomb, headers={"Content-Encoding": "gzip"})
        elif head == "redirect-loop":
            self._send(h, 302, b"", headers={"Location": "/redirect-loop"})
        elif head == "chunked":
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for piece in (b"status: ", b"heal", b"thy"):
                h.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            h.wfile.write(b"0\r\n\r\n")
        elif head == "maintenance":
            self._send(h, 200, b"down for maintenance")
        elif head == "inject":
            self._send(h, 500, b"<!channel> \x1b[2J", reason=INJECT_REASON)
        elif head == "flaky":
            with self.rng_lock:
                fail = self.rng.random() * 100 < float(arg or 10)
            self._send(h, 503 if fail else 200, b"flake" if fail else b"status: healthy")
        elif head == "scenario":
            mode = "ok"
            t = self.now()
            for start, end, m in self.timelines.get(arg, []):
                if start <= t < end:
                    mode = m
                    break
            self._mode(h, mode)
        else:
            self._send(h, 404, b"no such chaos route")


class Blackhole:
    """A port that never completes a TCP handshake, to produce real connect timeouts.

    listen(0) gives a tiny accept queue; we fill it with our own half-open
    connections and never call accept(). Linux then silently drops new SYN
    packets, which is exactly what a firewalled or crashed host looks like.
    """

    def __init__(self, host: str = "127.0.0.1"):
        import socket
        self._srv = socket.socket()
        self._srv.bind((host, 0))
        self._srv.listen(0)
        self.port = self._srv.getsockname()[1]
        self.url = f"http://{host}:{self.port}/"
        self._fillers = []
        for _ in range(4):
            c = socket.socket()
            c.setblocking(False)
            try:
                c.connect((host, self.port))
            except BlockingIOError:
                pass
            self._fillers.append(c)
        time.sleep(0.05)

    def close(self) -> None:
        for c in self._fillers:
            c.close()
        self._srv.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def closed_port_url(host: str = "127.0.0.1") -> str:
    """A URL on a port nothing listens on: connection refused, instantly."""
    import socket
    s = socket.socket()
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://{host}:{port}/"


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the misbehaving test server.")
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    srv = ChaosServer(port=args.port).start()
    print(f"chaos server on {srv.base}  (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
