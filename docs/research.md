# Why Webhook Drill exists

Research checked on 13 October 2026.

## The problem

Webhook integrations cross two separate boundaries: receiving a request and committing the intended business result. A successful response alone does not establish that the second boundary behaved correctly.

The provider documentation describes concrete failure modes:

- [Stripe webhook delivery behavior](https://docs.stripe.com/webhooks): duplicate events, delivery without guaranteed ordering, retries, and signature validation over raw request bodies.
- [GitHub failed deliveries](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries): failed requests require recovery; GitHub does not automatically retry them.
- [GitHub troubleshooting](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/troubleshooting-webhooks): deliveries can arrive out of order.
- [GitHub signature validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries): a published test vector lets signing code be verified independently of the demo.

These are engineering requirements, not evidence of product adoption or market size. Webhook Drill does not claim measured business savings or novelty for the category.

## Existing options

| Tool or approach | Existing strength | Why this project takes a narrower route |
| :--- | :--- | :--- |
| [Hookdeck Event Gateway](https://hookdeck.com/docs) | Managed routing, retries, deduplication, and inspection | Test the consumer's observable business result in a versioned suite |
| [Hookdeck Outpost](https://hookdeck.com/docs/outpost/overview) | Outbound event delivery and retry infrastructure | Exercise an existing receiving workflow without adopting delivery infrastructure |
| [Webhook Tester](https://github.com/tarampampam/webhook-tester) | Capture and inspect incoming requests | Assert that duplicate deliveries do not duplicate downstream work |
| Provider CLI or manual resend | Reproduce a provider delivery | Keep repeatable multi-step scenarios and CI assertions in the application repository |

Webhook Drill complements these tools. It does not replace their hosting, tunnels, SDKs, queues, dashboards, or provider APIs.

## Decisions carried into the implementation

1. **State assertions are first-class.** A `probe` reads the application's result after a delivery sequence. There is no automatically inferred idempotency score.
2. **Fixtures preserve raw bytes.** Signing happens immediately before each attempt. Body tampering happens after signing, so it tests the raw-body boundary.
3. **Retries are bounded and visible.** Reports keep each attempt's status and timing. Retry policy is explicit and intended for a short test, not a simulation of a provider's full retry schedule.
4. **Ordering is domain-specific.** The demo uses a synthetic monotonic `revision`. It is not a Stripe event model. Stripe `created` timestamps should not be treated as a reliable ordering key; retrieve canonical provider state when the integration requires it.
5. **A broken receiver is a regression fixture.** Removing deduplication and stale-revision protection must make the corresponding drills fail.
6. **No runtime dependencies or hosted account.** JSON, HTTP, HMAC, threads, and reports use the Python standard library. Build tooling is required only to package the project.

## Boundaries

The tool can reveal observable defects before a release. It cannot prove every race, reconstruct missing upstream events, assert unobservable external side effects, or guarantee exactly-once execution. Asynchronous integrations need a read-only probe whose semantics match the business invariant being tested.

