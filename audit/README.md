# Coil functional audit

Each auditor writes one JSON file per slice into `audit/results/<slice>.json`:

```json
[{"group": "Clio Manage", "tool": "Client statement", "route": "/statements/<id>",
  "result": "pass|fail|partial|not testable", "checked": "what was actually exercised",
  "notes": "what went wrong, or a caveat; empty when a clean pass",
  "severity": "", "repro": ""}]
```

Rules for auditors:
- Exercise the real workflow through `app.test_client()` against a freshly seeded DB you own.
  A 200 is not a pass. Create the thing, act on it, then assert the state changed correctly.
- This is an audit, not a repair job. Do not fix code. Record the failure precisely with a repro.
- "not testable" is only for things needing a live third party (Stripe, Twilio, IMAP, xAI, a real
  browser). Say what would be needed and whether the code path degrades cleanly without it.
- Money, dates and counts get checked against expected values, not eyeballed.
