"""Invoices: builder (flat fee, hourly, hybrid, contingency), bulk builder, split billing across payers,
approval workflow, interest on overdue balances, detail, edit, send, PDF, public view, open pixel."""
import os
import secrets
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from types import SimpleNamespace
from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort, current_app,
                   send_file, Response)
from werkzeug.datastructures import MultiDict
from ..extensions import db
from ..models import (Firm, Matter, Invoice, InvoiceLine, InvoiceEvent, TimeEntry, Expense, FlatFeeMilestone,
                      audit, now)
from ..helpers import login_required, current_user, parse_money, parse_date, client_ip, cents_to_str
from ..services.mail import send_email
from ..services.pdf import DocPDF, save_pdf

try:  # Agent B's multi-currency formatter. Fall back to a local copy if helpers.py is older than this module.
    from ..helpers import fmt_money
except ImportError:  # pragma: no cover
    _SYMBOLS = {"USD": "$", "CAD": "CA$", "GBP": "£", "EUR": "€", "AUD": "A$", "MXN": "MX$"}

    def fmt_money(cents, code="USD"):
        code = (code or "USD").upper()
        return cents_to_str(cents, _SYMBOLS.get(code, code + " "))

bp = Blueprint("invoices", __name__)

STATUSES = ["all", "draft", "pending", "sent", "viewed", "partial", "paid", "overdue", "void"]
OPEN_STATUSES = ("sent", "viewed", "partial")
CURRENCIES = ["USD", "CAD", "GBP", "EUR", "AUD", "MXN"]
APPROVER_ROLES = ("owner", "billing")
PIXEL_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,"
             b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def _base_url():
    return current_app.config["BASE_URL"].rstrip("/")


def public_url(inv):
    return f"{_base_url()}/p/{inv.public_token}"


def _pdf_txt(s):
    """Make text safe for the core Helvetica font. cp1252 so GBP and EUR symbols survive."""
    return (str(s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
            .replace("–", "-").replace("—", "-").replace("•", "-")
            .encode("cp1252", "replace").decode("cp1252"))


def _dollars(cents):
    return f"{int(cents or 0) / 100:.2f}"


def _line_kind_for_amount(amount):
    return "discount" if amount < 0 else "adjustment"


def _role(user):
    r = (user.role or "") if user else ""
    return "attorney" if r == "staff" else r


def can_approve(user):
    return _role(user) in APPROVER_ROLES


def _initial_approval(user, firm):
    """Invoices built by anyone other than an owner or billing user wait for approval when the firm asks for it."""
    if firm.require_invoice_approval and not can_approve(user):
        return "pending"
    return "none"


def _next_number(firm):
    number = f"{firm.invoice_prefix or ''}{firm.next_invoice_number}"
    firm.next_invoice_number = (firm.next_invoice_number or 1000) + 1
    return number


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
        if tab == "pending":
            return inv.approval_status == "pending" and inv.status == "draft"
        return inv.status == tab

    tabs = []
    for t in STATUSES:
        rows = [i for i in all_invoices if in_tab(i, t)]
        tabs.append({"key": t, "count": len(rows), "total": sum(i.total_cents or 0 for i in rows),
                     "balance": sum(i.balance_cents for i in rows if i.status != "void")})
    invoices = [i for i in all_invoices if in_tab(i, status)]
    matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    mixed = len({(i.currency or "USD") for i in all_invoices}) > 1
    return render_template("invoices/index.html", invoices=invoices, tabs=tabs, status=status, today=today,
                           matters=matters, mixed_currencies=mixed, can_approve=can_approve(current_user()))


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
    payers = list(matter.payers)
    return dict(matter=matter, milestones=milestones, time_entries=time_entries, expenses=expenses, firm_settings=firm,
                issued_on=issued, due_on=issued + timedelta(days=firm.invoice_terms_days or 30),
                show_flat=show_flat, show_hourly=show_hourly, show_contingency=show_contingency,
                default_flat_cents=max(0, (matter.flat_fee_cents or 0) - already_flat),
                first_milestone_id=milestones[0].id if milestones else None,
                time_total=sum(t.amount_cents for t in time_entries),
                expense_total=sum(e.amount_cents for e in expenses), user=u,
                payers=payers, payers_ok=payers_total_ok(payers), payers_total=sum(p.percent or 0 for p in payers),
                currency=matter.currency_code, needs_approval=_initial_approval(u, firm) == "pending")


def payers_total_ok(payers):
    """Split builds are only allowed when the payer percentages add up to exactly 100."""
    if not payers:
        return True
    return abs(sum(float(p.percent or 0) for p in payers) - 100.0) < 0.005


def _lines_from_form(matter, ctx, f, issued_on):
    """Turn builder picks into unsaved InvoiceLine rows plus the source records they consume."""
    lines, sort = [], 0
    picked_milestones, picked_time, picked_expenses = [], [], []
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
    if ctx["show_contingency"]:
        amt = parse_money(f.get("fee_amount"))
        if amt:
            desc = (f.get("fee_description") or "").strip() or f"Contingency fee: {matter.name}"
            lines.append(InvoiceLine(kind="flat", date=issued_on, description=desc, quantity=1.0,
                                     unit_cents=amt, amount_cents=amt, sort=sort))
            sort += 1
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
    return lines, picked_time, picked_expenses, picked_milestones


def split_cents(amount, percents):
    """Share `amount` (int cents) across percents, rounding half up, remainder on the last share so the
    shares always sum to the amount exactly. Works for negative amounts (discounts) too."""
    shares, used = [], 0
    for pct in percents[:-1]:
        c = int((Decimal(amount) * Decimal(str(pct)) / Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        shares.append(c)
        used += c
    shares.append(amount - used)
    return shares


def _copy_line(src, amount, keep_sources):
    l = InvoiceLine(kind=src.kind, date=src.date, description=src.description, sort=src.sort,
                    amount_cents=amount)
    if src.kind == "time":
        l.quantity = src.quantity
        l.unit_cents = src.unit_cents
    else:
        l.quantity = 1.0
        l.unit_cents = amount
    if keep_sources:
        l.time_entry_id, l.expense_id, l.milestone_id = src.time_entry_id, src.expense_id, src.milestone_id
    return l


def create_invoices(matter, user, lines, picked_time, picked_expenses, picked_milestones, issued_on, due_on,
                    notes=""):
    """Persist one invoice, or one per payer when the matter has split payers. Caller commits.

    Returns the list of invoices created (first one carries the source links)."""
    firm = Firm.get()
    kind = matter.billing_type if matter.billing_type in ("flat", "hourly", "hybrid", "contingency") else "flat"
    approval = _initial_approval(user, firm)
    currency = matter.currency_code
    payers = list(matter.payers)
    if payers and not payers_total_ok(payers):
        raise ValueError("Split payers on this matter do not add up to 100%. Fix the payers on the matter first.")
    created = []
    if not payers:
        inv = Invoice(number=_next_number(firm), matter_id=matter.id, client_id=matter.client_id, kind=kind,
                      status="draft", issued_on=issued_on, due_on=due_on, notes=notes, approval_status=approval,
                      created_by_id=user.id if user else None, currency=currency, split_pct=100.0)
        db.session.add(inv)
        db.session.flush()
        for l in lines:
            l.invoice_id = inv.id
            db.session.add(l)
        created.append(inv)
    else:
        group = secrets.token_hex(6)
        percents = [float(p.percent or 0) for p in payers]
        per_line = [split_cents(int(l.amount_cents or 0), percents) for l in lines]
        for idx, payer in enumerate(payers):
            inv = Invoice(number=_next_number(firm), matter_id=matter.id, client_id=payer.contact_id, kind=kind,
                          status="draft", issued_on=issued_on, due_on=due_on, notes=notes, approval_status=approval,
                          created_by_id=user.id if user else None, currency=currency,
                          payer_contact_id=payer.contact_id, split_pct=percents[idx], split_group=group)
            db.session.add(inv)
            db.session.flush()
            for li, src in enumerate(lines):
                l = _copy_line(src, per_line[li][idx], keep_sources=(idx == 0))
                l.invoice_id = inv.id
                db.session.add(l)
            created.append(inv)
    first = created[0]
    for t in picked_time:
        t.invoice_id = first.id
    for e in picked_expenses:
        e.invoice_id = first.id
    for m in picked_milestones:
        m.invoice_id = first.id
    db.session.flush()
    for inv in created:
        inv.recalc()
        detail = f"{inv.number} {inv.kind} {inv.total_cents}c {inv.currency} for {matter.number}"
        if inv.split_group:
            detail += f" (split {inv.split_pct:g}% {inv.client.display_name}, group {inv.split_group})"
        if approval == "pending":
            detail += " pending approval"
        audit("create", "invoice", inv.id, detail, user.id if user else None)
    return created


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
    lines, picked_time, picked_expenses, picked_milestones = _lines_from_form(matter, ctx, f, issued_on)
    if not lines:
        flash("Pick at least one item to invoice, or enter an amount.", "error")
        return render_template("invoices/new.html", **ctx), 400
    if not ctx["payers_ok"]:
        flash("Split payers on this matter do not add up to 100%. Fix the payers on the matter first.", "error")
        return render_template("invoices/new.html", **ctx), 400
    created = create_invoices(matter, u, lines, picked_time, picked_expenses, picked_milestones, issued_on, due_on,
                              notes=(f.get("notes") or "").strip())
    db.session.commit()
    inv = created[0]
    if len(created) > 1:
        flash(f"Built {len(created)} split invoices ({', '.join(i.number for i in created)}) as drafts.", "ok")
    else:
        flash(f"Invoice {inv.number} created as a draft" + (
            " and submitted for approval." if inv.approval_status == "pending" else "."), "ok")
    return redirect(url_for("invoices.detail", id=inv.id))


# ---------------------------------------------------------------- bulk builder
def bulk_rows(today=None):
    """Every open matter with something billable today: unbilled time, unbilled expenses, or milestones due."""
    today = today or date.today()
    rows = []
    for m in Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all():
        times = [t for t in m.time_entries if t.billable and t.invoice_id is None]
        exps = [e for e in m.expenses if e.billable and e.invoice_id is None]
        ms = [x for x in m.milestones if x.invoice_id is None and x.due_on and x.due_on <= today] \
            if m.billing_type in ("flat", "hybrid") else []
        if not (times or exps or ms):
            continue
        t_total = sum(t.amount_cents for t in times)
        e_total = sum(e.amount_cents for e in exps)
        m_total = sum(x.amount_cents for x in ms)
        payers = list(m.payers)
        rows.append({"matter": m, "time": times, "expenses": exps, "milestones": ms, "time_total": t_total,
                     "expense_total": e_total, "milestone_total": m_total, "total": t_total + e_total + m_total,
                     "payers": payers, "payers_ok": payers_total_ok(payers)})
    return rows


@bp.route("/invoices/bulk", methods=["GET", "POST"])
@login_required
def bulk():
    today = date.today()
    firm = Firm.get()
    rows = bulk_rows(today)
    if request.method == "GET":
        return render_template("invoices/bulk.html", rows=rows, today=today, firm_settings=firm,
                               issued_on=today, due_on=today + timedelta(days=firm.invoice_terms_days or 30),
                               grand_total=sum(r["total"] for r in rows))
    u = current_user()
    wanted = {int(x) for x in request.form.getlist("matter_ids") if x.isdigit()}
    issued_on = parse_date(request.form.get("issued_on"), today)
    due_on = parse_date(request.form.get("due_on"), issued_on + timedelta(days=firm.invoice_terms_days or 30))
    built, matters_done, skipped = [], 0, []
    for r in rows:
        m = r["matter"]
        if m.id not in wanted:
            continue
        if not r["payers_ok"]:
            skipped.append(f"{m.number} (split payers do not total 100%)")
            continue
        ctx = _builder_context(m)
        picks = MultiDict([("milestone_ids", str(x.id)) for x in r["milestones"]]
                          + [("time_ids", str(t.id)) for t in r["time"]]
                          + [("expense_ids", str(e.id)) for e in r["expenses"]])
        lines, pt, pe, pm = _lines_from_form(m, ctx, picks, issued_on)
        if not lines:
            skipped.append(f"{m.number} (nothing billable)")
            continue
        created = create_invoices(m, u, lines, pt, pe, pm, issued_on, due_on)
        built.extend(created)
        matters_done += 1
    db.session.commit()
    if built:
        flash(f"Built {len(built)} draft invoice{'s' if len(built) != 1 else ''} for {matters_done} "
              f"matter{'s' if matters_done != 1 else ''}: {', '.join(i.number for i in built)}.", "ok")
    else:
        flash("No invoices were built. Tick at least one matter.", "error")
    for s in skipped:
        flash(f"Skipped {s}.", "error")
    return redirect(url_for("invoices.index", status="draft"))


# ---------------------------------------------------------------- detail
def group_siblings(inv):
    if not inv.split_group:
        return []
    return Invoice.query.filter(Invoice.split_group == inv.split_group, Invoice.id != inv.id).order_by(Invoice.id).all()


@bp.route("/invoices/<int:id>")
@login_required
def detail(id):
    inv = db.session.get(Invoice, id) or abort(404)
    trust_balance = inv.client.trust_balance_cents()
    apply_default = min(inv.balance_cents, trust_balance) if trust_balance > 0 else 0
    viewed_events = [e for e in inv.events if e.event == "viewed"]
    firm = Firm.get()
    u = current_user()
    return render_template("invoices/detail.html", inv=inv, trust_balance=trust_balance,
                           apply_default=apply_default, public_url=public_url(inv), today=date.today(),
                           viewed_events=viewed_events, dollars=_dollars, siblings=group_siblings(inv),
                           firm_settings=firm, can_approve=can_approve(u),
                           interest_ready=interest_due(inv, firm, date.today()) > 0,
                           send_block=send_blocked_reason(inv, u))


# ---------------------------------------------------------------- edit (draft only)
@bp.route("/invoices/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit(id):
    inv = db.session.get(Invoice, id) or abort(404)
    if inv.status != "draft":
        flash("Only draft invoices can be edited. Void it and rebuild if the lines are wrong.", "error")
        return redirect(url_for("invoices.detail", id=id))
    if inv.split_group:
        flash("This invoice is one share of a split group. Void the group and rebuild it to change the lines.",
              "error")
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
        # An edited invoice goes back through approval when the editor cannot approve it themselves.
        if inv.approval_status in ("pending", "approved", "rejected") and _initial_approval(u, Firm.get()) == "pending":
            inv.approval_status = "pending"
            inv.approved_by_id, inv.approved_at = None, None
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


# ---------------------------------------------------------------- approval
@bp.route("/invoices/<int:id>/submit", methods=["POST"])
@login_required
def submit(id):
    inv = db.session.get(Invoice, id) or abort(404)
    if inv.status != "draft":
        flash("Only draft invoices can be submitted for approval.", "error")
        return redirect(url_for("invoices.detail", id=id))
    inv.approval_status = "pending"
    inv.approved_by_id, inv.approved_at = None, None
    audit("submit", "invoice", inv.id, f"{inv.number} submitted for approval", current_user().id)
    db.session.commit()
    flash(f"Invoice {inv.number} submitted for approval.", "ok")
    return redirect(url_for("invoices.detail", id=id))


@bp.route("/invoices/<int:id>/approve", methods=["POST"])
@login_required
def approve(id):
    inv = db.session.get(Invoice, id) or abort(404)
    u = current_user()
    if not can_approve(u):
        abort(403)
    if inv.status == "void":
        flash("Cannot approve a void invoice.", "error")
        return redirect(url_for("invoices.detail", id=id))
    inv.approval_status = "approved"
    inv.approved_by_id = u.id
    inv.approved_at = now()
    inv.approval_note = (request.form.get("note") or "").strip()[:300]
    audit("approve", "invoice", inv.id, f"{inv.number} approved", u.id)
    db.session.commit()
    flash(f"Invoice {inv.number} approved. It can be sent now.", "ok")
    return redirect(url_for("invoices.detail", id=id))


@bp.route("/invoices/<int:id>/reject", methods=["POST"])
@login_required
def reject(id):
    inv = db.session.get(Invoice, id) or abort(404)
    u = current_user()
    if not can_approve(u):
        abort(403)
    note = (request.form.get("note") or "").strip()[:300]
    inv.approval_status = "rejected"
    inv.approval_note = note
    inv.approved_by_id = u.id
    inv.approved_at = now()
    audit("reject", "invoice", inv.id, f"{inv.number} rejected: {note}", u.id)
    db.session.commit()
    flash(f"Invoice {inv.number} sent back. It stays a draft and can be edited and resubmitted.", "ok")
    return redirect(url_for("invoices.detail", id=id))


def send_blocked_reason(inv, user):
    """Why this user may not send this invoice yet, or None. Owners and billing users approve on send."""
    firm = Firm.get()
    if not firm.require_invoice_approval:
        return None
    if inv.approval_status == "pending" and not can_approve(user):
        return "This invoice is waiting for approval by the owner or a billing user before it can be sent."
    if inv.approval_status == "rejected" and not can_approve(user):
        return "This invoice was rejected. Edit it and resubmit for approval before sending." + (
            f" Note: {inv.approval_note}" if inv.approval_note else "")
    return None


# ---------------------------------------------------------------- interest
def interest_due(inv, firm=None, today=None):
    """Cents of interest to add this month, or 0 when none applies (not overdue past grace, already charged
    this month, no rate, nothing owed)."""
    firm = firm or Firm.get()
    today = today or date.today()
    if not firm.interest_apr_bps or inv.status not in OPEN_STATUSES or not inv.due_on:
        return 0
    if today <= inv.due_on + timedelta(days=firm.interest_grace_days or 0):
        return 0
    if inv.last_interest_on and (inv.last_interest_on.year, inv.last_interest_on.month) == (today.year, today.month):
        return 0
    balance = inv.balance_cents
    if balance <= 0:
        return 0
    cents = int((Decimal(balance) * Decimal(firm.interest_apr_bps) / Decimal(10000) / Decimal(12))
                .quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return max(0, cents)


def apply_interest(inv, user_id=None, firm=None, today=None):
    """Add this month's interest line to an overdue invoice. Returns the cents added (0 = nothing done).
    Caller commits."""
    firm = firm or Firm.get()
    today = today or date.today()
    cents = interest_due(inv, firm, today)
    if cents <= 0:
        return 0
    apr = (firm.interest_apr_bps or 0) / 100.0
    inv.lines.append(InvoiceLine(kind="interest", date=today,
                                 description=f"Interest on overdue balance, {apr:g}% APR, {today:%B %Y}",
                                 quantity=1.0, unit_cents=cents, amount_cents=cents,
                                 sort=(max([l.sort or 0 for l in inv.lines] or [0]) + 1)))
    inv.interest_cents = (inv.interest_cents or 0) + cents
    inv.last_interest_on = today
    db.session.flush()
    inv.recalc()
    inv.pdf_path = ""
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="interest", detail=f"{fmt_money(cents, inv.currency)} added"))
    audit("interest", "invoice", inv.id, f"{inv.number} +{cents}c for {today:%Y-%m}", user_id)
    return cents


@bp.route("/invoices/<int:id>/interest", methods=["POST"])
@login_required
def interest(id):
    inv = db.session.get(Invoice, id) or abort(404)
    firm = Firm.get()
    if not firm.interest_apr_bps:
        flash("Set an interest rate under Settings before charging interest.", "error")
        return redirect(url_for("invoices.detail", id=id))
    cents = apply_interest(inv, user_id=current_user().id, firm=firm)
    if not cents:
        flash("No interest to add: the invoice is not past its grace period, is paid, or was already charged "
              "this month.", "error")
        return redirect(url_for("invoices.detail", id=id))
    db.session.commit()
    flash(f"Added {fmt_money(cents, inv.currency)} interest to invoice {inv.number}.", "ok")
    return redirect(url_for("invoices.detail", id=id))


# ---------------------------------------------------------------- void
def _void_one(inv, uid):
    for t in list(inv.time_entries):
        t.invoice_id = None
    for e in list(inv.expenses):
        e.invoice_id = None
    for m in FlatFeeMilestone.query.filter_by(invoice_id=inv.id).all():
        m.invoice_id = None
    inv.status = "void"
    audit("void", "invoice", inv.id, inv.number, uid)


@bp.route("/invoices/<int:id>/void", methods=["POST"])
@login_required
def void(id):
    inv = db.session.get(Invoice, id) or abort(404)
    if inv.status == "void":
        flash("Already void.", "error")
        return redirect(url_for("invoices.detail", id=id))
    group = [inv] + group_siblings(inv)
    paid = [i for i in group if (i.paid_cents or 0) > 0]
    if paid:
        who = ", ".join(i.number for i in paid)
        flash(f"Payments are recorded against {who}, so this invoice cannot be voided." + (
            " (Voiding any invoice in a split group voids the whole group.)" if len(group) > 1 else ""), "error")
        return redirect(url_for("invoices.detail", id=id))
    uid = current_user().id
    for i in group:
        if i.status != "void":
            _void_one(i, uid)
    db.session.commit()
    if len(group) > 1:
        flash(f"Voided the split group: {', '.join(i.number for i in group)}. Its time, expenses and milestones "
              f"can be billed again.", "ok")
    else:
        flash(f"Invoice {inv.number} voided. Its time, expenses and milestones can be billed again.", "ok")
    return redirect(url_for("invoices.detail", id=id))


def _para(pdf, text):
    """multi_cell that returns the cursor to the left margin so the next paragraph has full width."""
    pdf.multi_cell(0, 5, _pdf_txt(text), new_x="LMARGIN", new_y="NEXT", align="L")


# ---------------------------------------------------------------- PDF
def _letterhead(firm, inv):
    """Firm name with the matter's office address when the matter belongs to an office."""
    office = inv.matter.office if inv.matter else None
    if not office:
        return firm
    return SimpleNamespace(name=firm.name, address=office.address or firm.address, phone=office.phone or firm.phone,
                           email=office.email or firm.email, website=firm.website)


def build_pdf(inv):
    """Render the invoice PDF, save it to PDF_DIR, set inv.pdf_path (caller commits). Returns the path."""
    firm = Firm.get()
    cur = inv.currency or "USD"

    def money(c):
        return _pdf_txt(fmt_money(c, cur))

    pdf = DocPDF(_letterhead(firm, inv), f"Invoice {inv.number}")
    pdf.core_fonts_encoding = "cp1252"
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
    if cur != "USD":
        pdf.cell(0, 5, _pdf_txt(f"Currency: {cur}"), new_x="LMARGIN", new_y="NEXT")
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
    if inv.split_group:
        pdf.set_font("Helvetica", "I", 9.5)
        pdf.cell(0, 5, _pdf_txt(f"This invoice is {inv.split_pct:g}% of the charges on this matter, billed to "
                                f"{inv.client.display_name}. The remainder is billed separately."),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
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
                row.cell(money(l.unit_cents))
            else:
                row.cell("")
                row.cell("")
            row.cell(money(l.amount_cents))
    pdf.ln(3)

    def total_row(label, amount, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 10)
        pdf.cell(130, 6, "")
        pdf.cell(22, 6, label, align="R")
        pdf.cell(22, 6, money(amount), align="R", new_x="LMARGIN", new_y="NEXT")

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
    head = _letterhead(firm, inv)
    mail_to = " ".join([x.strip() for x in (head.address or "").splitlines() if x.strip()])
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
    cur = inv.currency or "USD"
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
    if inv.split_group:
        intro += (f" This invoice covers your {inv.split_pct:g}% share of the charges on this matter; "
                  f"the remainder is billed separately.")
    rows = "".join(
        f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee'>{escape(l.description or '')}</td>"
        f"<td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:right'>{fmt_money(l.amount_cents, cur)}</td></tr>"
        for l in inv.lines)
    currency_note = f" Amounts are in {cur}." if cur != "USD" else ""
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
<tr><td style="padding:6px 8px"><strong>Balance due</strong></td><td style="padding:6px 8px;text-align:right"><strong>{fmt_money(inv.balance_cents, cur)}</strong></td></tr>
</table>
<p style="margin:22px 0"><a href="{link}" style="background:#1f5f8b;color:#fff;padding:11px 20px;border-radius:6px;text-decoration:none;display:inline-block">View and pay</a></p>
<p style="font-size:13px;color:#66707d">Bank transfer (ACH) carries no fee.{(' A ' + format(firm.surcharge_bps / 100, '.2f') + '% surcharge applies to card payments.') if firm.surcharge_enabled and firm.surcharge_bps else ''}{currency_note} A PDF copy is attached.</p>
<p style="font-size:13px;color:#66707d">If the button does not work, open this link: <a href="{link}">{link}</a></p>
<p>{escape(firm.name)}{(' | ' + escape(firm.phone)) if firm.phone else ''}</p>
<img src="{pixel}" width="1" height="1" alt="" style="display:block">
</div>"""
    text = (f"{intro}\n\nInvoice {inv.number} for {inv.matter.label}\nBalance due: {fmt_money(inv.balance_cents, cur)}\n"
            f"Due: {inv.due_on.isoformat() if inv.due_on else 'on receipt'}\n\nView and pay: {link}\n")
    attachments = [(f"{inv.number}.pdf", pdf_data, "application/pdf")] if pdf_data else []
    send_email(to, subject, html, text=text, attachments=attachments, reply_to=firm.email or None)
    return None


@bp.route("/invoices/<int:id>/send", methods=["POST"])
@login_required
def send(id):
    inv = db.session.get(Invoice, id) or abort(404)
    u = current_user()
    if inv.status in ("void", "paid"):
        flash(f"Cannot send a {inv.status} invoice.", "error")
        return redirect(url_for("invoices.detail", id=id))
    if not inv.lines:
        flash("This invoice has no lines.", "error")
        return redirect(url_for("invoices.detail", id=id))
    block = send_blocked_reason(inv, u)
    if block:
        flash(block, "error")
        return redirect(url_for("invoices.detail", id=id))
    if inv.approval_status in ("pending", "rejected") and can_approve(u):
        # Owners and billing users approve as they send.
        inv.approval_status = "approved"
        inv.approved_by_id = u.id
        inv.approved_at = now()
        audit("approve", "invoice", inv.id, f"{inv.number} approved on send", u.id)
    err = _send_invoice_email(inv)
    if err:
        db.session.rollback()
        flash(err, "error")
        return redirect(url_for("invoices.detail", id=id))
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="sent", detail=f"to {inv.sent_to}"))
    audit("send", "invoice", inv.id, f"{inv.number} to {inv.sent_to}", u.id)
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
