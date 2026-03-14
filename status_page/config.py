from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import os
import re

import yaml


DEFAULT_CONFIG_PATH = "config/status-page.yaml"
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


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


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    if line.startswith("export "):
        line = line[len("export ") :].strip()

    if "=" not in line:
        return None

    key, raw_value = line.split("=", 1)
    key = key.strip()
    if not key:
        return None

    value = raw_value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]
    else:
        value = value.split(" #", 1)[0].strip()

    return key, value


def _load_dotenv_file(dotenv_path: Path) -> None:
    if not dotenv_path.exists() or not dotenv_path.is_file():
        return

    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_dotenv_line(line)
        if not parsed:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)


def _load_dotenv(config_path: Path) -> None:
    dotenv_override = os.environ.get("STATUS_PAGE_DOTENV")
    candidates: list[Path] = []

    if dotenv_override:
        candidates.append(Path(dotenv_override))
    else:
        candidates.extend(
            [
                Path(".env"),
                config_path.parent / ".env",
                config_path.parent.parent / ".env",
            ]
        )

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        _load_dotenv_file(candidate)


def _expand_env_vars(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(
            lambda match: os.environ.get(match.group(1), ""),
            value,
        )
    if isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    return value


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
    _load_dotenv(path)
    if not path.exists():
        return MonitorConfig()

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = _expand_env_vars(raw)
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
