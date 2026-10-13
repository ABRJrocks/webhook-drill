"""Ordered scenarios with concurrent deliveries, bounded retries and state probes."""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from .signing import delivery_signature
from .suite import Step, Suite
from .transport import Response, Target, request


def pointer(document: Any, path: str) -> Any:
    """Evaluate an RFC 6901 pointer; distinguish absent values from JSON null."""
    if path == "":
        return document
    current = document
    for segment in path[1:].split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[segment]
        elif isinstance(current, list):
            if not segment.isascii() or not segment.isdigit() or (len(segment) > 1 and segment[0] == "0"):
                raise KeyError(path)
            current = current[int(segment)]
        else:
            raise KeyError(path)
    return current


def _equal(actual: Any, expected: Any) -> bool:
    # Python treats True == 1; JSON contract assertions must not do that.
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(_equal(actual[k], expected[k]) for k in actual)
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(_equal(a, b) for a, b in zip(actual, expected))
    return actual == expected


def assertions(response: Response, expect: dict[str, Any]) -> list[str]:
    """Return redacted diagnostics, never response bodies, values or headers."""
    failures = []
    if "error" in expect:
        if response.error != expect["error"]:
            failures.append("expected transport error was not observed")
    elif response.error:
        failures.append("transport error: " + response.error)
    elif response.status not in expect["status"]:
        failures.append(f"unexpected HTTP status: {response.status}")
    if "max_elapsed_ms" in expect and response.elapsed_ms > expect["max_elapsed_ms"]:
        failures.append("response exceeded max_elapsed_ms")
    for name, value in expect.get("headers", {}).items():
        if response.headers.get(name.lower()) != value:
            failures.append("response header assertion failed")
    if "json" in expect:
        try:
            document = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeError, ValueError):
            failures.append("response is not valid JSON")
        else:
            for path, expected in expect["json"].items():
                try:
                    actual = pointer(document, path)
                except (KeyError, IndexError, ValueError):
                    failures.append("JSON pointer is missing")
                else:
                    if not _equal(actual, expected):
                        failures.append("JSON value assertion failed")
    return failures


def retry_delay(header: str | None, attempt: int, base: float, cap: float,
                now: float | None = None) -> float:
    delay = min(cap, base * 2 ** (attempt - 1))
    if header is not None:
        try:
            seconds = float(header)
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
                seconds = date.timestamp() - (time.time() if now is None else now)
            except (TypeError, ValueError, OverflowError):
                return delay
        if math.isfinite(seconds) and seconds >= 0:
            delay = max(delay, min(cap, seconds))
    return delay


def _summary(response: Response, attempt: int) -> dict[str, Any]:
    return {"attempt": attempt, "status": response.status, "error": response.error,
            "elapsed_ms": round(response.elapsed_ms, 3)}


def _delivery(suite: Suite, step: Step, target: Target, transport: Callable,
              clock: Callable, sleep: Callable) -> dict[str, Any]:
    attempts = []
    for number in range(1, step.attempts + 1):
        headers = {"Content-Type": "application/json", **step.headers}
        body = step.body
        if suite.provider:
            body, signed = delivery_signature(suite.provider, suite.secret, body,
                                               int(clock()), step.signature)
            headers.update(signed)
        response = transport(target, "POST", step.path, headers, body, step.timeout)
        attempts.append(_summary(response, number))
        retryable = (response.error in {"connection", "timeout"}
                     or response.status in {408, 429}
                     or (response.status is not None and 500 <= response.status <= 599))
        if not retryable or number == step.attempts:
            break
        sleep(retry_delay(response.headers.get("retry-after"), number,
                          step.base_delay, step.max_delay))
    failures = assertions(response, step.expect)
    return {"passed": not failures, "attempts": attempts, "failures": failures}


def _probe(step: Step, target: Target, transport: Callable) -> dict[str, Any]:
    deadline = time.monotonic() + step.eventually
    observations = []
    first = True
    while first or time.monotonic() < deadline:
        # Once polling starts, a slow request cannot overrun the configured window by a full timeout.
        timeout = step.timeout if first and not step.eventually else min(step.timeout, max(.001, deadline-time.monotonic()))
        response = transport(target, "GET", step.path, step.headers, None, timeout)
        observations.append(_summary(response, len(observations)+1))
        failures = assertions(response, step.expect)
        if not failures or not step.eventually:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(step.interval, remaining))
        first = False
    return {"passed": not failures, "attempts": observations, "failures": failures}


def run_suite(suite: Suite, target: Target, *, transport: Callable = request,
              clock: Callable = time.time, sleep: Callable = time.sleep,
              on_step: Callable | None = None) -> dict[str, Any]:
    """Execute ordered steps; stop a scenario after failure to avoid misleading state checks."""
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    results = []
    for scenario in suite.scenarios:
        steps, failed = [], False
        for step in scenario.steps:
            if failed:
                result = {"name": step.name, "kind": step.kind, "status": "skipped", "deliveries": []}
            else:
                if step.kind == "send":
                    sleep(step.delay_ms / 1000)
                    with ThreadPoolExecutor(max_workers=min(step.repeat, step.concurrency)) as pool:
                        deliveries = list(pool.map(lambda _: _delivery(suite, step, target, transport, clock, sleep),
                                                   range(step.repeat)))
                else:
                    deliveries = [_probe(step, target, transport)]
                failed = not all(d["passed"] for d in deliveries)
                result = {"name": step.name, "kind": step.kind,
                          "status": "failed" if failed else "passed", "deliveries": deliveries}
            steps.append(result)
            if on_step:
                on_step(scenario.name, result)
        results.append({"name": scenario.name, "passed": not failed, "steps": steps})
    return {"schema_version": 1, "suite": suite.name, "run_id": suite.run_id,
            "started_at": started_at,
            "elapsed_ms": round((time.monotonic()-started)*1000, 3),
            "passed": all(s["passed"] for s in results), "scenarios": results}

