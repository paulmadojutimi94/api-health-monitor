PY ?= python3
export PYTHONPATH := src

.PHONY: install test demo check report eval readme lint video

install:          ## install the package and dev tools
	$(PY) -m pip install -e ".[dev]"

test:             ## run the test suite (~20 s)
	$(PY) -m pytest -q

demo:             ## watch it catch planted outages on a local chaos server (30 s)
	$(PY) -m api_health_monitor demo

check:            ## check every endpoint in monitor.toml once
	$(PY) -m api_health_monitor check -c monitor.toml

report:           ## uptime report as HTML from the demo database
	$(PY) -m api_health_monitor report --db data/demo.db --format html -o reports/status.html

eval:             ## re-run every eval (~5 min) and rewrite the README results
	$(PY) evals/run_all.py

readme:           ## rebuild README results from evals/results without re-running
	$(PY) evals/run_all.py --readme

lint:
	$(PY) -m ruff check src tests evals
	$(PY) scripts/check_invisible_chars.py

video:            ## rebuild docs/demo/api-health-monitor-demo.mp4
	$(PY) docs/demo/make_video.py
