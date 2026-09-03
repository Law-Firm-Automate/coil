"""Invoices: builder (flat fee, hourly, hybrid, contingency), detail, edit, send, PDF, public view, open pixel."""
import os
from datetime import date, timedelta
from html import escape
from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort, current_app,
                   send_file, Response)
from ..extensions import db
from ..models import (Firm, Matter, Invoice, InvoiceLine, InvoiceEvent, TimeEntry, Expense, FlatFeeMilestone,
                      audit, now)
from ..helpers import login_required, current_user, parse_money, parse_date, client_ip, cents_to_str
from ..services.mail import send_email
from ..services.pdf import DocPDF, save_pdf

bp = Blueprint("invoices", __name__)

STATUSES = ["all", "draft", "sent", "viewed", "partial", "paid", "overdue", "void"]
OPEN_STATUSES = ("sent", "viewed", "partial")
PIXEL_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,"
             b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def _base_url():
    return current_app.config["BASE_URL"].rstrip("/")


def public_url(inv):
    return f"{_base_url()}/p/{inv.public_token}"


def _pdf_txt(s):
    """Make text safe for the core Helvetica font (latin-1)."""
    return (str(s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
            .replace("–", "-").replace("—", "-").replace("•", "-")
            .encode("latin-1", "replace").decode("latin-1"))


def _dollars(cents):
    return f"{int(cents or 0) / 100:.2f}"


def _line_kind_for_amount(amount):
    return "discount" if amount < 0 else "adjustment"


# ---------------------------------------------------------------- list
@bp.route("/invoices")
@login_required
def index():
    status = request.args.get("status", "all")
    if status not in STATUSES:
        status = "all"
    today = date.today()
    all_invoices = Invoice.query.order_by(Invoice.issued_on.desc(), Invoice.id.desc()).all()

    def in_tab(inv, tab):
        if tab == "all":
            return True
        if tab == "overdue":
            return inv.status in OPEN_STATUSES and inv.due_on and inv.due_on < today
        return inv.status == tab

    tabs = []
    for t in STATUSES:
        rows = [i for i in all_invoices if in_tab(i, t)]
        tabs.append({"key": t, "count": len(rows), "total": sum(i.total_cents or 0 for i in rows),
                     "balance": sum(i.balance_cents for i in rows if i.status != "void")})
    invoices = [i for i in all_invoices if in_tab(i, status)]
    matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    return render_template("invoices/index.html", invoices=invoices, tabs=tabs, status=status, today=today,
                           matters=matters)


# ---------------------------------------------------------------- builder
def _builder_context(matter):
    u = current_user()
    milestones = [m for m in matter.milestones if m.invoice_id is None]
    time_entries = [t for t in sorted(matter.time_entries, key=lambda t: (t.date, t.id))
                    if t.billable and t.invoice_id is None]
    expenses = [e for e in sorted(matter.expenses, key=lambda e: (e.date or date.min, e.id))
                if e.billable and e.invoice_id is None]
    firm = Firm.get()
    issued = date.today()
    show_flat = matter.billing_type in ("flat", "hybrid")
    show_hourly = matter.billing_type in ("hourly", "hybrid")
    show_contingency = matter.billing_type == "contingency"
    already_flat = sum(l.amount_cents for i in matter.invoices if i.status != "void"
                       for l in i.lines if l.kind == "flat")
    return dict(matter=matter, milestones=milestones, time_entries=time_entries, expenses=expenses, firm_settings=firm,
                issued_on=issued, due_on=issued + timedelta(days=firm.invoice_terms_days or 30),
                show_flat=show_flat, show_hourly=show_hourly, show_contingency=show_contingency,
                default_flat_cents=max(0, (matter.flat_fee_cents or 0) - already_flat),
                first_milestone_id=milestones[0].id if milestones else None,
                time_total=sum(t.amount_cents for t in time_entries),
                expense_total=sum(e.amount_cents for e in expenses), user=u)


@bp.route("/invoices/new", methods=["GET", "POST"])
@login_required
def new():
    matter_id = request.args.get("matter_id", type=int) or request.form.get("matter_id", type=int)
    if not matter_id:
        matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
        return render_template("invoices/pick_matter.html", matters=matters)
    matter = db.session.get(Matter, matter_id) or abort(404)
    ctx = _builder_context(matter)
    if request.method == "GET":
        return render_template("invoices/new.html", **ctx)

    u = current_user()
    f = request.form
    issued_on = parse_date(f.get("issued_on"), date.today())
    due_on = parse_date(f.get("due_on"), issued_on + timedelta(days=ctx["firm_settings"].invoice_terms_days or 30))
    lines = []
    sort = 0

    # Flat fee: milestones, or a free amount when the matter has no milestones.
    picked_milestones = []
    if ctx["show_flat"]:
        ids = {int(x) for x in f.getlist("milestone_ids") if x.isdigit()}
        for m in ctx["milestones"]:
            if m.id in ids:
                picked_milestones.append(m)
                lines.append(InvoiceLine(kind="flat", date=issued_on, description=m.description, quantity=1.0,
                                         unit_cents=m.amount_cents, amount_cents=m.amount_cents,
                                         milestone_id=m.id, sort=sort))
                sort += 1
        if not ctx["milestones"]:
            amt = parse_money(f.get("flat_amount"))
            if amt:
                desc = (f.get("flat_description") or "").strip() or f"Flat fee: {matter.name}"
                lines.append(InvoiceLine(kind="flat", date=issued_on, description=desc, quantity=1.0,
                                         unit_cents=amt, amount_cents=amt, sort=sort))
                sort += 1

    # Contingency: free amount with the settlement x pct helper on the form.
    if ctx["show_contingency"]:
        amt = parse_money(f.get("fee_amount"))
        if amt:
            desc = (f.get("fee_description") or "").strip() or f"Contingency fee: {matter.name}"
            lines.append(InvoiceLine(kind="flat", date=issued_on, description=desc, quantity=1.0,
                                     unit_cents=amt, amount_cents=amt, sort=sort))
            sort += 1

    # Time and expenses (hourly, hybrid, and contingency matters can all carry costs).
    picked_time, picked_expenses = [], []
    time_ids = {int(x) for x in f.getlist("time_ids") if x.isdigit()}
    expense_ids = {int(x) for x in f.getlist("expense_ids") if x.isdigit()}
    for t in ctx["time_entries"]:
        if t.id in time_ids:
            picked_time.append(t)
            lines.append(InvoiceLine(kind="time", date=t.date, description=t.description or "Legal services",
                                     quantity=round(t.minutes / 60.0, 2), unit_cents=t.rate_cents,
                                     amount_cents=t.amount_cents, time_entry_id=t.id, sort=sort))
            sort += 1
    for e in ctx["expenses"]:
        if e.id in expense_ids:
            picked_expenses.append(e)
            desc = e.description or e.category or "Expense"
            if e.category and e.category not in desc:
                desc = f"{e.category}: {desc}"
            lines.append(InvoiceLine(kind="expense", date=e.date, description=desc, quantity=1.0,
                                     unit_cents=e.amount_cents, amount_cents=e.amount_cents, expense_id=e.id,
                                     sort=sort))
            sort += 1

    adj = parse_money(f.get("adjustment_amount"))
    if adj:
        desc = (f.get("adjustment_description") or "").strip() or ("Discount" if adj < 0 else "Adjustment")
        lines.append(InvoiceLine(kind=_line_kind_for_amount(adj), date=issued_on, description=desc, quantity=1.0,
                                 unit_cents=adj, amount_cents=adj, sort=sort))
        sort += 1

    if not lines:
        flash("Pick at least one item to invoice, or enter an amount.", "error")
        return render_template("invoices/new.html", **ctx), 400

    firm = Firm.get()
    number = f"{firm.invoice_prefix or ''}{firm.next_invoice_number}"
    firm.next_invoice_number = (firm.next_invoice_number or 1000) + 1
    kind = matter.billing_type if matter.billing_type in ("flat", "hourly", "hybrid", "contingency") else "flat"
    inv = Invoice(number=number, matter_id=matter.id, client_id=matter.client_id, kind=kind, status="draft",
                  issued_on=issued_on, due_on=due_on, notes=(f.get("notes") or "").strip())
    db.session.add(inv)
    db.session.flush()
    for l in lines:
        l.invoice_id = inv.id
        db.session.add(l)
    for t in picked_time:
        t.invoice_id = inv.id
    for e in picked_expenses:
        e.invoice_id = inv.id
    for m in picked_milestones:
        m.invoice_id = inv.id
    db.session.flush()
    inv.recalc()
    audit("create", "invoice", inv.id, f"{inv.number} {inv.kind} {inv.total_cents}c for {matter.number}", u.id)
    db.session.commit()
    flash(f"Invoice {inv.number} created as a draft.", "ok")
    return redirect(url_for("invoices.detail", id=inv.id))


# ---------------------------------------------------------------- detail
@bp.route("/invoices/<int:id>")
@login_required
def detail(id):
    inv = db.session.get(Invoice, id) or abort(404)
    trust_balance = inv.client.trust_balance_cents()
    apply_default = min(inv.balance_cents, trust_balance) if trust_balance > 0 else 0
    viewed_events = [e for e in inv.events if e.event == "viewed"]
    return render_template("invoices/detail.html", inv=inv, trust_balance=trust_balance,
                           apply_default=apply_default, public_url=public_url(inv), today=date.today(),
                           viewed_events=viewed_events, dollars=_dollars)


# ---------------------------------------------------------------- edit (draft only)
@bp.route("/invoices/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit(id):
    inv = db.session.get(Invoice, id) or abort(404)
    if inv.status != "draft":
        flash("Only draft invoices can be edited. Void it and rebuild if the lines are wrong.", "error")
        return redirect(url_for("invoices.detail", id=id))
    if request.method == "POST":
        f = request.form
        u = current_user()
        removed = 0
        for line in list(inv.lines):
            if f.get(f"remove_{line.id}"):
                _unlink_line(line)
                inv.lines.remove(line)
                removed += 1
                continue
            desc = f.get(f"desc_{line.id}")
            if desc is not None:
                line.description = desc.strip()
            amt = f.get(f"amount_{line.id}")
            if amt is not None and amt.strip() != "":
                line.amount_cents = parse_money(amt)
                if line.kind == "time" and line.quantity:
                    line.unit_cents = int(round(line.amount_cents / line.quantity))
                else:
                    line.unit_cents = line.amount_cents
        adj = parse_money(f.get("adj_amount"))
        if adj:
            desc = (f.get("adj_description") or "").strip() or ("Discount" if adj < 0 else "Adjustment")
            inv.lines.append(InvoiceLine(kind=_line_kind_for_amount(adj), date=inv.issued_on, description=desc,
                                         quantity=1.0, unit_cents=adj, amount_cents=adj,
                                         sort=(max([l.sort or 0 for l in inv.lines] or [0]) + 1)))
        inv.issued_on = parse_date(f.get("issued_on"), inv.issued_on)
        inv.due_on = parse_date(f.get("due_on"), inv.due_on)
        inv.notes = (f.get("notes") or "").strip()
        db.session.flush()
        inv.recalc()
        inv.pdf_path = ""  # stale after edits; regenerated on next send or download
        audit("update", "invoice", inv.id, f"edited lines, removed {removed}", u.id)
        db.session.commit()
        flash("Invoice updated.", "ok")
        return redirect(url_for("invoices.detail", id=id))
    return render_template("invoices/edit.html", inv=inv, dollars=_dollars)


def _unlink_line(line):
    """Release the source record so it can be billed again."""
    if line.time_entry_id:
        t = db.session.get(TimeEntry, line.time_entry_id)
        if t and t.invoice_id == line.invoice_id:
            t.invoice_id = None
    if line.expense_id:
        e = db.session.get(Expense, line.expense_id)
        if e and e.invoice_id == line.invoice_id:
            e.invoice_id = None
    if line.milestone_id:
        m = db.session.get(FlatFeeMilestone, line.milestone_id)
        if m and m.invoice_id == line.invoice_id:
            m.invoice_id = None


# ---------------------------------------------------------------- void
@bp.route("/invoices/<int:id>/void", methods=["POST"])
@login_required
def void(id):
    inv = db.session.get(Invoice, id) or abort(404)
    if inv.status == "void":
        flash("Already void.", "error")
        return redirect(url_for("invoices.detail", id=id))
    if (inv.paid_cents or 0) > 0:
        flash("This invoice has payments recorded against it and cannot be voided.", "error")
        return redirect(url_for("invoices.detail", id=id))
    for t in list(inv.time_entries):
        t.invoice_id = None
    for e in list(inv.expenses):
        e.invoice_id = None
    for m in FlatFeeMilestone.query.filter_by(invoice_id=inv.id).all():
        m.invoice_id = None
    inv.status = "void"
    audit("void", "invoice", inv.id, inv.number, current_user().id)
    db.session.commit()
    flash(f"Invoice {inv.number} voided. Its time, expenses and milestones can be billed again.", "ok")
    return redirect(url_for("invoices.detail", id=id))


def _para(pdf, text):
    """multi_cell that returns the cursor to the left margin so the next paragraph has full width."""
    pdf.multi_cell(0, 5, _pdf_txt(text), new_x="LMARGIN", new_y="NEXT", align="L")


# ---------------------------------------------------------------- PDF
def build_pdf(inv):
    """Render the invoice PDF, save it to PDF_DIR, set inv.pdf_path (caller commits). Returns the path."""
    firm = Firm.get()
    pdf = DocPDF(firm, f"Invoice {inv.number}")
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, "INVOICE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, _pdf_txt(f"Invoice number: {inv.number}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, _pdf_txt(f"Issued: {inv.issued_on.strftime('%B %d, %Y') if inv.issued_on else ''}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, _pdf_txt(f"Due: {inv.due_on.strftime('%B %d, %Y') if inv.due_on else 'On receipt'}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5, "Bill to", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, _pdf_txt(inv.client.display_name), new_x="LMARGIN", new_y="NEXT")
    for line in (inv.client.address or "").splitlines():
        if line.strip():
            pdf.cell(0, 5, _pdf_txt(line), new_x="LMARGIN", new_y="NEXT")
    if inv.client.email:
        pdf.cell(0, 5, _pdf_txt(inv.client.email), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(22, 5, "Matter:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, _pdf_txt(inv.matter.label), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "", 9.5)
    with pdf.table(col_widths=(22, 92, 16, 22, 22), text_align=("LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT"),
                   line_height=5.5, borders_layout="HORIZONTAL_LINES") as table:
        row = table.row()
        for h in ("Date", "Description", "Qty", "Rate", "Amount"):
            row.cell(h)
        for l in inv.lines:
            row = table.row()
            row.cell(l.date.strftime("%m/%d/%Y") if l.date else "")
            row.cell(_pdf_txt(l.description))
            if l.kind == "time":
                row.cell(f"{l.quantity:.2f}")
                row.cell(cents_to_str(l.unit_cents))
            else:
                row.cell("")
                row.cell("")
            row.cell(cents_to_str(l.amount_cents))
    pdf.ln(3)

    def total_row(label, amount, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 10)
        pdf.cell(130, 6, "")
        pdf.cell(22, 6, label, align="R")
        pdf.cell(22, 6, cents_to_str(amount), align="R", new_x="LMARGIN", new_y="NEXT")

    total_row("Subtotal", inv.subtotal_cents)
    if inv.tax_cents:
        total_row("Tax", inv.tax_cents)
    if inv.paid_cents:
        total_row("Paid", -(inv.paid_cents or 0))
    total_row("Balance due", inv.balance_cents, bold=True)
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5, "Payment instructions", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    link = public_url(inv)
    _para(pdf, _pdf_txt(f"Pay online by bank transfer (no fee) or card at: {link}"))
    if firm.surcharge_enabled and firm.surcharge_bps:
        _para(pdf, _pdf_txt(f"A {firm.surcharge_bps / 100:.2f}% surcharge applies to card payments. "
                                      f"Bank transfers carry no surcharge."))
    mail_to = " ".join([x.strip() for x in (firm.address or "").splitlines() if x.strip()])
    _para(pdf, _pdf_txt(f"Checks payable to {firm.name}" + (f", mailed to {mail_to}." if mail_to else ".")))
    if inv.notes:
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 5, "Notes", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9.5)
        _para(pdf, _pdf_txt(inv.notes))
    if firm.invoice_footer:
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", 9)
        _para(pdf, _pdf_txt(firm.invoice_footer))
    safe_number = "".join(ch for ch in inv.number if ch.isalnum() or ch in "-_") or str(inv.id)
    path = save_pdf(pdf, f"invoice-{safe_number}.pdf")
    inv.pdf_path = path
    return path


def _pdf_bytes(inv):
    path = inv.pdf_path
    if not path or not os.path.isfile(path):
        path = build_pdf(inv)
        db.session.commit()
    with open(path, "rb") as fh:
        return fh.read()


@bp.route("/invoices/<int:id>/pdf")
@login_required
def pdf(id):
    inv = db.session.get(Invoice, id) or abort(404)
    path = build_pdf(inv)
    db.session.commit()
    return send_file(path, mimetype="application/pdf", as_attachment=False, download_name=f"{inv.number}.pdf")


# ---------------------------------------------------------------- send / remind
def _send_invoice_email(inv, reminder=False):
    firm = Firm.get()
    to = (inv.client.email or "").strip()
    if not to:
        return "The client has no email address on file."
    if inv.status == "draft":
        inv.status = "sent"
    inv.sent_at = now()
    inv.sent_to = to
    pdf_data = None
    try:
        build_pdf(inv)
        with open(inv.pdf_path, "rb") as fh:
            pdf_data = fh.read()
    except Exception as e:  # PDF failure should not block the email
        current_app.logger.warning("invoice pdf failed: %s", e)
    link = public_url(inv)
    pixel = f"{_base_url()}/track/invoice/{inv.public_token}.gif"
    subject = (f"Reminder: invoice {inv.number} from {firm.name}" if reminder
               else f"Invoice {inv.number} from {firm.name}")
    intro = ("This is a friendly reminder that the invoice below is still open." if reminder
             else "Please find your invoice below.")
    rows = "".join(
        f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee'>{escape(l.description or '')}</td>"
        f"<td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:right'>{cents_to_str(l.amount_cents)}</td></tr>"
        for l in inv.lines)
    html = f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1c2430;max-width:600px">
<p><strong>{escape(firm.name)}</strong></p>
<p>Hello {escape(inv.client.display_name)},</p>
<p>{intro}</p>
<table style="border-collapse:collapse;width:100%;font-size:14px">
<tr><td style="padding:4px 8px"><strong>Invoice</strong></td><td style="padding:4px 8px;text-align:right">{escape(inv.number)}</td></tr>
<tr><td style="padding:4px 8px"><strong>Matter</strong></td><td style="padding:4px 8px;text-align:right">{escape(inv.matter.label)}</td></tr>
<tr><td style="padding:4px 8px"><strong>Issued</strong></td><td style="padding:4px 8px;text-align:right">{inv.issued_on.strftime('%b %d, %Y') if inv.issued_on else ''}</td></tr>
<tr><td style="padding:4px 8px"><strong>Due</strong></td><td style="padding:4px 8px;text-align:right">{inv.due_on.strftime('%b %d, %Y') if inv.due_on else 'On receipt'}</td></tr>
</table>
<table style="border-collapse:collapse;width:100%;font-size:14px;margin-top:10px">{rows}
<tr><td style="padding:6px 8px"><strong>Balance due</strong></td><td style="padding:6px 8px;text-align:right"><strong>{cents_to_str(inv.balance_cents)}</strong></td></tr>
</table>
<p style="margin:22px 0"><a href="{link}" style="background:#1f5f8b;color:#fff;padding:11px 20px;border-radius:6px;text-decoration:none;display:inline-block">View and pay</a></p>
<p style="font-size:13px;color:#66707d">Bank transfer (ACH) carries no fee.{(' A ' + format(firm.surcharge_bps / 100, '.2f') + '% surcharge applies to card payments.') if firm.surcharge_enabled and firm.surcharge_bps else ''} A PDF copy is attached.</p>
<p style="font-size:13px;color:#66707d">If the button does not work, open this link: <a href="{link}">{link}</a></p>
<p>{escape(firm.name)}{(' | ' + escape(firm.phone)) if firm.phone else ''}</p>
<img src="{pixel}" width="1" height="1" alt="" style="display:block">
</div>"""
    text = (f"{intro}\n\nInvoice {inv.number} for {inv.matter.label}\nBalance due: {cents_to_str(inv.balance_cents)}\n"
            f"Due: {inv.due_on.isoformat() if inv.due_on else 'on receipt'}\n\nView and pay: {link}\n")
    attachments = [(f"{inv.number}.pdf", pdf_data, "application/pdf")] if pdf_data else []
    send_email(to, subject, html, text=text, attachments=attachments, reply_to=firm.email or None)
    return None


@bp.route("/invoices/<int:id>/send", methods=["POST"])
@login_required
def send(id):
    inv = db.session.get(Invoice, id) or abort(404)
    if inv.status in ("void", "paid"):
        flash(f"Cannot send a {inv.status} invoice.", "error")
        return redirect(url_for("invoices.detail", id=id))
    if not inv.lines:
        flash("This invoice has no lines.", "error")
        return redirect(url_for("invoices.detail", id=id))
    err = _send_invoice_email(inv)
    if err:
        db.session.rollback()
        flash(err, "error")
        return redirect(url_for("invoices.detail", id=id))
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="sent", detail=f"to {inv.sent_to}"))
    audit("send", "invoice", inv.id, f"{inv.number} to {inv.sent_to}", current_user().id)
    db.session.commit()
    flash(f"Invoice {inv.number} sent to {inv.sent_to}.", "ok")
    return redirect(url_for("invoices.detail", id=id))


@bp.route("/invoices/<int:id>/remind", methods=["POST"])
@login_required
def remind(id):
    inv = db.session.get(Invoice, id) or abort(404)
    if inv.status in ("void", "paid"):
        flash(f"Cannot remind on a {inv.status} invoice.", "error")
        return redirect(url_for("invoices.detail", id=id))
    err = _send_invoice_email(inv, reminder=True)
    if err:
        db.session.rollback()
        flash(err, "error")
        return redirect(url_for("invoices.detail", id=id))
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="reminder", detail=f"to {inv.sent_to}"))
    audit("remind", "invoice", inv.id, f"{inv.number} to {inv.sent_to}", current_user().id)
    db.session.commit()
    flash(f"Reminder sent to {inv.sent_to}.", "ok")
    return redirect(url_for("invoices.detail", id=id))


# ---------------------------------------------------------------- public view + open pixel
def _mark_viewed(inv, source):
    """Log a view unless the last one was under 60 seconds ago. Returns True if a new view was logged."""
    last = (InvoiceEvent.query.filter_by(invoice_id=inv.id, event="viewed")
            .order_by(InvoiceEvent.created_at.desc(), InvoiceEvent.id.desc()).first())
    if last and last.created_at and (now() - last.created_at).total_seconds() < 60:
        return False
    inv.view_count = (inv.view_count or 0) + 1
    if not inv.first_viewed_at:
        inv.first_viewed_at = now()
    if inv.status == "sent":
        inv.status = "viewed"
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="viewed", ip=client_ip(),
                                ua=(request.user_agent.string or "")[:300], detail=source))
    db.session.commit()
    return True


@bp.route("/p/<token>")
def public_view(token):
    inv = Invoice.query.filter_by(public_token=token).first() or abort(404)
    if inv.status != "void":
        _mark_viewed(inv, "page")
    firm = Firm.get()
    surcharge_pct = (firm.surcharge_bps or 0) / 100.0 if firm.surcharge_enabled else 0
    trust_balance = inv.client.trust_balance_cents()
    return render_template("invoices/public.html", inv=inv, firm_settings=firm, surcharge_pct=surcharge_pct,
                           trust_balance=trust_balance, payments=[p for p in inv.payments])


@bp.route("/p/<token>/pdf")
def public_pdf(token):
    inv = Invoice.query.filter_by(public_token=token).first() or abort(404)
    data = _pdf_bytes(inv)
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{inv.number}.pdf"'})


@bp.route("/track/invoice/<token>.gif")
def track(token):
    inv = Invoice.query.filter_by(public_token=token).first()
    if inv and inv.status != "void":
        _mark_viewed(inv, "email open")
    return Response(PIXEL_GIF, mimetype="image/gif",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                             "Pragma": "no-cache", "Expires": "0", "Content-Length": str(len(PIXEL_GIF))})
