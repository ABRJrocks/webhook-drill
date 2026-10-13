# Suite reference

Suites are UTF-8 JSON. Unknown fields, duplicate object keys, missing secrets, invalid paths, and out-of-range numbers are configuration errors. The complete suite and fixtures are loaded before any request is sent.

## Root

| Field | Required | Meaning |
| :--- | :--- | :--- |
| `version` | Yes | Integer `1` |
| `name` | Yes | Human-readable name, up to 160 characters |
| `signing` | No | `{"provider":"github" or "stripe", "secret_env":"ENV_NAME"}` |
| `headers` | No | Shared request headers; literal strings or `{"env":"ENV_NAME"}` values |
| `scenarios` | Yes | Array of 1 to 100 scenarios |

Environment references are supported only in headers and signing configuration. Secrets are never interpolated into fixture bodies or URLs. Literal header values can use `{{run_id}}`; environment-derived values remain untouched.

A scenario has a unique `name` and 1 to 100 `steps`. A step has a unique `name` within its scenario and exactly one of `send` or `probe`. Steps execute in order. A failed step skips the remaining steps in that scenario; other scenarios continue. Give each scenario separate data identities when later scenarios should be independent.

## Send

| Field | Default | Meaning |
| :--- | :--- | :--- |
| `path` | Required | Origin-relative ASCII path; percent-encode non-ASCII characters |
| `body` | One body source required | JSON value serialized once as UTF-8 |
| `body_file` | One body source required | Raw fixture bytes inside the suite directory, including whitespace |
| `headers` | `{}` | Overrides shared headers by case-insensitive name |
| `signature` | `valid` | `valid`, `invalid`, `missing`, `tampered`, or Stripe-only `stale` |
| `repeat` | `1` | Identical logical deliveries, 1 to 100 |
| `concurrency` | `1` | Maximum workers, 1 to 20; starts are concurrent but not guaranteed simultaneous |
| `delay_ms` | `0` | Wait before the step, 0 to 10,000 milliseconds |
| `timeout` | `5` | Network timeout in seconds, 0.01 to 60 |
| `retry` | One attempt | `attempts` (1 to 10), `base_delay` (0 to 30 seconds), `max_delay` (base to 30 seconds) |
| `expect` | Required | Assertions on the final attempt of each repeated delivery |

`body` and `body_file` are mutually exclusive. Bodies and responses are capped at 1 MiB. `{{run_id}}` is replaced in body bytes before signing; no other template expressions are evaluated. Each run uses a random namespace unless `--run-id` is supplied. Reusing a run ID deliberately reuses event identities.

Retryable outcomes are connection failures, timeouts, `408`, `429`, and `5xx`. Other responses are not retried. Delays follow capped exponential backoff. `Retry-After` seconds and HTTP dates are respected up to the configured cap. Signing is regenerated on every attempt while the body stays unchanged. There is no jitter, to keep a short CI drill repeatable.

The total maximum POST budget across the suite, including repeats and retries, is 1,000. Polling GETs are separately bounded by their window and interval. This is not a throughput benchmark.

### Signing modes

- `valid`: sign the actual bytes; unsigned when the suite has no signing configuration.
- `invalid`: alter the digest so it is guaranteed to differ.
- `missing`: omit provider signatures.
- `tampered`: sign the original bytes, then append a newline. JSON remains valid, but its signature does not.
- `stale`: Stripe only; sign with a timestamp one hour in the past.

For GitHub, add the event and delivery headers appropriate for your fixture. `examples/github.json` demonstrates the signature scheme against the teaching receiver; its body is a synthetic record, not a complete GitHub event payload. Replace it with a captured, sanitized provider fixture for your real handler.

## Probe

A probe performs a GET on the same origin. It does not inherit webhook signatures.

| Field | Default | Meaning |
| :--- | :--- | :--- |
| `path` | Required | Read-only result endpoint |
| `headers` | `{}` | Overrides shared headers; use an environment reference for staging authentication |
| `timeout` | `5` | Request timeout in seconds |
| `eventually` | `0` | Polling window, 0 to 30 seconds; zero means one request |
| `interval` | `0.1` | Poll interval, 0.01 to 5 seconds |
| `expect` | Required | Assertions on a matching response or the final unsuccessful observation |

An eventual probe passes as soon as all assertions match. It does not establish that the state stays stable forever. Choose a window suitable for your queue and assert the actual business invariant.

## Expectations

Exactly one of `status` or `error` is required.

```json
{
  "status": [200, 202],
  "json": {"/result/created_tasks": 1, "/result/items/0/name": "test"},
  "headers": {"Content-Type": "application/json"},
  "max_elapsed_ms": 500
}
```

- `status` is a nonempty list of accepted HTTP status codes.
- `error` is `timeout`, `connection`, or `response_too_large`. Content assertions cannot accompany a transport-error expectation.
- `json` maps [RFC 6901 pointers](https://www.rfc-editor.org/rfc/rfc6901) to exact JSON values. The empty pointer asserts the entire document. `/a~1b` addresses a key named `a/b`; `/~0x` addresses `~x`. Array indices are zero-based. Missing and `null` differ; booleans and numbers differ.
- `headers` checks exact strings with case-insensitive header names.
- `max_elapsed_ms` checks one HTTP attempt, not the complete retry sequence. Shared CI hosts can vary in latency.

Failure diagnostics omit actual body and header values. Inspect your own server logs using the run ID when deeper debugging is needed.

## Exit codes and reports

| Code | Meaning |
| :--- | :--- |
| `0` | All scenarios passed, or dry-run validation succeeded |
| `1` | At least one assertion failed |
| `2` | Invalid configuration, missing file/secret, or report-writing failure |
| `130` | Interrupted |

JSON report schema version `1` contains the suite, run ID, start time, elapsed time, and per-scenario/per-step results. Each delivery records its attempt statuses, transport errors, timings, and redacted assertion failures. A skipped step has no deliveries. JUnit creates one testcase per step, with failure and skipped elements where appropriate.

