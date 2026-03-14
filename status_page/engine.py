from __future__ import annotations

import threading
import time
from typing import Any

from status_page.checks import CheckContext, run_check
from status_page.config import MonitorConfig, ServiceConfig
from status_page.models import STATE_PRIORITY, CheckResult, ServiceResult, State
from status_page.storage import Storage, summarize_checks


class MonitorEngine:
    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.storage = Storage(config.storage.path)
        self._latest: dict[str, ServiceResult] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_run: dict[str, float] = {svc.id: 0.0 for svc in config.services}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="status-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            now = time.time()
            for service in self.config.services:
                if now >= self._next_run.get(service.id, 0):
                    result = self._run_service(service)
                    with self._lock:
                        self._latest[service.id] = result
                    self.storage.insert_service_result(result)
                    self._next_run[service.id] = now + service.interval_seconds

            self.storage.prune_old(self.config.storage.retention_hours)
            self._stop_event.wait(1.0)

    def _run_service(self, service: ServiceConfig) -> ServiceResult:
        checks: list[CheckResult] = []
        now = int(time.time())

        for raw_check in service.checks:
            timeout = float(raw_check.get("timeout_seconds", 10.0))
            checks.append(run_check(raw_check, CheckContext(timeout_seconds=timeout)))

        state = self._derive_state(checks)
        summary = summarize_checks(checks)
        return ServiceResult(
            service_id=service.id,
            name=service.name,
            state=state,
            summary=summary,
            checked_at=now,
            checks=checks,
        )

    @staticmethod
    def _derive_state(checks: list[CheckResult]) -> State:
        if not checks:
            return State.GREY
        worst = max(checks, key=lambda c: STATE_PRIORITY[c.state])
        return worst.state

    def _effective_sla(self, check_config: dict[str, Any]) -> float:
        raw_sla = check_config.get("sla", self.config.default_sla)
        try:
            sla = float(raw_sla)
        except (TypeError, ValueError):
            sla = self.config.default_sla
        return max(0.0, min(100.0, sla))

    def snapshot(self) -> dict[str, Any]:
        bucket_minutes = self.config.ui.bucket_minutes
        bucket_size = bucket_minutes * 60
        since_ts = int(time.time()) - (24 * 3600)
        bucket_count = max(1, (24 * 3600) // bucket_size)

        latest_persisted = self.storage.latest_for_services()

        services: list[dict[str, Any]] = []
        with self._lock:
            latest_memory = dict(self._latest)

        for service in self.config.services:
            result = latest_memory.get(service.id)
            persisted = latest_persisted.get(service.id)

            if result:
                checks = [
                    {
                        "check_type": chk.check_type,
                        "state": chk.state.value,
                        "message": chk.message,
                        "duration_ms": chk.duration_ms,
                        "detail": chk.detail,
                    }
                    for chk in result.checks
                ]
                checked_at = result.checked_at
                probe_summary = result.summary
            elif persisted:
                checks = [
                    {
                        "check_type": c["check_type"],
                        "state": State.GREEN.value if c["passed"] else State.RED.value,
                        "message": c["message"],
                        "duration_ms": c["duration_ms"],
                        "detail": {},
                    }
                    for c in persisted["checks"]
                ]
                checked_at = persisted["checked_at"]
                probe_summary = summarize_checks(
                    [
                        CheckResult(
                            check_type=c["check_type"],
                            state=State.GREEN if c["passed"] else State.RED,
                            message=c["message"],
                            duration_ms=c["duration_ms"],
                        )
                        for c in persisted["checks"]
                    ]
                )
            else:
                checks = []
                checked_at = None
                probe_summary = "No data collected yet"

            # Per-check bucket data: {check_index: [(passed_count, total_count), ...]}
            check_bucket_data = self.storage.bucket_uptimes(service.id, since_ts, bucket_size, bucket_count)

            buckets = []
            for bucket_idx in range(bucket_count):
                worst = State.GREY
                worst_uptime: float | None = None
                worst_sla: float | None = None
                for chk_idx, raw_check in enumerate(service.checks):
                    sla = self._effective_sla(raw_check)
                    per_check = check_bucket_data.get(chk_idx)
                    if per_check is None:
                        continue
                    passed_count, total_count = per_check[bucket_idx]
                    if total_count == 0:
                        continue
                    uptime_pct = (passed_count / total_count) * 100.0
                    if uptime_pct >= sla:
                        state = State.GREEN
                    elif passed_count > 0:
                        state = State.YELLOW
                    else:
                        state = State.RED
                    if STATE_PRIORITY[state] > STATE_PRIORITY[worst]:
                        worst = state
                        worst_uptime = uptime_pct
                        worst_sla = sla
                buckets.append({
                    "state": worst.value,
                    "uptime_pct": round(worst_uptime, 2) if worst_uptime is not None else None,
                    "sla": round(worst_sla, 2) if worst_sla is not None else None,
                })

            current_state = buckets[-1]["state"] if buckets else State.GREY.value
            summary = probe_summary

            services.append(
                {
                    "id": service.id,
                    "name": service.name,
                    "state": current_state,
                    "summary": summary,
                    "checked_at": checked_at,
                    "checks": checks,
                    "buckets": buckets,
                }
            )

        return {
            "generated_at": int(time.time()),
            "refresh_seconds": self.config.ui.refresh_seconds,
            "bucket_minutes": bucket_minutes,
            "title": self.config.ui.title,
            "services": services,
        }
