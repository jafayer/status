from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import os

import yaml


DEFAULT_CONFIG_PATH = "config/status-page.yaml"


@dataclass(slots=True)
class StorageConfig:
    path: str = "data/status.db"
    retention_hours: int = 24


@dataclass(slots=True)
class UIConfig:
    refresh_seconds: int = 30
    bucket_minutes: int = 15
    title: str = "Service Status"


@dataclass(slots=True)
class ServiceConfig:
    id: str
    name: str
    interval_seconds: int = 60
    checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class MonitorConfig:
    storage: StorageConfig = field(default_factory=StorageConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    default_sla: float = 100.0
    services: list[ServiceConfig] = field(default_factory=list)


class ConfigError(ValueError):
    pass


def _normalize_service(raw: dict[str, Any], idx: int) -> ServiceConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"services[{idx}] must be an object")

    service_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not service_id:
        raise ConfigError(f"services[{idx}].id is required")
    if not name:
        raise ConfigError(f"services[{idx}].name is required")

    interval_seconds = int(raw.get("interval_seconds", 60))
    checks = raw.get("checks", [])
    if not isinstance(checks, list):
        raise ConfigError(f"services[{idx}].checks must be a list")

    return ServiceConfig(
        id=service_id,
        name=name,
        interval_seconds=max(5, interval_seconds),
        checks=checks,
    )


def load_config(config_path: str | None = None) -> MonitorConfig:
    path = Path(config_path or os.environ.get("STATUS_PAGE_CONFIG") or DEFAULT_CONFIG_PATH)
    if not path.exists():
        return MonitorConfig()

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Top-level YAML structure must be an object")

    storage_raw = raw.get("storage", {}) or {}
    ui_raw = raw.get("ui", {}) or {}
    services_raw = raw.get("services", []) or []
    default_sla_raw = raw.get("default_sla", 100.0)

    if not isinstance(storage_raw, dict):
        raise ConfigError("storage must be an object")
    if not isinstance(ui_raw, dict):
        raise ConfigError("ui must be an object")
    if not isinstance(services_raw, list):
        raise ConfigError("services must be a list")

    try:
        default_sla = float(default_sla_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("default_sla must be a number") from exc
    default_sla = max(0.0, min(100.0, default_sla))

    config = MonitorConfig(
        storage=StorageConfig(
            path=str(storage_raw.get("path", "data/status.db")),
            retention_hours=max(1, int(storage_raw.get("retention_hours", 24))),
        ),
        ui=UIConfig(
            refresh_seconds=max(5, int(ui_raw.get("refresh_seconds", 30))),
            bucket_minutes=max(1, int(ui_raw.get("bucket_minutes", 15))),
            title=str(ui_raw.get("title") or "Service Status").strip() or "Service Status",
        ),
        default_sla=default_sla,
        services=[_normalize_service(svc, idx) for idx, svc in enumerate(services_raw)],
    )

    seen: set[str] = set()
    for service in config.services:
        if service.id in seen:
            raise ConfigError(f"Duplicate service id: {service.id}")
        seen.add(service.id)

    return config
