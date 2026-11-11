"""Uptime reports: a terminal table, Markdown, and a self-contained HTML status page."""

from __future__ import annotations

import html
import time

from .sanitize import clean_text
from .storage import Store, UptimeRow

STATUS_DOT = {"up": "🟢", "degraded": "🟡", "down": "🔴", None: "⚪"}


def _ms(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}%"


def _dur(s: float | None) -> str:
    if s is None:
        return "-"
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.1f}m"


HEADERS = ("Endpoint", "Now", "Uptime", "Checks", "p50 ms", "p95 ms", "Incidents", "Downtime", "MTTR")


def _cells(r: UptimeRow) -> tuple[str, ...]:
    return (clean_text(r.endpoint, 40), r.last_status or "-", _pct(r.uptime_pct), str(r.checks),
            _ms(r.p50_ms), _ms(r.p95_ms), str(r.incidents), _dur(r.downtime_s), _dur(r.mttr_s))


def table(rows: list[UptimeRow]) -> str:
    data = [HEADERS] + [_cells(r) for r in rows]
    widths = [max(len(row[c]) for row in data) for c in range(len(HEADERS))]
    lines = []
    for n, row in enumerate(data):
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def markdown(rows: list[UptimeRow], title: str = "Uptime report") -> str:
    out = [f"# {title}", "", f"Generated {time.strftime('%Y-%m-%d %H:%M:%S %Z')}", "",
           "| " + " | ".join(HEADERS) + " |", "|" + "---|" * len(HEADERS)]
    for r in rows:
        cells = list(_cells(r))
        cells[0] = cells[0].replace("|", "\\|")
        cells[1] = f"{STATUS_DOT.get(r.last_status, '⚪')} {cells[1]}"
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def html_page(rows: list[UptimeRow], store: Store | None = None, title: str = "Status") -> str:
    def esc(s: object) -> str:
        return html.escape(str(s), quote=True)

    body_rows = []
    for r in rows:
        c = _cells(r)
        body_rows.append(
            f'<tr class="{esc(r.last_status or "unknown")}"><td>{STATUS_DOT.get(r.last_status, "⚪")} '
            + "</td><td>".join(esc(x) for x in c) + "</td></tr>")
    incidents_html = ""
    if store is not None:
        items = []
        for inc in store.incidents()[-20:][::-1]:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(inc["started_at"]))
            dur = _dur((inc["resolved_at"] or time.time()) - inc["started_at"])
            state = "ongoing" if inc["resolved_at"] is None else f"lasted {dur}"
            items.append(f"<li><b>{esc(inc['endpoint'])}</b> {esc(inc['kind'])} at {esc(when)}"
                         f" ({esc(inc['reason'])}, {esc(state)})</li>")
        incidents_html = "<h2>Recent incidents</h2><ul>" + ("".join(items) or "<li>None</li>") + "</ul>"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#1d1d1f}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:.45rem .6rem;border-bottom:1px solid #e5e5ea;text-align:left;white-space:nowrap}}
th{{font-weight:600;color:#6e6e73}}tr.down td{{background:#fff1f0}}tr.degraded td{{background:#fffbe6}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}th,td{{border-color:#333}}tr.down td{{background:#3a1717}}tr.degraded td{{background:#3a3417}}}}
</style></head><body>
<h1>{esc(title)}</h1><p>Generated {esc(time.strftime('%Y-%m-%d %H:%M:%S'))}</p>
<div style="overflow-x:auto"><table><tr><th></th>{''.join(f'<th>{esc(h)}</th>' for h in HEADERS)}</tr>
{''.join(body_rows)}</table></div>
{incidents_html}
</body></html>
"""
