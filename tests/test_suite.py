import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

from webhook_drill.signing import delivery_signature, sign
from webhook_drill.suite import SuiteError, load_suite


def minimal():
    return {"version": 1, "name": "test", "scenarios": [{"name": "a", "steps": [
        {"name": "send", "send": {"path": "/hook", "body": {"id": "{{run_id}}"},
                                  "expect": {"status": [200]}}}]}]}


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "suite.json"

    def load(self, data, env=None):
        self.path.write_text(json.dumps(data))
        return load_suite(self.path, "test-run", env or {})

    def test_templates_and_default_budget(self):
        suite = self.load(minimal())
        self.assertEqual(json.loads(suite.scenarios[0].steps[0].body), {"id": "test-run"})
        self.assertEqual(suite.delivery_budget, 1)

    def test_raw_fixture_is_preserved(self):
        raw = b'{  "id" : "{{run_id}}", "note": "caf\xc3\xa9" }\n'
        (self.path.parent / "event.json").write_bytes(raw)
        data = minimal()
        spec = data["scenarios"][0]["steps"][0]["send"]
        del spec["body"]
        spec["body_file"] = "event.json"
        self.assertEqual(self.load(data).scenarios[0].steps[0].body,
                         raw.replace(b"{{run_id}}", b"test-run"))

    def test_invalid_inputs_rejected(self):
        cases = []
        for key, value in [("repeet", 2), ("repeat", True), ("repeat", 0),
                           ("timeout", float("nan")), ("path", "//evil.test"),
                           ("path", "https://evil.test"), ("path", "/bad\r\n"),
                           ("signature", "stale"), ("expect", {"status": [200], "error": "timeout"}),
                           ("expect", {"json": {"bad": 1}}),
                           ("headers", {"Content-Length": "2"}),
                           ("headers", {"Authorization": "a\r\nb"}),
                           ("headers", {"x-A": "a", "X-a": "b"}),
                           ("retry", {"attempts": 100})]:
            data = minimal()
            data["scenarios"][0]["steps"][0]["send"][key] = value
            cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(SuiteError):
                self.load(data)

    def test_missing_secret_fails_before_execution(self):
        data = minimal()
        data["signing"] = {"provider": "github", "secret_env": "KEY"}
        with self.assertRaisesRegex(SuiteError, "missing environment"):
            self.load(data)

    def test_header_environment_reference_and_override(self):
        data = minimal()
        data["headers"] = {"Authorization": {"env": "AUTH"}, "X-Test": "old"}
        data["scenarios"][0]["steps"][0]["send"]["headers"] = {"x-test": "new"}
        headers = self.load(data, {"AUTH": "Bearer private"}).scenarios[0].steps[0].headers
        self.assertEqual(headers, {"Authorization": "Bearer private", "x-test": "new"})

    def test_fixture_escape_is_rejected(self):
        data = minimal()
        spec = data["scenarios"][0]["steps"][0]["send"]
        del spec["body"]
        spec["body_file"] = "../outside.json"
        with self.assertRaisesRegex(SuiteError, "inside the suite directory"):
            self.load(data)

    def test_duplicate_names_rejected(self):
        data = minimal()
        data["scenarios"].append(data["scenarios"][0])
        with self.assertRaisesRegex(SuiteError, "unique"):
            self.load(data)

    def test_duplicate_json_keys_rejected(self):
        self.path.write_text('{"version":1,"version":1}')
        with self.assertRaisesRegex(SuiteError, "duplicate JSON"):
            load_suite(self.path, "a", {})

    def test_delivery_budget_includes_retries(self):
        data = minimal()
        spec = data["scenarios"][0]["steps"][0]["send"]
        spec.update(repeat=100, retry={"attempts": 10})
        self.assertEqual(self.load(data).delivery_budget, 1000)
        data["scenarios"][0]["steps"].append({"name": "overflow", "send": {
            "path": "/hook", "body": {}, "expect": {"status": [200]}}})
        with self.assertRaisesRegex(SuiteError, "delivery budget"):
            self.load(data)


class SigningTests(unittest.TestCase):
    def test_github_official_test_vector(self):
        expected = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
        self.assertEqual(sign("github", "It's a Secret to Everybody", b"Hello, World!", 1),
                         {"X-Hub-Signature-256": expected})

    def test_stripe_timestamp_and_unicode_bytes(self):
        body = '{"name":"Rafay \u2605"}'.encode()
        digest = hmac.new(b"test", b"123." + body, hashlib.sha256).hexdigest()
        self.assertEqual(sign("stripe", "test", body, 123),
                         {"Stripe-Signature": "t=123,v1=" + digest})

    def test_tampering_changes_body_after_signing(self):
        body, headers = delivery_signature("github", "key", b"{}", 10, "tampered")
        self.assertEqual(body, b"{}\n")
        self.assertNotEqual(headers, sign("github", "key", body, 10))

    def test_invalid_signature_is_guaranteed_different(self):
        _, headers = delivery_signature("github", "key", b"{}", 10, "invalid")
        self.assertNotEqual(headers, sign("github", "key", b"{}", 10))

    def test_missing_and_stale(self):
        self.assertEqual(delivery_signature("stripe", "key", b"{}", 4000, "missing"), (b"{}", {}))
        self.assertEqual(delivery_signature("stripe", "key", b"{}", 4000, "stale")[1],
                         sign("stripe", "key", b"{}", 400))
