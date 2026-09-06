"""Mapping engine for the "switch to Coil" importer.

For every (source, entity) pair there is a dict of Coil field -> accepted header aliases. Headers are
normalised (lower case, punctuation to spaces) before matching, source-specific aliases win over the generic
list, and the user can override any field's source column on the preview page. Everything here is pure
(no Flask request access) so it can be tested directly; the blueprint feeds it rows and a context.

Real export columns this was built against (checked September 2026):

Clio Manage
  Contacts table export: Name, Type (Person | Company), Company, Primary Email Address, Primary Phone Number,
    Primary Address, tags, custom fields. The bulk "Excel Ready CSV" from the Exports page splits emails and
    phones into several columns (Email Address (Work), Email Address (Home), Phone Number (Work), Phone Number
    (Mobile) ...) and carries an Id column. Migration template: first_name, last_name, company, email_address,
    primary_phone, business_street/city/state/postal_code, title, job_title.
  Matters export: Billable, Client ID, Client Location, Client Name, Client Reference, Close Date, Created Date,
    Custom Number, Description, Display Number, Group, Import, Last Modified, Number, Open Date, Originating
    Attorney, Pending Date, Practice Area, Responsible Attorney, Status (Open | Pending | Closed), Statute of
    Limitations Date, Unique ID, User. Dates MM/DD/YYYY. "Display Number" is "00012-Smith" style.
  Activities export: Type (TimeEntry | ExpenseEntry | HardCostEntry | SoftCostEntry), Date, User, Matter,
    Client, Activity Description (the category), Description/Note, Quantity (decimal hours, or count for an
    expense), Rate, Total, Billable, Non-billable, Billed, Bill state (Paid | Unpaid | blank), Created at, Updated at.
  Bills export: Bill # / ID, Client, Matter, Issued, Due, Total, Paid, Balance, State (Draft | Pending approval |
    Unpaid | Paid | Void), Bill type.
  Bank transactions export (per account, matter or contact): Date, Type, Source/Destination, Client, Matter,
    Description, Check or reference no., Payment method, Funds In, Funds Out, Cleared. No running balance in CSV.

MyCase
  Full backup ZIP with one CSV per area. Case template: Case/Matter Name, Number, Open Date, Practice Area,
    Case Description, Case Closed (TRUE), Closed Date, Lead Attorney, SOL Date, Outstanding Balance, Note.
  Contacts template: First Name, Last Name, Company, Home Phone, Work Phone, Cell Phone, Email, Address, City,
    State, Zip, Case Link IDs, Archived.

PracticePanther
  Contacts: Export to Excel (First Name, Middle Name, Last Name, Company, Email, Phone, Mobile, Address ...).
  Matters grid: Matter name, Matter number, Linked client, Assigned user, Originating attorney, Status.
  Time entries (Detailed Time report / template): Matter Number, Date (MM/DD/YYYY), Hours, Rate, Description,
    Status (Billable | Billed | NotBillable), Assigned To (user email), Item Code.
"""
import re
from datetime import datetime, date, time

from ..helpers import parse_money

SOURCES = [("clio", "Clio"), ("mycase", "MyCase"), ("practicepanther", "PracticePanther"), ("generic", "Generic CSV")]
SOURCE_LABELS = dict(SOURCES)

# Order matters: this is the order the hub tells the firm to export and upload.
ENTITIES = [
    ("contacts", "Contacts", "People and companies. Import these first."),
    ("matters", "Matters", "Cases, linked to their client by the old system's client id, name or email."),
    ("activities", "Time and expenses", "One file for both (Clio) or one file each. Billed rows come in as non-billable."),
    ("bills", "Bills", "One invoice per old bill with its balance and a payment for what was paid."),
    ("trust", "Trust ledger", "Trust or bank transactions. Add opening balances first if the export starts mid-history."),
    ("tasks", "Tasks", "To-dos and deadlines."),
    ("calendar", "Calendar", "Events with start, end and matter."),
    ("notes", "Notes", "Matter notes with date and author."),
    ("documents", "Documents", "A ZIP whose top-level folders are matter numbers or matter names."),
]
ENTITY_LABELS = {k: v for k, v, _ in ENTITIES}

# Coil fields per entity: (field, label, required, multi). "multi" fields accept several source columns and take
# the first non-empty value (Clio's Email Address (Work) / (Home) pairs).
FIELDS = {
    "contacts": [
        ("external_id", "Old system id", False, False),
        ("kind", "Type (person/company)", False, False),
        ("name", "Full name", False, False),
        ("first_name", "First name", False, False),
        ("last_name", "Last name", False, False),
        ("company_name", "Company", False, False),
        ("email", "Email", False, True),
        ("phone", "Phone", False, True),
        ("address", "Address", False, True),
        ("tags", "Tags", False, False),
        ("aliases", "Aliases", False, False),
        ("notes", "Notes", False, False),
        ("is_client", "Is client", False, False),
    ],
    "matters": [
        ("external_id", "Old system id", False, False),
        ("number", "Matter number", False, False),
        ("name", "Matter name", True, False),
        ("description", "Description", False, False),
        ("client_external_id", "Client id (old system)", False, False),
        ("client_name", "Client name", False, False),
        ("client_email", "Client email", False, False),
        ("status", "Status", False, False),
        ("practice_area", "Practice area", False, False),
        ("responsible", "Responsible attorney", False, False),
        ("opened_on", "Opened", False, False),
        ("closed_on", "Closed", False, False),
        ("hourly_rate", "Hourly rate", False, False),
        ("flat_fee", "Flat fee", False, False),
        ("billing_type", "Billing type", False, False),
        ("case_number", "Court case number", False, False),
        ("court", "Court", False, False),
        ("sol_date", "Limitations date", False, False),
    ],
    "activities": [
        ("external_id", "Old system id", False, False),
        ("type", "Type (time/expense)", False, False),
        ("date", "Date", True, False),
        ("matter_external_id", "Matter id (old system)", False, False),
        ("matter_number", "Matter number", False, False),
        ("matter_name", "Matter name", False, False),
        ("user", "User", False, False),
        ("description", "Description", False, False),
        ("category", "Activity / expense category", False, False),
        ("quantity", "Hours (or quantity)", False, False),
        ("rate", "Rate", False, False),
        ("total", "Total", False, False),
        ("billable", "Billable", False, False),
        ("billed", "Billed", False, False),
        ("task_code", "UTBMS task code", False, False),
        ("activity_code", "UTBMS activity code", False, False),
        ("expense_code", "UTBMS expense code", False, False),
    ],
    "bills": [
        ("external_id", "Old system id", False, False),
        ("number", "Bill number", False, False),
        ("client_external_id", "Client id (old system)", False, False),
        ("client_name", "Client name", False, False),
        ("matter_external_id", "Matter id (old system)", False, False),
        ("matter_number", "Matter number", False, False),
        ("matter_name", "Matter name", False, False),
        ("issued_on", "Issued", False, False),
        ("due_on", "Due", False, False),
        ("total", "Total", True, False),
        ("paid", "Paid", False, False),
        ("balance", "Balance", False, False),
        ("status", "Status", False, False),
        ("paid_on", "Paid on", False, False),
    ],
    "trust": [
        ("external_id", "Old system id", False, False),
        ("date", "Date", True, False),
        ("client_external_id", "Client id (old system)", False, False),
        ("client_name", "Client name", False, False),
        ("matter_external_id", "Matter id (old system)", False, False),
        ("matter_number", "Matter number", False, False),
        ("matter_name", "Matter name", False, False),
        ("type", "Type", False, False),
        ("amount", "Amount (signed)", False, False),
        ("funds_in", "Funds in", False, False),
        ("funds_out", "Funds out", False, False),
        ("description", "Description", False, False),
        ("payee", "Payee / source", False, False),
        ("reference", "Reference", False, False),
        ("cleared", "Cleared", False, False),
    ],
    "tasks": [
        ("external_id", "Old system id", False, False),
        ("title", "Title", True, False),
        ("matter_external_id", "Matter id (old system)", False, False),
        ("matter_number", "Matter number", False, False),
        ("matter_name", "Matter name", False, False),
        ("due_on", "Due", False, False),
        ("assignee", "Assignee", False, False),
        ("done", "Done", False, False),
        ("priority", "Priority", False, False),
        ("notes", "Notes", False, False),
    ],
    "calendar": [
        ("external_id", "Old system id", False, False),
        ("title", "Title", True, False),
        ("starts_at", "Start", True, False),
        ("ends_at", "End", False, False),
        ("all_day", "All day", False, False),
        ("matter_external_id", "Matter id (old system)", False, False),
        ("matter_number", "Matter number", False, False),
        ("matter_name", "Matter name", False, False),
        ("location", "Location", False, False),
        ("description", "Description", False, False),
    ],
    "notes": [
        ("external_id", "Old system id", False, False),
        ("matter_external_id", "Matter id (old system)", False, False),
        ("matter_number", "Matter number", False, False),
        ("matter_name", "Matter name", False, False),
        ("date", "Date", False, False),
        ("body", "Note", True, False),
        ("author", "Author", False, False),
        ("subject", "Subject", False, False),
    ],
}

_MATTER_LINK = {
    "matter_external_id": ["matter id", "matter unique id", "matter uid", "case id", "project id"],
    "matter_number": ["matter number", "matter", "matter display number", "display number", "case number", "number",
                      "matter no", "case no", "file number", "project number"],
    "matter_name": ["matter name", "matter description", "case name", "case", "project", "project name",
                    "matter title", "case matter name"],
}
_CLIENT_LINK = {
    "client_external_id": ["client id", "contact id", "client unique id", "customer id"],
    "client_name": ["client name", "client", "contact", "contact name", "customer", "linked client", "bill to"],
}

# Generic aliases. Normalised (lower case, punctuation stripped) before comparison.
GENERIC = {
    "contacts": {
        "external_id": ["id", "contact id", "unique id", "external id", "clio id", "uid"],
        "kind": ["type", "contact type", "kind", "record type"],
        "name": ["name", "full name", "contact name", "display name", "client name", "client", "contact"],
        "first_name": ["first name", "first", "given name", "firstname", "given"],
        "last_name": ["last name", "last", "surname", "family name", "lastname", "family"],
        "company_name": ["company", "company name", "organization", "organisation", "firm", "business", "org",
                         "employer"],
        "email": ["email", "e mail", "email address", "e mail address", "primary email", "primary email address",
                  "email 1 value", "email address work", "email address home", "email address other", "work email",
                  "home email", "email work", "email home"],
        "phone": ["phone", "phone number", "primary phone", "primary phone number", "mobile", "cell", "cell phone",
                  "mobile phone", "telephone", "phone 1 value", "work phone", "home phone", "phone number work",
                  "phone number mobile", "phone number home", "phone work", "phone mobile", "phone home"],
        "address": ["address", "primary address", "street", "mailing address", "home address", "address 1 formatted",
                    "billing address", "business street", "work address", "address line 1", "street address"],
        "tags": ["tags", "labels", "groups", "group membership", "category", "contact tags"],
        "aliases": ["aliases", "aka", "also known as", "other names", "nickname", "nick name"],
        "notes": ["notes", "note", "comments", "description"],
        "is_client": ["is client", "client", "client status"],
    },
    "matters": {
        "external_id": ["unique id", "id", "matter id", "external id", "case id", "uid"],
        "number": ["custom number", "number", "matter number", "display number", "case number", "matter no", "file number",
                   "matter"],
        "name": ["matter name", "name", "description", "case name", "case matter name", "matter description", "title",
                 "project name", "project"],
        "description": ["matter description", "case description", "summary", "details", "notes", "note"],
        "client_external_id": ["client id", "contact id", "client unique id"],
        "client_name": ["client name", "client", "contact", "contact name", "linked client", "customer"],
        "client_email": ["client email", "email", "contact email", "client email address"],
        "status": ["status", "matter status", "case status", "state", "case closed", "closed", "archived"],
        "practice_area": ["practice area", "area of law", "matter type", "case type", "type", "practice"],
        "responsible": ["responsible attorney", "responsible", "lead attorney", "attorney", "assigned user", "assigned to",
                        "responsible user", "owner", "responsible lawyer", "lawyer", "originating attorney"],
        "opened_on": ["open date", "opened", "opened on", "date opened", "created date", "start date", "date", "created"],
        "closed_on": ["close date", "closed", "closed on", "date closed", "closed date", "end date"],
        "hourly_rate": ["rate", "hourly rate", "matter rate", "custom rate", "billing rate", "default rate"],
        "flat_fee": ["flat fee", "flat fee amount", "fixed fee", "fee"],
        "billing_type": ["billing type", "billing method", "fee type", "fee arrangement", "bill type", "billable"],
        "case_number": ["court case number", "docket number", "cause number", "client reference", "reference"],
        "court": ["court", "location", "client location", "jurisdiction", "venue"],
        "sol_date": ["statute of limitations date", "sol date", "sol", "limitations date", "statute of limitations"],
    },
    "activities": {
        "external_id": ["id", "activity id", "entry id", "unique id", "uid"],
        "type": ["type", "entry type", "activity type", "kind"],
        "date": ["date", "activity date", "entry date", "work date"],
        "user": ["user", "timekeeper", "assigned to", "employee", "attorney", "staff", "created by", "user name",
                 "firm user", "worked by"],
        "description": ["description", "note", "notes", "narrative", "details", "memo"],
        "category": ["activity description", "category", "expense category", "activity category", "activity"],
        "quantity": ["quantity", "hours", "quantity hours", "duration", "time", "qty", "units", "hrs"],
        "rate": ["rate", "hourly rate", "price", "unit price", "unit cost"],
        "total": ["total", "amount", "total amount", "line total", "cost", "value"],
        "billable": ["billable", "is billable", "billable status", "status", "non billable"],
        "billed": ["billed", "bill state", "invoiced", "on bill", "bill status", "is billed", "billed status"],
        "task_code": ["task code", "utbms task code", "utbms task", "l code"],
        "activity_code": ["activity code", "utbms activity code", "utbms activity", "a code", "item code", "code"],
        "expense_code": ["expense code", "utbms expense code", "utbms expense", "e code"],
        **_MATTER_LINK,
    },
    "bills": {
        "external_id": ["id", "bill id", "invoice id", "unique id", "uid"],
        "number": ["bill", "bill number", "invoice", "invoice number", "invoice no", "bill no", "number", "invoice #",
                   "bill #", "reference"],
        "issued_on": ["issued", "issue date", "issued on", "invoice date", "date", "bill date", "created"],
        "due_on": ["due", "due date", "due on", "payment due"],
        "total": ["total", "amount", "invoice total", "bill total", "total amount", "grand total"],
        "paid": ["paid", "amount paid", "payments", "paid amount", "received", "total paid"],
        "balance": ["balance", "balance due", "outstanding", "amount due", "outstanding balance", "due amount",
                    "remaining"],
        "status": ["state", "status", "bill status", "invoice status", "bill state"],
        "paid_on": ["paid on", "paid date", "date paid", "payment date", "last payment", "last payment date"],
        **_CLIENT_LINK,
        **_MATTER_LINK,
    },
    "trust": {
        "external_id": ["id", "transaction id", "unique id", "uid"],
        "date": ["date", "transaction date", "posted", "posted on"],
        "type": ["type", "transaction type", "kind"],
        "amount": ["amount", "signed amount", "net", "value"],
        "funds_in": ["funds in", "deposit", "deposits", "credit", "in", "money in", "received", "debit amount in"],
        "funds_out": ["funds out", "disbursement", "disbursements", "withdrawal", "debit", "out", "money out", "paid"],
        "description": ["description", "memo", "note", "notes", "details", "purpose"],
        "payee": ["payee", "source destination", "source", "destination", "paid to", "from to", "received from",
                  "vendor", "party"],
        "reference": ["reference", "check or reference no", "check number", "check", "cheque number", "ref",
                      "reference number", "check no", "transaction number", "payment method"],
        "cleared": ["cleared", "reconciled", "clears", "cleared on", "is cleared", "bank cleared"],
        **_CLIENT_LINK,
        **_MATTER_LINK,
    },
    "tasks": {
        "external_id": ["id", "task id", "unique id", "uid"],
        "title": ["title", "task", "name", "task name", "subject", "description", "summary"],
        "due_on": ["due", "due date", "due on", "deadline", "date due", "date"],
        "assignee": ["assignee", "assigned to", "assigned user", "user", "owner", "responsible", "assigned"],
        "done": ["done", "complete", "completed", "status", "is complete", "finished", "completed at", "completed on"],
        "priority": ["priority", "importance"],
        "notes": ["notes", "note", "details", "description", "comments"],
        **_MATTER_LINK,
    },
    "calendar": {
        "external_id": ["id", "event id", "unique id", "uid"],
        "title": ["title", "subject", "summary", "event", "name", "event name", "event title"],
        "starts_at": ["start", "starts at", "start time", "start date", "starts", "begin", "date", "start date time",
                      "from"],
        "ends_at": ["end", "ends at", "end time", "end date", "ends", "finish", "end date time", "to"],
        "all_day": ["all day", "allday", "all day event", "is all day"],
        "location": ["location", "where", "place", "venue", "address"],
        "description": ["description", "notes", "note", "details", "body"],
        **_MATTER_LINK,
    },
    "notes": {
        "external_id": ["id", "note id", "unique id", "uid"],
        "date": ["date", "created", "created at", "created on", "note date", "updated at"],
        "body": ["note", "body", "notes", "detail", "details", "content", "text", "description", "message"],
        "author": ["author", "user", "created by", "by", "owner", "user name"],
        "subject": ["subject", "title", "summary"],
        **_MATTER_LINK,
    },
}

# Source-specific aliases: checked before the generic list so, for instance, Clio's "Description" is the matter
# name (Clio calls the name "Description" and the matter number "Display Number") while a generic file's
# "Description" is the description.
SOURCE_ALIASES = {
    "clio": {
        "contacts": {
            "external_id": ["id", "contact id", "unique id"],
            "kind": ["type"],
            "email": ["primary email address", "email address work", "email address home", "email address other",
                      "email address", "email"],
            "phone": ["primary phone number", "phone number work", "phone number mobile", "phone number home",
                      "phone number other", "primary phone", "phone number"],
            "address": ["primary address", "address work", "address home", "business street"],
            "notes": ["notes"],
        },
        "matters": {
            "external_id": ["unique id", "id", "matter id"],
            "number": ["custom number", "display number", "number"],
            "name": ["description", "matter description"],
            "description": ["note", "notes"],
            "client_external_id": ["client id"],
            "client_name": ["client name", "client"],
            "status": ["status"],
            "responsible": ["responsible attorney"],
            "opened_on": ["open date"],
            "closed_on": ["close date"],
            "court": ["client location", "location"],
            "case_number": ["client reference"],
            "sol_date": ["statute of limitations date"],
            "billing_type": ["billable"],
        },
        "activities": {
            "external_id": ["id", "activity id"],
            "type": ["type"],
            "matter_number": ["matter", "matter display number"],
            "matter_name": ["matter description"],
            "user": ["user", "firm user"],
            "category": ["activity description", "activity category", "expense category"],
            "description": ["description", "note", "notes"],
            "quantity": ["quantity", "quantity hours", "hours"],
            "billable": ["billable", "non billable"],
            "billed": ["billed", "bill state"],
        },
        "bills": {
            "external_id": ["id", "bill id"],
            "number": ["bill", "bill number", "invoice number", "bill #"],
            "issued_on": ["issued", "issue date"],
            "due_on": ["due", "due date"],
            "status": ["state", "status"],
            "paid": ["paid", "payments", "amount paid"],
        },
        "trust": {
            "payee": ["source destination", "source", "destination"],
            "reference": ["check or reference no", "check or reference number", "reference"],
        },
    },
    "mycase": {
        "contacts": {
            "phone": ["cell phone", "work phone", "home phone", "phone"],
            "kind": ["contact type", "type"],
            "is_client": ["is client", "client"],
        },
        "matters": {
            "name": ["case matter name", "case name", "name", "matter name"],
            "number": ["number", "case number"],
            "description": ["case description", "description"],
            "status": ["case closed", "status"],
            "closed_on": ["closed date", "close date"],
            "responsible": ["lead attorney", "attorney"],
            "sol_date": ["sol date"],
            "client_name": ["client", "client name", "contact", "contacts"],
        },
        "activities": {
            "matter_name": ["case", "case name", "case matter name"],
            "quantity": ["hours", "duration", "quantity"],
            "billable": ["billable", "status"],
            "billed": ["invoiced", "invoice", "billed"],
            "user": ["user", "staff", "employee"],
            "type": ["type", "entry type"],
        },
        "bills": {
            "number": ["invoice number", "invoice", "number"],
            "matter_name": ["case", "case name"],
            "issued_on": ["invoice date", "date"],
            "balance": ["balance due", "balance"],
        },
        "trust": {
            "matter_name": ["case", "case name"],
        },
        "tasks": {"matter_name": ["case", "case name"], "title": ["name", "task name", "task"]},
        "calendar": {"matter_name": ["case", "case name"], "title": ["name", "event name", "title"]},
        "notes": {"matter_name": ["case", "case name"]},
    },
    "practicepanther": {
        "contacts": {
            "phone": ["phone", "mobile", "mobile phone", "work phone", "home phone"],
            "kind": ["contact type", "type"],
        },
        "matters": {
            "name": ["matter name", "name"],
            "number": ["matter number", "number"],
            "client_name": ["linked client", "client", "contact"],
            "responsible": ["assigned user", "assigned to", "assigned"],
            "opened_on": ["open date", "opened", "created"],
            "hourly_rate": ["custom rate", "matter custom rate", "rate"],
            "status": ["status"],
        },
        "activities": {
            "matter_number": ["matter number", "matter"],
            "matter_name": ["matter name"],
            "quantity": ["hours", "quantity"],
            "user": ["assigned to", "user", "assigned user"],
            "billable": ["status", "billable"],
            "billed": ["status", "billed"],
            "activity_code": ["item code", "code"],
            "type": ["type", "entry type"],
        },
        "bills": {"number": ["invoice number", "invoice", "number"]},
        "tasks": {"assignee": ["assigned to", "assigned user"], "title": ["subject", "name", "task"]},
        "calendar": {"title": ["subject", "name", "title"]},
    },
    "generic": {},
}


# ---------------------------------------------------------------- headers
def norm_header(h):
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower().replace("#", " number ")).strip()


def field_defs(entity):
    return FIELDS.get(entity, [])


def aliases_for(source, entity, field):
    """Source-specific aliases first, then generic. Duplicates removed, order kept."""
    out = []
    for lst in (SOURCE_ALIASES.get(source, {}).get(entity, {}).get(field, []), GENERIC.get(entity, {}).get(field, [])):
        for a in lst:
            if a not in out:
                out.append(a)
    return out


def auto_map(source, entity, headers):
    """Return {field: "Header" or "Header A|Header B" (multi fields) or ""}. A header is used for one field only,
    except that multi fields may collect several headers."""
    normed = [(norm_header(h), h) for h in headers if h]
    used = set()
    mapping = {}
    # First pass: exact alias hits, best alias rank wins per field, fields in FIELDS order.
    for field, _label, _req, multi in field_defs(entity):
        alist = aliases_for(source, entity, field)
        hits = []
        for rank, a in enumerate(alist):
            for n, h in normed:
                if n == a and h not in used:
                    hits.append((rank, h))
        hits.sort(key=lambda t: t[0])
        chosen = []
        for _, h in hits:
            if h not in chosen:
                chosen.append(h)
            if not multi:
                break
        if multi and not chosen:
            # Prefix match for split columns like "Email Address (Work)".
            for a in alist:
                for n, h in normed:
                    if h not in used and h not in chosen and n.startswith(a + " "):
                        chosen.append(h)
        if chosen:
            used.update(chosen)
            mapping[field] = "|".join(chosen)
        else:
            mapping[field] = ""
    return mapping


def unmapped_headers(headers, mapping):
    used = set()
    for v in mapping.values():
        for h in (v or "").split("|"):
            if h:
                used.add(h)
    return [h for h in headers if h and h not in used and not h.startswith("_")]


def row_values(mapping, raw):
    """Apply the mapping to one raw row. Multi-column fields take the first non-empty value."""
    out = {}
    for field, src in mapping.items():
        val = ""
        for h in (src or "").split("|"):
            if h and (raw.get(h) or "").strip():
                val = raw.get(h).strip()
                break
        out[field] = val
    return out


# ---------------------------------------------------------------- parsers
_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d", "%d %b %Y",
                 "%d %B %Y", "%m-%d-%Y", "%Y%m%d"]
_DT_FORMATS = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S",
               "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%y %H:%M", "%m/%d/%y %I:%M %p",
               "%Y-%m-%d %I:%M %p", "%b %d, %Y %I:%M %p", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S %z"]


def parse_any_date(s):
    """ISO, MM/DD/YYYY, MM/DD/YY, 'Jan 5, 2026', or a datetime string (date part kept). None when unreadable."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    dt = parse_any_datetime(s)
    if dt:
        return dt.date()
    # ISO with timezone suffix, keep the date part
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def parse_any_datetime(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=None)
        except ValueError:
            continue
    s2 = re.sub(r"(\.\d+)?(Z|[+-]\d{2}:?\d{2})$", "", s)
    if s2 != s:
        return parse_any_datetime(s2)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.combine(datetime.strptime(s, fmt).date(), time(0, 0))
        except ValueError:
            continue
    return None


def parse_hours_to_minutes(s):
    """'1.50' hours, '1h 30m', '1:30', '90m', '0.1' -> minutes. None when unreadable."""
    s = str(s or "").strip().lower().replace(",", "")
    if not s:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?\s*(\d+(?:\.\d+)?)?\s*m?(?:in(?:ute)?s?)?$", s)
    if m and (m.group(2) is not None or "h" in s):
        hours = float(m.group(1))
        mins = float(m.group(2) or 0)
        return int(round(hours * 60 + mins))
    m = re.match(r"^(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?$", s)
    if m:
        return int(round(float(m.group(1))))
    if ":" in s:
        h, _, mm = s.partition(":")
        try:
            return int(h or 0) * 60 + int(float(mm or 0))
        except ValueError:
            return None
    try:
        return int(round(float(s) * 60))
    except ValueError:
        return None


TRUE_WORDS = {"yes", "y", "true", "t", "1", "x", "billable", "done", "complete", "completed", "closed", "paid",
              "billed", "cleared", "reconciled", "on", "checked", "all day"}
FALSE_WORDS = {"no", "n", "false", "f", "0", "", "non-billable", "nonbillable", "not billable", "notbillable", "open",
               "unbilled", "unpaid", "pending", "off", "incomplete", "uncleared", "none"}


def parse_bool(s, default=False):
    v = str(s or "").strip().lower()
    if v in TRUE_WORDS:
        return True
    if v in FALSE_WORDS:
        return False
    return default


def money_or_none(s):
    s = str(s or "").strip()
    if not s:
        return None
    try:
        return parse_money(s)
    except (ValueError, TypeError):
        return None


def clean_name(s):
    return " ".join((s or "").split())


def split_name(full):
    full = clean_name(full)
    if not full:
        return "", ""
    if "," in full:
        last, _, first = full.partition(",")
        return first.strip(), last.strip()
    parts = full.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def looks_like_company(name):
    n = (name or "").lower()
    return bool(re.search(r"\b(llc|l\.l\.c\.|inc\.?|corp\.?|corporation|ltd\.?|limited|pllc|llp|l\.p\.|lp|plc|company|"
                          r"co\.|trust|bank|group|partners|associates|holdings|enterprises|services|foundation|"
                          r"church|county|city of|state of|university|hospital|clinic|dental|law firm|pc)\b", n))


def matter_status(s):
    v = (s or "").strip().lower()
    if v in ("closed", "close", "archived", "inactive", "complete", "completed", "true", "yes", "done", "settled"):
        return "closed"
    if v in ("pending", "intake", "prospect", "lead"):
        return "pending"
    return "open"


def invoice_status(s, total, paid):
    v = (s or "").strip().lower()
    if v in ("void", "voided", "deleted", "cancelled", "canceled", "written off", "write off"):
        return "void"
    if v in ("draft", "pending approval", "pending", "unapproved", "awaiting approval"):
        return "draft"
    if total > 0 and paid >= total:
        return "paid"
    if paid > 0:
        return "partial"
    return "sent"


def activity_type(type_value, category="", quantity_present=True):
    v = (type_value or "").strip().lower().replace("_", "").replace(" ", "")
    if v in ("timeentry", "time", "t", "hours", "fee", "fees", "service", "timeactivity"):
        return "time"
    if v in ("expenseentry", "expense", "e", "hardcostentry", "softcostentry", "cost", "hardcost", "softcost",
             "disbursement", "expenses"):
        return "expense"
    if "expense" in v or "cost" in v:
        return "expense"
    if "time" in v:
        return "time"
    return "time" if quantity_present else "expense"


def trust_type(type_value, description, signed_amount):
    """deposit | disbursement | to_operating from a type column or description keywords, then the sign."""
    text = f"{type_value or ''} {description or ''}".lower()
    if re.search(r"\b(to operating|transfer to operating|apply(ied)? to (bill|invoice)|bill payment|fee transfer|"
                 r"earned fees?|payment of (bill|invoice))\b", text):
        return "to_operating"
    if re.search(r"\b(refund|return(ed)? to client)\b", text):
        return "refund"
    if re.search(r"\b(bank fee|service charge|bank charge)\b", text):
        return "bank_fee"
    if re.search(r"\b(interest)\b", text):
        return "interest"
    if re.search(r"\b(deposit|retainer|receipt|received|funds in|money in|credit)\b", text):
        return "deposit"
    if re.search(r"\b(disburse|disbursement|withdrawal|withdraw|check|cheque|payment|paid|funds out|money out|debit|"
                 r"wire out)\b", text):
        return "disbursement"
    if signed_amount is not None:
        return "deposit" if signed_amount >= 0 else "disbursement"
    return "deposit"


NEGATIVE_TRUST_TYPES = {"disbursement", "to_operating", "refund", "bank_fee"}
