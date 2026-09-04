"""Operating accounting: chart of accounts, the operating ledger, automatic postings from payments and
expenses, bank CSV import with matching, an operating reconciliation, and the P&L / balance reports.

Ledger amounts are signed from the bank's point of view (money in positive, money out negative), so the
running bank balance is a plain cumulative sum and the book balance is sum(amount_cents).

Automatic postings run inside SQLAlchemy mapper listeners on Payment and Expense. Listeners get the flush
connection, not the session, so everything they write goes through Core statements on that connection.
"""
import csv
import io
import json
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, Response
from sqlalchemy import event, func, select, insert, update, delete, or_, and_, inspect as sa_inspect

from ..extensions import db
from ..models import (Account, LedgerEntry, BankImport, OperatingReconciliation, Payment, Expense, Invoice,
                      InvoiceLine, Matter, Contact, TrustTransaction, Firm, audit)
from ..helpers import login_required, permission_required, current_user, parse_money, parse_date, cents_to_str

bp = Blueprint("accounting", __name__, url_prefix="/accounting")

ACCOUNT_TYPES = ["income", "expense", "asset", "liability", "equity"]

# (code, name, type). Seeded lazily the first time anything needs an account.
CHART = [
    ("1000", "Operating Bank", "asset"),
    ("1300", "Client Costs Advanced", "asset"),
    ("2100", "Client Trust Liability", "liability"),
    ("3000", "Owner Contribution", "equity"),
    ("3100", "Owner Draw", "equity"),
    ("4000", "Legal Fees", "income"),
    ("4100", "Reimbursed Expenses", "income"),
    ("4200", "Interest Income", "income"),
    ("4300", "Card Surcharge Income", "income"),
    ("6100", "Merchant Fees", "expense"),
    ("6200", "Rent", "expense"),
    ("6300", "Software", "expense"),
    ("6400", "Salaries", "expense"),
    ("6500", "Marketing", "expense"),
    ("6600", "Office", "expense"),
]
CODE_BANK, CODE_CLIENT_COSTS, CODE_LEGAL, CODE_REIMBURSED, CODE_INTEREST, CODE_SURCHARGE, CODE_MERCHANT, CODE_OFFICE = (
    "1000", "1300", "4000", "4100", "4200", "4300", "6100", "6600")

_acc = Account.__table__
_led = LedgerEntry.__table__
_ready_cache = {}


# ---------------------------------------------------------------- chart of accounts
def ensure_chart(conn=None):
    """Seed the chart when the accounts table is empty. `conn` may be a Connection (from a listener) or
    None to use db.session. Returns {code: id}."""
    ex = conn if conn is not None else db.session
    rows = ex.execute(select(_acc.c.code, _acc.c.id)).all()
    if not rows:
        ex.execute(insert(_acc), [dict(code=c, name=n, type=t, is_active=True, is_system=True) for c, n, t in CHART])
        rows = ex.execute(select(_acc.c.code, _acc.c.id)).all()
        if conn is None:
            db.session.commit()
    return {code: aid for code, aid in rows}


def account_id(code, conn=None):
    return ensure_chart(conn).get(code)


def active_accounts():
    ensure_chart()
    return Account.query.filter_by(is_active=True).order_by(Account.code).all()


# ---------------------------------------------------------------- automatic postings
def _ready(connection):
    key = str(connection.engine.url)
    if key not in _ready_cache:
        insp = sa_inspect(connection)
        _ready_cache[key] = insp.has_table("ledger_entries") and insp.has_table("accounts")
    return _ready_cache[key]


def _contact_name(conn, contact_id):
    if not contact_id:
        return ""
    ct = Contact.__table__
    r = conn.execute(select(ct.c.kind, ct.c.first_name, ct.c.last_name, ct.c.company_name)
                     .where(ct.c.id == contact_id)).first()
    if not r:
        return ""
    if r.kind == "company" and r.company_name:
        return r.company_name
    return f"{r.first_name or ''} {r.last_name or ''}".strip() or (r.company_name or "")


def payment_postings(payment, conn):
    """[(account_code, amount_cents, description)] for one operating payment. Fees are split between Legal
    Fees, Reimbursed Expenses and Interest Income in proportion to the invoice's line kinds; surcharge and
    merchant fee get their own lines. The sum equals what actually hit the bank."""
    amount = int(payment.amount_cents or 0)
    legal, reimbursed, interest = amount, 0, 0
    number = ""
    if payment.invoice_id:
        it = Invoice.__table__
        row = conn.execute(select(it.c.number).where(it.c.id == payment.invoice_id)).first()
        number = (row.number if row else "") or ""
        lt = InvoiceLine.__table__
        lines = conn.execute(select(lt.c.kind, lt.c.amount_cents).where(lt.c.invoice_id == payment.invoice_id)).all()
        subtotal = sum(int(l.amount_cents or 0) for l in lines)
        if subtotal > 0 and amount:
            exp = sum(int(l.amount_cents or 0) for l in lines if l.kind == "expense")
            intr = sum(int(l.amount_cents or 0) for l in lines if l.kind == "interest")
            reimbursed = int(round(amount * exp / subtotal))
            interest = int(round(amount * intr / subtotal))
            legal = amount - reimbursed - interest
    label = f"Payment on {number}" if number else "Payment"
    out = [(CODE_LEGAL, legal, f"{label}: legal fees"),
           (CODE_REIMBURSED, reimbursed, f"{label}: reimbursed expenses"),
           (CODE_INTEREST, interest, f"{label}: interest"),
           (CODE_SURCHARGE, int(payment.surcharge_cents or 0), f"{label}: card surcharge"),
           (CODE_MERCHANT, -int(payment.stripe_fee_cents or 0), f"{label}: processing fee")]
    return [o for o in out if o[1]]


def post_payment(payment, conn):
    """Write the ledger lines for an operating Payment on `conn` (a flush connection)."""
    if (payment.account or "operating") != "operating":
        return
    if not _ready(conn):
        return
    ids = ensure_chart(conn)
    payee = _contact_name(conn, payment.client_id)
    ref = payment.reference or payment.stripe_payment_intent or ""
    when = payment.received_on or date.today()
    rows = [dict(date=when, account_id=ids[code], amount_cents=amt, description=desc[:300], payee=payee[:200],
                 reference=ref[:120], matter_id=payment.matter_id, payment_id=payment.id, source="payment",
                 cleared=False, created_at=datetime.utcnow())
            for code, amt, desc in payment_postings(payment, conn)]
    if rows:
        conn.execute(insert(_led), rows)


def expense_values(expense, ids):
    code = CODE_CLIENT_COSTS if expense.billable else CODE_OFFICE
    return dict(date=expense.date or date.today(), account_id=ids[code], amount_cents=-int(expense.amount_cents or 0),
                description=(expense.description or expense.category or "Expense")[:300], payee="",
                reference=(expense.category or "")[:120], matter_id=expense.matter_id, expense_id=expense.id,
                source="expense")


def post_expense(expense, conn):
    if not _ready(conn):
        return
    if not int(expense.amount_cents or 0):
        return
    ids = ensure_chart(conn)
    conn.execute(insert(_led).values(cleared=False, created_at=datetime.utcnow(), **expense_values(expense, ids)))


@event.listens_for(Payment, "after_insert")
def _payment_inserted(mapper, connection, p):
    post_payment(p, connection)


@event.listens_for(Payment, "after_delete")
def _payment_deleted(mapper, connection, p):
    if _ready(connection):
        connection.execute(delete(_led).where(_led.c.payment_id == p.id))


@event.listens_for(Expense, "after_insert")
def _expense_inserted(mapper, connection, e):
    post_expense(e, connection)


@event.listens_for(Expense, "after_update")
def _expense_updated(mapper, connection, e):
    if not _ready(connection):
        return
    watched = ("amount_cents", "billable", "date", "description", "category", "matter_id")
    if not any(sa_inspect(e).attrs[a].history.has_changes() for a in watched):
        return
    ids = ensure_chart(connection)
    vals = expense_values(e, ids)
    existing = connection.execute(select(_led.c.id).where(_led.c.expense_id == e.id)).first()
    if existing:
        connection.execute(update(_led).where(_led.c.expense_id == e.id).values(**vals))
    elif int(e.amount_cents or 0):
        connection.execute(insert(_led).values(cleared=False, created_at=datetime.utcnow(), **vals))


@event.listens_for(Expense, "after_delete")
def _expense_deleted(mapper, connection, e):
    if _ready(connection):
        connection.execute(delete(_led).where(_led.c.expense_id == e.id))


# ---------------------------------------------------------------- balances
def book_balance(as_of=None):
    q = db.session.query(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
    if as_of:
        q = q.filter(LedgerEntry.date <= as_of)
    return int(q.scalar() or 0)


def outstanding_filter(period_end):
    return or_(LedgerEntry.cleared == False,  # noqa: E712
               and_(LedgerEntry.cleared_on != None, LedgerEntry.cleared_on > period_end))  # noqa: E711


def uncategorised_count():
    return LedgerEntry.query.filter(LedgerEntry.account_id == None).count()  # noqa: E711


def _month_bounds(month_str):
    try:
        y, m = [int(x) for x in (month_str or "").split("-")[:2]]
        start = date(y, m, 1)
    except (ValueError, TypeError):
        start = date.today().replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end


# ---------------------------------------------------------------- ledger
@bp.route("/")
@login_required
@permission_required("trust")
def index():
    start, end = _month_bounds(request.args.get("month"))
    account_filter = request.args.get("account_id", type=int)
    opening = book_balance(start - timedelta(days=1))
    q = LedgerEntry.query.filter(LedgerEntry.date >= start, LedgerEntry.date <= end)
    entries = q.order_by(LedgerEntry.date, LedgerEntry.id).all()
    running = opening
    rows = []
    for e in entries:
        running += e.amount_cents
        if account_filter and e.account_id != account_filter:
            continue
        rows.append((e, running))
    money_in = sum(e.amount_cents for e, _ in rows if e.amount_cents > 0)
    money_out = sum(e.amount_cents for e, _ in rows if e.amount_cents < 0)
    prev_month = (start - timedelta(days=1)).strftime("%Y-%m")
    next_month = (end + timedelta(days=1)).strftime("%Y-%m")
    return render_template("accounting/index.html", rows=rows, start=start, end=end, opening=opening,
                           closing=running, money_in=money_in, money_out=money_out, accounts=active_accounts(),
                           account_filter=account_filter, month=start.strftime("%Y-%m"), prev_month=prev_month,
                           next_month=next_month, uncategorised=uncategorised_count(), book=book_balance())


def _entry_from_form(e, form):
    e.date = parse_date(form.get("date"), date.today())
    acc_id = form.get("account_id", type=int)
    acc = db.session.get(Account, acc_id) if acc_id else None
    if not acc:
        raise ValueError("Pick an account.")
    e.account_id = acc.id
    amount = parse_money(form.get("amount"))
    direction = form.get("direction") or ("in" if acc.type in ("income", "liability", "equity") else "out")
    if amount == 0:
        raise ValueError("Enter an amount.")
    amount = abs(amount)
    e.amount_cents = amount if direction == "in" else -amount
    e.payee = (form.get("payee") or "").strip()[:200]
    e.description = (form.get("description") or "").strip()[:300]
    e.reference = (form.get("reference") or "").strip()[:120]
    mid = form.get("matter_id", type=int)
    e.matter_id = mid if mid and db.session.get(Matter, mid) else None
    e.cleared = bool(form.get("cleared"))
    if e.cleared and not e.cleared_on:
        e.cleared_on = e.date
    if not e.cleared:
        e.cleared_on = None


def _entry_form_context(e, is_new):
    return dict(e=e, is_new=is_new, accounts=active_accounts(),
                matters=Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all(),
                direction="in" if (e.amount_cents or 0) > 0 else "out")


@bp.route("/new", methods=["GET", "POST"])
@login_required
@permission_required("trust")
def new():
    e = LedgerEntry(date=date.today(), amount_cents=0, source="manual")
    if request.method == "POST":
        try:
            _entry_from_form(e, request.form)
        except ValueError as ex:
            flash(str(ex), "error")
            return render_template("accounting/entry_form.html", **_entry_form_context(e, True))
        db.session.add(e)
        db.session.flush()
        audit("create", "ledger_entry", e.id, f"{cents_to_str(e.amount_cents)} {e.account.name}", current_user().id)
        db.session.commit()
        flash("Entry added.", "ok")
        return redirect(url_for("accounting.index", month=e.date.strftime("%Y-%m")))
    return render_template("accounting/entry_form.html", **_entry_form_context(e, True))


@bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("trust")
def edit(id):
    e = db.session.get(LedgerEntry, id) or abort(404)
    if e.source not in ("manual", "import"):
        flash("Entries posted from payments and expenses are edited through the payment or expense itself.", "error")
        return redirect(url_for("accounting.index", month=e.date.strftime("%Y-%m")))
    if request.method == "POST":
        try:
            _entry_from_form(e, request.form)
        except ValueError as ex:
            flash(str(ex), "error")
            return render_template("accounting/entry_form.html", **_entry_form_context(e, False))
        audit("update", "ledger_entry", e.id, f"{cents_to_str(e.amount_cents)} {e.account.name}", current_user().id)
        db.session.commit()
        flash("Entry saved.", "ok")
        return redirect(url_for("accounting.index", month=e.date.strftime("%Y-%m")))
    return render_template("accounting/entry_form.html", **_entry_form_context(e, False))


@bp.route("/<int:id>/delete", methods=["POST"])
@login_required
@permission_required("trust")
def delete_entry(id):
    e = db.session.get(LedgerEntry, id) or abort(404)
    if e.source not in ("manual", "import"):
        flash("Delete the payment or expense instead; its ledger lines go with it.", "error")
        return redirect(url_for("accounting.index", month=e.date.strftime("%Y-%m")))
    month = e.date.strftime("%Y-%m")
    audit("delete", "ledger_entry", e.id, f"{cents_to_str(e.amount_cents)} {e.description[:80]}", current_user().id)
    db.session.delete(e)
    db.session.commit()
    flash("Entry deleted.", "ok")
    return redirect(url_for("accounting.index", month=month))


@bp.route("/<int:id>/clear", methods=["POST"])
@login_required
@permission_required("trust")
def clear(id):
    e = db.session.get(LedgerEntry, id) or abort(404)
    e.cleared = not e.cleared
    e.cleared_on = parse_date(request.form.get("cleared_on"), date.today()) if e.cleared else None
    db.session.commit()
    return redirect(request.form.get("next") or url_for("accounting.index", month=e.date.strftime("%Y-%m")))


@bp.route("/<int:id>/categorise", methods=["POST"])
@login_required
@permission_required("trust")
def categorise(id):
    e = db.session.get(LedgerEntry, id) or abort(404)
    acc = db.session.get(Account, request.form.get("account_id", type=int) or 0)
    if not acc:
        flash("Pick an account.", "error")
        return redirect(url_for("accounting.uncategorised"))
    e.account_id = acc.id
    mid = request.form.get("matter_id", type=int)
    if mid and db.session.get(Matter, mid):
        e.matter_id = mid
    audit("update", "ledger_entry", e.id, f"categorised as {acc.code} {acc.name}", current_user().id)
    db.session.commit()
    flash(f"Filed under {acc.code} {acc.name}.", "ok")
    return redirect(request.form.get("next") or url_for("accounting.uncategorised"))


@bp.route("/uncategorised", methods=["GET", "POST"])
@login_required
@permission_required("trust")
def uncategorised():
    if request.method == "POST":
        done = 0
        for key, val in request.form.items():
            if not key.startswith("account_") or not val:
                continue
            e = db.session.get(LedgerEntry, int(key[8:]))
            acc = db.session.get(Account, int(val))
            if not e or not acc or e.account_id:
                continue
            e.account_id = acc.id
            mid = request.form.get(f"matter_{e.id}", type=int)
            if mid and db.session.get(Matter, mid):
                e.matter_id = mid
            audit("update", "ledger_entry", e.id, f"categorised as {acc.code} {acc.name}", current_user().id)
            done += 1
        db.session.commit()
        flash(f"Filed {done} item{'s' if done != 1 else ''}." if done else "Nothing was picked.", "ok" if done else "error")
        return redirect(url_for("accounting.uncategorised"))
    rows = LedgerEntry.query.filter(LedgerEntry.account_id == None).order_by(LedgerEntry.date.desc()).all()  # noqa: E711
    return render_template("accounting/uncategorised.html", rows=rows, accounts=active_accounts(),
                           matters=Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all())


# ---------------------------------------------------------------- accounts CRUD
@bp.route("/accounts")
@login_required
@permission_required("trust")
def accounts():
    ensure_chart()
    rows = Account.query.order_by(Account.code, Account.name).all()
    counts = dict(db.session.query(LedgerEntry.account_id, func.count(LedgerEntry.id)).group_by(LedgerEntry.account_id).all())
    return render_template("accounting/accounts.html", rows=rows, counts=counts)


def _fill_account(a, form):
    a.code = (form.get("code") or "").strip()[:10]
    a.name = (form.get("name") or "").strip()[:120]
    a.type = form.get("type") if form.get("type") in ACCOUNT_TYPES else "expense"
    a.is_active = bool(form.get("is_active", "1" if a.id is None else ""))
    if not a.name:
        raise ValueError("Give the account a name.")


@bp.route("/accounts/new", methods=["GET", "POST"])
@login_required
@permission_required("trust")
def account_new():
    a = Account(type="expense", is_active=True)
    if request.method == "POST":
        try:
            _fill_account(a, request.form)
        except ValueError as ex:
            flash(str(ex), "error")
            return render_template("accounting/account_form.html", a=a, is_new=True, types=ACCOUNT_TYPES)
        db.session.add(a)
        db.session.commit()
        flash(f"Account {a.code} {a.name} added.", "ok")
        return redirect(url_for("accounting.accounts"))
    return render_template("accounting/account_form.html", a=a, is_new=True, types=ACCOUNT_TYPES)


@bp.route("/accounts/<int:id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("trust")
def account_edit(id):
    a = db.session.get(Account, id) or abort(404)
    if request.method == "POST":
        try:
            _fill_account(a, request.form)
            if a.is_system:
                a.is_active = True  # system accounts stay live; the postings need them
        except ValueError as ex:
            flash(str(ex), "error")
            return render_template("accounting/account_form.html", a=a, is_new=False, types=ACCOUNT_TYPES)
        db.session.commit()
        flash("Account saved.", "ok")
        return redirect(url_for("accounting.accounts"))
    return render_template("accounting/account_form.html", a=a, is_new=False, types=ACCOUNT_TYPES)


@bp.route("/accounts/<int:id>/delete", methods=["POST"])
@login_required
@permission_required("trust")
def account_delete(id):
    a = db.session.get(Account, id) or abort(404)
    if a.is_system:
        flash("System accounts cannot be deleted. The automatic postings depend on them.", "error")
        return redirect(url_for("accounting.accounts"))
    n = LedgerEntry.query.filter_by(account_id=a.id).count()
    if n:
        a.is_active = False
        db.session.commit()
        flash(f"{a.name} has {n} entries, so it was deactivated instead of deleted.", "ok")
        return redirect(url_for("accounting.accounts"))
    db.session.delete(a)
    db.session.commit()
    flash("Account deleted.", "ok")
    return redirect(url_for("accounting.accounts"))


# ---------------------------------------------------------------- bank CSV import
DATE_COLS = ("date", "posted", "posting date", "transaction date", "post date", "trans date")
DESC_COLS = ("description", "memo", "name", "payee", "details", "transaction", "narrative")
AMOUNT_COLS = ("amount", "amt", "transaction amount")
DEBIT_COLS = ("debit", "withdrawal", "withdrawals", "money out", "debits", "paid out")
CREDIT_COLS = ("credit", "deposit", "deposits", "money in", "credits", "paid in")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%m-%d-%Y")
MATCH_DAYS = 3


def _find_col(headers, names):
    low = {h.strip().lower(): h for h in headers if h}
    for n in names:
        if n in low:
            return low[n]
    for key, h in low.items():
        if any(n in key for n in names):
            return h
    return None


def _parse_row_date(s):
    s = (s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def parse_bank_csv(text):
    """-> (rows, errors). Each row: {date, description, amount_cents}. Detects Date/Description/Amount
    or Debit/Credit columns by header name."""
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    dcol = _find_col(headers, DATE_COLS)
    tcol = _find_col(headers, DESC_COLS)
    acol = _find_col(headers, AMOUNT_COLS)
    debit = _find_col(headers, DEBIT_COLS)
    credit = _find_col(headers, CREDIT_COLS)
    errors = []
    if not dcol or not (acol or debit or credit):
        return [], [f"Could not find the date and amount columns. Headers seen: {', '.join(h for h in headers if h)}"]
    rows = []
    for i, r in enumerate(reader, start=2):
        d = _parse_row_date(r.get(dcol))
        if not d:
            errors.append(f"Line {i}: unreadable date '{r.get(dcol)}'")
            continue
        if acol and (r.get(acol) or "").strip():
            amt = parse_money(r.get(acol))
        else:
            amt = parse_money(r.get(credit)) - abs(parse_money(r.get(debit))) if (debit or credit) else 0
        if amt == 0:
            continue
        rows.append({"date": d, "description": (r.get(tcol) or "").strip()[:300] if tcol else "", "amount_cents": amt})
    return rows, errors


def match_rows(rows):
    """Attach match_id to each row: an existing uncleared entry with the same amount within MATCH_DAYS.
    Each ledger entry is matched at most once."""
    if not rows:
        return rows
    lo = min(r["date"] for r in rows) - timedelta(days=MATCH_DAYS)
    hi = max(r["date"] for r in rows) + timedelta(days=MATCH_DAYS)
    candidates = LedgerEntry.query.filter(LedgerEntry.date >= lo, LedgerEntry.date <= hi,
                                          LedgerEntry.cleared == False).all()  # noqa: E712
    used = set()
    for r in rows:
        best = None
        for e in candidates:
            if e.id in used or e.amount_cents != r["amount_cents"]:
                continue
            gap = abs((e.date - r["date"]).days)
            if gap <= MATCH_DAYS and (best is None or gap < best[0]):
                best = (gap, e)
        if best:
            used.add(best[1].id)
            r["match_id"] = best[1].id
            r["match_label"] = f"{best[1].date.isoformat()} {best[1].description or best[1].payee}"
        else:
            r["match_id"] = None
    return rows


@bp.route("/import", methods=["GET", "POST"])
@login_required
@permission_required("trust")
def import_csv():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a CSV file exported from your bank.", "error")
            return redirect(url_for("accounting.import_csv"))
        try:
            text = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            f.seek(0)
            text = f.read().decode("latin-1")
        rows, errors = parse_bank_csv(text)
        if not rows:
            flash(" ".join(errors) or "No rows with an amount were found in that file.", "error")
            return redirect(url_for("accounting.import_csv"))
        rows = match_rows(rows)
        payload = json.dumps([{"date": r["date"].isoformat(), "description": r["description"],
                               "amount_cents": r["amount_cents"], "match_id": r["match_id"]} for r in rows])
        return render_template("accounting/import_preview.html", rows=rows, errors=errors, rows_json=payload,
                               filename=f.filename, matched=sum(1 for r in rows if r["match_id"]))
    past = BankImport.query.order_by(BankImport.imported_at.desc()).limit(20).all()
    return render_template("accounting/import.html", past=past)


@bp.route("/import/commit", methods=["POST"])
@login_required
@permission_required("trust")
def import_commit():
    try:
        rows = json.loads(request.form.get("rows_json") or "[]")
    except ValueError:
        rows = []
    if not rows:
        flash("Nothing to import.", "error")
        return redirect(url_for("accounting.import_csv"))
    matched = created = skipped = 0
    for i, r in enumerate(rows):
        action = request.form.get(f"action_{i}", "auto")
        if action == "skip":
            skipped += 1
            continue
        d = parse_date(r.get("date"), date.today())
        entry = db.session.get(LedgerEntry, r.get("match_id")) if r.get("match_id") and action != "new" else None
        if entry:
            entry.cleared = True
            entry.cleared_on = d
            if not entry.reference:
                entry.reference = "bank import"
            matched += 1
        else:
            db.session.add(LedgerEntry(date=d, account_id=None, amount_cents=int(r.get("amount_cents") or 0),
                                       description=(r.get("description") or "")[:300], source="import",
                                       cleared=True, cleared_on=d))
            created += 1
    imp = BankImport(filename=(request.form.get("filename") or "")[:300], rows=len(rows), matched=matched,
                     created=created, created_by_id=current_user().id)
    db.session.add(imp)
    db.session.flush()
    audit("bank_import", "bank_import", imp.id, f"{len(rows)} rows, {matched} matched, {created} new", current_user().id)
    db.session.commit()
    flash(f"Imported {len(rows)} rows: {matched} matched and marked cleared, {created} new entries"
          + (f", {skipped} skipped" if skipped else "") + ".", "ok")
    return redirect(url_for("accounting.uncategorised") if created else url_for("accounting.index"))


# ---------------------------------------------------------------- reconciliation
@bp.route("/reconcile", methods=["GET", "POST"])
@login_required
@permission_required("trust")
def reconcile():
    if request.method == "POST":
        period_end = parse_date(request.form.get("period_end"))
        statement = parse_money(request.form.get("statement_balance"))
        if not period_end:
            flash("Enter the statement period end date.", "error")
            return redirect(url_for("accounting.reconcile"))
        book = book_balance(period_end)
        base = LedgerEntry.query.filter(LedgerEntry.date <= period_end, outstanding_filter(period_end))
        oin = base.filter(LedgerEntry.amount_cents > 0).all()
        oout = base.filter(LedgerEntry.amount_cents < 0).all()
        in_cents = sum(e.amount_cents for e in oin)
        out_cents = sum(e.amount_cents for e in oout)  # negative
        adjusted = statement + in_cents + out_cents
        balanced = adjusted == book
        detail = {"outstanding_in_ids": [e.id for e in oin], "outstanding_out_ids": [e.id for e in oout],
                  "adjusted_bank_cents": adjusted, "difference_cents": adjusted - book,
                  "uncategorised": LedgerEntry.query.filter(LedgerEntry.account_id == None,  # noqa: E711
                                                            LedgerEntry.date <= period_end).count()}
        r = OperatingReconciliation(period_end=period_end, statement_balance_cents=statement, book_balance_cents=book,
                                    outstanding_in_cents=in_cents, outstanding_out_cents=out_cents, balanced=balanced,
                                    detail_json=json.dumps(detail), notes=(request.form.get("notes") or "").strip(),
                                    created_by_id=current_user().id)
        db.session.add(r)
        db.session.flush()
        audit("operating_reconcile", "operating_reconciliation", r.id,
              f"period {period_end.isoformat()} {'balanced' if balanced else 'out of balance'}", current_user().id)
        db.session.commit()
        return redirect(url_for("accounting.reconcile_report", recon_id=r.id))
    past = OperatingReconciliation.query.order_by(OperatingReconciliation.period_end.desc(),
                                                  OperatingReconciliation.id.desc()).all()
    return render_template("accounting/reconcile.html", past=past, today=date.today(), book=book_balance())


@bp.route("/reconcile/<int:recon_id>")
@login_required
@permission_required("trust")
def reconcile_report(recon_id):
    r = db.session.get(OperatingReconciliation, recon_id) or abort(404)
    try:
        detail = json.loads(r.detail_json or "{}")
    except ValueError:
        detail = {}
    ids = list(detail.get("outstanding_in_ids", [])) + list(detail.get("outstanding_out_ids", []))
    found = {e.id: e for e in LedgerEntry.query.filter(LedgerEntry.id.in_(ids or [0])).all()}
    oin = [found[i] for i in detail.get("outstanding_in_ids", []) if i in found]
    oout = [found[i] for i in detail.get("outstanding_out_ids", []) if i in found]
    adjusted = r.statement_balance_cents + r.outstanding_in_cents + r.outstanding_out_cents
    return render_template("accounting/reconcile_report.html", r=r, detail=detail, oin=oin, oout=oout,
                           adjusted=adjusted, diff=adjusted - r.book_balance_cents)


# ---------------------------------------------------------------- reports
def _range():
    today = date.today()
    start = parse_date(request.args.get("from"), today.replace(month=1, day=1))
    end = parse_date(request.args.get("to"), today)
    return start, end


def pnl_data(start, end):
    ensure_chart()
    sums = dict(db.session.query(LedgerEntry.account_id, func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
                .filter(LedgerEntry.date >= start, LedgerEntry.date <= end, LedgerEntry.account_id != None)  # noqa: E711
                .group_by(LedgerEntry.account_id).all())
    accounts = Account.query.order_by(Account.code).all()
    income = [(a, int(sums.get(a.id, 0))) for a in accounts if a.type == "income" and sums.get(a.id)]
    expense = [(a, int(sums.get(a.id, 0))) for a in accounts if a.type == "expense" and sums.get(a.id)]
    other = [(a, int(sums.get(a.id, 0))) for a in accounts if a.type not in ("income", "expense") and sums.get(a.id)]
    total_income = sum(v for _, v in income)
    total_expense = sum(v for _, v in expense)  # negative from the bank's view
    unc = db.session.query(func.count(LedgerEntry.id), func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).filter(
        LedgerEntry.date >= start, LedgerEntry.date <= end, LedgerEntry.account_id == None).first()  # noqa: E711
    return dict(income=income, expense=expense, other=other, total_income=total_income, total_expense=total_expense,
                net=total_income + total_expense, uncategorised_count=int(unc[0] or 0), uncategorised_cents=int(unc[1] or 0))


def _csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


@bp.route("/pnl")
@login_required
@permission_required("trust")
def pnl():
    start, end = _range()
    d = pnl_data(start, end)
    if request.args.get("format") == "csv":
        rows = [("Income", a.code, a.name, cents_to_str(v)) for a, v in d["income"]]
        rows.append(("Income", "", "Total income", cents_to_str(d["total_income"])))
        rows += [("Expense", a.code, a.name, cents_to_str(-v)) for a, v in d["expense"]]
        rows.append(("Expense", "", "Total expenses", cents_to_str(-d["total_expense"])))
        rows.append(("Net", "", "Net income", cents_to_str(d["net"])))
        return _csv_response(f"pnl_{start.isoformat()}_{end.isoformat()}.csv", ["Section", "Code", "Account", "Amount"], rows)
    return render_template("accounting/pnl.html", start=start, end=end, **d)


def balance_data():
    ensure_chart()
    bank_book = book_balance()
    bank_cleared = int(db.session.query(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
                       .filter(LedgerEntry.cleared == True).scalar() or 0)  # noqa: E712
    trust = int(db.session.query(func.coalesce(func.sum(TrustTransaction.amount_cents), 0)).scalar() or 0)
    open_inv = Invoice.query.filter(Invoice.status.in_(["sent", "viewed", "partial"])).all()
    receivables = sum(i.balance_cents for i in open_inv)
    client_costs = int(db.session.query(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
                       .join(Account, Account.id == LedgerEntry.account_id)
                       .filter(Account.code == CODE_CLIENT_COSTS).scalar() or 0)
    return dict(bank_book=bank_book, bank_cleared=bank_cleared, uncleared=bank_book - bank_cleared, trust=trust,
                receivables=receivables, receivable_count=len(open_inv), client_costs=-client_costs,
                uncategorised=uncategorised_count(), f=Firm.get())


@bp.route("/balance")
@login_required
@permission_required("trust")
def balance():
    d = balance_data()
    if request.args.get("format") == "csv":
        rows = [("Operating bank (book)", cents_to_str(d["bank_book"])),
                ("Operating bank (cleared)", cents_to_str(d["bank_cleared"])),
                ("Uncleared items", cents_to_str(d["uncleared"])),
                ("Accounts receivable", cents_to_str(d["receivables"])),
                ("Client costs advanced (unrecovered)", cents_to_str(d["client_costs"])),
                ("Client trust liability", cents_to_str(d["trust"]))]
        return _csv_response(f"balance_{date.today().isoformat()}.csv", ["Line", "Amount"], rows)
    return render_template("accounting/balance.html", today=date.today(), **d)
