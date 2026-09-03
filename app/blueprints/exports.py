"""CSV exports, including QuickBooks Online import layouts."""
import csv
import io
from datetime import date
from flask import Blueprint, render_template, Response
from ..models import Invoice, Payment, Contact, TimeEntry, TrustTransaction
from ..helpers import login_required

bp = Blueprint("exports", __name__, url_prefix="/exports")

QBO_INVOICE_COLUMNS = ["InvoiceNo", "Customer", "InvoiceDate", "DueDate", "Item(Product/Service)", "ItemDescription",
                       "ItemQuantity", "ItemRate", "ItemAmount"]
QBO_PAYMENT_COLUMNS = ["PaymentDate", "Customer", "InvoiceNo", "Amount", "Method", "Reference"]
QBO_CUSTOMER_COLUMNS = ["Name", "Company", "Email", "Phone", "Billing Address"]
TIME_COLUMNS = ["Id", "Date", "MatterNumber", "Matter", "Client", "User", "Hours", "Minutes", "Rate", "Amount",
                "Billable", "InvoiceNo", "ActivityCode", "Description"]
TRUST_COLUMNS = ["Id", "Date", "Type", "Client", "MatterNumber", "Matter", "Amount", "Description", "Payee",
                 "Reference", "InvoiceNo", "Cleared", "ClearedOn", "CreatedBy", "CreatedAt"]
CONTACT_COLUMNS = ["Id", "Kind", "FirstName", "LastName", "Company", "Email", "Phone", "Address", "Tags", "IsClient",
                   "Aliases", "CreatedAt"]


def _csv(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def _d(v):
    return v.strftime("%m/%d/%Y") if v else ""


def _dollars(cents):
    return f"{(cents or 0) / 100:.2f}"


def _addr(s):
    return ", ".join(l.strip() for l in (s or "").splitlines() if l.strip())


def _item(kind):
    if kind in ("time", "flat"):
        return "Legal Services"
    if kind == "expense":
        return "Reimbursable Expenses"
    return "Adjustment"


@bp.route("")
@login_required
def index():
    return render_template("exports/index.html", today=date.today())


@bp.route("/quickbooks/invoices.csv")
@login_required
def qb_invoices():
    rows = []
    invoices = Invoice.query.filter(Invoice.status.notin_(["draft", "void"])).order_by(Invoice.issued_on, Invoice.id).all()
    for inv in invoices:
        customer = inv.client.display_name if inv.client else ""
        lines = inv.lines or []
        if not lines:
            rows.append([inv.number, customer, _d(inv.issued_on), _d(inv.due_on), "Legal Services",
                         f"Invoice {inv.number}", "1", _dollars(inv.total_cents), _dollars(inv.total_cents)])
            continue
        for l in lines:
            qty = l.quantity if l.quantity not in (None, 0) else 1.0
            rows.append([inv.number, customer, _d(inv.issued_on), _d(inv.due_on), _item(l.kind),
                         (l.description or "").replace("\n", " ").strip() or _item(l.kind),
                         f"{qty:g}", _dollars(l.unit_cents if l.unit_cents else l.amount_cents), _dollars(l.amount_cents)])
    return _csv("quickbooks-invoices.csv", QBO_INVOICE_COLUMNS, rows)


@bp.route("/quickbooks/payments.csv")
@login_required
def qb_payments():
    rows = []
    for p in Payment.query.order_by(Payment.received_on, Payment.id).all():
        customer = p.client.display_name if p.client else (p.invoice.client.display_name if p.invoice and p.invoice.client else "")
        rows.append([_d(p.received_on), customer, p.invoice.number if p.invoice else "", _dollars(p.amount_cents),
                     p.method or "", p.reference or p.stripe_payment_intent or ""])
    return _csv("quickbooks-payments.csv", QBO_PAYMENT_COLUMNS, rows)


@bp.route("/quickbooks/customers.csv")
@login_required
def qb_customers():
    rows = []
    for c in Contact.query.filter_by(is_client=True).order_by(Contact.company_name, Contact.last_name, Contact.first_name).all():
        rows.append([c.display_name, c.company_name or "", c.email or "", c.phone or "", _addr(c.address)])
    return _csv("quickbooks-customers.csv", QBO_CUSTOMER_COLUMNS, rows)


@bp.route("/time.csv")
@login_required
def time_csv():
    rows = []
    for t in TimeEntry.query.order_by(TimeEntry.date, TimeEntry.id).all():
        m = t.matter
        rows.append([t.id, t.date.isoformat() if t.date else "", m.number if m else "", m.name if m else "",
                     m.client.display_name if m and m.client else "", t.user.name if t.user else "",
                     f"{t.hours:.2f}", t.minutes, _dollars(t.rate_cents), _dollars(t.amount_cents),
                     "yes" if t.billable else "no", t.invoice.number if t.invoice else "", t.activity_code or "",
                     (t.description or "").replace("\n", " ")])
    return _csv("time-entries.csv", TIME_COLUMNS, rows)


@bp.route("/trust.csv")
@login_required
def trust_csv():
    rows = []
    for tx in TrustTransaction.query.order_by(TrustTransaction.date, TrustTransaction.id).all():
        m = tx.matter
        rows.append([tx.id, tx.date.isoformat() if tx.date else "", tx.type, tx.client.display_name if tx.client else "",
                     m.number if m else "", m.name if m else "", _dollars(tx.amount_cents), tx.description or "",
                     tx.payee or "", tx.reference or "", tx.invoice.number if tx.invoice else "",
                     "yes" if tx.cleared else "no", tx.cleared_on.isoformat() if tx.cleared_on else "",
                     tx.created_by.name if tx.created_by else "", tx.created_at.isoformat() if tx.created_at else ""])
    return _csv("trust-ledger.csv", TRUST_COLUMNS, rows)


@bp.route("/contacts.csv")
@login_required
def contacts_csv():
    rows = []
    for c in Contact.query.order_by(Contact.company_name, Contact.last_name, Contact.first_name).all():
        rows.append([c.id, c.kind, c.first_name or "", c.last_name or "", c.company_name or "", c.email or "",
                     c.phone or "", _addr(c.address), c.tags or "", "yes" if c.is_client else "no",
                     "; ".join(a.strip() for a in (c.aliases or "").splitlines() if a.strip()),
                     c.created_at.isoformat() if c.created_at else ""])
    return _csv("contacts.csv", CONTACT_COLUMNS, rows)
