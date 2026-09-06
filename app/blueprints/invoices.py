"""Invoices: builder (flat fee, hourly, hybrid, contingency), bulk builder, split billing across payers,
approval workflow, interest on overdue balances, detail, edit, send, PDF, public view, open pixel."""
import json
import os
import secrets
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from types import SimpleNamespace
from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort, current_app,
                   send_file, Response)
from fpdf.fonts import FontFace
from werkzeug.datastructures import MultiDict
from ..extensions import db
from ..models import (Firm, Matter, Invoice, InvoiceLine, InvoiceEvent, TimeEntry, Expense, FlatFeeMilestone,
                      User, audit, now)
from ..helpers import login_required, current_user, parse_money, parse_date, client_ip, cents_to_str
from ..i18n import lang_for
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


# ---------------------------------------------------------------- invoice template (Settings > Invoice template)
COLUMN_KEYS = ["date", "description", "timekeeper", "code", "qty", "rate", "amount"]
COLUMN_TITLES = {"date": "Date", "description": "Description", "timekeeper": "Tk", "code": "Code", "qty": "Qty",
                 "rate": "Rate", "amount": "Amount"}
COLUMN_HELP = {"date": "Date of the work or expense", "description": "What was done", "timekeeper": "Initials of who did it",
               "code": "UTBMS activity, task or expense code", "qty": "Hours on time lines", "rate": "Hourly rate",
               "amount": "Line total"}
DEFAULT_COLUMNS = ["date", "description", "qty", "rate", "amount"]
LABEL_KEYS = ["bill_to", "matter", "invoice_number", "due", "balance_due"]
DEFAULT_LABELS = {"bill_to": "Bill to", "matter": "Matter", "invoice_number": "Invoice number", "due": "Due",
                  "balance_due": "Balance due"}
DEFAULT_ACCENT = "#1f5f8b"
DEFAULT_TITLE = "INVOICE"
LOGO_EXTS = ("png", "jpg", "jpeg")
LOGO_DIR = "firm"  # under UPLOAD_DIR


def hex_rgb(h, default=DEFAULT_ACCENT):
    """'#1f5f8b' -> (31, 95, 139). Anything malformed falls back to the default accent."""
    h = (h or "").strip()
    if not (len(h) == 7 and h.startswith("#")):
        h = default
    try:
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return hex_rgb(default, default)


def valid_hex(h):
    h = (h or "").strip().lower()
    return h if len(h) == 7 and h[0] == "#" and all(c in "0123456789abcdef" for c in h[1:]) else ""


def logo_abs_path(rel):
    if not rel:
        return None
    path = os.path.join(current_app.config["UPLOAD_DIR"], rel)
    return path if os.path.isfile(path) else None


def invoice_settings(firm=None, override=None):
    """The resolved invoice template: columns in order, labels, accent, title, flags, logo, payment text.

    `override` is a dict of the same keys from an unsaved form (the Preview button in Settings). Every value
    is validated here so the PDF and public page never see a bad column or colour."""
    firm = firm or Firm.get()
    o = override or {}
    cols = o.get("columns")
    if cols is None:
        try:
            cols = json.loads(firm.invoice_columns_json or "")
        except (TypeError, ValueError):
            cols = None
    cols = [c for c in (cols or []) if c in COLUMN_KEYS]
    if not cols:
        cols = list(DEFAULT_COLUMNS)
    for must in ("description", "amount"):
        if must not in cols:
            cols.insert(0, must) if must == "description" else cols.append(must)
    labels = o.get("labels")
    if labels is None:
        try:
            labels = json.loads(firm.invoice_labels_json or "{}")
        except (TypeError, ValueError):
            labels = {}
    labels = {k: str(v).strip()[:60] for k, v in (labels or {}).items() if k in LABEL_KEYS and str(v).strip()}
    accent = valid_hex(o.get("accent") if "accent" in o else firm.invoice_accent) or DEFAULT_ACCENT
    title = (o.get("title") if "title" in o else firm.invoice_title) or DEFAULT_TITLE
    logo_rel = o.get("logo_path") if "logo_path" in o else (firm.invoice_logo_path or "")
    return SimpleNamespace(
        columns=cols, labels=labels, accent=accent, accent_rgb=hex_rgb(accent), title=str(title).strip()[:60] or DEFAULT_TITLE,
        show_timekeeper=bool(o["show_timekeeper"] if "show_timekeeper" in o else firm.invoice_show_timekeeper),
        show_codes=bool(o["show_codes"] if "show_codes" in o else firm.invoice_show_activity_codes),
        payment_instructions=str((o.get("payment_instructions") if "payment_instructions" in o
                                  else firm.invoice_payment_instructions) or "").strip(),
        statement_footer=str((o.get("statement_footer") if "statement_footer" in o else firm.statement_footer) or "").strip(),
        logo_path=logo_rel or "", logo_abs=logo_abs_path(logo_rel),
        label=lambda key, fallback=None: labels.get(key) or (fallback if fallback is not None else DEFAULT_LABELS.get(key, key)),
    )


def _initials(user):
    if not user:
        return ""
    if user.initials:
        return user.initials
    return "".join(w[0] for w in (user.name or "").split() if w)[:3].upper()


def _user(user_id):
    """The User behind a user_id column, or None. Expense carries user_id with no relationship, and the
    person may have been deleted since, so never reach for a `.user` attribute that is not there."""
    return db.session.get(User, user_id) if user_id else None


def line_meta(line):
    """(timekeeper initials, UTBMS code) for an invoice line, read from its source time entry or expense.
    Copied split lines and hand-typed lines have neither."""
    meta = getattr(line, "meta", None)
    if meta is not None:
        return meta
    if getattr(line, "time_entry_id", None):
        t = db.session.get(TimeEntry, line.time_entry_id)
        if t:
            return _initials(t.user), (t.activity_code or t.task_code or "")
    if getattr(line, "expense_id", None):
        e = db.session.get(Expense, line.expense_id)
        if e:
            return _initials(_user(e.user_id)), (e.expense_code or "")
    return "", ""


def line_hours(line):
    """The hours behind a time line. Lines built before the precision fix stored the figure rounded to two
    decimals, so prefer the source time entry's minutes when it is still there."""
    tid = getattr(line, "time_entry_id", None)
    if tid:
        t = db.session.get(TimeEntry, tid)
        if t and t.minutes:
            return t.minutes / 60.0
    return float(line.quantity or 0)


def format_quantity(line):
    """Hours on a time line, printed with just enough decimals that hours x rate comes to the line amount.
    Two decimals cannot express a 7-minute entry (0.12 x 333.33 = 40.00, not the 38.89 charged), so short
    entries print more. Non-time lines have no quantity to show, and a line whose amount was typed by hand
    falls back to two decimals."""
    if line.kind != "time":
        return ""
    hours = line_hours(line)
    unit = int(line.unit_cents or 0)
    amount = int(line.amount_cents or 0)
    if unit:
        for places in (2, 3, 4, 5):
            text = f"{hours:.{places}f}"
            if int(round(float(text) * unit)) == amount:
                return text
    return f"{hours:.2f}"


def line_description(line, tpl):
    """Description text with initials and code appended when the firm wants them shown but has not given them
    their own column."""
    desc = line.description or ""
    initials, code = line_meta(line) if (tpl.show_timekeeper or tpl.show_codes) else ("", "")
    extra = []
    if tpl.show_timekeeper and initials and "timekeeper" not in tpl.columns:
        extra.append(initials)
    if tpl.show_codes and code and "code" not in tpl.columns:
        extra.append(code)
    return desc + (" [" + " ".join(extra) + "]" if extra else "")


def visible_columns(tpl):
    """Columns to render: the ticked list, minus timekeeper/code when their flag is off."""
    out = []
    for c in tpl.columns:
        if c == "timekeeper" and not tpl.show_timekeeper:
            continue
        if c == "code" and not tpl.show_codes:
            continue
        out.append(c)
    return out


def line_cells(line, cols, tpl, money):
    """One string per visible column for a line. `money` formats cents in the invoice currency."""
    initials, code = line_meta(line) if ("timekeeper" in cols or "code" in cols) else ("", "")
    is_time = line.kind == "time"
    cells = []
    for c in cols:
        if c == "date":
            cells.append(line.date.strftime("%m/%d/%Y") if line.date else "")
        elif c == "description":
            cells.append(line_description(line, tpl))
        elif c == "timekeeper":
            cells.append(initials)
        elif c == "code":
            cells.append(code)
        elif c == "qty":
            cells.append(format_quantity(line))
        elif c == "rate":
            cells.append(money(line.unit_cents) if is_time else "")
        elif c == "amount":
            cells.append(money(line.amount_cents))
    return cells


_COL_WIDTH = {"date": 22, "timekeeper": 12, "code": 16, "qty": 16, "rate": 22, "amount": 22}
_COL_ALIGN = {"date": "LEFT", "description": "LEFT", "timekeeper": "CENTER", "code": "LEFT", "qty": "RIGHT",
              "rate": "RIGHT", "amount": "RIGHT"}


def column_widths(cols, total=174):
    """Description takes whatever the fixed columns leave over."""
    fixed = sum(_COL_WIDTH.get(c, 0) for c in cols if c != "description")
    return tuple(_COL_WIDTH[c] if c != "description" else max(40, total - fixed) for c in cols)


class TemplatePDF(DocPDF):
    """DocPDF with the firm's invoice template: logo top-left (the firm block moves right of it) and an accent
    colour for the title and table headings. Used by invoices and client statements."""

    def __init__(self, firm, title, tpl):
        super().__init__(firm, title)
        self.tpl = tpl
        self.core_fonts_encoding = "cp1252"
        self._logo_w = 0
        if tpl.logo_abs:
            try:
                from PIL import Image
                with Image.open(tpl.logo_abs) as im:
                    w, h = im.size
                self._logo_w = min(45.0, 16.0 * (w / float(h or 1)))
            except Exception:  # unreadable image: print without it
                self._logo_w = 0

    def header(self):
        if self._logo_w:
            try:
                self.image(self.tpl.logo_abs, x=18, y=14, w=self._logo_w, h=16)
                self.set_left_margin(18 + self._logo_w + 6)
                self.set_x(18 + self._logo_w + 6)
                super().header()
            finally:
                self.set_left_margin(18)
            self.set_x(18)
            if self.get_y() < 36:
                self.set_y(36)
            return
        super().header()

    def heading(self, text):
        self.set_font("Helvetica", "B", 20)
        self.set_text_color(*self.tpl.accent_rgb)
        self.cell(0, 10, _pdf_txt(text), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.set_font("Helvetica", "", 10)

    def heading_style(self):
        return FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=self.tpl.accent_rgb)

    def sample_mark(self):
        self.set_font("Helvetica", "B", 44)
        self.set_text_color(215, 215, 215)
        with self.rotation(20, x=105, y=150):
            self.text(55, 160, "SAMPLE")
        self.set_text_color(0, 0, 0)


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
def _builder_context(matter, user=None):
    u = user or current_user()
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
            # Full precision, not two decimals: 7 minutes is 0.11667 hours, and 0.12 x the rate does not
            # come to the amount the client is charged.
            lines.append(InvoiceLine(kind="time", date=t.date, description=t.description or "Legal services",
                                     quantity=t.minutes / 60.0, unit_cents=t.rate_cents,
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
def bulk_row(m, today=None):
    """What is billable on one matter today, or None when nothing is: unbilled time, unbilled expenses, and
    flat-fee milestones due on or before today."""
    today = today or date.today()
    times = [t for t in m.time_entries if t.billable and t.invoice_id is None]
    exps = [e for e in m.expenses if e.billable and e.invoice_id is None]
    ms = [x for x in m.milestones if x.invoice_id is None and x.due_on and x.due_on <= today] \
        if m.billing_type in ("flat", "hybrid") else []
    if not (times or exps or ms):
        return None
    t_total = sum(t.amount_cents for t in times)
    e_total = sum(e.amount_cents for e in exps)
    m_total = sum(x.amount_cents for x in ms)
    payers = list(m.payers)
    return {"matter": m, "time": times, "expenses": exps, "milestones": ms, "time_total": t_total,
            "expense_total": e_total, "milestone_total": m_total, "total": t_total + e_total + m_total,
            "payers": payers, "payers_ok": payers_total_ok(payers)}


def bulk_rows(today=None):
    """Every open matter with something billable today."""
    today = today or date.today()
    rows = []
    for m in Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all():
        r = bulk_row(m, today)
        if r:
            rows.append(r)
    return rows


def build_for_matter(matter, user, issued_on, due_on, today=None):
    """Bulk-style build for one matter with no request context: every unbilled time entry and expense plus the
    milestones due. Returns the invoices created (empty when nothing is billable). Caller commits.
    Raises ValueError when split payers do not total 100%."""
    r = bulk_row(matter, today)
    if not r:
        return []
    if not r["payers_ok"]:
        raise ValueError("split payers do not total 100%")
    ctx = _builder_context(matter, user=user)
    picks = MultiDict([("milestone_ids", str(x.id)) for x in r["milestones"]]
                      + [("time_ids", str(t.id)) for t in r["time"]]
                      + [("expense_ids", str(e.id)) for e in r["expenses"]])
    lines, pt, pe, pm = _lines_from_form(matter, ctx, picks, issued_on)
    if not lines:
        return []
    return create_invoices(matter, user, lines, pt, pe, pm, issued_on, due_on)


@bp.route("/invoices/bulk", methods=["GET", "POST"])
@login_required
def bulk():
    today = date.today()
    firm = Firm.get()
    rows = bulk_rows(today)
    if request.method == "GET":
        open_matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
        return render_template("invoices/bulk.html", rows=rows, today=today, firm_settings=firm,
                               issued_on=today, due_on=today + timedelta(days=firm.invoice_terms_days or 30),
                               grand_total=sum(r["total"] for r in rows), open_matters=open_matters,
                               monthly_count=sum(1 for m in open_matters if m.auto_invoice_monthly))
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


@bp.route("/invoices/bulk/monthly", methods=["POST"])
@login_required
def bulk_monthly():
    """Bulk toggle: which open matters are in the monthly invoicing run (Matter.auto_invoice_monthly)."""
    wanted = {int(x) for x in request.form.getlist("monthly_ids") if x.isdigit()}
    on = off = 0
    for m in Matter.query.filter(Matter.status != "closed").all():
        want = m.id in wanted
        if bool(m.auto_invoice_monthly) != want:
            m.auto_invoice_monthly = want
            on += want
            off += (not want)
    firm = Firm.get()
    audit("update", "firm", firm.id, f"monthly invoicing: {on} matter(s) added, {off} removed", current_user().id)
    db.session.commit()
    day = firm.monthly_billing_day or 0
    when = f"on day {day} of each month" if day else "when a billing day is set under Settings, Invoice template"
    flash(f"Monthly invoicing: {len(wanted)} matter(s) opted in. Drafts are built {when}.", "ok")
    return redirect(url_for("invoices.bulk"))


# ---------------------------------------------------------------- detail
def group_siblings(inv):
    if not inv.split_group:
        return []
    return Invoice.query.filter(Invoice.split_group == inv.split_group, Invoice.id != inv.id).order_by(Invoice.id).all()


@bp.route("/invoices/<int:id>")
@login_required
def detail(id):
    inv = db.session.get(Invoice, id) or abort(404)
    # What this invoice can actually draw: the matter's own trust funds plus the client's
    # unallocated balance. The pooled client total overstates it when another matter is earmarked,
    # and /trust/apply would refuse the difference.
    try:
        from .trust import available_for_matter
        _own, _unalloc, trust_balance = available_for_matter(inv.client, inv.matter)
    except Exception:  # noqa: BLE001
        trust_balance = inv.client.trust_balance_cents()
    apply_default = min(inv.balance_cents, trust_balance) if trust_balance > 0 else 0
    viewed_events = [e for e in inv.events if e.event == "viewed"]
    firm = Firm.get()
    u = current_user()
    return render_template("invoices/detail.html", inv=inv, trust_balance=trust_balance,
                           apply_default=apply_default, public_url=public_url(inv), today=date.today(),
                           viewed_events=viewed_events, dollars=_dollars, siblings=group_siblings(inv),
                           format_quantity=format_quantity,
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
    return render_template("invoices/edit.html", inv=inv, dollars=_dollars, format_quantity=format_quantity)


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


def _sample_invoice(firm):
    """A made-up invoice for the template preview. Clearly fake names and a SAMPLE watermark on the page."""
    today = date.today()
    client = SimpleNamespace(display_name="Sample Client LLC", address="100 Example Street\nSuite 400\nAustin, TX 78701",
                             email="sample@example.com")
    matter = SimpleNamespace(label=f"{firm.matter_prefix or 'M-'}0000 Sample matter (preview)", office=None)
    L = SimpleNamespace
    lines = [
        L(kind="time", date=today - timedelta(days=9), description="Reviewed the contract and drafted a summary of the open issues for the client",
          quantity=1.5, unit_cents=35000, amount_cents=52500, time_entry_id=None, expense_id=None, meta=("AB", "A104")),
        L(kind="time", date=today - timedelta(days=6), description="Telephone call with opposing counsel regarding the settlement proposal",
          quantity=0.5, unit_cents=35000, amount_cents=17500, time_entry_id=None, expense_id=None, meta=("AB", "A106")),
        L(kind="expense", date=today - timedelta(days=5), description="Postage: certified mail to the court",
          quantity=1.0, unit_cents=1245, amount_cents=1245, time_entry_id=None, expense_id=None, meta=("", "E108")),
        L(kind="flat", date=today - timedelta(days=2), description="Flat fee: preparation of the engagement documents",
          quantity=1.0, unit_cents=50000, amount_cents=50000, time_entry_id=None, expense_id=None, meta=("", "")),
    ]
    subtotal = sum(l.amount_cents for l in lines)
    return SimpleNamespace(id=0, number=f"{firm.invoice_prefix or ''}0000", issued_on=today,
                           due_on=today + timedelta(days=firm.invoice_terms_days or 30), currency=firm.currency or "USD",
                           client=client, matter=matter, split_group="", split_pct=100.0, lines=lines,
                           subtotal_cents=subtotal, tax_cents=0, paid_cents=25000, balance_cents=subtotal - 25000,
                           notes="Thank you for the opportunity to help with this matter.", public_token="sample")


def render_invoice_pdf(inv, tpl=None, sample=False):
    """Build the fpdf object for an invoice using the firm's template. `inv` may be a SimpleNamespace stand-in
    (the Settings preview). Nothing is saved here."""
    firm = Firm.get()
    tpl = tpl or invoice_settings(firm)
    cur = inv.currency or "USD"

    def money(c):
        return _pdf_txt(fmt_money(c, cur))

    pdf = TemplatePDF(_letterhead(firm, inv), f"Invoice {inv.number}", tpl)
    pdf.alias_nb_pages()
    pdf.add_page()
    if sample:
        pdf.sample_mark()
    pdf.heading(tpl.title)
    pdf.cell(0, 5, _pdf_txt(f"{tpl.label('invoice_number')}: {inv.number}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, _pdf_txt(f"Issued: {inv.issued_on.strftime('%B %d, %Y') if inv.issued_on else ''}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, _pdf_txt(f"{tpl.label('due')}: {inv.due_on.strftime('%B %d, %Y') if inv.due_on else 'On receipt'}"),
             new_x="LMARGIN", new_y="NEXT")
    if cur != "USD":
        pdf.cell(0, 5, _pdf_txt(f"Currency: {cur}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5, _pdf_txt(tpl.label("bill_to")), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, _pdf_txt(inv.client.display_name), new_x="LMARGIN", new_y="NEXT")
    for line in (inv.client.address or "").splitlines():
        if line.strip():
            pdf.cell(0, 5, _pdf_txt(line), new_x="LMARGIN", new_y="NEXT")
    if inv.client.email:
        pdf.cell(0, 5, _pdf_txt(inv.client.email), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 10)
    matter_label = _pdf_txt(tpl.label("matter")) + ":"
    pdf.cell(max(22, pdf.get_string_width(matter_label) + 3), 5, matter_label)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, _pdf_txt(inv.matter.label), new_x="LMARGIN", new_y="NEXT")
    if inv.split_group:
        pdf.set_font("Helvetica", "I", 9.5)
        pdf.cell(0, 5, _pdf_txt(f"This invoice is {inv.split_pct:g}% of the charges on this matter, billed to "
                                f"{inv.client.display_name}. The remainder is billed separately."),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
    pdf.ln(4)

    cols = visible_columns(tpl)
    pdf.set_font("Helvetica", "", 9.5)
    with pdf.table(col_widths=column_widths(cols), text_align=tuple(_COL_ALIGN[c] for c in cols),
                   line_height=5.5, borders_layout="HORIZONTAL_LINES", headings_style=pdf.heading_style()) as table:
        row = table.row()
        for c in cols:
            row.cell(COLUMN_TITLES[c])
        for l in inv.lines:
            row = table.row()
            for cell in line_cells(l, cols, tpl, money):
                row.cell(_pdf_txt(cell))
    pdf.ln(3)

    def total_row(label, amount, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 10)
        pdf.cell(110, 6, "")
        pdf.cell(42, 6, _pdf_txt(label), align="R")
        pdf.cell(22, 6, money(amount), align="R", new_x="LMARGIN", new_y="NEXT")

    total_row("Subtotal", inv.subtotal_cents)
    if inv.tax_cents:
        total_row("Tax", inv.tax_cents)
    if inv.paid_cents:
        total_row("Paid", -(inv.paid_cents or 0))
    total_row(tpl.label("balance_due"), inv.balance_cents, bold=True)
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5, "Payment instructions", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    link = public_url(inv)
    if tpl.payment_instructions:
        for para in tpl.payment_instructions.replace("{link}", link).split("\n"):
            if para.strip():
                _para(pdf, _pdf_txt(para.strip()))
    else:
        if online_payment_ok(inv):
            _para(pdf, _pdf_txt(f"Pay online by bank transfer (no fee) or card at: {link}"))
            if firm.surcharge_enabled and firm.surcharge_bps:
                _para(pdf, _pdf_txt(f"A {firm.surcharge_bps / 100:.2f}% surcharge applies to card payments. "
                                    f"Bank transfers carry no surcharge."))
        else:
            # The online pages only take US dollars, so do not point this client at them.
            _para(pdf, _pdf_txt(f"This invoice is in {cur}. Please pay by bank transfer, or contact us and we "
                                f"will send you payment instructions. Your invoice is at: {link}"))
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
    return pdf


def build_pdf(inv):
    """Render the invoice PDF, save it to PDF_DIR, set inv.pdf_path (caller commits). Returns the path."""
    pdf = render_invoice_pdf(inv)
    safe_number = "".join(ch for ch in inv.number if ch.isalnum() or ch in "-_") or str(inv.id)
    path = save_pdf(pdf, f"invoice-{safe_number}.pdf")
    inv.pdf_path = path
    return path


def sample_pdf_bytes(tpl=None):
    """The Settings preview: a sample invoice rendered with `tpl` (unsaved settings) and marked SAMPLE."""
    firm = Firm.get()
    pdf = render_invoice_pdf(_sample_invoice(firm), tpl or invoice_settings(firm), sample=True)
    return bytes(pdf.output())


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
    # Build the PDF before anything is marked sent. A client should never get an invoice email with no
    # invoice attached, and the firm should never read "sent" when nothing went out. The caller rolls the
    # session back on an error, so a failure leaves the invoice exactly as it was and it can be sent again.
    try:
        build_pdf(inv)
        with open(inv.pdf_path, "rb") as fh:
            pdf_data = fh.read()
    except Exception as e:
        current_app.logger.exception("invoice pdf failed for invoice %s", inv.id)
        return (f"The invoice PDF could not be built, so invoice {inv.number} was not sent: {e}. "
                f"The invoice is unchanged. Fix the problem and send it again.")
    if inv.status == "draft":
        inv.status = "sent"
    inv.sent_at = now()
    inv.sent_to = to
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
    # Do not promise online payment on an invoice the pay page will refuse (see online_payment_ok).
    if online_payment_ok(inv):
        pay_note = "Bank transfer (ACH) carries no fee." + (
            f" A {firm.surcharge_bps / 100:.2f}% surcharge applies to card payments."
            if firm.surcharge_enabled and firm.surcharge_bps else "")
    else:
        pay_note = (f"This invoice is in {cur}, which our online payment pages do not take, so please pay by "
                    f"bank transfer or contact us for payment instructions.")
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
<p style="font-size:13px;color:#66707d">{pay_note}{currency_note} A PDF copy is attached.</p>
<p style="font-size:13px;color:#66707d">If the button does not work, open this link: <a href="{link}">{link}</a></p>
<p>{escape(firm.name)}{(' | ' + escape(firm.phone)) if firm.phone else ''}</p>
<img src="{pixel}" width="1" height="1" alt="" style="display:block">
</div>"""
    text = (f"{intro}\n\nInvoice {inv.number} for {inv.matter.label}\nBalance due: {fmt_money(inv.balance_cents, cur)}\n"
            f"Due: {inv.due_on.isoformat() if inv.due_on else 'on receipt'}\n\nView and pay: {link}\n")
    attachments = [(f"{inv.number}.pdf", pdf_data, "application/pdf")]
    try:
        send_email(to, subject, html, text=text, attachments=attachments, reply_to=firm.email or None)
    except Exception as e:
        current_app.logger.exception("invoice email failed for invoice %s", inv.id)
        return (f"The email to {to} could not be sent: {e}. Invoice {inv.number} is unchanged and can be "
                f"sent again.")
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
def online_payment_ok(inv):
    """Whether the Stripe pay buttons may be shown for this invoice.

    Stripe Checkout is built in US dollars in payments.py, and the amount, the surcharge and the recorded
    payment are all treated as invoice cents. Offering it on a GBP invoice would charge the client the same
    number in the wrong currency, so a non-USD invoice is pointed at bank transfer instead."""
    return (inv.currency or "USD").upper() == "USD"



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
    tpl = invoice_settings(firm)
    return render_template("invoices/public.html", inv=inv, firm_settings=firm, surcharge_pct=surcharge_pct,
                           trust_balance=trust_balance, payments=[p for p in inv.payments], tpl=tpl,
                           columns=visible_columns(tpl), line_meta=line_meta, line_description=line_description,
                           column_titles=COLUMN_TITLES, format_quantity=format_quantity,
                           lang=lang_for(inv.client), online_payment=online_payment_ok(inv))


@bp.route("/p/firm-logo")
def firm_logo():
    """The firm's invoice logo (Settings > Invoice template). Public because the invoice page is public."""
    path = logo_abs_path(Firm.get().invoice_logo_path)
    if not path:
        abort(404)
    return send_file(path, max_age=300)


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
