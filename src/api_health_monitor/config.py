"""Load and validate the monitor's configuration from a TOML file.

A config file has three parts:

    [defaults]      settings every endpoint inherits unless it overrides them
    [[endpoint]]    one block per URL to watch
    [alerts]        where alerts go (console, a JSONL file, a webhook)

Everything is turned into frozen dataclasses so the rest of the code can
trust the values: a bad timeout is rejected here, at start-up, not at 3 a.m.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when the config file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class Defences:
    """Safety features that protect the monitor from the endpoints it watches.

    All on by default. Each exists so the attack eval can switch one off and
    prove it is doing real work (see evals/eval_attacks.py).
    """

    body_cap: bool = True          # stop reading after max_body_bytes
    total_deadline: bool = True    # stop reading once deadline_s has passed
    socket_timeouts: bool = True   # connect/read timeouts on every socket call
    redirect_limit: bool = True    # follow at most max_redirects redirects
    sanitize_text: bool = True     # scrub server-controlled text before alerting
    watchdog: bool = True          # scheduler gives up on a stuck check thread


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str
    method: str = "GET"
    interval_s: float = 30.0
    connect_timeout_s: float = 3.0
    read_timeout_s: float = 5.0
    deadline_s: float = 10.0
    expect_status: tuple[int, ...] = (200,)
    expect_body: str | None = None
    latency_slo_ms: float = 1000.0
    failures_to_alert: int = 3
    successes_to_recover: int = 2
    max_body_bytes: int = 1_000_000
    max_redirects: int = 5
    realert_every_s: float = 0.0   # 0 = alert once per incident
    headers: tuple[tuple[str, str], ...] = ()
    defences: Defences = field(default_factory=Defences)

    def __post_init__(self) -> None:
        problems: list[str] = []
        if not self.name or not self.name.strip():
            problems.append("name is empty")
        parsed = urlparse(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            problems.append(f"url must be http(s)://host/..., got {self.url!r}")
        if self.method.upper() not in ("GET", "HEAD", "POST", "OPTIONS"):
            problems.append(f"method {self.method!r} not supported")
        for name in ("interval_s", "connect_timeout_s", "read_timeout_s", "deadline_s"):
            if getattr(self, name) <= 0:
                problems.append(f"{name} must be > 0")
        if self.deadline_s < self.connect_timeout_s:
            problems.append("deadline_s must be >= connect_timeout_s")
        if self.failures_to_alert < 1 or self.successes_to_recover < 1:
            problems.append("failures_to_alert and successes_to_recover must be >= 1")
        if self.max_body_bytes < 0 or self.max_redirects < 0:
            problems.append("max_body_bytes and max_redirects must be >= 0")
        if not self.expect_status or any(not 100 <= s <= 599 for s in self.expect_status):
            problems.append("expect_status must be a non-empty list of HTTP codes")
        if problems:
            raise ConfigError(f"endpoint {self.name!r}: " + "; ".join(problems))


@dataclass(frozen=True)
class AlertConfig:
    console: bool = True
    jsonl_path: str | None = None
    webhook_url: str | None = None
    webhook_format: str = "slack"   # slack | discord | generic

    def __post_init__(self) -> None:
        if self.webhook_format not in ("slack", "discord", "generic"):
            raise ConfigError("alerts.webhook_format must be slack, discord or generic")


@dataclass(frozen=True)
class Config:
    endpoints: tuple[Endpoint, ...]
    alerts: AlertConfig = field(default_factory=AlertConfig)
    db_path: str = "data/monitor.db"
    use_env_proxy: bool = True

    def __post_init__(self) -> None:
        if not self.endpoints:
            raise ConfigError("config has no [[endpoint]] blocks")
        names = [e.name for e in self.endpoints]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ConfigError(f"duplicate endpoint names: {', '.join(dupes)}")


_ENDPOINT_KEYS = {f.name for f in fields(Endpoint)} - {"defences"}


def _endpoint_from(raw: dict[str, Any], defaults: dict[str, Any]) -> Endpoint:
    merged = {**defaults, **raw}
    unknown = set(merged) - _ENDPOINT_KEYS
    if unknown:
        raise ConfigError(f"unknown endpoint setting(s): {', '.join(sorted(unknown))}")
    if "expect_status" in merged:
        merged["expect_status"] = tuple(int(s) for s in merged["expect_status"])
    if "headers" in merged:
        merged["headers"] = tuple(sorted((str(k), str(v)) for k, v in merged["headers"].items()))
    try:
        return Endpoint(**merged)
    except TypeError as exc:  # missing name/url
        raise ConfigError(f"endpoint {raw.get('name', '?')!r}: {exc}") from None


def parse_config(data: dict[str, Any]) -> Config:
    defaults = data.get("defaults", {})
    endpoints = tuple(_endpoint_from(raw, defaults) for raw in data.get("endpoint", []))
    a = data.get("alerts", {})
    alerts = AlertConfig(
        console=bool(a.get("console", True)),
        jsonl_path=a.get("jsonl") or None,
        webhook_url=a.get("webhook_url") or None,
        webhook_format=a.get("webhook_format", "slack"),
    )
    return Config(
        endpoints=endpoints,
        alerts=alerts,
        db_path=data.get("storage", {}).get("db_path", "data/monitor.db"),
        use_env_proxy=bool(data.get("network", {}).get("use_env_proxy", True)),
    )


def load_config(path: str | Path) -> Config:
    p = Path(path)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p}: invalid TOML: {exc}") from None
    return parse_config(data)


def with_defences(ep: Endpoint, **off: bool) -> Endpoint:
    """Copy of `ep` with some defences changed, e.g. with_defences(ep, body_cap=False)."""
    return replace(ep, defences=replace(ep.defences, **off))
