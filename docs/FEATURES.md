# Coil features, by where they come from

Every feature is filed under the company whose feature it mirrors. The last group is what no competitor does. Rendered live at /features inside the app; source of truth is `app/feature_map.py`.


## Clio Manage

Core practice management. Clio's plans run $49 to $149 per user per month.

- Client statement: every invoice, payment and trust application with a running balance (`/statements`) · built
- Invoice template editor: logo, colour, columns, labels, payment wording, preview (`/settings/invoice-template`) · built
- Contacts and matters with custom fields (`/matters`) · built
- Time tracking with a timer (6-minute rounding) (`/time`) · built
- Expenses with receipt capture (`/time/expenses`) · built
- Invoicing: flat fee, hourly, contingency, hybrid (`/invoices`) · built
- Bulk invoicing and approval workflow (`/invoices/bulk`) · built
- Split billing across payers (`/matters`) · built
- Interest on overdue balances (`/invoices`) · built
- Trust (IOLTA) accounting with three-way reconciliation (`/trust/`) · built
- Evergreen retainer top-up requests (`/trust/`) · built
- Tasks, deadlines, limitations dates (`/tasks`) · built
- Calendar with recurrence, per-user calendars, ICS feeds (`/calendar`) · built
- Court rules calendaring (generic starter sets) (`/settings/rules`) · partial
- Document automation from DOCX and HTML templates (`/doctemplates`) · built
- Client portal with magic-link login (`/portal/login`) · built
- Secure client messaging in the portal (`/messages`) · built
- Conflict check across names, files and messages (`/conflicts`) · built
- Matter templates with task workflows (`/settings/templates`) · built
- Roles and permissions, offices, audit log (`/settings`) · built
- Reports: AR aging, WIP, revenue, productivity, origination, realization, profitability (`/reports`) · built
- UTBMS codes and LEDES 1998B export (`/exports`) · built
- Multi-currency matters and invoices (`/matters`) · built
- Mobile apps · planned
- Chrome time-tracking extension (`/settings/api`) · built

## Clio Grow

Clio's separate intake and CRM product. In Coil it is one flow with the matter.

- Public intake form and lead list (`/intake`) · built
- Intake to signed engagement letter in one step (`/intake`) · built
- Engagement letters with open and signature tracking (`/engagements`) · built
- Lead pipeline (kanban) and follow-up sequences (`/intake/pipeline`) · built

## Clio Manage AI

The $10 add-on people on Reddit actually use: dates from documents, client update emails, monthly invoicing, AR questions.

- Scheduled monthly invoicing per matter (python -m app.cli monthly_invoicing) (`/invoices/bulk`) · built
- Draft client update email from notes, work, tasks and dates (`/matters`) · built
- A/R and hours questions answered from your data without the model (`/ai/search`) · built
- Pull dates from a document into tasks and calendar (`/documents`) · built
- Matter summary (`/matters`) · built
- Invoice narrative cleanup (`/invoices`) · built
- Natural-language search (`/ai/search`) · built

## Clio Work

Research and drafting. Several Reddit users say it replaces Westlaw or Lexis at about $200 per attorney.

- Case law search on CourtListener (free public database) (`/research`) · built
- Full opinion reader with optional AI summary and holding (`/research`) · built
- Save authorities to a matter with notes (`/research/saved`) · built
- Research memo export as a PDF filed on the matter (`/research/saved`) · built
- Citation check that flags citations which do not resolve (`/research/cite-check`) · built

## Clio Payments / LawPay

Card and ACH processing with trust-safe handling. Coil uses your own Stripe account at Stripe rates.

- Public invoice page with ACH (no fee) and card (surcharge you set) (`/invoices`) · built
- Stripe Checkout with idempotent webhooks (`/settings/integrations`) · built
- Online trust deposits (`/trust/`) · built
- Invoice open tracking (`/invoices`) · built

## Filevine

Personal-injury case management: providers, records, liens, settlement.

- PI case facts and stage board (`/pi`) · built
- Provider and records tracking with request letters (`/pi`) · built
- Lien tracking with reduction letters (`/pi`) · built
- Demand package builder (`/pi`) · built
- Settlement disbursement worksheet with trust postings (`/pi`) · built
- Standard PI task set (`/pi`) · built

## NetDocuments

Document management.

- Documents per matter, shared to the portal (`/documents`) · built
- Versions, folders, tags (`/documents`) · built
- Full-text search inside files (`/documents/search`) · built
- Email filing to matters (IMAP) (`/messages/unfiled`) · built

## HubSpot

What one Reddit user said he would use instead of Grow.

- Pipeline stages with drag and drop (`/intake/pipeline`) · built
- Follow-up sequences (drafts unless the firm turns on sending) (`/intake/sequences`) · built

## PracticePanther

The simple, cheaper suite several solos in the thread prefer.

- Two-way texting attached to the matter (`/messages`) · built
- E-signature on any document (`/signatures`) · built
- Custom dashboard (`/dashboard/customize`) · built

## LeanLaw / QuickBooks

Billing that lives on QuickBooks Online.

- QuickBooks CSV exports (invoices, payments, customers) (`/exports`) · built
- Operating ledger, bank import, reconciliation, P&L (`/accounting/`) · built
- Live two-way QuickBooks sync · planned

## TimeSolv / Bill4Time / OnPoint

The $10 to $40 time-and-billing tools named as Clio alternatives.

- Time, expenses, invoices, trust in one place (`/time`) · built

## Smokeball

Known for automatic time capture from documents and email.

- Automatic time capture · planned

## Coil only

Not modelled on any competitor.

- Priced at cost, no per-seat tiers, no add-ons (`/`) · built
- Self-hosted with a free install key (`/settings`) · built
- REST API with tokens and outgoing webhooks (`/settings/api`) · built
- Spanish client-facing pages and emails (`/settings`) · built
- Installable web app (PWA) (`/`) · built
