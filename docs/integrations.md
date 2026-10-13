# Integrate a drill into your workflow

## Application contract

Webhook Drill sends HTTP requests; it does not depend on FastAPI, Django, Express, or a particular workflow platform. For useful duplicate and ordering tests, your staging application must expose a read-only way to observe the result.

1. Keep sanitized webhook fixtures with the application code.
2. Use unique event and entity IDs through `{{run_id}}`.
3. Point send steps at the application's normal webhook route.
4. Point probe steps at a test-only or appropriately authenticated read-only result endpoint.
5. Assert business outcomes, not only an internal deduplication counter.

Useful invariants include one downstream task per event, one CRM record per external ID, no rollback to a stale domain revision, and zero changes from rejected authentication. Protect state-probe endpoints like other application APIs. Do not add an unauthenticated production admin endpoint solely for testing.

## GitHub Actions against your own staging service

Keep `drills/orders.json` in your application repository. This recipe checks out the suite, installs Webhook Drill from a pinned release, and publishes reports even when a drill fails.

```yaml
name: Webhook reliability
on: [workflow_dispatch]
permissions:
  contents: read
jobs:
  drill:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install 'git+https://github.com/ABRJrocks/webhook-drill.git@v0.1.0'
      - name: Run staging drill
        env:
          WEBHOOK_DRILL_SECRET: ${{ secrets.STAGING_WEBHOOK_SECRET }}
          STAGING_AUTH: ${{ secrets.STAGING_AUTH }}
        run: >-
          webhook-drill run drills/orders.json
          --target https://staging.example.com --allow-remote
          --json-report reports/drill.json --junit reports/drill.xml
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: webhook-drill-reports
          path: reports/
```

Replace the staging origin and suite. Add an `Authorization` header environment reference when required. Store the staging signing secret in Actions secrets, not in JSON. For stricter supply-chain policies, pin actions and the installation to reviewed commit SHAs.

## Python API

The Python API uses the same loader, validation, transport, and assertions as the CLI.

```python
import uuid
from webhook_drill.runner import run_suite
from webhook_drill.suite import load_suite
from webhook_drill.transport import validate_target

suite = load_suite("drills/orders.json", run_id=uuid.uuid4().hex)
target = validate_target("http://127.0.0.1:8000")
report = run_suite(suite, target)
assert report["passed"], "Webhook reliability drill failed"
```

Configure secrets in the process environment, or pass an explicit environment mapping to `load_suite`. No report is written until you call `save_reports` or use the CLI flags.

## GitHub and Stripe fixtures

The signing adapter produces the provider's signature format, not a provider event or an API call. Capture a sanitized fixture with the structure your handler expects, configure its event headers, and point the probe at your application's result. A fixture under GitHub signing is not automatically a valid GitHub issue event; a synthetic body under Stripe signing is not a payment.

For Stripe event ordering, avoid using `created` as a strict sequence number. Use the provider's canonical object state or an integration-specific policy. The demo's monotonic revision is intentionally a generic domain example.

