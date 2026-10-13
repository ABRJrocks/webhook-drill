"""Strict, side-effect-free suite loading and validation."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

MAX_BYTES = 1_048_576
MAX_DELIVERIES = 1000
TOKEN = "{{run_id}}"
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")
RESERVED = {"host", "content-length", "transfer-encoding", "connection",
            "x-hub-signature-256", "stripe-signature"}


class SuiteError(ValueError):
    """An invalid suite or missing secret. Raised before any request is sent."""


@dataclass(frozen=True)
class Step:
    name: str
    kind: str
    path: str
    headers: dict[str, str]
    body: bytes
    signature: str
    repeat: int
    concurrency: int
    delay_ms: int
    timeout: float
    attempts: int
    base_delay: float
    max_delay: float
    expect: dict[str, Any]
    eventually: float
    interval: float


@dataclass(frozen=True)
class Scenario:
    name: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Suite:
    name: str
    run_id: str
    provider: str | None
    secret: str
    scenarios: tuple[Scenario, ...]
    delivery_budget: int


def _object(value: Any, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SuiteError(f"{where}: expected an object")
    unknown = set(value) - allowed
    if unknown:
        raise SuiteError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")
    return value


def _number(value: Any, low: float, high: float, where: str, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SuiteError(f"{where}: expected a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise SuiteError(f"{where}: must be between {low} and {high}")
    if integer and not isinstance(value, int):
        raise SuiteError(f"{where}: expected an integer")
    return value


def _name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise SuiteError(f"{where}: expected a nonempty string up to 160 characters")
    if any(ord(c) < 32 for c in value):
        raise SuiteError(f"{where}: control characters are not allowed")
    return value


def _path(value: Any, run_id: str, where: str) -> str:
    if not isinstance(value, str):
        raise SuiteError(f"{where}: expected an origin-relative path")
    value = value.replace(TOKEN, run_id)
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
        raise SuiteError(f"{where}: expected an ASCII origin-relative path")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise SuiteError(f"{where}: origins and fragments are not allowed")
    return value


def _headers(value: Any, env: Mapping[str, str], where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SuiteError(f"{where}: expected an object")
    result = {}
    for key, item in value.items():
        if not HEADER_NAME.fullmatch(key) or key.lower() in RESERVED:
            raise SuiteError(f"{where}: invalid or reserved header name")
        if key.lower() in {k.lower() for k in result}:
            raise SuiteError(f"{where}: duplicate case-insensitive header")
        if isinstance(item, dict):
            _object(item, {"env"}, where)
            variable = item.get("env", "")
            if not isinstance(variable, str) or not ENV_NAME.fullmatch(variable):
                raise SuiteError(f"{where}: invalid environment variable reference")
            item = env.get(variable)
            if not item:
                raise SuiteError(f"{where}: missing environment variable {variable}")
        if (not isinstance(item, str) or len(item) > 8192
                or any(ord(c) < 32 or ord(c) > 255 for c in item)):
            raise SuiteError(f"{where}: invalid header value")
        result[key] = item
    return result


def _merge_headers(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    result = {key: value for key, value in base.items()
              if key.lower() not in {k.lower() for k in extra}}
    return {**result, **extra}


def _expect(value: Any, where: str) -> dict[str, Any]:
    item = _object(value, {"status", "error", "json", "headers", "max_elapsed_ms"}, where)
    if ("status" in item) == ("error" in item):
        raise SuiteError(f"{where}: specify exactly one of status or error")
    if "status" in item:
        statuses = item["status"]
        if not isinstance(statuses, list) or not statuses:
            raise SuiteError(f"{where}.status: expected a nonempty status list")
        for status in statuses:
            _number(status, 100, 599, where + ".status", integer=True)
    elif item["error"] not in {"timeout", "connection", "response_too_large"}:
        raise SuiteError(f"{where}.error: unknown transport error")
    for section in ("json", "headers"):
        if section in item and not isinstance(item[section], dict):
            raise SuiteError(f"{where}.{section}: expected an object")
    for pointer in item.get("json", {}):
        if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
            raise SuiteError(f"{where}.json: keys must be RFC 6901 JSON pointers")
        if re.search(r"~(?![01])", pointer):
            raise SuiteError(f"{where}.json: invalid JSON pointer escape")
    for key, expected in item.get("headers", {}).items():
        if not HEADER_NAME.fullmatch(key) or not isinstance(expected, str):
            raise SuiteError(f"{where}.headers: expected header names and strings")
    if "error" in item and any(k in item for k in ("json", "headers")):
        raise SuiteError(f"{where}: transport errors cannot assert response content")
    if "max_elapsed_ms" in item:
        _number(item["max_elapsed_ms"], 0, 120_000, where + ".max_elapsed_ms")
    return item


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SuiteError("duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise SuiteError("non-finite JSON values are not supported")


def load_suite(path: str | Path, run_id: str, env: Mapping[str, str] | None = None) -> Suite:
    """Load all fixtures, secrets and expectations before any network side effects."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise SuiteError("run_id must be 1 to 64 ASCII letters, digits, underscores or hyphens")
    env = os.environ if env is None else env
    path = Path(path).resolve()
    if path.stat().st_size > MAX_BYTES:
        raise SuiteError("suite exceeds 1 MiB")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs,
                         parse_constant=_invalid_constant)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise SuiteError("suite is not valid UTF-8 JSON") from exc
    root = _object(raw, {"version", "name", "signing", "headers", "scenarios"}, "suite")
    if type(root.get("version")) is not int or root["version"] != 1:
        raise SuiteError("suite.version: expected 1")
    name = _name(root.get("name"), "suite.name")
    provider, secret = None, ""
    if "signing" in root:
        signing = _object(root["signing"], {"provider", "secret_env"}, "signing")
        provider = signing.get("provider")
        if provider not in {"github", "stripe"}:
            raise SuiteError("signing.provider: expected github or stripe")
        variable = signing.get("secret_env", "")
        if not isinstance(variable, str) or not ENV_NAME.fullmatch(variable):
            raise SuiteError("signing.secret_env: invalid environment variable")
        secret = env.get(variable, "")
        if not secret:
            raise SuiteError(f"signing: missing environment variable {variable}")
    headers = _headers(root.get("headers", {}), env, "suite.headers")
    items = root.get("scenarios")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise SuiteError("suite.scenarios: expected 1 to 100 scenarios")
    scenarios, names, budget = [], set(), 0
    for item in items:
        obj = _object(item, {"name", "steps"}, "scenario")
        scenario_name = _name(obj.get("name"), "scenario.name")
        if scenario_name in names:
            raise SuiteError("scenario names must be unique")
        names.add(scenario_name)
        step_items = obj.get("steps")
        if not isinstance(step_items, list) or not 1 <= len(step_items) <= 100:
            raise SuiteError("scenario.steps: expected 1 to 100 steps")
        steps, step_names = [], set()
        for step in step_items:
            where = f"{scenario_name}: step"
            value = _object(step, {"name", "send", "probe"}, where)
            step_name = _name(value.get("name"), where + ".name")
            if step_name in step_names:
                raise SuiteError(f"{where}: step names must be unique")
            step_names.add(step_name)
            if ("send" in value) == ("probe" in value):
                raise SuiteError(f"{where}: specify exactly one of send or probe")
            kind = "send" if "send" in value else "probe"
            allowed = {"path", "headers", "timeout", "expect"}
            allowed |= {"body", "body_file", "signature", "repeat", "concurrency", "delay_ms", "retry"} if kind == "send" else {"eventually", "interval"}
            spec = _object(value[kind], allowed, where + "." + kind)
            route = _path(spec.get("path"), run_id, where + ".path")
            step_headers = _merge_headers(headers, _headers(spec.get("headers", {}), env, where + ".headers"))
            timeout = _number(spec.get("timeout", 5), .01, 60, where + ".timeout")
            body, mode, repeat, concurrency, delay = b"", "valid", 1, 1, 0
            attempts, base_delay, max_delay, eventually, interval = 1, .1, 2., 0., .1
            if kind == "send":
                if ("body" in spec) == ("body_file" in spec):
                    raise SuiteError(f"{where}: specify exactly one of body or body_file")
                if "body_file" in spec:
                    if not isinstance(spec["body_file"], str):
                        raise SuiteError(f"{where}: body_file must be a string")
                    fixture = (path.parent / spec["body_file"]).resolve()
                    if path.parent not in fixture.parents or not fixture.is_file():
                        raise SuiteError(f"{where}: fixture must be a file inside the suite directory")
                    if fixture.stat().st_size > MAX_BYTES:
                        raise SuiteError(f"{where}: fixture exceeds 1 MiB")
                    body = fixture.read_bytes()
                else:
                    body = json.dumps(spec["body"], ensure_ascii=False, allow_nan=False,
                                      separators=(",", ":")).encode()
                body = body.replace(TOKEN.encode(), run_id.encode())
                if len(body) > MAX_BYTES:
                    raise SuiteError(f"{where}: body exceeds 1 MiB")
                mode = spec.get("signature", "valid")
                if mode not in {"valid", "invalid", "missing", "tampered", "stale"}:
                    raise SuiteError(f"{where}: invalid signature mode")
                if mode != "valid" and not provider:
                    raise SuiteError(f"{where}: signature modes require suite.signing")
                if mode == "stale" and provider != "stripe":
                    raise SuiteError(f"{where}: stale signatures require Stripe signing")
                repeat = _number(spec.get("repeat", 1), 1, 100, where + ".repeat", True)
                concurrency = _number(spec.get("concurrency", 1), 1, 20, where + ".concurrency", True)
                delay = _number(spec.get("delay_ms", 0), 0, 10_000, where + ".delay_ms", True)
                retry = _object(spec.get("retry", {}), {"attempts", "base_delay", "max_delay"}, where + ".retry")
                attempts = _number(retry.get("attempts", 1), 1, 10, where + ".retry.attempts", True)
                base_delay = _number(retry.get("base_delay", .1), 0, 30, where + ".retry.base_delay")
                max_delay = _number(retry.get("max_delay", 2), base_delay, 30, where + ".retry.max_delay")
                budget += repeat * attempts
            else:
                eventually = _number(spec.get("eventually", 0), 0, 30, where + ".eventually")
                interval = _number(spec.get("interval", .1), .01, 5, where + ".interval")
            expect = _expect(spec.get("expect"), where + ".expect")
            steps.append(Step(step_name, kind, route, step_headers, body, mode,
                              repeat, concurrency, delay, timeout, attempts,
                              base_delay, max_delay, expect, eventually, interval))
        scenarios.append(Scenario(scenario_name, tuple(steps)))
    if budget > MAX_DELIVERIES:
        raise SuiteError(f"suite exceeds the {MAX_DELIVERIES} delivery budget")
    return Suite(name, run_id, provider, secret, tuple(scenarios), budget)
