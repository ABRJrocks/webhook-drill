import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from webhook_drill.cli import main
from webhook_drill.demo import DEMO_SECRET, DemoServer
from webhook_drill.runner import run_suite
from webhook_drill.suite import load_suite
from webhook_drill.transport import validate_target

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def demo(broken=False):
    with DemoServer(("127.0.0.1", 0), broken=broken) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


class DemoTests(unittest.TestCase):
    def test_good_demo_passes_every_scenario(self):
        with demo() as origin:
            suite = load_suite(ROOT / "examples/reliability.json", "good", {"WEBHOOK_DRILL_SECRET": DEMO_SECRET})
            result = run_suite(suite, validate_target(origin))
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(result["scenarios"]), 4)
        self.assertEqual(result["scenarios"][2]["steps"][0]["deliveries"][0]["attempts"][0]["status"], 503)

    def test_broken_demo_fails_duplicates_and_ordering(self):
        with demo(broken=True) as origin:
            suite = load_suite(ROOT / "examples/reliability.json", "bad", {"WEBHOOK_DRILL_SECRET": DEMO_SECRET})
            result = run_suite(suite, validate_target(origin))
        self.assertFalse(result["passed"])
        self.assertFalse(result["scenarios"][0]["passed"])
        self.assertFalse(result["scenarios"][1]["passed"])
        self.assertTrue(result["scenarios"][2]["passed"])
        self.assertTrue(result["scenarios"][3]["passed"])

    def test_github_suite_passes_demo(self):
        with demo() as origin:
            result = run_suite(load_suite(ROOT / "examples/github.json", "github", {"WEBHOOK_DRILL_SECRET": DEMO_SECRET}),
                               validate_target(origin))
        self.assertTrue(result["passed"])

    def test_namespaced_runs_do_not_share_deduplication_keys(self):
        with demo() as origin:
            for name in ("first", "second"):
                result = run_suite(load_suite(ROOT / "examples/reliability.json", name,
                                              {"WEBHOOK_DRILL_SECRET": DEMO_SECRET}), validate_target(origin))
                self.assertTrue(result["passed"])


class CliTests(unittest.TestCase):
    def call(self, args, env=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), patch.dict(os.environ, env or {}, clear=True):
            code = main(args)
        return code, output.getvalue(), errors.getvalue()

    def test_dry_run_makes_no_requests(self):
        with patch("webhook_drill.cli.run_suite") as run:
            code, output, _ = self.call(["run", str(ROOT / "examples/reliability.json"),
                                        "--target", "http://127.0.0.1:1", "--dry-run"],
                                       {"WEBHOOK_DRILL_SECRET": DEMO_SECRET})
        self.assertEqual(code, 0)
        self.assertIn("VALID", output)
        run.assert_not_called()

    def test_missing_secret_and_remote_target_return_config_error(self):
        for origin in ["http://127.0.0.1:1", "https://staging.example"]:
            code, _, errors = self.call(["run", str(ROOT / "examples/reliability.json"), "--target", origin])
            self.assertEqual(code, 2)
            self.assertIn("ERROR", errors)

    def test_cli_reports_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as temp:
            for broken, expected in [(False, 0), (True, 1)]:
                with demo(broken=broken) as origin:
                    code, output, errors = self.call(["run", str(ROOT / "examples/reliability.json"),
                        "--target", origin, "--json-report", temp + "/report.json", "--junit", temp + "/report.xml"],
                        {"WEBHOOK_DRILL_SECRET": DEMO_SECRET})
                self.assertEqual(code, expected, output + errors)
                self.assertEqual(json.loads(Path(temp + "/report.json").read_text())["passed"], not broken)
                self.assertTrue(Path(temp + "/report.xml").is_file())

    def test_module_entrypoint(self):
        result = subprocess.run([sys.executable, "-m", "webhook_drill", "--version"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "0.1.0")
