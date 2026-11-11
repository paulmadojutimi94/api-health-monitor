"""Shared helpers for the evals."""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
RESULTS = ROOT / "evals" / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

from api_health_monitor.storage import percentile  # noqa: E402,F401


def save(name: str, data: dict) -> Path:
    data = {**data, "machine": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}"}
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {path.relative_to(ROOT)}")
    return path
