# Security and operational boundaries

Webhook Drill is a local/CI test tool that sends deliberately repeated and invalid requests. Use it against local or staging endpoints you control with test data and dedicated staging credentials.

## Defaults

- Loopback targets only unless `--allow-remote` is specified. Remote targets require HTTPS.
- TLS certificates are verified. HTTP redirects are never followed.
- Ambient HTTP proxy environment settings are not inherited.
- Payload and response bodies are capped at 1 MiB. Worker count, retries, delays, and suite POST budget are bounded.
- Fixture paths must resolve inside the suite directory. No shell commands, dynamic imports, or code evaluation come from a suite.
- Provider signature and transport-controlled headers cannot be overridden in suite headers.
- Secrets come from named environment variables. Reports omit bodies, headers, signatures, target URLs, and resolved credentials.

## Limits to understand

This is an early release, not an audited production service. The demo is an in-memory teaching server with synthetic events and a public demonstration secret. It binds to loopback and must not be deployed or exposed through a tunnel.

The network timeout uses the standard library's socket timeout plus checks while reading a response. It is not a hard wall-clock deadline: DNS resolution and a blocked socket operation can extend it. Run CI with an outer job timeout. Successful state probes establish only the properties checked at the time they match.

The runner does not guarantee exactly-once processing, protect an application from malicious webhook traffic, reconcile missed upstream events, or test live payment operations. A receiver's `200` response without an application-state assertion is only an HTTP check.

Suites, names, and fixtures remain operator-controlled input. Keep sensitive values out of scenario names, use sanitized fixtures, and review suites before execution. Reports are redacted by design, but your own application's logs may contain payloads.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature on this repository. Include the affected version, a minimal local reproduction using synthetic data, and expected versus observed behavior. Do not put real credentials or customer payloads in a public issue.

