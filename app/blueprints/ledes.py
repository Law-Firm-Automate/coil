"""UTBMS code sets and the LEDES 1998B file builder.

Not a blueprint. Imported by time.py (code selects), invoices.py (split lines, LEDES lookups) and exports.py
(the /exports/ledes route). Keep it free of Flask request context so the CLI can import it too.
"""
from datetime import date

# UTBMS: the common litigation task set (L100-L500), the activity set (A101-A111) and the expense set (E101-E124).
UTBMS = {
    "task": {
        "L100": "Case assessment, development and administration",
        "L110": "Fact investigation / development",
        "L120": "Analysis / strategy",
        "L130": "Experts / consultants",
        "L140": "Document / file management",
        "L150": "Budgeting",
        "L160": "Settlement / non-binding ADR",
        "L190": "Other case assessment, development and administration",
        "L200": "Pre-trial pleadings and motions",
        "L210": "Pleadings",
        "L220": "Preliminary injunctions / provisional remedies",
        "L230": "Court mandated conferences",
        "L240": "Dispositive motions",
        "L250": "Other written motions and submissions",
        "L260": "Class action certification and notice",
        "L300": "Discovery",
        "L310": "Written discovery",
        "L320": "Document production",
        "L330": "Depositions",
        "L340": "Expert discovery",
        "L350": "Discovery motions",
        "L390": "Other discovery",
        "L400": "Trial preparation and trial",
        "L410": "Fact witnesses",
        "L420": "Expert witnesses",
        "L430": "Written motions and submissions",
        "L440": "Other trial preparation and support",
        "L450": "Trial and hearing attendance",
        "L460": "Post-trial motions and submissions",
        "L470": "Enforcement",
        "L500": "Appeal",
        "L510": "Appellate motions and submissions",
        "L520": "Appellate briefs",
        "L530": "Oral argument",
    },
    "activity": {
        "A101": "Plan and prepare for",
        "A102": "Research",
        "A103": "Draft / revise",
        "A104": "Review / analyze",
        "A105": "Communicate (in firm)",
        "A106": "Communicate (with client)",
        "A107": "Communicate (other outside counsel)",
        "A108": "Communicate (other external)",
        "A109": "Appear for / attend",
        "A110": "Manage data / files",
        "A111": "Other",
    },
    "expense": {
        "E101": "Copying",
        "E102": "Outside printing",
        "E103": "Word processing",
        "E104": "Facsimile",
        "E105": "Telephone",
        "E106": "Online research",
        "E107": "Delivery services / messengers",
        "E108": "Postage",
        "E109": "Local travel",
        "E110": "Out-of-town travel",
        "E111": "Meals",
        "E112": "Court fees",
        "E113": "Subpoena fees",
        "E114": "Witness fees",
        "E115": "Deposition transcripts",
        "E116": "Trial transcripts",
        "E117": "Trial exhibits",
        "E118": "Litigation support vendors",
        "E119": "Experts",
        "E120": "Private investigators",
        "E121": "Arbitrators / mediators",
        "E122": "Local counsel",
        "E123": "Other professionals",
        "E124": "Other",
    },
}


def choices(kind):
    """[(code, 'code label'), ...] for a select, with a blank first row."""
    return [("", "(none)")] + [(c, f"{c} {label}") for c, label in UTBMS[kind].items()]


def valid_code(kind, code):
    code = (code or "").strip().upper()
    return code if code in UTBMS[kind] else ""


# Expense category -> UTBMS expense code, used as a fallback when an expense has no explicit code.
CATEGORY_TO_EXPENSE_CODE = {"Filing fee": "E112", "Postage": "E108", "Copies": "E101", "Travel": "E110",
                            "Expert": "E119", "Other": "E124"}

# LEDES timekeeper classification from the user's role.
ROLE_TO_CLASSIFICATION = {"owner": "PT", "attorney": "AS", "staff": "AS", "paralegal": "PL", "billing": "OT",
                          "readonly": "OT"}

# LEDES 1998B field 10 (EXP/FEE/INV_ADJ_TYPE) takes F (fee), E (expense), IF (invoice-level fee adjustment)
# and IE (invoice-level expense adjustment). Every invoice line kind has to land on one of them or the file
# will not sum to INVOICE_TOTAL: fees and flat/contingency fees are F, disbursements are E, and interest,
# discounts and adjustments are charges against the invoice as a whole, so they go out as IF.
LINE_TYPE = {"time": "F", "expense": "E", "flat": "F", "interest": "IF", "adjustment": "IF", "discount": "IF"}

LEDES_HEADER = "LEDES1998B[]"
LEDES_FIELDS = ["INVOICE_DATE", "INVOICE_NUMBER", "CLIENT_ID", "LAW_FIRM_MATTER_ID", "INVOICE_TOTAL",
                "BILLING_START_DATE", "BILLING_END_DATE", "INVOICE_DESCRIPTION", "LINE_ITEM_NUMBER",
                "EXP/FEE/INV_ADJ_TYPE", "LINE_ITEM_NUMBER_OF_UNITS", "LINE_ITEM_ADJUSTMENT_AMOUNT",
                "LINE_ITEM_TOTAL", "LINE_ITEM_DATE", "LINE_ITEM_TASK_CODE", "LINE_ITEM_EXPENSE_CODE",
                "LINE_ITEM_ACTIVITY_CODE", "TIMEKEEPER_ID", "LINE_ITEM_DESCRIPTION", "LAW_FIRM_ID",
                "LINE_ITEM_UNIT_COST", "TIMEKEEPER_NAME", "TIMEKEEPER_CLASSIFICATION", "CLIENT_MATTER_ID"]
assert len(LEDES_FIELDS) == 24


def _ymd(d):
    return d.strftime("%Y%m%d") if d else ""


def _amt(cents):
    return f"{int(cents or 0) / 100:.2f}"


def _txt(s, limit=15000):
    """LEDES fields cannot contain the pipe or line breaks."""
    return " ".join(str(s or "").replace("|", "/").split())[:limit]


def missing_ids(firm, invoices):
    """Human-readable list of the reasons this LEDES export must be refused: ids the file needs and does not
    have, and any invoice whose lines would not reconcile to its own INVOICE_TOTAL."""
    problems = []
    if not (firm.ledes_firm_id or "").strip():
        problems.append("Firm LEDES id (LAW_FIRM_ID) is blank. Set it under Settings.")
    seen_clients, seen_matters = set(), set()
    for inv in invoices:
        c = inv.client
        if c and c.id not in seen_clients and not (c.ledes_client_id or "").strip():
            seen_clients.add(c.id)
            problems.append(f"Client {c.display_name} has no LEDES client id.")
        m = inv.matter
        if m and m.id not in seen_matters and not (m.ledes_matter_id or "").strip():
            seen_matters.add(m.id)
            problems.append(f"Matter {m.number} has no LEDES matter id.")
    if not problems:  # the reconciliation check needs the ids to be there before it can build the records
        for inv in invoices:
            problem = unbalanced(firm, inv)
            if problem:
                problems.append(problem)
    return problems


def source_for_line(line):
    """The TimeEntry or Expense behind an invoice line. Split-group siblings carry copied lines with no source
    id, so fall back to the line at the same sort position on the group's primary invoice."""
    from ..models import Invoice, InvoiceLine, TimeEntry, Expense
    from ..extensions import db
    if line.time_entry_id:
        return db.session.get(TimeEntry, line.time_entry_id)
    if line.expense_id:
        return db.session.get(Expense, line.expense_id)
    inv = line.invoice
    if inv and inv.split_group:
        primary = (Invoice.query.filter_by(split_group=inv.split_group).order_by(Invoice.id).first())
        if primary and primary.id != inv.id:
            twin = InvoiceLine.query.filter_by(invoice_id=primary.id, sort=line.sort, kind=line.kind).first()
            if twin:
                if twin.time_entry_id:
                    return db.session.get(TimeEntry, twin.time_entry_id)
                if twin.expense_id:
                    return db.session.get(Expense, twin.expense_id)
    return None


def invoice_lines(firm, inv):
    """Yield one 24-field list per line on the invoice. Every line is emitted, whatever its kind, so that
    LINE_ITEM_TOTAL over the records equals the INVOICE_TOTAL each record carries."""
    from ..models import TimeEntry
    lines = list(inv.lines)
    # The billing period is the work period: interest and adjustments are dated when they were raised.
    dates = [l.date for l in lines if l.date and l.kind in ("time", "expense")] or [l.date for l in lines if l.date]
    start, end = (min(dates), max(dates)) if dates else (inv.issued_on, inv.issued_on)
    client_id = (inv.client.ledes_client_id or "").strip() if inv.client else ""
    matter = inv.matter
    n = 0
    for l in lines:
        n += 1
        kind = l.kind or "flat"
        rec_type = LINE_TYPE.get(kind, "F")
        amount = int(l.amount_cents or 0)
        src = source_for_line(l) if kind in ("time", "expense") else None
        task = activity = expense = ""
        tk_id = tk_name = tk_class = ""
        if kind == "time":
            units_txt = f"{float(l.quantity or 0):.2f}"
            unit_cost = int(l.unit_cents or 0)
            task = (getattr(src, "task_code", "") if src else "") or ""
            activity = (getattr(src, "activity_code", "") if src else "") or ""
            user = getattr(src, "user", None) if isinstance(src, TimeEntry) else None
            if user:
                tk_id = user.initials or str(user.id)
                tk_name = user.name or ""
                tk_class = ROLE_TO_CLASSIFICATION.get(user.role or "", "OT")
        elif kind == "expense":
            units_txt = "1.00"
            unit_cost = int(l.unit_cents or l.amount_cents or 0)
            expense = (getattr(src, "expense_code", "") if src else "") or \
                CATEGORY_TO_EXPENSE_CODE.get(getattr(src, "category", "") if src else "", "")
        elif rec_type in ("IF", "IE"):
            # An invoice-level adjustment has no units and no unit cost; the whole amount is the adjustment.
            units_txt, unit_cost = "1.00", 0
        else:
            # Flat fee, contingency fee, anything else: one unit of a fee priced at the line amount.
            units_txt, unit_cost = "1.00", amount
        # units x unit cost + adjustment = line total, using the units as they are printed in the file, so a
        # reader recomputing the record from the file gets the amount the client was charged.
        adjustment = amount - int(round(float(units_txt) * unit_cost))
        yield [
            _ymd(inv.issued_on), _txt(inv.number), _txt(client_id), _txt(matter.number if matter else ""),
            _amt(inv.total_cents), _ymd(start), _ymd(end), _txt(f"Invoice {inv.number} for {matter.name}" if matter else f"Invoice {inv.number}"),
            str(n), rec_type, units_txt, _amt(adjustment), _amt(amount),
            _ymd(l.date or inv.issued_on), _txt(task), _txt(expense), _txt(activity), _txt(tk_id),
            _txt(l.description), _txt(firm.ledes_firm_id), _amt(unit_cost), _txt(tk_name), tk_class,
            _txt(matter.ledes_matter_id if matter else ""),
        ]


class LedesImbalance(Exception):
    """Raised rather than writing a file whose records do not add up to the invoice total."""


def _cents(amount_text):
    return int(round(float(amount_text) * 100))


def unbalanced(firm, inv):
    """A sentence naming the problem when this invoice's records would not reconcile to INVOICE_TOTAL, else
    "". Tax and any hand-set total have no representation in 1998B, so they show up here."""
    rows = list(invoice_lines(firm, inv))
    line_sum = sum(_cents(r[12]) for r in rows)
    total = int(inv.total_cents or 0)
    if line_sum == total:
        return ""
    return (f"Invoice {inv.number} does not balance: its {len(rows)} line item(s) come to {_amt(line_sum)} "
            f"but INVOICE_TOTAL is {_amt(total)}. Correct the invoice before exporting it.")


def build_1998b(firm, invoices):
    """Return the full LEDES 1998B text: header, field row, then one record per invoice line.

    Refuses (LedesImbalance) rather than writing a file an e-billing validator would bounce."""
    out = [LEDES_HEADER, "|".join(LEDES_FIELDS) + "[]"]
    for inv in invoices:
        rows = list(invoice_lines(firm, inv))
        problem = unbalanced(firm, inv)
        if problem:
            raise LedesImbalance("LEDES export refused. " + problem)
        for row in rows:
            assert len(row) == 24
            out.append("|".join(row) + "[]")
    return "\n".join(out) + "\n"


def default_range(today=None):
    today = today or date.today()
    return today.replace(day=1), today
