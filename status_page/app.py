from __future__ import annotations

import atexit
from datetime import UTC, datetime
import os

from fasthtml.common import *
from starlette.responses import JSONResponse
import uvicorn

from status_page.config import ConfigError, MonitorConfig, load_config
from status_page.engine import MonitorEngine


CSS = """
:root {
  --bg: #f8fafc;
  --card: #ffffff;
  --text: #0f172a;
  --muted: #64748b;
  --border: #e2e8f0;
  --green: #16a34a;
  --yellow: #ca8a04;
  --red: #dc2626;
  --grey: #94a3b8;
  --error-bg: #fee2e2;
  --error-text: #7f1d1d;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b1220;
    --card: #111827;
    --text: #e5e7eb;
    --muted: #94a3b8;
    --border: #243041;
    --green: #22c55e;
    --yellow: #f59e0b;
    --red: #ef4444;
    --grey: #6b7280;
    --error-bg: #7f1d1d;
    --error-text: #fecaca;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  color: var(--text);
  background: var(--bg);
}
.container { max-width: 1100px; margin: 0 auto; padding: 1rem; }
.header { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; }
.title { margin: .25rem 0; font-size: 1.6rem; font-weight: 700; }
.small { color: var(--muted); font-size: .9rem; }
.refresh-wrap { margin-top: .35rem; max-width: 320px; }
.refresh-track { height: 4px; width: 100%; background: color-mix(in srgb, var(--muted) 25%, transparent); border-radius: 999px; overflow: hidden; }
.refresh-bar { height: 100%; width: 100%; background: var(--text); transform-origin: left center; transform: scaleX(1); transition: transform 1s linear; opacity: .4; }
.service { background: var(--card); border: 1px solid var(--border); border-radius: 10px; margin-top: 1rem; padding: .9rem; }
.service-top { display: flex; justify-content: space-between; gap: .8rem; align-items: baseline; flex-wrap: wrap; }
.badge { border-radius: 999px; padding: .2rem .6rem; font-size: .8rem; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; }
.state-green { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green); }
.state-yellow { background: color-mix(in srgb, var(--yellow) 18%, transparent); color: var(--yellow); }
.state-red { background: color-mix(in srgb, var(--red) 16%, transparent); color: var(--red); }
.state-grey { background: color-mix(in srgb, var(--grey) 16%, transparent); color: var(--grey); }
.timeline { margin-top: .8rem; display: flex; gap: 2px; align-items: end; min-height: 44px; overflow: hidden; }
.bar { flex: 1 1 0; min-width: 0; height: 40px; border-radius: 2px; opacity: .95; }
.bar.green { background: var(--green); }
.bar.yellow { background: var(--yellow); }
.bar.red { background: var(--red); }
.bar.grey { background: var(--grey); }
.checks { margin-top: .75rem; display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: .5rem; }
.check { background: transparent; border: 1px solid var(--border); border-radius: 8px; padding: .5rem .6rem; }
.empty { margin-top: 1rem; background: var(--card); border: 1px dashed var(--border); border-radius: 10px; padding: 1rem; color: var(--muted); }
.error { margin: 1rem 0; color: var(--error-text); background: var(--error-bg); border: 1px solid color-mix(in srgb, var(--error-text) 18%, transparent); padding: .8rem; border-radius: 8px; }
@media (max-width: 700px) {
  .title { font-size: 1.35rem; }
  .timeline { min-height: 34px; gap: 1px; }
  .bar { height: 30px; }
}
"""

COUNTDOWN_JS = """
(() => {
    const refreshEl = document.getElementById('refresh-val');
    const nextEl = document.getElementById('next-refresh');
    const progressEl = document.getElementById('refresh-progress');
    let total = 30;
    let remaining = 30;
    let timer = null;

    const readRefresh = () => {
        const parsed = Number.parseInt(refreshEl?.textContent ?? '30', 10);
        return Number.isFinite(parsed) && parsed > 0 ? Math.max(5, parsed) : 30;
    };

    const paint = () => {
        if (nextEl) nextEl.textContent = String(Math.max(0, remaining));
        if (progressEl) {
            const pct = total > 0 ? remaining / total : 0;
            progressEl.style.transform = `scaleX(${pct})`;
        }
    };

    const start = () => {
        total = readRefresh();
        remaining = total;
        if (timer) window.clearInterval(timer);
        paint();
        timer = window.setInterval(() => {
            remaining = Math.max(0, remaining - 1);
            paint();
            if (remaining <= 0) remaining = total;
        }, 1000);
    };

    window.addEventListener('DOMContentLoaded', start);
    document.body.addEventListener('htmx:afterSwap', (evt) => {
        if (evt?.detail?.target?.id === 'services') start();
    });
})();
"""

def _format_ts(ts: int | None) -> str:
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


_STATE_LABELS: dict[str, str] = {
    "green": "Operational",
    "yellow": "Degraded",
    "red": "Outage",
    "grey": "Unknown",
}

def _state_badge(state: str):
    safe = state if state in _STATE_LABELS else "grey"
    return Span(_STATE_LABELS[safe], cls=f"badge state-{safe}")


def _bucket_title(bucket: dict) -> str:
    state = bucket.get("state", "grey")
    uptime = bucket.get("uptime_pct")
    sla = bucket.get("sla")
    label = _STATE_LABELS.get(state, "Unknown")
    if uptime is None:
        return label
    if sla is not None:
        return f"{label} — {uptime:.1f}% uptime (SLA: {sla:.1f}%)"
    return f"{label} — {uptime:.1f}% uptime"


def _service_card(service: dict):
    checks = service.get("checks", [])
    buckets = service.get("buckets", [])
    return Article(
        Div(
            Div(
                Strong(service.get("name", "Unnamed service")),
                Div(service.get("summary", ""), cls="small"),
            ),
            Div(
                _state_badge(str(service.get("state", "grey"))),
                Div(_format_ts(service.get("checked_at")), cls="small"),
            ),
            cls="service-top",
        ),
        Div(
            *[
                Div(cls=f"bar {b.get('state', 'grey')}", title=_bucket_title(b))
                for b in buckets
            ],
            cls="timeline",
        ),
        Div(
            *[
                Div(
                    Div(f"{chk.get('check_type', 'check')} \u2022 {chk.get('duration_ms', 0)}ms", cls="small"),
                    Div(str(chk.get("message", ""))),
                    cls="check",
                )
                for chk in checks
            ],
            cls="checks",
        ),
        cls="service",
    )


def _services_fragment(snapshot: dict):
    services = snapshot.get("services", [])
    if not services:
        return Div("No services configured yet. Add services to your YAML config.", cls="empty", id="services")
    return Div(*[_service_card(service) for service in services], id="services")


def _status_oob_fragments(snapshot: dict):
    refresh_seconds = int(snapshot.get("refresh_seconds", 30))
    generated_at = _format_ts(snapshot.get("generated_at"))
    return (
        Span(str(refresh_seconds), id="refresh-val", hx_swap_oob="outerHTML"),
        Span(generated_at, id="updated-at", hx_swap_oob="outerHTML"),
    )


def _create_app() -> tuple[object, MonitorEngine, str | None]:
    try:
        config = load_config()
        engine = MonitorEngine(config)
        engine.start()
        error = None
    except ConfigError as exc:
        config = MonitorConfig()
        engine = MonitorEngine(config)
        engine.start()
        error = str(exc)

    app, rt = fast_app(hdrs=(Style(CSS),))

    @rt("/")
    def get_index():
        snapshot = engine.snapshot()
        refresh_seconds = snapshot.get("refresh_seconds", engine.config.ui.refresh_seconds)
        page_title = snapshot.get("title", "Service Status")

        content = [
            Header(
                Div(
                    H1(page_title, cls="title"),
                    Div(
                        "24h availability timeline • auto refresh every ",
                        Span(str(refresh_seconds), id="refresh-val"),
                        "s • next refresh in ",
                        Span(str(max(5, int(refresh_seconds))), id="next-refresh"),
                        "s",
                        cls="small",
                    ),
                    Div(Div(id="refresh-progress", cls="refresh-bar"), cls="refresh-track refresh-wrap"),
                ),
                Div("Last updated: ", Span(_format_ts(snapshot.get("generated_at")), id="updated-at"), cls="small"),
                cls="header",
            )
        ]

        if error:
            content.append(Div(f"Config error: {error}", cls="error"))

        content.extend(
            [
                _services_fragment(snapshot),
                Div(
                    id="poller",
                    hx_get="/fragments/status",
                    hx_trigger=f"every {max(5, int(refresh_seconds))}s",
                    hx_target="#services",
                    hx_swap="outerHTML",
                    style="display:none",
                ),
                Script(COUNTDOWN_JS),
            ]
        )
        return Title(page_title), Main(*content, cls="container")

    @rt("/fragments/status")
    def get_status_fragment():
        snapshot = engine.snapshot()
        return _services_fragment(snapshot), *_status_oob_fragments(snapshot)

    @rt("/api/status")
    def get_status():
        return JSONResponse(engine.snapshot())

    return app, engine, error


app, _engine, _error = _create_app()
atexit.register(_engine.stop)


def main() -> None:
    host = os.environ.get("STATUS_PAGE_HOST", "0.0.0.0")
    port = int(os.environ.get("STATUS_PAGE_PORT", "8080"))
    uvicorn.run("status_page.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
