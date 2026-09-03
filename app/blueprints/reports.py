"""Reports: A/R aging, WIP, revenue, trust balances, productivity. Each one has a CSV download (?format=csv)."""
import csv
import io
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from flask import Blueprint, render_template, request, Response
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
