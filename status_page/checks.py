from __future__ import annotations

from dataclasses import dataclass
import json
import re
import socket
import subprocess
import time
from typing import Any

import dns.resolver  # type: ignore[import-not-found]
import httpx

from status_page.models import CheckResult, State


@dataclass(slots=True)
class CheckContext:
    timeout_seconds: float = 10.0


def _as_int_list(value: Any, default: list[int]) -> list[int]:
    if value is None:
        return default
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        return [int(v) for v in value]
    return default


def run_check(check: dict[str, Any], ctx: CheckContext) -> CheckResult:
    check_type = str(check.get("type", "")).strip().lower()
    if check_type in {"http", "https"}:
        return _run_http_check(check, ctx)
    if check_type == "dns":
        return _run_dns_check(check, ctx)
    if check_type == "command":
        return _run_command_check(check, ctx)
    if check_type == "tcp":
        return _run_tcp_check(check, ctx)

    return CheckResult(
        check_type=check_type or "unknown",
        state=State.GREY,
        message="Unsupported check type",
        duration_ms=0,
        detail={"supported": ["http", "https", "dns", "command", "tcp"]},
    )


def _get_json_path(data: Any, path: str) -> tuple[bool, Any]:
    """Traverse a dot-notation path in a nested JSON structure.

    List elements can be addressed by numeric index (e.g. ``items.0.name``).
    Returns ``(found, value)``.
    """
    parts = path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, current


def _check_json_fields(
    response_text: str, json_fields: list[dict[str, Any]]
) -> str | None:
    """Validate *json_fields* assertions against *response_text*.

    Returns an error message on the first failure, or ``None`` when all
    assertions pass.
    """
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        return f"Response is not valid JSON: {exc}"

    for field_check in json_fields:
        path = str(field_check.get("path") or "").strip()
        if not path:
            continue

        expected = field_check.get("value")
        operator = str(field_check.get("operator") or "eq").strip().lower()

        found, actual = _get_json_path(data, path)
        if not found:
            return f"JSON path '{path}' not found in response"

        if operator == "eq":
            # Allow loose comparison: "true"/"false" strings match booleans, etc.
            if actual != expected:
                return f"JSON path '{path}': expected {expected!r}, got {actual!r}"
        elif operator == "ne":
            if actual == expected:
                return f"JSON path '{path}': expected value to differ from {expected!r}"
        elif operator in {"gt", "gte", "lt", "lte"}:
            try:
                actual_num = float(actual)  # type: ignore[arg-type]
                expected_num = float(expected)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return (
                    f"JSON path '{path}': cannot compare non-numeric values "
                    f"with operator '{operator}'"
                )
            checks_map = {
                "gt": (actual_num > expected_num, ">"),
                "gte": (actual_num >= expected_num, ">="),
                "lt": (actual_num < expected_num, "<"),
                "lte": (actual_num <= expected_num, "<="),
            }
            passed, symbol = checks_map[operator]
            if not passed:
                return (
                    f"JSON path '{path}': {actual_num} is not {symbol} {expected_num}"
                )
        elif operator == "contains":
            if str(expected) not in str(actual):
                return (
                    f"JSON path '{path}': {actual!r} does not contain {expected!r}"
                )
        elif operator == "regex":
            if not re.search(str(expected), str(actual)):
                return (
                    f"JSON path '{path}': {actual!r} does not match regex {expected!r}"
                )
        else:
            return f"JSON path '{path}': unsupported operator '{operator}'"

    return None


def _run_http_check(check: dict[str, Any], ctx: CheckContext) -> CheckResult:
    started = time.time()
    url = str(check.get("url", "")).strip()
    if not url:
        return CheckResult("http", State.GREY, "Missing url", 0)

    method = str(check.get("method", "GET")).upper()
    timeout = float(check.get("timeout_seconds", ctx.timeout_seconds))
    expected_status = _as_int_list(check.get("expected_status"), [200])
    degraded_statuses = _as_int_list(check.get("degraded_statuses"), [429, 503])
    body_contains = check.get("body_contains")
    body_regex = check.get("body_regex")
    json_fields: list[dict[str, Any]] = check.get("json_fields") or []
    verify_tls = bool(check.get("verify_tls", True))

    try:
        with httpx.Client(timeout=timeout, verify=verify_tls) as client:
            response = client.request(method, url)

        elapsed = int((time.time() - started) * 1000)

        if response.status_code in expected_status:
            if body_contains and str(body_contains) not in response.text:
                return CheckResult(
                    "http",
                    State.RED,
                    "Body does not contain expected text",
                    elapsed,
                    {"status_code": response.status_code},
                )
            if body_regex and not re.search(str(body_regex), response.text):
                return CheckResult(
                    "http",
                    State.RED,
                    "Body does not match regex",
                    elapsed,
                    {"status_code": response.status_code},
                )
            if json_fields:
                json_error = _check_json_fields(response.text, json_fields)
                if json_error:
                    return CheckResult(
                        "http",
                        State.RED,
                        json_error,
                        elapsed,
                        {"status_code": response.status_code},
                    )
            return CheckResult(
                "http",
                State.GREEN,
                f"HTTP {response.status_code}",
                elapsed,
                {"status_code": response.status_code},
            )

        if response.status_code in degraded_statuses:
            return CheckResult(
                "http",
                State.YELLOW,
                f"HTTP {response.status_code} (degraded)",
                elapsed,
                {"status_code": response.status_code},
            )

        return CheckResult(
            "http",
            State.RED,
            f"HTTP {response.status_code}",
            elapsed,
            {"status_code": response.status_code},
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.time() - started) * 1000)
        return CheckResult("http", State.RED, f"HTTP error: {exc}", elapsed)


def _run_dns_check(check: dict[str, Any], ctx: CheckContext) -> CheckResult:
    started = time.time()
    host = str(check.get("host", "")).strip()
    record_type = str(check.get("record_type", "A")).strip().upper()
    expected_rcode = str(check.get("expected_rcode", "NOERROR")).strip().upper()
    expected_values = [str(v) for v in check.get("expected_values", [])]
    nameserver = check.get("nameserver")
    timeout = float(check.get("timeout_seconds", ctx.timeout_seconds))

    if not host:
        return CheckResult("dns", State.GREY, "Missing host", 0)

    resolver = dns.resolver.Resolver(configure=True)
    resolver.lifetime = timeout
    resolver.timeout = timeout
    if nameserver:
        resolver.nameservers = [str(nameserver)]

    try:
        answers = resolver.resolve(host, record_type)
        values = [r.to_text().strip('"') for r in answers]
        elapsed = int((time.time() - started) * 1000)

        if expected_rcode != "NOERROR":
            return CheckResult(
                "dns",
                State.RED,
                f"Expected {expected_rcode}, got NOERROR",
                elapsed,
                {"values": values},
            )

        if expected_values and not set(expected_values).issubset(set(values)):
            return CheckResult(
                "dns",
                State.RED,
                "DNS values did not match expected_values",
                elapsed,
                {"values": values, "expected_values": expected_values},
            )

        return CheckResult(
            "dns",
            State.GREEN,
            f"DNS {record_type} NOERROR",
            elapsed,
            {"values": values},
        )
    except dns.resolver.NXDOMAIN:
        elapsed = int((time.time() - started) * 1000)
        state = State.GREEN if expected_rcode == "NXDOMAIN" else State.RED
        return CheckResult("dns", state, "DNS NXDOMAIN", elapsed)
    except dns.resolver.NoAnswer:
        elapsed = int((time.time() - started) * 1000)
        state = State.GREEN if expected_rcode == "NOANSWER" else State.RED
        return CheckResult("dns", state, "DNS NOANSWER", elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.time() - started) * 1000)
        return CheckResult("dns", State.RED, f"DNS error: {exc}", elapsed)


def _run_command_check(check: dict[str, Any], ctx: CheckContext) -> CheckResult:
    started = time.time()
    command = check.get("command")
    if not command:
        return CheckResult("command", State.GREY, "Missing command", 0)

    timeout = float(check.get("timeout_seconds", ctx.timeout_seconds))
    expected_return_codes = _as_int_list(check.get("expected_return_codes"), [0])
    shell = bool(check.get("shell", isinstance(command, str)))

    try:
        completed = subprocess.run(  # noqa: S603
            command,
            shell=shell,  # noqa: S602
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        elapsed = int((time.time() - started) * 1000)
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()

        if completed.returncode in expected_return_codes:
            return CheckResult(
                "command",
                State.GREEN,
                f"Exit code {completed.returncode}",
                elapsed,
                {"stdout": stdout[-500:], "stderr": stderr[-500:]},
            )

        return CheckResult(
            "command",
            State.RED,
            f"Exit code {completed.returncode}",
            elapsed,
            {"stdout": stdout[-500:], "stderr": stderr[-500:]},
        )
    except subprocess.TimeoutExpired:
        elapsed = int((time.time() - started) * 1000)
        return CheckResult("command", State.RED, "Command timed out", elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.time() - started) * 1000)
        return CheckResult("command", State.RED, f"Command error: {exc}", elapsed)


def _run_tcp_check(check: dict[str, Any], ctx: CheckContext) -> CheckResult:
    started = time.time()
    host = str(check.get("host", "")).strip()
    port = int(check.get("port", 0))
    timeout = float(check.get("timeout_seconds", ctx.timeout_seconds))

    if not host or not port:
        return CheckResult("tcp", State.GREY, "Missing host or port", 0)

    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        elapsed = int((time.time() - started) * 1000)
        return CheckResult("tcp", State.GREEN, f"TCP {host}:{port} reachable", elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.time() - started) * 1000)
        return CheckResult("tcp", State.RED, f"TCP error: {exc}", elapsed)
