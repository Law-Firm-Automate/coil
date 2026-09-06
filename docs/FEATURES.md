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

## Eve Legal

Plaintiff-side AI: records into chronologies, demands, discovery, depositions, a nightly case audit, case scoring.

- Discovery drafting: propound from starter sets tailored by AI, or parse a served set and draft responses with objections (`/discovery`) · built
- Deposition summaries with page:line citations and contradictions against the chronology (`/discovery/depositions`) · built
- Nightly case audit: records, gaps, imaging, limitations, liens, demands (rules) plus AI flags labelled as such (`/audit`) · built
- Case scoring at intake and on PI cases, with the factors shown (`/intake/pipeline`) · built
- Medical record extraction into a treatment chronology, with page references and a confirm step (`/pi`) · built
- Case overview written from the documents, chronology, providers, liens and notes (`/pi`) · built
- Narrative demand letter drafted in the firm's voice from style-example letters, saved as a PDF (`/pi`) · built

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

## Switching from Clio / MyCase / PracticePanther

Bring a whole practice across from CSV exports, one file at a time, with a preview and a column mapper.

- Contacts: person and company detection, first non-empty email and phone, tags, aliases (`/import`) · built
- Matters: linked to clients by the old id, name or email; old numbers kept when free (`/import`) · built
- Time and expenses from one activities file; billed rows arrive non-billable and marked (`/import`) · built
- Bills with balances: one invoice per old bill plus a payment, so A/R matches (`/import`) · built
- Trust ledger in date order; rows that would go negative are refused and listed; opening balances form (`/import`) · built
- Tasks and calendar with matter, assignee, done flag, start and end (`/import`) · built
- Documents by matter folder from a ZIP, filed under Imported (`/import`) · built
- Re-import updates instead of duplicating (Coil remembers the old system's ids) (`/import`) · built
- Manual column mapping for any CSV, failed rows downloadable for a second pass (`/import`) · built
- Export steps guide per source (`/import/guide`) · built

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

- Time capture suggestions from the browser extension: tabs become suggested entries with a guessed matter, accept or dismiss (`/time/suggestions`) · built

## Calendly / Lawmatics

Self-scheduled consults, paid or free, with intake questionnaires.


## Hona / Case Status

A client-facing stage timeline with automatic updates.


## Kenect / Podium

Review requests by text and email after a matter closes.


## CosmoLex

Fee splits and compensation reporting.

- Fee splits per matter: originating, working and referral percentages (`/matters`) · built
- Compensation report: collected fees allocated by working split, originating credit alongside, CSV (`/reports/compensation`) · built

## Gravity Legal

Cards on file and payment plans.

- Card on file with client consent (emailed link or from the portal), stored at Stripe (`/contacts`) · built
- Charge an invoice to the card on file, surcharge per firm settings (`/invoices`) · built
- Payment plans: weekly, biweekly or monthly installments, auto-charged or emailed (python -m app.cli payment_plans) (`/money/plans`) · built

## Gavel / Documate

Guided questionnaire-to-document generation.


## CaseFleet / TrialPad

Fact chronology tied to evidence, witnesses and exhibits; trial notebook.


## CourtDrive / Docketbird

Docket alerts on followed cases (via CourtListener RECAP).


## Adobe / Bates tools

Bates numbering, exhibit stamps, redaction boxes, table of authorities.


## Ruby / Smith.ai

Phone intake that lands in the pipeline.

- Phone intake API: POST /api/v1/leads creates and scores a lead, idempotent on an external reference (`/settings/api`) · built
- Voice intake agent hook: the after-hours DUI agent posts a finished intake to Coil when COIL_LEADS_URL is set (`/intake`) · built
- Client case status by phone after caller id plus name verification (stage, next event, next task, balance, last update date) (`/settings/voice`) · built
- Attorney voice line: caller id plus PIN, then dictate a note or log time on a matter by number or description (`/settings/voice`) · built
- Outbound reminder calls before calendar events and court dates (python -m app.cli voice_reminders) (`/settings/voice`) · built
- Call log: every voice call with summary, transcript, outcome and links to the lead, contact and matter (`/voice`) · built

## Vertical case types

Practice-area modules beyond PI.

- Criminal defense: stage board, case facts, charges with attorney-entered ranges, court date chain, speedy-trial check, disposition PDF (`/criminal`) · built

## Coil only

Not modelled on any competitor.

- Priced at cost, no per-seat tiers, no add-ons (`/`) · built
- Self-hosted with a free install key (`/settings`) · built
- REST API with tokens and outgoing webhooks (`/settings/api`) · built
- Spanish client-facing pages and emails (`/settings`) · built
- Installable web app (PWA) (`/`) · built
