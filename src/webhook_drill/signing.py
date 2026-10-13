"""Generate provider signatures over the exact bytes being delivered."""

from __future__ import annotations

import hashlib
import hmac


def sign(provider: str, secret: str, body: bytes, timestamp: int) -> dict[str, str]:
    if provider == "github":
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return {"X-Hub-Signature-256": "sha256=" + digest}
    if provider == "stripe":
        message = str(timestamp).encode() + b"." + body
        digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
        return {"Stripe-Signature": f"t={timestamp},v1={digest}"}
    raise ValueError("signing provider must be github or stripe")


def delivery_signature(
    provider: str, secret: str, body: bytes, timestamp: int, mode: str
) -> tuple[bytes, dict[str, str]]:
    if mode == "missing":
        return body, {}
    if mode == "stale":
        timestamp -= 3600
    headers = sign(provider, secret, body, timestamp)
    if mode == "invalid":
        headers = {key: value[:-1] + ("0" if value[-1] != "0" else "1")
                   for key, value in headers.items()}
    if mode == "tampered":
        # Whitespace keeps JSON valid while changing the signed raw bytes.
        body += b"\n"
    return body, headers

