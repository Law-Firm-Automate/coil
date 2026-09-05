"""Reports: A/R aging, WIP, revenue, trust balances, productivity, origination, realization, profitability.
Each one has a CSV download (?format=csv)."""
import csv
import io
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from flask import Blueprint, render_template, request, Response
from ..extensions import db
from ..models import Invoice, TimeEntry, Expense, Payment, TrustTransaction, User
from ..helpers import login_required, parse_date

bp = Blueprint("reports", __name__, url_prefix="/reports")

OPEN_STATUSES = ("sent", "viewed", "partial")
BUCKETS = ["current", "1-30", "31-60", "61-90", "90+"]


def _csv(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _money_csv(c):
    """Cents -> '1234.56' for spreadsheets (no symbol, no thousands separator)."""
    return f"{int(c or 0) / 100:.2f}"


def _wants_csv():
    return request.args.get("format") == "csv"


def _range():
    today = date.today()
    default_from = (today.replace(day=1) - timedelta(days=334)).replace(day=1)
    d_from = parse_date(request.args.get("from"), default_from)
    d_to = parse_date(request.args.get("to"), today)
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    return d_from, d_to


def _bucket(days):
    if days <= 0:
        return "current"
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


@bp.route("")
@login_required
def index():
    return render_template("reports/index.html")


# ---------------------------------------------------------------- A/R aging
@bp.route("/ar-aging")
@login_required
def ar_aging():
    today = date.today()
    invoices = Invoice.query.filter(Invoice.status.in_(OPEN_STATUSES)).all()
    by_client = OrderedDict()
    for inv in invoices:
        if inv.balance_cents <= 0:
            continue
        anchor = inv.due_on or inv.issued_on or today
        b = _bucket((today - anchor).days)
        row = by_client.setdefault(inv.client_id, {"client": inv.client, "invoices": [],
                                                    **{k: 0 for k in BUCKETS}, "total": 0})
        row[b] += inv.balance_cents
        row["total"] += inv.balance_cents
        row["invoices"].append((inv, b))
    rows = sorted(by_client.values(), key=lambda r: -r["total"])
    totals = {k: sum(r[k] for r in rows) for k in BUCKETS + ["total"]}
    if _wants_csv():
        out = [[r["client"].display_name] + [_money_csv(r[k]) for k in BUCKETS + ["total"]] for r in rows]
        out.append(["TOTAL"] + [_money_csv(totals[k]) for k in BUCKETS + ["total"]])
        return _csv("ar-aging.csv", ["Client", "Current", "1-30", "31-60", "61-90", "90+", "Total"], out)
    return render_template("reports/ar_aging.html", rows=rows, totals=totals, buckets=BUCKETS, today=today)


# ---------------------------------------------------------------- WIP
@bp.route("/wip")
@login_required
def wip():
    time_rows = TimeEntry.query.filter(TimeEntry.billable == True, TimeEntry.invoice_id == None).all()  # noqa: E712
    exp_rows = Expense.query.filter(Expense.billable == True, Expense.invoice_id == None).all()  # noqa: E712
    by_matter = {}
    for t in time_rows:
        r = by_matter.setdefault(t.matter_id, {"matter": t.matter, "minutes": 0, "time_cents": 0,
                                                "expense_cents": 0, "entries": 0, "expenses": 0, "oldest": None})
        r["minutes"] += t.minutes
        r["time_cents"] += t.amount_cents
        r["entries"] += 1
        if t.date and (r["oldest"] is None or t.date < r["oldest"]):
            r["oldest"] = t.date
    for e in exp_rows:
        r = by_matter.setdefault(e.matter_id, {"matter": e.matter, "minutes": 0, "time_cents": 0,
                                                "expense_cents": 0, "entries": 0, "expenses": 0, "oldest": None})
        r["expense_cents"] += e.amount_cents
        r["expenses"] += 1
        if e.date and (r["oldest"] is None or e.date < r["oldest"]):
            r["oldest"] = e.date
    rows = sorted(by_matter.values(), key=lambda r: -(r["time_cents"] + r["expense_cents"]))
    for r in rows:
        r["total"] = r["time_cents"] + r["expense_cents"]
    totals = {"minutes": sum(r["minutes"] for r in rows), "time_cents": sum(r["time_cents"] for r in rows),
              "expense_cents": sum(r["expense_cents"] for r in rows), "total": sum(r["total"] for r in rows)}
    if _wants_csv():
        out = [[r["matter"].number, r["matter"].name, r["matter"].client.display_name, r["matter"].billing_type,
                f"{r['minutes'] / 60:.2f}", _money_csv(r["time_cents"]), _money_csv(r["expense_cents"]),
                _money_csv(r["total"]), r["oldest"].isoformat() if r["oldest"] else ""] for r in rows]
        out.append(["TOTAL", "", "", "", f"{totals['minutes'] / 60:.2f}", _money_csv(totals["time_cents"]),
                    _money_csv(totals["expense_cents"]), _money_csv(totals["total"]), ""])
        return _csv("wip.csv", ["Matter", "Name", "Client", "Billing", "Unbilled hours", "Unbilled time",
                                "Unbilled expenses", "Total WIP", "Oldest item"], out)
    return render_template("reports/wip.html", rows=rows, totals=totals)


# ---------------------------------------------------------------- revenue
@bp.route("/revenue")
@login_required
def revenue():
    d_from, d_to = _range()
    payments = (Payment.query.filter(Payment.account == "operating", Payment.received_on >= d_from,
                                     Payment.received_on <= d_to)
                .order_by(Payment.received_on).all())
    by_month = OrderedDict()
    by_matter = {}
    by_method = defaultdict(int)
    for p in payments:
        key = p.received_on.strftime("%Y-%m") if p.received_on else "unknown"
        m = by_month.setdefault(key, {"month": key, "cents": 0, "surcharge": 0, "fees": 0, "count": 0})
        m["cents"] += p.amount_cents or 0
        m["surcharge"] += p.surcharge_cents or 0
        m["fees"] += p.stripe_fee_cents or 0
        m["count"] += 1
        matter = p.matter or (p.invoice.matter if p.invoice else None)
        mk = matter.id if matter else 0
        r = by_matter.setdefault(mk, {"matter": matter, "cents": 0, "count": 0})
        r["cents"] += p.amount_cents or 0
        r["count"] += 1
        by_method[p.method or "other"] += p.amount_cents or 0
    matter_rows = sorted(by_matter.values(), key=lambda r: -r["cents"])
    total = sum(p.amount_cents or 0 for p in payments)
    if _wants_csv():
        out = [["month", m["month"], "", m["count"], _money_csv(m["cents"]), _money_csv(m["surcharge"]),
                _money_csv(m["fees"])] for m in by_month.values()]
        out += [["matter", r["matter"].number if r["matter"] else "(none)",
                 r["matter"].name if r["matter"] else "", r["count"], _money_csv(r["cents"]), "", ""]
                for r in matter_rows]
        out.append(["total", f"{d_from.isoformat()} to {d_to.isoformat()}", "", len(payments), _money_csv(total),
                    "", ""])
        return _csv("revenue.csv", ["Group", "Key", "Name", "Payments", "Amount", "Surcharge", "Processor fees"],
                    out)
    return render_template("reports/revenue.html", months=list(by_month.values()), matter_rows=matter_rows,
                           by_method=dict(by_method), total=total, count=len(payments), d_from=d_from, d_to=d_to)


# ---------------------------------------------------------------- trust balances
@bp.route("/trust-balances")
@login_required
def trust_balances():
    txns = TrustTransaction.query.all()
    by_client = {}
    by_matter = {}
    for t in txns:
        c = by_client.setdefault(t.client_id, {"client": t.client, "cents": 0, "count": 0, "uncleared": 0})
        c["cents"] += t.amount_cents
        c["count"] += 1
        if not t.cleared:
            c["uncleared"] += 1
        mk = t.matter_id or 0
        m = by_matter.setdefault((t.client_id, mk), {"client": t.client, "matter": t.matter, "cents": 0})
        m["cents"] += t.amount_cents
    client_rows = sorted(by_client.values(), key=lambda r: r["client"].sort_name or "")
    matter_rows = sorted(by_matter.values(), key=lambda r: (r["client"].sort_name or "",
                                                              r["matter"].number if r["matter"] else ""))
    total = sum(r["cents"] for r in client_rows)
    negatives = [r for r in client_rows if r["cents"] < 0] + [r for r in matter_rows if r["cents"] < 0]
    if _wants_csv():
        out = [["client", r["client"].display_name, "", _money_csv(r["cents"])] for r in client_rows]
        out += [["matter", r["client"].display_name, r["matter"].label if r["matter"] else "(general)",
                 _money_csv(r["cents"])] for r in matter_rows]
        out.append(["total", "", "", _money_csv(total)])
        return _csv("trust-balances.csv", ["Level", "Client", "Matter", "Balance"], out)
    return render_template("reports/trust_balances.html", client_rows=client_rows, matter_rows=matter_rows,
                           total=total, negatives=negatives)


# ---------------------------------------------------------------- productivity
@bp.route("/productivity")
@login_required
def productivity():
    d_from, d_to = _range()
    entries = TimeEntry.query.filter(TimeEntry.date >= d_from, TimeEntry.date <= d_to).all()
    months = sorted({e.date.strftime("%Y-%m") for e in entries})
    users = {u.id: u for u in User.query.all()}
    grid = {}
    for e in entries:
        key = (e.user_id, e.date.strftime("%Y-%m"))
        cell = grid.setdefault(key, {"billable": 0, "nonbillable": 0, "billed": 0, "amount": 0})
        if e.billable:
            cell["billable"] += e.minutes
            cell["amount"] += e.amount_cents
            if e.invoice_id:
                cell["billed"] += e.minutes
        else:
            cell["nonbillable"] += e.minutes
    user_rows = []
    for uid in sorted({k[0] for k in grid}, key=lambda i: users[i].name if i in users else ""):
        u = users.get(uid)
        cells = [grid.get((uid, m), {"billable": 0, "nonbillable": 0, "billed": 0, "amount": 0}) for m in months]
        tot = {"billable": sum(c["billable"] for c in cells), "nonbillable": sum(c["nonbillable"] for c in cells),
               "billed": sum(c["billed"] for c in cells), "amount": sum(c["amount"] for c in cells)}
        user_rows.append({"user": u, "cells": cells, "total": tot})
    grand = {"billable": sum(r["total"]["billable"] for r in user_rows),
             "nonbillable": sum(r["total"]["nonbillable"] for r in user_rows),
             "amount": sum(r["total"]["amount"] for r in user_rows)}
    if _wants_csv():
        out = []
        for r in user_rows:
            for m, c in zip(months, r["cells"]):
                out.append([r["user"].name if r["user"] else uid, m, f"{c['billable'] / 60:.2f}",
                            f"{c['nonbillable'] / 60:.2f}", f"{(c['billable'] + c['nonbillable']) / 60:.2f}",
                            f"{c['billed'] / 60:.2f}", _money_csv(c["amount"])])
            out.append([r["user"].name if r["user"] else uid, "TOTAL", f"{r['total']['billable'] / 60:.2f}",
                        f"{r['total']['nonbillable'] / 60:.2f}",
                        f"{(r['total']['billable'] + r['total']['nonbillable']) / 60:.2f}",
                        f"{r['total']['billed'] / 60:.2f}", _money_csv(r["total"]["amount"])])
        return _csv("productivity.csv", ["User", "Month", "Billable hours", "Non-billable hours", "Total hours",
                                         "Billed hours", "Billable value"], out)
    return render_template("reports/productivity.html", months=months, user_rows=user_rows, grand=grand,
                           d_from=d_from, d_to=d_to)


# ================================================================ Phase 2: origination, realization, profitability
# Shared bits for the three attorney-level reports below.
from ..models import Matter, InvoiceLine  # noqa: E402


def _pct(num, den):
    """Percent as a float, or None when the denominator is zero (templates show a dash)."""
    if not den:
        return None
    return round(100.0 * num / den, 1)


def _pct_csv(v):
    return "" if v is None else f"{v:.1f}"


def _hours_csv(minutes):
    return f"{(minutes or 0) / 60:.2f}"


def _attorney_for(matter):
    """Originator, falling back to the responsible attorney. Returns (user_or_None, flagged).
    flagged is True when the matter has no originator recorded."""
    if matter is None:
        return None, True
    if matter.originating_user_id and matter.originator:
        return matter.originator, False
    return matter.responsible, True


# ---------------------------------------------------------------- origination
def origination_data(d_from, d_to):
    """Collected operating revenue in the range, grouped by originating attorney.
    Returns (rows, totals). Each row: {"user", "cents", "count", "matter_count", "flagged_count", "matters": [...]}.
    Matters with no originator use the responsible attorney and carry flagged=True."""
    payments = (Payment.query.filter(Payment.account == "operating", Payment.received_on >= d_from,
                                     Payment.received_on <= d_to).all())
    by_user = {}
    for p in payments:
        matter = p.matter or (p.invoice.matter if p.invoice else None)
        user, flagged = _attorney_for(matter)
        uid = user.id if user else 0
        row = by_user.setdefault(uid, {"user": user, "cents": 0, "count": 0, "matters": {}})
        row["cents"] += p.amount_cents or 0
        row["count"] += 1
        mk = matter.id if matter else 0
        mrow = row["matters"].setdefault(mk, {"matter": matter, "cents": 0, "count": 0, "flagged": flagged})
        mrow["cents"] += p.amount_cents or 0
        mrow["count"] += 1
    rows = []
    for row in by_user.values():
        matters = sorted(row["matters"].values(), key=lambda r: -r["cents"])
        row["matters"] = matters
        row["matter_count"] = len([m for m in matters if m["matter"]])
        row["flagged_count"] = len([m for m in matters if m["flagged"]])
        rows.append(row)
    rows.sort(key=lambda r: (-r["cents"], r["user"].name if r["user"] else "zzz"))
    totals = {"cents": sum(r["cents"] for r in rows), "count": sum(r["count"] for r in rows),
              "matter_count": sum(r["matter_count"] for r in rows),
              "flagged_count": sum(r["flagged_count"] for r in rows)}
    return rows, totals


@bp.route("/origination")
@login_required
def origination():
    d_from, d_to = _range()
    rows, totals = origination_data(d_from, d_to)
    if _wants_csv():
        out = []
        for r in rows:
            name = r["user"].name if r["user"] else "(no attorney)"
            for m in r["matters"]:
                out.append([name, m["matter"].number if m["matter"] else "", m["matter"].name if m["matter"] else "(no matter)",
                            m["matter"].client.display_name if m["matter"] else "", m["count"], _money_csv(m["cents"]),
                            "no originator, responsible attorney used" if m["flagged"] else ""])
            out.append([name, "TOTAL", f"{r['matter_count']} matters", "", r["count"], _money_csv(r["cents"]),
                        f"{r['flagged_count']} flagged" if r["flagged_count"] else ""])
        out.append(["ALL", f"{d_from.isoformat()} to {d_to.isoformat()}", f"{totals['matter_count']} matters", "",
                    totals["count"], _money_csv(totals["cents"]), ""])
        return _csv("origination.csv", ["Attorney", "Matter", "Name", "Client", "Payments", "Collected", "Flag"], out)
    return render_template("reports/origination.html", rows=rows, totals=totals, d_from=d_from, d_to=d_to)


# ---------------------------------------------------------------- realization
def _time_lines_by_entry():
    """{time_entry_id: [InvoiceLine]} for time lines on non-void invoices. Split-billed invoices scale their
    lines by split_pct, so a line is grossed back up to the full entry value here."""
    lines = (db.session.query(InvoiceLine).join(Invoice, Invoice.id == InvoiceLine.invoice_id)
             .filter(InvoiceLine.time_entry_id != None, Invoice.status != "void").all())  # noqa: E711
    out = defaultdict(list)
    for ln in lines:
        out[ln.time_entry_id].append(ln)
    return out


def _line_full_value(ln):
    inv = ln.invoice
    pct = inv.split_pct if inv and inv.split_group and inv.split_pct else 100.0
    if pct and pct != 100.0:
        return int(round((ln.amount_cents or 0) * 100.0 / pct))
    return ln.amount_cents or 0


def realization_data(d_from, d_to):
    """Worked / billed / collected per attorney and per matter for time entries dated in the range.
    worked   = every time entry at its rate
    billed   = the time lines those entries produced on non-void invoices (entry value when the line is missing)
    collected = payments on those invoices, prorated by each time line's share of the invoice total
    write-downs = worked minus billed, only for matters that have at least one non-void invoice."""
    entries = TimeEntry.query.filter(TimeEntry.date >= d_from, TimeEntry.date <= d_to).all()
    lines_by_entry = _time_lines_by_entry()
    invoiced_matters = {mid for (mid,) in db.session.query(Invoice.matter_id).filter(Invoice.status != "void")
                        .distinct().all()}
    paid_cache = {}

    def paid_on(inv):
        if inv.id not in paid_cache:
            paid_cache[inv.id] = sum(p.amount_cents or 0 for p in inv.payments)
        return paid_cache[inv.id]

    def blank(key):
        return {key: None, "minutes": 0, "worked": 0, "billed": 0, "collected": 0, "writedown": 0, "entries": 0}

    by_user, by_matter = {}, {}
    for e in entries:
        worked = e.amount_cents
        billed = 0
        collected = 0.0
        lines = lines_by_entry.get(e.id, [])
        if lines:
            for ln in lines:
                billed += _line_full_value(ln)
                inv = ln.invoice
                if inv and inv.total_cents:
                    collected += paid_on(inv) * (ln.amount_cents or 0) / inv.total_cents
        elif e.invoice_id and e.invoice and e.invoice.status != "void":
            inv = e.invoice
            billed = worked
            if inv.total_cents:
                collected = paid_on(inv) * worked / inv.total_cents
        collected = int(round(collected))
        writedown = (worked - billed) if e.matter_id in invoiced_matters else 0
        for key, store, obj in (("user", by_user, e.user), ("matter", by_matter, e.matter)):
            k = obj.id if obj else 0
            r = store.setdefault(k, blank(key))
            r[key] = obj
            r["minutes"] += e.minutes or 0
            r["worked"] += worked
            r["billed"] += billed
            r["collected"] += collected
            r["writedown"] += writedown
            r["entries"] += 1

    def finish(rows):
        for r in rows:
            r["billing_pct"] = _pct(r["billed"], r["worked"])
            r["collection_pct"] = _pct(r["collected"], r["billed"])
        return rows

    user_rows = finish(sorted(by_user.values(), key=lambda r: -r["worked"]))
    matter_rows = finish(sorted(by_matter.values(), key=lambda r: -r["worked"]))
    for r in matter_rows:
        r["invoiced"] = (r["matter"].id in invoiced_matters) if r["matter"] else False
    totals = {k: sum(r[k] for r in user_rows) for k in ("minutes", "worked", "billed", "collected", "writedown",
                                                        "entries")}
    totals["billing_pct"] = _pct(totals["billed"], totals["worked"])
    totals["collection_pct"] = _pct(totals["collected"], totals["billed"])
    return user_rows, matter_rows, totals


@bp.route("/realization")
@login_required
def realization():
    d_from, d_to = _range()
    user_rows, matter_rows, totals = realization_data(d_from, d_to)
    if _wants_csv():
        out = []
        for r in user_rows:
            out.append(["attorney", r["user"].name if r["user"] else "(unknown)", "", _hours_csv(r["minutes"]),
                        _money_csv(r["worked"]), _money_csv(r["billed"]), _money_csv(r["collected"]),
                        _pct_csv(r["billing_pct"]), _pct_csv(r["collection_pct"]), _money_csv(r["writedown"]), ""])
        for r in matter_rows:
            m = r["matter"]
            out.append(["matter", m.number if m else "", m.name if m else "(no matter)", _hours_csv(r["minutes"]),
                        _money_csv(r["worked"]), _money_csv(r["billed"]), _money_csv(r["collected"]),
                        _pct_csv(r["billing_pct"]), _pct_csv(r["collection_pct"]), _money_csv(r["writedown"]),
                        "" if r["invoiced"] else "not yet invoiced"])
        out.append(["total", f"{d_from.isoformat()} to {d_to.isoformat()}", "", _hours_csv(totals["minutes"]),
                    _money_csv(totals["worked"]), _money_csv(totals["billed"]), _money_csv(totals["collected"]),
                    _pct_csv(totals["billing_pct"]), _pct_csv(totals["collection_pct"]),
                    _money_csv(totals["writedown"]), ""])
        return _csv("realization.csv", ["Group", "Key", "Name", "Hours", "Worked", "Billed", "Collected",
                                        "Billing realization %", "Collection realization %", "Write-downs", "Flag"],
                    out)
    return render_template("reports/realization.html", user_rows=user_rows, matter_rows=matter_rows, totals=totals,
                           d_from=d_from, d_to=d_to)


# ---------------------------------------------------------------- profitability
MATTER_STATUSES = ("pending", "open", "closed")


def profitability_data(d_from, d_to, status=""):
    """Per matter: collected operating revenue in the range, cost = time at each user's cost rate plus
    non-billable expenses, margin and margin %. A matter is flagged when any user who logged time on it has no
    cost rate, because its cost is understated rather than zero."""
    payments = Payment.query.filter(Payment.account == "operating", Payment.received_on >= d_from,
                                    Payment.received_on <= d_to).all()
    entries = TimeEntry.query.filter(TimeEntry.date >= d_from, TimeEntry.date <= d_to).all()
    expenses = Expense.query.filter(Expense.billable == False, Expense.date >= d_from,  # noqa: E712
                                    Expense.date <= d_to).all()
    users = {u.id: u for u in User.query.all()}

    def blank():
        return {"matter": None, "revenue": 0, "payments": 0, "minutes": 0, "time_cost": 0, "expense_cost": 0,
                "cost": 0, "missing_rate_users": [], "users": set()}

    by_matter = {}
    for p in payments:
        matter = p.matter or (p.invoice.matter if p.invoice else None)
        r = by_matter.setdefault(matter.id if matter else 0, blank())
        r["matter"] = matter
        r["revenue"] += p.amount_cents or 0
        r["payments"] += 1
    for e in entries:
        r = by_matter.setdefault(e.matter_id, blank())
        r["matter"] = e.matter
        r["minutes"] += e.minutes or 0
        u = users.get(e.user_id)
        rate = (u.cost_rate_cents or 0) if u else 0
        r["time_cost"] += int(round((e.minutes or 0) * rate / 60.0))
        r["users"].add(e.user_id)
        if not rate:
            name = u.name if u else f"user #{e.user_id}"
            if name not in r["missing_rate_users"]:
                r["missing_rate_users"].append(name)
    for x in expenses:
        r = by_matter.setdefault(x.matter_id, blank())
        r["matter"] = x.matter
        r["expense_cost"] += x.amount_cents or 0
    rows = []
    for r in by_matter.values():
        if status and (not r["matter"] or r["matter"].status != status):
            continue
        r["cost"] = r["time_cost"] + r["expense_cost"]
        r["margin"] = r["revenue"] - r["cost"]
        r["margin_pct"] = _pct(r["margin"], r["revenue"])
        r["cost_rate_missing"] = bool(r["missing_rate_users"])
        r["users"] = sorted(r["users"])
        rows.append(r)
    rows.sort(key=lambda r: -r["margin"])
    totals = {k: sum(r[k] for r in rows) for k in ("revenue", "payments", "minutes", "time_cost", "expense_cost",
                                                    "cost", "margin")}
    totals["margin_pct"] = _pct(totals["margin"], totals["revenue"])
    totals["flagged"] = len([r for r in rows if r["cost_rate_missing"]])
    return rows, totals


@bp.route("/profitability")
@login_required
def profitability():
    d_from, d_to = _range()
    status = request.args.get("status", "")
    if status not in MATTER_STATUSES:
        status = ""
    rows, totals = profitability_data(d_from, d_to, status)
    if _wants_csv():
        out = []
        for r in rows:
            m = r["matter"]
            out.append([m.number if m else "", m.name if m else "(no matter)", m.client.display_name if m else "",
                        m.status if m else "", _money_csv(r["revenue"]), _hours_csv(r["minutes"]),
                        _money_csv(r["time_cost"]), _money_csv(r["expense_cost"]), _money_csv(r["cost"]),
                        _money_csv(r["margin"]), _pct_csv(r["margin_pct"]),
                        "cost rate not set: " + ", ".join(r["missing_rate_users"]) if r["cost_rate_missing"] else ""])
        out.append(["TOTAL", f"{d_from.isoformat()} to {d_to.isoformat()}", "", status or "all",
                    _money_csv(totals["revenue"]), _hours_csv(totals["minutes"]), _money_csv(totals["time_cost"]),
                    _money_csv(totals["expense_cost"]), _money_csv(totals["cost"]), _money_csv(totals["margin"]),
                    _pct_csv(totals["margin_pct"]), f"{totals['flagged']} flagged" if totals["flagged"] else ""])
        return _csv("profitability.csv", ["Matter", "Name", "Client", "Status", "Revenue", "Hours", "Time cost",
                                          "Non-billable expenses", "Total cost", "Margin", "Margin %", "Flag"], out)
    return render_template("reports/profitability.html", rows=rows, totals=totals, d_from=d_from, d_to=d_to,
                           status=status, statuses=MATTER_STATUSES)


# ---------------------------------------------------------------- compensation (Agent P, data in money.py)
@bp.route("/compensation")
@login_required
def compensation():
    from .money import compensation_data
    d_from, d_to = _range()
    matter_rows, user_rows, totals = compensation_data(d_from, d_to)
    if _wants_csv():
        out = []
        for r in matter_rows:
            m = r["matter"]
            for user, pct, cents in r["working"]:
                out.append([m.number or "", m.name, m.client.display_name if m.client else "", "working",
                            user.name if user else "", f"{pct:g}", _money_csv(cents), _money_csv(r["fee"]),
                            _money_csv(r["gross"]), "no working split, responsible attorney used" if r["flagged"] else ""])
            for user, pct, cents in r["originating"]:
                out.append([m.number or "", m.name, m.client.display_name if m.client else "", "originating",
                            user.name if user else "", f"{pct:g}", _money_csv(cents), _money_csv(r["fee"]),
                            _money_csv(r["gross"]), "default originator credit" if r["defaulted"] else ""])
            for user, pct, cents in r["referral"]:
                out.append([m.number or "", m.name, m.client.display_name if m.client else "", "referral",
                            user.name if user else "", f"{pct:g}", _money_csv(cents), _money_csv(r["fee"]),
                            _money_csv(r["gross"]), ""])
        for r in user_rows:
            out.append(["TOTAL", r["user"].name if r["user"] else "(nobody)", f"{r['matter_count']} matters", "working",
                        "", "", _money_csv(r["working"]), "", "", ""])
            out.append(["TOTAL", r["user"].name if r["user"] else "(nobody)", "", "originating", "", "",
                        _money_csv(r["originating"]), "", "", ""])
            out.append(["TOTAL", r["user"].name if r["user"] else "(nobody)", "", "referral", "", "",
                        _money_csv(r["referral"]), "", "", ""])
        out.append(["ALL", f"{d_from.isoformat()} to {d_to.isoformat()}", "", "working", "", "",
                    _money_csv(totals["working"]), _money_csv(totals["fee"]), _money_csv(totals["gross"]),
                    f"{totals['flagged']} flagged" if totals["flagged"] else ""])
        return _csv("compensation.csv", ["Matter", "Name", "Client", "Role", "Person", "Percent", "Allocated",
                                         "Fee collected", "Payment total", "Flag"], out)
    return render_template("reports/compensation.html", matter_rows=matter_rows, user_rows=user_rows, totals=totals,
                           d_from=d_from, d_to=d_to)
