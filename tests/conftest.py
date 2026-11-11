import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api_health_monitor.chaos_server import ChaosServer  # noqa: E402
from api_health_monitor.config import Endpoint  # noqa: E402


@pytest.fixture(scope="session")
def chaos():
    srv = ChaosServer(seed=42).start()
    yield srv
    srv.stop()


@pytest.fixture
def fast_ep():
    """Factory for endpoints with short timeouts so tests stay quick."""
    def make(url: str, **kw) -> Endpoint:
        base = dict(name=kw.pop("name", "t"), url=url, interval_s=0.2, connect_timeout_s=0.5,
                    read_timeout_s=0.8, deadline_s=1.2, latency_slo_ms=300)
        base.update(kw)
        return Endpoint(**base)
    return make
