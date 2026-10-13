"""CLI exit codes: 0 pass, 1 failed assertions, 2 configuration or report error."""

from __future__ import annotations

import argparse
import os
import sys
import uuid

from . import __version__
from .demo import DEMO_SECRET, DemoServer
from .report import save_reports
from .runner import run_suite
from .suite import SuiteError, load_suite
from .transport import validate_target


def parser():
    root = argparse.ArgumentParser(prog="webhook-drill", description="Repeatable webhook reliability tests for local development and CI.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run a JSON suite against a test endpoint")
    run.add_argument("suite")
    run.add_argument("--target", required=True, help="HTTP(S) origin, for example http://127.0.0.1:8765")
    run.add_argument("--allow-remote", action="store_true", help="Explicitly allow an HTTPS staging origin")
    run.add_argument("--run-id", default=None, help="Override the unique fixture namespace (ASCII, max 64 chars)")
    run.add_argument("--dry-run", action="store_true", help="Validate the suite and target without sending requests")
    run.add_argument("--json-report", metavar="PATH")
    run.add_argument("--junit", metavar="PATH")
    demo = commands.add_parser("demo", help="Start a local-only teaching receiver")
    demo.add_argument("--port", type=int, default=8765)
    demo.add_argument("--broken", action="store_true", help="Demonstrate failures from duplicates and stale updates")
    return root


def _progress(scenario, step):
    print(f"{step['status'].upper():7} {scenario} / {step['name']}")
    for failure in sorted({f for delivery in step["deliveries"] for f in delivery["failures"]}):
        print("        " + failure)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            secret = os.environ.get("WEBHOOK_DRILL_SECRET", DEMO_SECRET)
            with DemoServer(("127.0.0.1", args.port), secret=secret, broken=args.broken) as server:
                print(f"Demo listening on http://127.0.0.1:{server.server_port}", flush=True)
                print("Local teaching receiver; do not expose it to the internet.", flush=True)
                server.serve_forever()
            return 0
        target = validate_target(args.target, args.allow_remote)
        suite = load_suite(args.suite, args.run_id or uuid.uuid4().hex)
        if args.dry_run:
            print(f"VALID {suite.name}: {len(suite.scenarios)} scenarios, at most {suite.delivery_budget} POST attempts")
            return 0
        report = run_suite(suite, target, on_step=_progress)
        save_reports(report, args.json_report, args.junit)
        total = len(report["scenarios"])
        passed = sum(s["passed"] for s in report["scenarios"])
        print(f"\n{passed}/{total} scenarios passed ({report['elapsed_ms']/1000:.2f}s)")
        return 0 if report["passed"] else 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (SuiteError, OSError, ValueError) as exc:
        # Configuration messages identify fields, never resolved credentials or bodies.
        message = str(exc) if isinstance(exc, SuiteError) else "Cannot read configuration, bind demo, or write reports."
        print("ERROR: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

