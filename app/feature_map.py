"""Where each Coil feature comes from.

Ian organises the product by the company whose feature it mirrors, so every feature is listed
under its origin. "Coil only" is for things no named competitor does. Agents building a feature
add one entry to the right list; keep entries one line, plain English, with the in-app route.
status: built | partial | planned
"""

FEATURE_MAP = [
    {
        "company": "Clio Manage",
        "blurb": "Core practice management. Clio's plans run $49 to $149 per user per month.",
        "features": [
            ("Contacts and matters with custom fields", "/matters", "built"),
            ("Time tracking with a timer (6-minute rounding)", "/time", "built"),
            ("Expenses with receipt capture", "/time/expenses", "built"),
            ("Invoicing: flat fee, hourly, contingency, hybrid", "/invoices", "built"),
            ("Bulk invoicing and approval workflow", "/invoices/bulk", "built"),
            ("Split billing across payers", "/matters", "built"),
            ("Interest on overdue balances", "/invoices", "built"),
            ("Trust (IOLTA) accounting with three-way reconciliation", "/trust/", "built"),
            ("Evergreen retainer top-up requests", "/trust/", "built"),
            ("Tasks, deadlines, limitations dates", "/tasks", "built"),
            ("Calendar with recurrence, per-user calendars, ICS feeds", "/calendar", "built"),
            ("Court rules calendaring (generic starter sets)", "/settings/rules", "partial"),
            ("Document automation from DOCX and HTML templates", "/doctemplates", "built"),
            ("Client portal with magic-link login", "/portal/login", "built"),
            ("Secure client messaging in the portal", "/messages", "built"),
            ("Conflict check across names, files and messages", "/conflicts", "built"),
            ("Matter templates with task workflows", "/settings/templates", "built"),
            ("Roles and permissions, offices, audit log", "/settings", "built"),
            ("Reports: AR aging, WIP, revenue, productivity, origination, realization, profitability", "/reports", "built"),
            ("UTBMS codes and LEDES 1998B export", "/exports", "built"),
            ("Multi-currency matters and invoices", "/matters", "built"),
            ("Mobile apps", "", "planned"),
            ("Chrome time-tracking extension", "/settings/api", "built"),
        ],
    },
    {
        "company": "Clio Grow",
        "blurb": "Clio's separate intake and CRM product. In Coil it is one flow with the matter.",
        "features": [
            ("Public intake form and lead list", "/intake", "built"),
            ("Intake to signed engagement letter in one step", "/intake", "built"),
            ("Engagement letters with open and signature tracking", "/engagements", "built"),
            ("Lead pipeline (kanban) and follow-up sequences", "/intake/pipeline", "built"),
        ],
    },
    {
        "company": "Clio Manage AI",
        "blurb": "The $10 add-on people on Reddit actually use: dates from documents, client update emails, monthly invoicing, AR questions.",
        "features": [
            ("Pull dates from a document into tasks and calendar", "/documents", "built"),
            ("Matter summary", "/matters", "built"),
            ("Invoice narrative cleanup", "/invoices", "built"),
            ("Natural-language search", "/ai/search", "built"),
        ],
    },
    {
        "company": "Clio Work",
        "blurb": "Research and drafting. Several Reddit users say it replaces Westlaw or Lexis at about $200 per attorney.",
        "features": [],
    },
    {
        "company": "Clio Payments / LawPay",
        "blurb": "Card and ACH processing with trust-safe handling. Coil uses your own Stripe account at Stripe rates.",
        "features": [
            ("Public invoice page with ACH (no fee) and card (surcharge you set)", "/invoices", "built"),
            ("Stripe Checkout with idempotent webhooks", "/settings/integrations", "built"),
            ("Online trust deposits", "/trust/", "built"),
            ("Invoice open tracking", "/invoices", "built"),
        ],
    },
    {
        "company": "Filevine",
        "blurb": "Personal-injury case management: providers, records, liens, settlement.",
        "features": [],
    },
    {
        "company": "NetDocuments",
        "blurb": "Document management.",
        "features": [
            ("Documents per matter, shared to the portal", "/documents", "built"),
            ("Versions, folders, tags", "/documents", "built"),
            ("Full-text search inside files", "/documents/search", "built"),
            ("Email filing to matters (IMAP)", "/messages/unfiled", "built"),
        ],
    },
    {
        "company": "HubSpot",
        "blurb": "What one Reddit user said he would use instead of Grow.",
        "features": [
            ("Pipeline stages with drag and drop", "/intake/pipeline", "built"),
            ("Follow-up sequences (drafts unless the firm turns on sending)", "/intake/sequences", "built"),
        ],
    },
    {
        "company": "PracticePanther",
        "blurb": "The simple, cheaper suite several solos in the thread prefer.",
        "features": [
            ("Two-way texting attached to the matter", "/messages", "built"),
            ("E-signature on any document", "/signatures", "built"),
            ("Custom dashboard", "/dashboard/customize", "built"),
        ],
    },
    {
        "company": "LeanLaw / QuickBooks",
        "blurb": "Billing that lives on QuickBooks Online.",
        "features": [
            ("QuickBooks CSV exports (invoices, payments, customers)", "/exports", "built"),
            ("Operating ledger, bank import, reconciliation, P&L", "/accounting/", "built"),
            ("Live two-way QuickBooks sync", "", "planned"),
        ],
    },
    {
        "company": "TimeSolv / Bill4Time / OnPoint",
        "blurb": "The $10 to $40 time-and-billing tools named as Clio alternatives.",
        "features": [
            ("Time, expenses, invoices, trust in one place", "/time", "built"),
        ],
    },
    {
        "company": "Smokeball",
        "blurb": "Known for automatic time capture from documents and email.",
        "features": [
            ("Automatic time capture", "", "planned"),
        ],
    },
    {
        "company": "Coil only",
        "blurb": "Not modelled on any competitor.",
        "features": [
            ("Priced at cost, no per-seat tiers, no add-ons", "/", "built"),
            ("Self-hosted with a free install key", "/settings", "built"),
            ("REST API with tokens and outgoing webhooks", "/settings/api", "built"),
            ("Spanish client-facing pages and emails", "/settings", "built"),
            ("Installable web app (PWA)", "/", "built"),
        ],
    },
]
