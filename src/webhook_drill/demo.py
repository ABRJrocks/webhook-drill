"""Local-only teaching receiver. Never deploy this demo as an application server."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .suite import MAX_BYTES

DEMO_SECRET = "webhook-drill-demo-only"


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address=("127.0.0.1", 8765), *, secret=DEMO_SECRET, broken=False):
        super().__init__(address, DemoHandler)
        self.secret = secret
        self.broken = broken
        self.lock = threading.Lock()
        self.seen = set()
        self.records = {}
        self.failures = set()


class DemoHandler(BaseHTTPRequestHandler):
    server: DemoServer

    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def reply(self, status, payload, extra=None):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def verified(self, body):
        github = self.headers.get("X-Hub-Signature-256")
        if github:
            expected = "sha256=" + hmac.new(self.server.secret.encode(), body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, github)
        stripe = self.headers.get("Stripe-Signature", "")
        parts = [part.strip().split("=", 1) for part in stripe.split(",")]
        try:
            timestamps = [value for key, value in parts if key == "t"]
            signatures = [value for key, value in parts if key == "v1"]
            if len(timestamps) != 1 or abs(time.time()-int(timestamps[0])) > 300:
                return False
            message = timestamps[0].encode() + b"." + body
            expected = hmac.new(self.server.secret.encode(), message, hashlib.sha256).hexdigest()
            return any(hmac.compare_digest(expected, signature) for signature in signatures)
        except (ValueError, TypeError):
            return False

    def do_POST(self):
        if self.path not in {"/webhooks", "/webhooks/flaky"}:
            self.reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.reply(411, {"error": "length required"})
            return
        if not 0 < length <= MAX_BYTES:
            self.reply(413, {"error": "payload size"})
            return
        body = self.rfile.read(length)
        if not self.verified(body):
            self.reply(401, {"error": "signature rejected"})
            return
        try:
            event = json.loads(body)
            event_id, entity = event["id"], event["entity"]
            revision, status = event["revision"], event["status"]
            if (not isinstance(event_id, str) or not isinstance(entity, str)
                    or type(revision) is not int or not isinstance(status, str)):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            self.reply(400, {"error": "invalid event"})
            return
        with self.server.lock:
            if self.path == "/webhooks/flaky" and event_id not in self.server.failures:
                self.server.failures.add(event_id)
                self.reply(503, {"error": "synthetic failure"}, {"Retry-After": "0"})
                return
            record = self.server.records.setdefault(entity, {"processed": 0, "revision": 0, "status": "new"})
            # shortcut: in-memory atomic state for a local demo; real handlers need durable transactions.
            if self.server.broken or event_id not in self.server.seen:
                self.server.seen.add(event_id)
                record["processed"] += 1
                if self.server.broken or revision > record["revision"]:
                    record["revision"], record["status"] = revision, status
        self.reply(200, {"accepted": True})

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"ok": True})
        elif self.path.startswith("/state/"):
            entity = self.path.removeprefix("/state/")
            with self.server.lock:
                record = dict(self.server.records.get(entity, {"processed": 0, "revision": 0, "status": "new"}))
            self.reply(200, record)
        else:
            self.reply(404, {"error": "not found"})

