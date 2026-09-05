"""Client statements: every invoice, payment and trust application for one client with a running balance,
grouped by matter, as HTML, PDF (firm invoice template) and an email to the client.

Answers the Reddit complaint about Clio: "ever tried to print a statement showing all of the bills you've sent to
a client?" Invoices in draft or void are left out; everything else the client was sent is on it.
"""
from collections import OrderedDict
from datetime import date
from html import escape
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, Response
from ..extensions import db
from ..models import Firm, Contact, Matter, Invoice, Payment, audit
from ..helpers import login_required, current_user, parse_date, fmt_money
from ..services.mail import send_email
from .invoices import TemplatePDF, invoice_settings, _pdf_txt, _para, OPEN_STATUSES

bp = Blueprint("statements", __name__, url_prefix="/statements")

STATEMENT_STATUSES = ("sent", "viewed", "partial", "paid")


def _clients():
    """Anyone who has been invoiced, plus every contact marked as a client."""
    invoiced = {i.client_id for i in db.session.query(Invoice.client_id).distinct()}
    rows = Contact.query.filter(db.or_(Contact.is_client == True, Contact.id.in_(invoiced) if invoiced else db.false()))  # noqa: E712
    return sorted(rows.all(), key=lambda c: (c.sort_name or "").lower())


def _range_args():
    d_from = parse_date(request.args.get("from"))
    d_to = parse_date(request.args.get("to"))
    if d_from and d_to and d_to < d_from:
        d_from, d_to = d_to, d_from
    matter_id = request.args.get("matter_id", type=int) or None
    return d_from, d_to, matter_id


def build_statement(client, d_from=None, d_to=None, matter_id=None, today=None):
    """Everything on the statement, computed once for the HTML, the PDF and the email.

    Returns a dict: entries (dated rows with a running balance), groups (per matter with subtotals), totals,
    opening balance (activity before d_from), currency, and the filters used."""
    today = today or date.today()
    inv_q = Invoice.query.filter(Invoice.client_id == client.id, Invoice.status.in_(STATEMENT_STATUSES))
    if matter_id:
        inv_q = inv_q.filter(Invoice.matter_id == matter_id)
    invoices = inv_q.order_by(Invoice.issued_on, Invoice.id).all()
    inv_ids = [i.id for i in invoices]
    payments = (Payment.query.filter(Payment.invoice_id.in_(inv_ids)).order_by(Payment.received_on, Payment.id).all()
                if inv_ids else [])

    def in_range(d):
        if d_from and d and d < d_from:
            return False
        if d_to and d and d > d_to:
            return False
        return True

    opening = 0
    entries = []
    for inv in invoices:
        d = inv.issued_on or inv.created_at.date()
        if in_range(d):
            entries.append({"date": d, "kind": "invoice", "invoice": inv, "matter": inv.matter, "sort": 0,
                            "description": f"Invoice {inv.number}", "charge": inv.total_cents or 0, "credit": 0,
                            "payment": None, "currency": inv.currency or "USD"})
        elif d_from and d < d_from:
            opening += inv.total_cents or 0
    for p in payments:
        d = p.received_on or p.created_at.date()
        inv = p.invoice
        if in_range(d):
            trust = p.method == "trust"
            label = "Applied from trust" if trust else f"Payment ({p.method})"
            if p.reference:
                label += f" {p.reference}"
            entries.append({"date": d, "kind": "trust" if trust else "payment", "invoice": inv, "matter": inv.matter,
                            "sort": 1, "description": f"{label} on {inv.number}", "charge": 0,
                            "credit": p.amount_cents or 0, "payment": p, "currency": inv.currency or "USD"})
        elif d_from and d < d_from:
            opening -= p.amount_cents or 0
    entries.sort(key=lambda e: (e["date"], e["sort"], e["invoice"].id))
    running = opening
    for e in entries:
        running += e["charge"] - e["credit"]
        e["balance"] = running

    groups = OrderedDict()
    shown = [i for i in invoices if in_range(i.issued_on or i.created_at.date())]
    for inv in shown:
        g = groups.setdefault(inv.matter_id, {"matter": inv.matter, "invoices": [], "invoiced": 0, "paid": 0, "balance": 0,
                                              "overdue": 0})
        g["invoices"].append(inv)
        g["invoiced"] += inv.total_cents or 0
        g["paid"] += inv.paid_cents or 0
        g["balance"] += inv.balance_cents
        if inv.is_overdue:
            g["overdue"] += inv.balance_cents
    totals = {"invoiced": sum(g["invoiced"] for g in groups.values()),
              "paid": sum(g["paid"] for g in groups.values()),
              "balance": sum(g["balance"] for g in groups.values()),
              "overdue": sum(g["overdue"] for g in groups.values()),
              "payments": sum(e["credit"] for e in entries)}
    currencies = {i.currency or "USD" for i in shown}
    currency = next(iter(currencies)) if len(currencies) == 1 else (Firm.get().currency or "USD")
    open_balance = sum(i.balance_cents for i in invoices if i.status in OPEN_STATUSES)
    return {"client": client, "entries": entries, "groups": list(groups.values()), "totals": totals,
            "opening": opening, "closing": running, "currency": currency, "mixed": len(currencies) > 1,
            "d_from": d_from, "d_to": d_to, "matter_id": matter_id,
            "invoices": shown, "open_balance": open_balance, "today": today, "trust_balance": client.trust_balance_cents()}


@bp.route("")
@login_required
def index():
    clients = _clients()
    client_id = request.args.get("client_id", type=int)
    if client_id:
        d_from, d_to, matter_id = _range_args()
        return redirect(url_for("statements.detail", client_id=client_id, **{k: v for k, v in
                                (("from", request.args.get("from")), ("to", request.args.get("to")),
                                 ("matter_id", matter_id)) if v}))
    return render_template("statements/index.html", clients=clients)


def _client_matters(client):
    return Matter.query.filter_by(client_id=client.id).order_by(Matter.number).all()


@bp.route("/<int:client_id>")
@login_required
def detail(client_id):
    client = db.session.get(Contact, client_id) or abort(404)
    d_from, d_to, matter_id = _range_args()
    st = build_statement(client, d_from, d_to, matter_id)
    return render_template("statements/detail.html", st=st, client=client, matters=_client_matters(client),
                           firm_settings=Firm.get(), tpl=invoice_settings())


# ---------------------------------------------------------------- PDF
def render_statement_pdf(st):
    """Statement PDF in the firm's invoice template (logo, accent, labels) with the statement footer."""
    firm = Firm.get()
    tpl = invoice_settings(firm)
    cur = st["currency"]
    client = st["client"]

    def money(c):
        return _pdf_txt(fmt_money(c, cur))

    pdf = TemplatePDF(firm, f"Statement for {client.display_name}", tpl)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.heading("STATEMENT OF ACCOUNT")
    when = st["today"].strftime("%B %d, %Y")
    period = ""
    if st["d_from"] or st["d_to"]:
        period = (f"{st['d_from'].strftime('%B %d, %Y') if st['d_from'] else 'the beginning'} to "
                  f"{st['d_to'].strftime('%B %d, %Y') if st['d_to'] else when}")
    pdf.cell(0, 5, _pdf_txt(f"Statement date: {when}"), new_x="LMARGIN", new_y="NEXT")
    if period:
        pdf.cell(0, 5, _pdf_txt(f"Period: {period}"), new_x="LMARGIN", new_y="NEXT")
    if st["matter_id"]:
        m = db.session.get(Matter, st["matter_id"])
        if m:
            pdf.cell(0, 5, _pdf_txt(f"{tpl.label('matter')}: {m.label}"), new_x="LMARGIN", new_y="NEXT")
    if cur != "USD":
        pdf.cell(0, 5, _pdf_txt(f"Currency: {cur}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5, _pdf_txt(tpl.label("bill_to")), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, _pdf_txt(client.display_name), new_x="LMARGIN", new_y="NEXT")
    for line in (client.address or "").splitlines():
        if line.strip():
            pdf.cell(0, 5, _pdf_txt(line), new_x="LMARGIN", new_y="NEXT")
    if client.email:
        pdf.cell(0, 5, _pdf_txt(client.email), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Summary box
    pdf.set_font("Helvetica", "", 10)
    with pdf.table(col_widths=(58, 58, 58), text_align=("LEFT", "LEFT", "LEFT"), line_height=6,
                   borders_layout="NONE", headings_style=pdf.heading_style()) as table:
        row = table.row()
        for h in ("Invoiced", "Paid or applied", tpl.label("balance_due")):
            row.cell(h)
        row = table.row()
        row.cell(money(st["totals"]["invoiced"]))
        row.cell(money(st["totals"]["paid"]))
        row.cell(money(st["totals"]["balance"]))
    if st["totals"]["overdue"]:
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.cell(0, 6, _pdf_txt(f"Past due: {money(st['totals']['overdue'])}"), new_x="LMARGIN", new_y="NEXT")
    if st["trust_balance"] > 0:
        pdf.set_font("Helvetica", "", 9.5)
        pdf.cell(0, 6, _pdf_txt(f"Held in trust for you: {money(st['trust_balance'])}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Activity with running balance
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "Activity", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    with pdf.table(col_widths=(22, 34, 52, 22, 22, 22), text_align=("LEFT", "LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT"),
                   line_height=5.5, borders_layout="HORIZONTAL_LINES", headings_style=pdf.heading_style()) as table:
        row = table.row()
        for h in ("Date", tpl.label("matter"), "Description", "Charge", "Payment", "Balance"):
            row.cell(h)
        if st["d_from"]:
            row = table.row()
            row.cell(st["d_from"].strftime("%m/%d/%Y"))
            row.cell("")
            row.cell("Balance forward")
            row.cell("")
            row.cell("")
            row.cell(money(st["opening"]))
        for e in st["entries"]:
            row = table.row()
            row.cell(e["date"].strftime("%m/%d/%Y"))
            row.cell(_pdf_txt(e["matter"].number if e["matter"] else ""))
            row.cell(_pdf_txt(e["description"]))
            row.cell(money(e["charge"]) if e["charge"] else "")
            row.cell(money(e["credit"]) if e["credit"] else "")
            row.cell(money(e["balance"]))
        if not st["entries"]:
            row = table.row()
            row.cell("")
            row.cell("")
            row.cell("No activity in this period.")
            row.cell("")
            row.cell("")
            row.cell(money(st["opening"]))
    pdf.ln(4)

    # Per matter
    if st["groups"]:
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 6, "By matter", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9.5)
        with pdf.table(col_widths=(30, 34, 22, 22, 22, 22, 22),
                       text_align=("LEFT", "LEFT", "LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT"), line_height=5.5,
                       borders_layout="HORIZONTAL_LINES", headings_style=pdf.heading_style()) as table:
            row = table.row()
            for h in ("Invoice", tpl.label("matter"), "Date", tpl.label("due"), "Total", "Paid", "Balance"):
                row.cell(h)
            for g in st["groups"]:
                for inv in g["invoices"]:
                    row = table.row()
                    row.cell(_pdf_txt(inv.number))
                    row.cell(_pdf_txt(g["matter"].number if g["matter"] else ""))
                    row.cell(inv.issued_on.strftime("%m/%d/%Y") if inv.issued_on else "")
                    row.cell(inv.due_on.strftime("%m/%d/%Y") if inv.due_on else "")
                    row.cell(money(inv.total_cents))
                    row.cell(money(inv.paid_cents))
                    row.cell(money(inv.balance_cents))
                row = table.row()
                row.cell("")
                row.cell(_pdf_txt(f"Subtotal {g['matter'].number if g['matter'] else ''}"))
                row.cell("")
                row.cell("")
                row.cell(money(g["invoiced"]))
                row.cell(money(g["paid"]))
                row.cell(money(g["balance"]))
        pdf.ln(3)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(116, 6, "")
    pdf.cell(36, 6, _pdf_txt(tpl.label("balance_due")), align="R")
    pdf.cell(22, 6, money(st["totals"]["balance"]), align="R", new_x="LMARGIN", new_y="NEXT")
    footer = tpl.statement_footer or firm.invoice_footer
    if footer:
        pdf.ln(5)
        pdf.set_font("Helvetica", "I", 9)
        for para in footer.split("\n"):
            if para.strip():
                _para(pdf, _pdf_txt(para.strip()))
    return pdf


def statement_pdf_bytes(st):
    return bytes(render_statement_pdf(st).output())


def _filename(client):
    safe = "".join(ch if ch.isalnum() else "-" for ch in client.display_name).strip("-") or str(client.id)
    return f"statement-{safe}-{date.today().isoformat()}.pdf"


@bp.route("/<int:client_id>/pdf")
@login_required
def pdf(client_id):
    client = db.session.get(Contact, client_id) or abort(404)
    d_from, d_to, matter_id = _range_args()
    st = build_statement(client, d_from, d_to, matter_id)
    return Response(statement_pdf_bytes(st), mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{_filename(client)}"'})


# ---------------------------------------------------------------- send
@bp.route("/<int:client_id>/send", methods=["POST"])
@login_required
def send(client_id):
    client = db.session.get(Contact, client_id) or abort(404)
    f = request.form
    d_from = parse_date(f.get("from"))
    d_to = parse_date(f.get("to"))
    matter_id = f.get("matter_id", type=int) or None
    back = redirect(url_for("statements.detail", client_id=client.id, **{k: v for k, v in
                            (("from", f.get("from")), ("to", f.get("to")), ("matter_id", matter_id)) if v}))
    to = (f.get("to_email") or client.email or "").strip()
    if not to:
        flash("The client has no email address on file. Add one on the contact, or download the PDF.", "error")
        return back
    firm = Firm.get()
    st = build_statement(client, d_from, d_to, matter_id)
    data = statement_pdf_bytes(st)
    note = (f.get("note") or "").strip()[:2000]
    cur = st["currency"]
    balance = fmt_money(st["totals"]["balance"], cur)
    subject = f"Statement of account from {firm.name}"
    intro = note or (f"Attached is your statement of account with {firm.name}, showing every invoice we have sent "
                     f"you and the payments received.")
    html = f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1c2430;max-width:600px">
<p><strong>{escape(firm.name)}</strong></p>
<p>Hello {escape(client.display_name)},</p>
<p>{escape(intro).replace(chr(10), '<br>')}</p>
<table style="border-collapse:collapse;font-size:14px">
<tr><td style="padding:4px 8px"><strong>Invoiced</strong></td><td style="padding:4px 8px;text-align:right">{fmt_money(st['totals']['invoiced'], cur)}</td></tr>
<tr><td style="padding:4px 8px"><strong>Paid or applied</strong></td><td style="padding:4px 8px;text-align:right">{fmt_money(st['totals']['paid'], cur)}</td></tr>
<tr><td style="padding:4px 8px"><strong>Balance due</strong></td><td style="padding:4px 8px;text-align:right"><strong>{balance}</strong></td></tr>
</table>
<p style="font-size:13px;color:#66707d">The statement is attached as a PDF. Each open invoice can be paid from the link in its own email.</p>
<p>{escape(firm.name)}{(' | ' + escape(firm.phone)) if firm.phone else ''}</p>
</div>"""
    text = f"{intro}\n\nInvoiced: {fmt_money(st['totals']['invoiced'], cur)}\nPaid: {fmt_money(st['totals']['paid'], cur)}\nBalance due: {balance}\n"
    send_email(to, subject, html, text=text, attachments=[(_filename(client), data, "application/pdf")],
               reply_to=firm.email or None)
    audit("send", "statement", client.id, f"statement to {to}, balance {balance}" + (
        f", {d_from or ''}..{d_to or ''}" if (d_from or d_to) else ""), current_user().id)
    db.session.commit()
    flash(f"Statement sent to {to}.", "ok")
    return back
