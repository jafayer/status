from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class State(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    GREY = "grey"


STATE_PRIORITY: dict[State, int] = {
    State.GREY: 0,
    State.GREEN: 1,
    State.YELLOW: 2,
    State.RED: 3,
}


@dataclass(slots=True)
class CheckResult:
    check_type: str
    state: State
    message: str
    duration_ms: int
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ServiceResult:
    service_id: str
    name: str
    state: State
    summary: str
    checked_at: int
    checks: list[CheckResult]
