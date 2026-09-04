# Coil

Coil is practice management for solo and small law firms. Built after reading what solos actually use in
practice-management software: intake, an engagement letter that gets signed, billing (flat fee first),
collecting payment, and a trust ledger that reconciles. Everything else is deliberately left out.

## What it does

- **Intake to signed engagement in one flow.** Public intake form, lead review with an inline conflict
  preview, one-click convert to contact + matter + fee schedule + engagement letter, sent for click-to-sign.
- **Engagement letters with open and signature tracking.** Every view is logged (pixel + page load), the
  signature record carries signer name, IP, user agent, timestamp, document hash and signature hash, and a
  signed PDF goes to both sides.
- **Flat fee as the default billing mode.** Milestones (retainer on signing, balance on completion) are
  invoiced by checkbox. Hourly, contingency and hybrid also supported. Timer rounds to 6 minutes.
- **Invoices with open tracking and online pay.** Public invoice page with ACH (no fee) and card (optional
  surcharge, never on ACH). Stripe Checkout, webhook-driven, idempotent.
- **Trust (IOLTA) ledger.** Per-client and per-matter balances, no-negative guard, apply trust to invoice,
  three-way reconciliation (bank vs book vs client ledgers) with outstanding items.
- **Client portal by magic link.** Matters, invoices, shared documents, client uploads, letters to sign.
- **Conflict check** with fuzzy matching across contacts, aliases, parties, matters, notes and leads.
- **Tasks, deadlines, statute-of-limitations dates, month calendar, ICS feed.**
- **Documents** per matter with portal sharing. No versioning on purpose.
- **Two-way texting** via Twilio. **Reports:** AR aging, WIP, revenue, trust balances, productivity.
- **QuickBooks Online CSV exports** for invoices, payments and customers.
- **Daily agenda and reminder emails** from a cron-friendly CLI.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python seed.py          # owner@example.com / password123 plus demo data
.venv/bin/python wsgi.py          # http://localhost:5055
```

Without SMTP configured, outbound email is captured at `/dev/outbox` (owner only). Without Stripe keys,
pay pages explain that online payment is not set up. Without Twilio, texts are stored but not sent.

## Deploy (idcprojects VPS)

```bash
scp -r -i ~/.ssh/id_ed25519 . root@idcprojects.tail15f079.ts.net:/home/deploy/apps/practice.iandolan.com/
ssh -i ~/.ssh/id_ed25519 root@idcprojects.tail15f079.ts.net 'cd /home/deploy/apps/practice.iandolan.com && docker compose up -d --build'
```

Set `BASE_URL`, `SECRET_KEY`, SMTP, Stripe and Twilio values in `.env` on the server. Point the Stripe
webhook at `BASE_URL/webhooks/stripe` and the Twilio inbound webhook at `BASE_URL/webhooks/twilio`.

Cron on the server:

```
15 7 * * *  cd /home/deploy/apps/practice.iandolan.com && docker compose exec -T web python -m app.cli agenda
30 7 * * *  cd /home/deploy/apps/practice.iandolan.com && docker compose exec -T web python -m app.cli reminders
```

## Money handling notes

- All amounts are integer cents.
- Card surcharge is a firm setting in basis points and is only ever added on card payments. Check your
  state's surcharge rules and card-network caps before enabling it.
- Processor fees on trust deposits must be funded from operating so the client is credited the gross
  amount. The trust overview shows the running total owed.
- The trust ledger refuses any disbursement that would take a client or matter below zero.

## Tests

```bash
.venv/bin/python seed.py && .venv/bin/python -m pytest -q
```
