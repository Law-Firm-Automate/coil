"""Dashboard: a set of cards, each with a key, that every user can pick and order under /dashboard/customize.
The selection lives in User.dashboard_json (a JSON list of keys). Empty means the default set, which matches the
original fixed dashboard."""
import json
from datetime import date, timedelta
from collections import OrderedDict
from flask import Blueprint, render_template, request, redirect, url_for, flash
from sqlalchemy import func
from ..extensions import db
from ..models import (Matter, Invoice, Task, TimeEntry, IntakeLead, Engagement, TrustTransaction, Timer,
                      DocumentSignature, Message)
from ..helpers import login_required, current_user

bp = Blueprint("dashboard", __name__)

# key -> (label, kind, description). kind: "stat" (one column), "list" (two columns), "wide" (full row).
CARDS = OrderedDict([
    ("open_matters", ("Open matters", "stat", "Count of matters with status open.")),
    ("ar", ("Outstanding A/R", "stat", "Balance on sent, viewed and partially paid invoices, with the overdue count.")),
    ("wip", ("Unbilled time", "stat", "Billable time not yet on an invoice.")),
    ("trust", ("Trust balance", "stat", "Book balance of the trust account.")),
    ("my_hours_week", ("My hours this week", "stat", "Time you logged in the last 7 days.")),
    ("pending_approvals", ("Invoices awaiting approval", "list", "Invoices submitted for approval before sending.")),
    ("tasks", ("Tasks", "list", "Open tasks, soonest due first.")),
    ("deadlines", ("Limitations periods", "list", "Open matters with a statute of limitations within 90 days.")),
    ("leads", ("New intake leads", "list", "Intake form submissions not yet contacted.")),
    ("engagements", ("Engagement letters out", "list", "Engagement letters sent and not yet signed.")),
    ("overdue", ("Overdue invoices", "list", "Unpaid invoices past their due date.")),
    ("evergreen", ("Evergreen retainers", "list", "Matters whose trust balance is below the minimum you set.")),
    ("unsigned_documents", ("Documents awaiting signature", "list", "Signature requests that are sent or viewed but not signed.")),
    ("portal_messages", ("Unread portal messages", "list", "Secure messages from clients nobody has opened yet.")),
    ("recent_matters", ("Recent matters", "wide", "The eight newest matters with unbilled and trust figures.")),
])

# Same content as the original fixed dashboard, in the order it appeared.
DEFAULT_CARDS = ["open_matters", "ar", "wip", "trust", "tasks", "deadlines", "leads", "engagements", "overdue",
                 "recent_matters", "my_hours_week"]

OPEN_INVOICE = ("sent", "viewed", "partial")


def parse_cards(raw):
    """User.dashboard_json -> ordered list of known keys. Anything unusable falls back to the defaults."""
    try:
        keys = json.loads(raw or "[]")
    except Exception:
        keys = []
    if not isinstance(keys, list):
        keys = []
    seen, out = set(), []
    for k in keys:
        if isinstance(k, str) and k in CARDS and k not in seen:
            seen.add(k)
            out.append(k)
    return out or list(DEFAULT_CARDS)


def user_cards(u):
    return parse_cards(u.dashboard_json if u else "")


# ---- loaders: one per card so only the cards on screen are queried ----
def _overdue(today):
    return Invoice.query.filter(Invoice.status.in_(OPEN_INVOICE), Invoice.due_on < today).order_by(
        Invoice.due_on).all()


def _evergreen():
    """[(matter, balance, shortfall)] from the trust module. Agent A adds evergreen_shortfalls there; until it
    exists the card shows "no data" instead of failing."""
    try:
        from .trust import evergreen_shortfalls
    except ImportError:
        return None
    try:
        return list(evergreen_shortfalls())
    except Exception:  # a bug in the helper should not take the whole dashboard down
        return None


def load_card_data(keys, u, today):
    ctx = {}
    week_ago = today - timedelta(days=7)
    for k in keys:
        if k == "open_matters":
            ctx["open_matters"] = Matter.query.filter_by(status="open").count()
        elif k == "ar":
            ctx["ar"] = int(db.session.query(func.coalesce(func.sum(Invoice.total_cents - Invoice.paid_cents), 0))
                            .filter(Invoice.status.in_(OPEN_INVOICE)).scalar() or 0)
            ctx["ar_overdue_count"] = Invoice.query.filter(Invoice.status.in_(OPEN_INVOICE),
                                                           Invoice.due_on < today).count()
        elif k == "wip":
            ctx["wip"] = int(db.session.query(func.coalesce(func.sum(TimeEntry.minutes * TimeEntry.rate_cents / 60), 0))
                             .filter(TimeEntry.billable == True, TimeEntry.invoice_id == None).scalar() or 0)  # noqa
        elif k == "trust":
            ctx["trust_total"] = int(db.session.query(func.coalesce(func.sum(TrustTransaction.amount_cents), 0))
                                     .scalar() or 0)
        elif k == "my_hours_week":
            ctx["week_minutes"] = int(db.session.query(func.coalesce(func.sum(TimeEntry.minutes), 0)).filter(
                TimeEntry.date >= week_ago, TimeEntry.user_id == u.id).scalar() or 0)
        elif k == "pending_approvals":
            ctx["pending_approvals"] = Invoice.query.filter_by(approval_status="pending").order_by(
                Invoice.created_at.desc()).limit(8).all()
            ctx["pending_approvals_count"] = Invoice.query.filter_by(approval_status="pending").count()
        elif k == "tasks":
            ctx["tasks"] = Task.query.filter(Task.done == False).order_by(  # noqa: E712
                Task.due_on.asc().nulls_last()).limit(12).all()
        elif k == "deadlines":
            ctx["deadlines"] = Matter.query.filter(Matter.status != "closed", Matter.sol_date != None,  # noqa: E711
                                                   Matter.sol_date <= today + timedelta(days=90)).order_by(
                Matter.sol_date).all()
        elif k == "leads":
            ctx["leads"] = IntakeLead.query.filter_by(status="new").order_by(IntakeLead.created_at.desc()).limit(8).all()
        elif k == "engagements":
            ctx["engagements"] = Engagement.query.filter(Engagement.status.in_(["sent", "viewed"])).order_by(
                Engagement.sent_at.desc()).limit(8).all()
        elif k == "overdue":
            ctx["overdue"] = _overdue(today)
        elif k == "evergreen":
            ctx["evergreen"] = _evergreen()
        elif k == "unsigned_documents":
            ctx["unsigned_documents"] = DocumentSignature.query.filter(
                DocumentSignature.status.in_(["sent", "viewed"])).order_by(DocumentSignature.sent_at.desc()).limit(8).all()
        elif k == "portal_messages":
            ctx["portal_messages"] = Message.query.filter(Message.channel == "portal", Message.direction == "in",
                                                          Message.read_at == None).order_by(  # noqa: E711
                Message.created_at.desc()).limit(8).all()
            ctx["portal_messages_count"] = Message.query.filter(Message.channel == "portal", Message.direction == "in",
                                                                Message.read_at == None).count()  # noqa: E711
        elif k == "recent_matters":
            ctx["recent_matters"] = Matter.query.order_by(Matter.created_at.desc()).limit(8).all()
    return ctx


@bp.route("/")
@login_required
def index():
    u = current_user()
    today = date.today()
    keys = user_cards(u)
    ctx = load_card_data(keys, u, today)
    timer = Timer.query.filter_by(user_id=u.id).first()
    return render_template("dashboard.html", cards=keys, card_defs=CARDS, timer=timer, today=today,
                           customized=bool(u.dashboard_json), **ctx)


@bp.route("/dashboard/customize", methods=["GET", "POST"])
@login_required
def customize():
    u = current_user()
    if request.method == "POST":
        if request.form.get("reset"):
            u.dashboard_json = ""
            db.session.commit()
            flash("Dashboard reset to the default cards.", "ok")
            return redirect(url_for("dashboard.index"))
        picked = []
        for i, key in enumerate(CARDS):
            if request.form.get(f"card_{key}"):
                raw = (request.form.get(f"order_{key}") or "").strip()
                try:
                    pos = int(raw)
                except ValueError:
                    pos = 1000
                picked.append((pos, i, key))
        picked.sort()
        keys = [k for _, _, k in picked]
        if not keys:
            flash("Pick at least one card.", "error")
            return redirect(url_for("dashboard.customize"))
        u.dashboard_json = json.dumps(keys)
        db.session.commit()
        flash(f"Dashboard saved with {len(keys)} card{'' if len(keys) == 1 else 's'}.", "ok")
        return redirect(url_for("dashboard.index"))
    current = user_cards(u)
    order = {k: i + 1 for i, k in enumerate(current)}
    nxt = len(current) + 1
    rows = []
    for key, (label, kind, desc) in CARDS.items():
        on = key in order
        rows.append({"key": key, "label": label, "kind": kind, "desc": desc, "on": on,
                     "order": order.get(key, nxt + list(CARDS).index(key))})
    rows.sort(key=lambda r: (0 if r["on"] else 1, r["order"]))
    return render_template("dashboard_customize.html", rows=rows, customized=bool(u.dashboard_json),
                           default_keys=DEFAULT_CARDS)
