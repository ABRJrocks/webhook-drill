"""CI reports intentionally exclude request and response content."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET


def write_atomic(path: str | Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def junit(report: dict) -> str:
    cases = [(scenario, step) for scenario in report["scenarios"] for step in scenario["steps"]]
    suite = ET.Element("testsuite", name=report["suite"], tests=str(len(cases)),
                       failures=str(sum(step["status"] == "failed" for _, step in cases)),
                       skipped=str(sum(step["status"] == "skipped" for _, step in cases)),
                       time=str(report["elapsed_ms"] / 1000))
    for scenario, step in cases:
        elapsed = sum(a["elapsed_ms"] for d in step["deliveries"] for a in d["attempts"])
        case = ET.SubElement(suite, "testcase", classname=scenario["name"], name=step["name"],
                             time=str(elapsed / 1000))
        if step["status"] == "skipped":
            ET.SubElement(case, "skipped", message="Earlier step in this scenario failed")
        elif step["status"] == "failed":
            errors = [error for d in step["deliveries"] for error in d["failures"]]
            ET.SubElement(case, "failure", message="; ".join(sorted(set(errors))))
    return ET.tostring(suite, encoding="unicode", xml_declaration=True) + "\n"


def save_reports(report: dict, json_path=None, junit_path=None):
    if json_path:
        write_atomic(json_path, json.dumps(report, indent=2, allow_nan=False) + "\n")
    if junit_path:
        write_atomic(junit_path, junit(report))
