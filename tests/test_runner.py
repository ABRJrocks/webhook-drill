import dataclasses
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from webhook_drill.report import junit, save_reports
from webhook_drill.runner import assertions, pointer, retry_delay, run_suite
from webhook_drill.suite import MAX_BYTES, SuiteError, load_suite
from webhook_drill.transport import Response, request, validate_target


class Handler(BaseHTTPRequestHandler):
    counts = {}
    requests = []
    lock = threading.Lock()

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with self.lock:
            self.counts[self.path] = self.counts.get(self.path, 0) + 1
            number = self.counts[self.path]
            self.requests.append((self.path, body, dict(self.headers)))
        if self.path == "/timeout":
            time.sleep(.12)
        status = 200
        if self.path == "/retry" and number < 3:
            status = 503
        if self.path == "/permanent":
            status = 400
        if self.path == "/redirect":
            status = 307
        payload = b"x" * (MAX_BYTES + 1) if self.path == "/large" else b'{"ok":true}'
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Retry-After", "0")
        self.send_header("X-Test", "yes")
        self.send_header("Location", "/redirected")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        with self.lock:
            counts = dict(self.counts)
        payload = json.dumps({"counts": counts}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.target = validate_target(f"http://127.0.0.1:{cls.server.server_port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.counts = {}
        Handler.requests = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "suite.json"

    def suite(self, steps):
        self.path.write_text(json.dumps({"version": 1, "name": "integration", "scenarios": [
            {"name": "HTTP", "steps": steps}]}))
        return load_suite(self.path, "integration", {})

    def send(self, path="/ok", **extra):
        return {"name": path, "send": {"path": path, "body": {"id": 1},
                                      "expect": {"status": [200]}, **extra}}

    def test_real_concurrent_deliveries_and_state_probe(self):
        suite = self.suite([self.send(repeat=12, concurrency=4), {"name": "count", "probe": {
            "path": "/state", "expect": {"status": [200], "json": {"/counts/~1ok": 12}}}}])
        report = run_suite(suite, self.target)
        self.assertTrue(report["passed"])
        self.assertEqual(Handler.counts["/ok"], 12)
        self.assertEqual({body for _, body, _ in Handler.requests}, {b'{"id":1}'})

    def test_retries_recover_from_real_503(self):
        suite = self.suite([self.send("/retry", retry={"attempts": 4, "base_delay": .01, "max_delay": .02})])
        report = run_suite(suite, self.target)
        self.assertTrue(report["passed"])
        attempts = report["scenarios"][0]["steps"][0]["deliveries"][0]["attempts"]
        self.assertEqual([a["status"] for a in attempts], [503, 503, 200])

    def test_permanent_errors_not_retried_and_following_step_skipped(self):
        suite = self.suite([self.send("/permanent", retry={"attempts": 5}), self.send()])
        report = run_suite(suite, self.target)
        self.assertFalse(report["passed"])
        self.assertEqual(Handler.counts, {"/permanent": 1})
        self.assertEqual(report["scenarios"][0]["steps"][1]["status"], "skipped")

    def test_redirect_is_not_followed(self):
        response = request(self.target, "POST", "/redirect", {}, b"{}", 1)
        self.assertEqual(response.status, 307)
        self.assertNotIn("/redirected", Handler.counts)

    def test_timeout_is_an_assertable_failure(self):
        report = run_suite(self.suite([self.send("/timeout", timeout=.03,
                                                expect={"error": "timeout"})]), self.target)
        self.assertTrue(report["passed"])
        time.sleep(.15)

    def test_response_size_limit(self):
        response = request(self.target, "POST", "/large", {}, b"{}", 1)
        self.assertEqual(response.error, "response_too_large")
        self.assertEqual(response.body, b"")

    def test_probe_waits_for_application_state(self):
        count = 0
        def delayed(*_):
            nonlocal count
            count += 1
            return Response(200, {}, json.dumps({"ready": count > 2}).encode(), 1)
        suite = self.suite([{"name": "eventual", "probe": {"path": "/state", "eventually": .2,
              "interval": .01, "expect": {"status": [200], "json": {"/ready": True}}}}])
        report = run_suite(suite, self.target, transport=delayed)
        self.assertTrue(report["passed"])
        self.assertEqual(count, 3)

    def test_probe_stops_at_deadline(self):
        suite = self.suite([{"name": "eventual", "probe": {"path": "/state", "eventually": .03,
              "interval": .01, "expect": {"status": [200], "json": {"/ready": True}}}}])
        start = time.monotonic()
        report = run_suite(suite, self.target, transport=lambda *_: Response(200, {}, b'{}', 1))
        self.assertFalse(report["passed"])
        self.assertLess(time.monotonic()-start, .2)

    def test_stripe_signature_refreshed_on_retry_same_bytes(self):
        suite = dataclasses.replace(self.suite([self.send(retry={"attempts": 2})]), provider="stripe", secret="private")
        seen, ticks = [], iter([100, 101])
        def transport(target, method, path, headers, body, timeout):
            seen.append((body, headers["Stripe-Signature"]))
            return Response(503 if len(seen) == 1 else 200, {}, b"{}", 1)
        self.assertTrue(run_suite(suite, self.target, transport=transport,
                                 clock=lambda: next(ticks), sleep=lambda _: None)["passed"])
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertNotEqual(seen[0][1], seen[1][1])

    def test_reports_exclude_credentials_payload_and_response_values(self):
        suite = self.suite([self.send(body={"password": "SUPERSECRET"},
                                    headers={"Authorization": "Bearer SUPERSECRET"},
                                    expect={"status": [200], "json": {"/password": "SUPERSECRET"}}), self.send("/later")])
        report = run_suite(suite, self.target, transport=lambda *_: Response(200, {"authorization": "SUPERSECRET"}, b'{"password":"DIFFERENTSECRET"}', 1))
        encoded = json.dumps(report) + junit(report)
        self.assertNotIn("SUPERSECRET", encoded)
        self.assertNotIn("DIFFERENTSECRET", encoded)
        xml = ElementTree.fromstring(junit(report))
        self.assertEqual(xml.attrib["failures"], "1")
        self.assertEqual(xml.attrib["skipped"], "1")
        save_reports(report, self.path.parent / "nested/report.json", self.path.parent / "nested/junit.xml")
        self.assertEqual(json.loads((self.path.parent / "nested/report.json").read_text()), report)


class AssertionTests(unittest.TestCase):
    def test_json_pointer_nested_arrays_and_escaping(self):
        document = {"a/b": {"~x": [None, {"v": 2}]}}
        self.assertEqual(pointer(document, "/a~1b/~0x/1/v"), 2)
        self.assertIsNone(pointer(document, "/a~1b/~0x/0"))
        self.assertIs(pointer(document, ""), document)
        for path in ["/missing", "/a~1b/~0x/01", "/a~1b/~0x/-1", "/a~1b/~0x/99"]:
            with self.subTest(path=path), self.assertRaises((KeyError, IndexError)):
                pointer(document, path)

    def test_boolean_is_not_an_integer_in_contract_assertions(self):
        for payload, expected in [(b'{"n":true}', 1), (b'{"n":[true]}', [1]),
                                  (b'{"n":{"k":true}}', {"k": 1})]:
            self.assertTrue(assertions(Response(200, {}, payload, 1),
                                       {"status": [200], "json": {"/n": expected}}))

    def test_null_is_not_missing(self):
        self.assertFalse(assertions(Response(200, {}, b'{"n":null}', 1),
                                    {"status": [200], "json": {"/n": None}}))
        self.assertTrue(assertions(Response(200, {}, b'{}', 1),
                                   {"status": [200], "json": {"/n": None}}))

    def test_status_headers_latency_and_malformed_json(self):
        response = Response(200, {"x-test": "yes"}, b"oops", 20)
        failures = assertions(response, {"status": [201], "headers": {"X-Test": "no"},
                                          "max_elapsed_ms": 10, "json": {"": {}}})
        self.assertEqual(len(failures), 4)

    def test_retry_after_seconds_dates_and_caps(self):
        self.assertEqual(retry_delay("10", 1, .1, 2), 2)
        self.assertEqual(retry_delay("0", 3, .1, 2), .4)
        date = format_datetime(datetime.fromtimestamp(110, timezone.utc))
        self.assertEqual(retry_delay(date, 1, .1, 2, now=100), 2)
        for value in ["not-a-date", "nan", "inf", "-1"]:
            self.assertEqual(retry_delay(value, 2, .1, 2), .2)


class TargetTests(unittest.TestCase):
    def test_loopback_only_default_and_remote_https_opt_in(self):
        for origin in ["http://127.0.0.1:1234", "http://localhost", "http://[::1]"]:
            self.assertTrue(validate_target(origin).local)
        for origin in ["http://127.0.0.1/path", "http://u:p@localhost", "http://localhost?key=x",
                       "http://localhost#x", "http://localhost:invalid", "http://localhost\\bad",
                       "http://localhost\r\n", "https://staging.example"]:
            with self.subTest(origin=origin), self.assertRaises(SuiteError):
                validate_target(origin)
        self.assertFalse(validate_target("https://staging.example", True).local)
        with self.assertRaisesRegex(SuiteError, "HTTPS"):
            validate_target("http://staging.example", True)

    def test_non_loopback_localhost_resolution_is_rejected(self):
        fake = [(2, 1, 6, "", ("198.51.100.1", 80))]
        with patch("webhook_drill.transport.socket.getaddrinfo", return_value=fake):
            result = request(validate_target("http://localhost"), "GET", "/", {}, None, 1)
        self.assertEqual(result.error, "connection")
