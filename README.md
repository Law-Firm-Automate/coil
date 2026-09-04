# Coil

Coil is practice management for solo and small law firms, offered at cost. No per-seat plans, no add-ons,
no yearly price creep. Built after reading what solos actually use in
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
- **Billing depth:** UTBMS codes and LEDES 1998B export, bulk invoicing, an approval workflow, split billing across
  payers, interest on overdue balances, evergreen retainer top-up requests, and per-matter currency.
- **Firm structure:** five roles with a permission matrix, offices, a searchable audit log, matter templates that
  create milestones, tasks, custom fields and limitations dates, and custom fields on contacts.
- **Reports:** origination, realization and matter profitability, plus a dashboard each user arranges.
- **Client-facing:** secure portal messaging, click-to-sign on any document with a certificate PDF, and Spanish on
  every client page and email when a contact prefers it.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python seed.py          # owner@example.com / password123 plus demo data
.venv/bin/python wsgi.py          # http://localhost:5055
```

Without SMTP configured, outbound email is captured at `/dev/outbox` (owner only). Without Stripe keys,
pay pages explain that online payment is not set up. Without Twilio, texts are stored but not sent.

## Run it yourself (free)

One container, one database file. Mac or Linux with Docker:

```bash
curl -fsSL https://raw.githubusercontent.com/Coil-Legal/coil/main/install.sh | sh
```

Then open http://localhost:8080 and create the owner account. Windows, email, backups, remote
access and updates are covered in [docs/SELF-HOSTING.md](docs/SELF-HOSTING.md).

Hosted for you instead: request beta access at https://lawfirmautomate.com/coil.

## Deploying behind a reverse proxy

`docker-compose.yml` carries Traefik labels and reads `DOMAIN` from `.env`. Any proxy that
terminates HTTPS and forwards to port 8000 works. Set `BASE_URL` to the public address so links
in emails resolve. Point the Stripe webhook at `BASE_URL/webhooks/stripe` and the Twilio inbound
webhook at `BASE_URL/webhooks/twilio`.

Scheduled jobs (cron on the host):

```
15 7 * * *  docker compose exec -T web python -m app.cli agenda
30 7 * * *  docker compose exec -T web python -m app.cli reminders
0  8 * * *  docker compose exec -T web python -m app.cli sequences
*/10 * * * * docker compose exec -T web python -m app.cli emailin
*/15 * * * * docker compose exec -T web python -m app.cli webhooks
0  6 1 * *  docker compose exec -T web python -m app.cli interest
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

## License

AGPL-3.0. Run it, change it, host it for others; if you host a modified version, share the changes.
