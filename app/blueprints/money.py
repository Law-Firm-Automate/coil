"""Money: fee splits (CosmoLex lane), cards on file, charge-on-invoice and payment plans (Gravity Legal lane).

Staff routes live under /money/... The client-facing card and installment pages must be CSRF-exempt, so they sit
under the existing /pay/ prefix (/pay/card/<token>, /pay/plan/<id>/<token>). The blueprint therefore carries no
url_prefix and every route spells its full path.

Every Stripe call goes through app/blueprints/_stripe.py; tests monkeypatch those functions. Money is integer
cents throughout and every Stripe amount is the integer cents.
"""
import math
from calendar import monthrange
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, abort
from markupsafe import escape
from ..extensions import db
from ..models import (Matter, MatterFeeSplit, User, Contact, Invoice, InvoiceEvent, Payment, PaymentPlan,
                      PortalToken, AuditLog, Firm, audit, now)
from ..helpers import (login_required, portal_required, current_user, portal_contact, parse_money, parse_date,
                       cents_to_str)
from ..services.mail import send_email
from . import _stripe

bp = Blueprint("money", __name__)

SPLIT_ROLES = ("originating", "working", "referral")
FREQUENCIES = ("weekly", "biweekly", "monthly")
FREQUENCY_LABELS = {"weekly": "every week", "biweekly": "every two weeks", "monthly": "every month"}
PLAN_STATUSES = ("active", "paused", "failed", "completed", "cancelled")
OPEN_INVOICE = ("sent", "viewed", "partial")
CARD_TOKEN_DAYS = 7
NOT_CONFIGURED = _stripe.NOT_CONFIGURED_MSG


def _base():
    return current_app.config["BASE_URL"]


def _dollars(cents):
    return f"{int(cents or 0) / 100:.2f}"


def _pct(bps):
    return f"{(bps or 0) / 100:g}"


# ---------------------------------------------------------------------------
# Fee splits
# ---------------------------------------------------------------------------
def effective_splits(matter):
    """Splits to use for a matter. When none are stored, the originating credit defaults to 100% for
    Matter.originating_user_id (computed, never written). Returns (splits, defaulted) where each split is a dict
    {user, role, percent}."""
    if matter.fee_splits:
        return [{"user": s.user, "role": s.role, "percent": float(s.percent or 0)} for s in matter.fee_splits], False
    if matter.originating_user_id and matter.originator:
        return [{"user": matter.originator, "role": "originating", "percent": 100.0}], True
    return [], True


def working_allocation(matter):
    """[(user, percent)] to allocate collected fees by. Falls back to 100% for the responsible attorney when the
    matter has no working split, with flagged=True so the report can say so. Returns (rows, flagged)."""
    splits, _ = effective_splits(matter)
    working = [(s["user"], s["percent"]) for s in splits if s["role"] == "working" and s["user"]]
    if working:
        return working, False
    if matter.responsible:
        return [(matter.responsible, 100.0)], True
    return [], True


def allocate_cents(total, rows):
    """Split `total` cents across [(user, percent)] rows. Rounds half up per row and puts any remainder on the
    last row so the pieces always sum to the total exactly."""
    if not rows or total == 0:
        return []
    out = []
    running = 0
    for i, (user, pct) in enumerate(rows):
        if i == len(rows) - 1:
            cents = total - running
        else:
            cents = int(math.floor(total * pct / 100.0 + 0.5))
            running += cents
        out.append((user, pct, cents))
    return out


def validate_splits(rows):
    """rows: [{user_id, role, percent}] already parsed. Returns an error string or None."""
    working = sum(r["percent"] for r in rows if r["role"] == "working")
    if any(r["role"] == "working" for r in rows) and abs(working - 100.0) > 0.005:
        return f"Working percentages must add up to 100 (they add up to {working:g})."
    for role in ("originating", "referral"):
        tot = sum(r["percent"] for r in rows if r["role"] == role)
        if tot > 100.005:
            return f"{role.capitalize()} percentages cannot exceed 100 (they add up to {tot:g})."
    for r in rows:
        if r["percent"] <= 0 or r["percent"] > 100:
            return "Each percent must be between 0 and 100."
        if r["role"] not in SPLIT_ROLES:
            return "Role must be originating, working or referral."
    seen = set()
    for r in rows:
        key = (r["user_id"], r["role"])
        if key in seen:
            return "The same person is listed twice with the same role. Combine the rows."
        seen.add(key)
    return None


def _parse_split_rows(form):
    rows = []
    idx = 0
    while f"user_id_{idx}" in form or idx < 3:
        uid = (form.get(f"user_id_{idx}") or "").strip()
        role = (form.get(f"role_{idx}") or "working").strip().lower()
        pct_s = (form.get(f"percent_{idx}") or "").strip().replace("%", "")
        idx += 1
        if not uid or not pct_s:
            continue
        try:
            pct = float(pct_s)
        except ValueError:
            return None, f"'{pct_s}' is not a number."
        if not uid.isdigit():
            return None, "Pick a person for each row."
        rows.append({"user_id": int(uid), "role": role, "percent": pct})
        if idx > 40:
            break
    return rows, None


@bp.route("/money/splits/<int:matter_id>", methods=["GET", "POST"])
@login_required
def splits(matter_id):
    m = db.session.get(Matter, matter_id) or abort(404)
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    if request.method == "POST":
        rows, err = _parse_split_rows(request.form)
        if err is None:
            err = validate_splits(rows)
        if err is None:
            ids = {u.id for u in users}
            if any(r["user_id"] not in ids for r in rows):
                err = "One of the people picked is not an active user."
        if err:
            flash(err, "error")
            return render_template("money/splits.html", m=m, users=users, rows=rows, defaulted=False), 400
        for s in list(m.fee_splits):
            db.session.delete(s)
        db.session.flush()
        for r in rows:
            db.session.add(MatterFeeSplit(matter_id=m.id, user_id=r["user_id"], role=r["role"], percent=r["percent"]))
        detail = "; ".join(f"{r['role']} {r['percent']:g}% user {r['user_id']}" for r in rows) or "cleared"
        audit("fee_splits_set", "matter", m.id, detail[:1000], current_user().id)
        db.session.commit()
        flash("Fee splits saved." if rows else "Fee splits cleared. The originator gets 100% by default.", "ok")
        return redirect(url_for("matters.detail", id=m.id))
    eff, defaulted = effective_splits(m)
    rows = [{"user_id": s["user"].id, "role": s["role"], "percent": s["percent"]} for s in eff]
    return render_template("money/splits.html", m=m, users=users, rows=rows, defaulted=defaulted)


# ---------------------------------------------------------------------------
# Compensation data (rendered by reports.py at /reports/compensation)
# ---------------------------------------------------------------------------
def fee_share_of_payment(payment):
    """The part of a Payment that paid for fees: prorated to the invoice's non-expense lines. Surcharges are not
    in Payment.amount_cents to begin with. Payments with no invoice (trust deposits) return 0."""
    inv = payment.invoice
    if not inv or not inv.total_cents:
        return 0
    fee_lines = sum((ln.amount_cents or 0) for ln in inv.lines if ln.kind != "expense")
    if fee_lines <= 0:
        return 0
    if fee_lines >= inv.total_cents:
        return payment.amount_cents or 0
    return int(math.floor((payment.amount_cents or 0) * fee_lines / inv.total_cents + 0.5))


def compensation_data(d_from, d_to):
    """Collected fees per matter in the range, allocated to users by working split, with originating and referral
    credit shown separately. Returns (matter_rows, user_rows, totals)."""
    payments = (Payment.query.filter(Payment.account == "operating", Payment.received_on >= d_from,
                                     Payment.received_on <= d_to, Payment.invoice_id != None)  # noqa: E711
                .order_by(Payment.received_on, Payment.id).all())
    by_matter = {}
    for p in payments:
        inv = p.invoice
        matter = p.matter or (inv.matter if inv else None)
        if not matter:
            continue
        fee = fee_share_of_payment(p)
        row = by_matter.setdefault(matter.id, {"matter": matter, "fee": 0, "payments": 0, "gross": 0})
        row["fee"] += fee
        row["gross"] += p.amount_cents or 0
        row["payments"] += 1
    users = {}

    def urow(user):
        key = user.id if user else 0
        return users.setdefault(key, {"user": user, "working": 0, "originating": 0, "referral": 0,
                                      "matters": set(), "flagged": 0})

    matter_rows = []
    for row in by_matter.values():
        matter = row["matter"]
        alloc_rows, flagged = working_allocation(matter)
        row["working"] = allocate_cents(row["fee"], alloc_rows)
        row["flagged"] = flagged
        splits, defaulted = effective_splits(matter)
        row["originating"] = [(s["user"], s["percent"], int(math.floor(row["fee"] * s["percent"] / 100.0 + 0.5)))
                              for s in splits if s["role"] == "originating" and s["user"]]
        row["referral"] = [(s["user"], s["percent"], int(math.floor(row["fee"] * s["percent"] / 100.0 + 0.5)))
                           for s in splits if s["role"] == "referral" and s["user"]]
        row["defaulted"] = defaulted
        for user, pct, cents in row["working"]:
            r = urow(user)
            r["working"] += cents
            r["matters"].add(matter.id)
            if flagged:
                r["flagged"] += 1
        for user, pct, cents in row["originating"]:
            r = urow(user)
            r["originating"] += cents
            r["matters"].add(matter.id)
        for user, pct, cents in row["referral"]:
            r = urow(user)
            r["referral"] += cents
            r["matters"].add(matter.id)
        matter_rows.append(row)
    matter_rows.sort(key=lambda r: (-r["fee"], r["matter"].number or ""))
    user_rows = sorted(users.values(), key=lambda r: (-r["working"], -r["originating"], r["user"].name if r["user"] else "zzz"))
    for r in user_rows:
        r["matter_count"] = len(r["matters"])
    totals = {"fee": sum(r["fee"] for r in matter_rows), "gross": sum(r["gross"] for r in matter_rows),
              "payments": sum(r["payments"] for r in matter_rows),
              "working": sum(r["working"] for r in user_rows),
              "originating": sum(r["originating"] for r in user_rows),
              "referral": sum(r["referral"] for r in user_rows),
              "flagged": sum(1 for r in matter_rows if r["flagged"])}
    return matter_rows, user_rows, totals


# ---------------------------------------------------------------------------
# Cards on file
# ---------------------------------------------------------------------------
def has_card(contact):
    return bool(contact and contact.stripe_customer_id and contact.stripe_payment_method_id)


def card_label(contact):
    if not has_card(contact):
        return ""
    brand = (contact.card_brand or "card").capitalize()
    return f"{brand} ending {contact.card_last4}" if contact.card_last4 else brand


def surcharge_cents(amount_cents, firm=None):
    firm = firm or Firm.get()
    if firm.surcharge_enabled and (firm.surcharge_bps or 0) > 0:
        return int(math.floor(int(amount_cents) * firm.surcharge_bps / 10000 + 0.5))
    return 0


def new_card_token(contact):
    tok = PortalToken(contact_id=contact.id, expires_at=datetime.utcnow() + timedelta(days=CARD_TOKEN_DAYS))
    db.session.add(tok)
    db.session.flush()
    return tok


def _card_token(token):
    tok = PortalToken.query.filter_by(token=token).first() or abort(404)
    if tok.used_at or tok.expires_at < datetime.utcnow():
        return tok, False
    return tok, True


def card_link(tok):
    return f"{_base()}/pay/card/{tok.token}"


def send_card_request(contact, tok, user=None):
    firm = Firm.get()
    url = card_link(tok)
    who = escape(contact.first_name or contact.display_name)
    html = (f"<div style='font-family:Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1c2430'>"
            f"<p>Hello {who},</p>"
            f"<p>{escape(firm.name)} would like to keep a card on file so invoices can be paid without you having to "
            f"log in each time. The link below explains what you are agreeing to and takes you to a secure Stripe "
            f"page to enter the card. We never see the card number.</p>"
            f"<p><a href='{url}' style='background:#1f5f8b;color:#fff;padding:10px 18px;border-radius:6px;"
            f"text-decoration:none;display:inline-block'>Save a card</a></p>"
            f"<p style='font-size:12px;color:#666'>Link: {url}<br>It works for {CARD_TOKEN_DAYS} days.</p>"
            f"<p style='font-size:13px;color:#666'>{escape(firm.name or '')}<br>{escape(firm.phone or '')}</p></div>")
    send_email(contact.email, f"{firm.name}: save a card on file", html,
               text=f"Save a card on file for {firm.name}: {url}", reply_to=firm.email or None)


@bp.route("/money/cards/<int:contact_id>/request", methods=["POST"])
@login_required
def card_request(contact_id):
    c = db.session.get(Contact, contact_id) or abort(404)
    back = redirect(f"/contacts/{c.id}")
    if not _stripe.configured():
        flash(NOT_CONFIGURED, "error")
        return back
    if not (c.email or "").strip():
        flash("This contact has no email address. Add one first.", "error")
        return back
    tok = new_card_token(c)
    send_card_request(c, tok, current_user())
    audit("card_requested", "contact", c.id, f"link emailed to {c.email}", current_user().id)
    db.session.commit()
    flash(f"Emailed {c.email} a link to save a card. It works for {CARD_TOKEN_DAYS} days.", "ok")
    return back


@bp.route("/money/cards/<int:contact_id>/remove", methods=["POST"])
@login_required
def card_remove(contact_id):
    c = db.session.get(Contact, contact_id) or abort(404)
    back = redirect(f"/contacts/{c.id}")
    if not has_card(c):
        flash("No card on file.", "error")
        return back
    label = card_label(c)
    pm = c.stripe_payment_method_id
    if _stripe.configured():
        try:
            _stripe.detach_payment_method(pm)
        except Exception as e:  # the local record is cleared either way
            current_app.logger.warning("could not detach %s at Stripe: %s", pm, e)
    c.stripe_payment_method_id = ""
    c.card_brand = ""
    c.card_last4 = ""
    c.card_authorised_on = None
    for plan in PaymentPlan.query.filter_by(contact_id=c.id, status="active", auto_charge=True).all():
        plan.auto_charge = False
        audit("plan_autocharge_off", "payment_plan", plan.id, "card removed; reminders by email from now on",
              current_user().id)
    audit("card_removed", "contact", c.id, f"{label} ({pm}) removed", current_user().id)
    db.session.commit()
    flash(f"Removed {label}. Any automatic payment plans now send email reminders instead.", "ok")
    return back


@bp.route("/money/portal/card", methods=["POST"])
@portal_required
def portal_card():
    """Portal home button: mint a 7-day token for the signed-in client and send them into the same flow."""
    c = portal_contact()
    tok = new_card_token(c)
    audit("card_link_portal", "contact", c.id, "client started the save-a-card flow from the portal")
    db.session.commit()
    return redirect(url_for("money.card_page", token=tok.token))


@bp.route("/pay/card/<token>", methods=["GET", "POST"])
def card_page(token):
    tok, ok = _card_token(token)
    c = tok.contact
    firm = Firm.get()
    if not ok:
        return render_template("money/card_expired.html", c=c, f=firm), 410
    if not _stripe.configured():
        return render_template("money/card_unconfigured.html", c=c, f=firm)
    if request.method == "GET":
        return render_template("money/card_consent.html", c=c, f=firm, tok=tok, pct=_pct(firm.surcharge_bps))
    if not request.form.get("agree"):
        flash("Please tick the box to confirm you agree before continuing.", "error")
        return render_template("money/card_consent.html", c=c, f=firm, tok=tok, pct=_pct(firm.surcharge_bps)), 400
    try:
        if not c.stripe_customer_id:
            cust = _stripe.create_customer(email=c.email, name=c.display_name, metadata={"contact_id": str(c.id)})
            c.stripe_customer_id = cust["id"] if hasattr(cust, "get") else getattr(cust, "id")
            db.session.commit()
        sess = _stripe.create_setup_session(
            c.stripe_customer_id,
            success_url=f"{_base()}/pay/card/{token}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{_base()}/pay/card/{token}/cancel",
            metadata={"kind": "card_setup", "contact_id": str(c.id), "token": token})
    except _stripe.StripeNotConfigured:
        return render_template("money/card_unconfigured.html", c=c, f=firm)
    except Exception as e:
        current_app.logger.exception("stripe setup session failed for contact %s", c.id)
        flash(f"We could not start the secure card page: {e}", "error")
        return render_template("money/card_consent.html", c=c, f=firm, tok=tok, pct=_pct(firm.surcharge_bps))
    audit("card_setup_started", "contact", c.id, f"session {sess['id']}")
    db.session.commit()
    return redirect(sess["url"], code=303)


@bp.route("/pay/card/<token>/success")
def card_success(token):
    tok = PortalToken.query.filter_by(token=token).first() or abort(404)
    c = tok.contact
    sid = request.args.get("session_id", "")
    saved = False
    if sid and _stripe.configured():
        try:
            sess = _stripe.retrieve_setup_session(sid)
            saved = store_card_from_session(sess) is not None
        except Exception:
            current_app.logger.exception("could not verify setup session %s", sid)
    db.session.refresh(c)
    return render_template("money/card_saved.html", c=c, f=Firm.get(), saved=saved or has_card(c),
                           label=card_label(c))


@bp.route("/pay/card/<token>/cancel")
def card_cancel(token):
    tok = PortalToken.query.filter_by(token=token).first() or abort(404)
    return render_template("money/card_cancel.html", c=tok.contact, f=Firm.get(), token=token)


def _obj_id(v):
    if not v:
        return ""
    if isinstance(v, str):
        return v
    return v.get("id", "") if hasattr(v, "get") else str(getattr(v, "id", ""))


def store_card_from_session(sess):
    """Setup-mode Checkout Session completed: store the customer and payment method on the Contact named in the
    metadata. Idempotent (re-running just rewrites the same values). Returns the Contact, or None when the session
    is not a card setup we recognise. Commits."""
    if (sess.get("mode") or "setup") != "setup":
        return None
    meta = sess.get("metadata") or {}
    cid = meta.get("contact_id") if hasattr(meta, "get") else getattr(meta, "contact_id", None)
    c = db.session.get(Contact, int(cid)) if cid and str(cid).isdigit() else None
    if not c:
        current_app.logger.error("setup session %s has no usable contact_id (%r)", sess.get("id"), cid)
        return None
    si = sess.get("setup_intent")
    if isinstance(si, str):
        si = _stripe.retrieve_setup_intent(si)
    pm = si.get("payment_method") if si else None
    if isinstance(pm, str):
        pm = _stripe.retrieve_payment_method(pm)
    if not pm:
        current_app.logger.error("setup session %s has no payment method", sess.get("id"))
        return None
    card = pm.get("card") or {}
    customer = _obj_id(sess.get("customer")) or c.stripe_customer_id
    c.stripe_customer_id = customer
    c.stripe_payment_method_id = _obj_id(pm)
    c.card_brand = str(card.get("brand") or "")[:20]
    c.card_last4 = str(card.get("last4") or "")[:4]
    c.card_authorised_on = date.today()
    token = meta.get("token") if hasattr(meta, "get") else None
    if token:
        tok = PortalToken.query.filter_by(token=token).first()
        if tok and not tok.used_at:
            tok.used_at = now()
    audit("card_saved", "contact", c.id, f"{card_label(c)} via session {sess.get('id')}")
    db.session.commit()
    return c


# ---------------------------------------------------------------------------
# Charge card on file
# ---------------------------------------------------------------------------
def charge_card(inv, amount, user_id=None, note="Charged card on file", firm=None, plan=None):
    """Charge the invoice client's saved card for `amount` cents plus the firm surcharge, record the Payment and
    recalc the invoice. Returns (payment, error). On any failure nothing is written. Caller commits on success."""
    firm = firm or Firm.get()
    c = inv.client
    if not _stripe.configured():
        return None, NOT_CONFIGURED
    if not has_card(c):
        return None, f"{c.display_name} has no card on file."
    if inv.status not in OPEN_INVOICE:
        return None, f"Invoice {inv.number} is {inv.status}; only sent, viewed or partial invoices can be charged."
    amount = int(amount)
    if amount <= 0:
        return None, "Enter a positive amount."
    if amount > inv.balance_cents:
        return None, f"That is more than the balance of {cents_to_str(inv.balance_cents)}."
    sc = surcharge_cents(amount, firm)
    total = amount + sc
    desc = f"Invoice {inv.number}" + (f" (payment plan {plan.id})" if plan else "")
    try:
        pi = _stripe.charge_payment_method(
            c.stripe_customer_id, c.stripe_payment_method_id, total, description=desc,
            metadata={"kind": "invoice", "invoice_id": str(inv.id), "surcharge_cents": str(sc), "method": "card",
                      "plan_id": str(plan.id) if plan else ""})
    except _stripe.StripeNotConfigured as e:
        return None, str(e)
    except Exception as e:
        current_app.logger.warning("card charge failed for invoice %s: %s", inv.id, e)
        msg = getattr(e, "user_message", None) or str(e) or e.__class__.__name__
        return None, f"The card was not charged: {msg}"
    status = pi.get("status") if hasattr(pi, "get") else getattr(pi, "status", "")
    if status != "succeeded":
        return None, f"The card was not charged (Stripe status: {status or 'unknown'})."
    pi_id = _obj_id(pi)
    fee = None
    try:
        fee = _stripe.fee_cents_for_payment_intent(pi_id)
    except Exception:
        fee = None
    p = Payment(invoice_id=inv.id, matter_id=inv.matter_id, client_id=inv.client_id, amount_cents=amount,
                surcharge_cents=sc, stripe_fee_cents=fee or 0, method="card", account="operating",
                stripe_payment_intent=pi_id, received_on=date.today(), reference=pi_id, note=note[:300])
    inv.payments.append(p)
    db.session.flush()
    inv.recalc()
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="paid",
                                detail=f"{cents_to_str(amount)} charged to {card_label(c)}"
                                + (f" plus {cents_to_str(sc)} surcharge" if sc else "")))
    audit("card_charged", "invoice", inv.id, f"{cents_to_str(amount)} + {cents_to_str(sc)} surcharge, {pi_id}", user_id)
    return p, None


@bp.route("/money/charge/<int:invoice_id>", methods=["POST"])
@login_required
def charge(invoice_id):
    inv = db.session.get(Invoice, invoice_id) or abort(404)
    back = redirect(f"/invoices/{inv.id}")
    if not _stripe.configured():
        flash(NOT_CONFIGURED, "error")
        return back
    amount = parse_money(request.form.get("amount")) or inv.balance_cents
    p, err = charge_card(inv, amount, user_id=current_user().id)
    if err:
        db.session.rollback()
        flash(err, "error")
        return back
    db.session.commit()
    msg = f"Charged {cents_to_str(p.amount_cents)} to {card_label(inv.client)}"
    if p.surcharge_cents:
        msg += f" plus a {cents_to_str(p.surcharge_cents)} surcharge"
    flash(msg + ".", "ok")
    return back


# ---------------------------------------------------------------------------
# Payment plans
# ---------------------------------------------------------------------------
def advance_date(d, frequency):
    if frequency == "weekly":
        return d + timedelta(days=7)
    if frequency == "biweekly":
        return d + timedelta(days=14)
    y = d.year + (d.month // 12)
    m = d.month % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def plan_schedule(plan):
    """[(n, due_date, amount_cents, state)] for the whole plan. The last installment absorbs rounding so the
    amounts sum to the balance the plan was set up for. state: paid | next | upcoming."""
    inv = plan.invoice
    paid_n = plan.paid_installments or 0
    planned_total = plan.installment_cents * plan.installments
    # The plan's opening balance: what has gone through the plan so far plus what is still owed, never more
    # than the installments could add up to.
    opening = min(planned_total, plan.installment_cents * paid_n + (inv.balance_cents if inv else 0)) if inv else planned_total
    first = plan.next_charge_on or date.today()
    for _ in range(paid_n):
        first = _rewind_date(first, plan.frequency)
    rows = []
    d = first
    left = opening
    for n in range(1, plan.installments + 1):
        amt = max(0, min(plan.installment_cents, left) if n < plan.installments else left)
        state = "paid" if n <= paid_n else ("next" if n == paid_n + 1 else "upcoming")
        rows.append((n, d, amt, state))
        left -= amt
        d = advance_date(d, plan.frequency)
    return rows


def _rewind_date(d, frequency):
    if frequency == "weekly":
        return d - timedelta(days=7)
    if frequency == "biweekly":
        return d - timedelta(days=14)
    y = d.year - (1 if d.month == 1 else 0)
    m = 12 if d.month == 1 else d.month - 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def next_installment_cents(plan):
    inv = plan.invoice
    if not inv:
        return 0
    if (plan.paid_installments or 0) >= plan.installments - 1:
        return inv.balance_cents
    return min(plan.installment_cents, inv.balance_cents)


def plans_for_invoice(inv):
    return PaymentPlan.query.filter_by(invoice_id=inv.id).order_by(PaymentPlan.id.desc()).all()


def active_plan_for(inv):
    return PaymentPlan.query.filter(PaymentPlan.invoice_id == inv.id,
                                    PaymentPlan.status.in_(["active", "paused", "failed"])).first()


def plans_for_contact(contact, statuses=("active",)):
    return PaymentPlan.query.filter(PaymentPlan.contact_id == contact.id, PaymentPlan.status.in_(list(statuses))) \
        .order_by(PaymentPlan.next_charge_on).all()


def plan_payments(plan):
    return Payment.query.filter(Payment.invoice_id == plan.invoice_id,
                                Payment.note.like(f"Payment plan {plan.id}:%")).order_by(Payment.id).all()


def _complete_if_done(plan):
    inv = plan.invoice
    if (plan.paid_installments or 0) >= plan.installments or (inv and inv.balance_cents <= 0):
        plan.status = "completed"
        return True
    return False


@bp.route("/money/plans/new", methods=["POST"])
@login_required
def plan_new():
    inv_id = request.form.get("invoice_id", "")
    inv = db.session.get(Invoice, int(inv_id)) if inv_id.isdigit() else None
    if not inv:
        abort(404)
    back = redirect(f"/invoices/{inv.id}")
    if inv.status not in OPEN_INVOICE or inv.balance_cents <= 0:
        flash("A payment plan needs a sent invoice with a balance.", "error")
        return back
    if active_plan_for(inv):
        flash("This invoice already has a payment plan. Cancel it before setting up another.", "error")
        return back
    try:
        n = int(request.form.get("installments") or 0)
    except ValueError:
        n = 0
    if n < 2 or n > 60:
        flash("Installments must be between 2 and 60.", "error")
        return back
    freq = (request.form.get("frequency") or "monthly").strip().lower()
    if freq not in FREQUENCIES:
        flash("Frequency must be weekly, biweekly or monthly.", "error")
        return back
    first = parse_date(request.form.get("first_charge_on"), None)
    if not first:
        flash("Pick the date of the first charge.", "error")
        return back
    auto = bool(request.form.get("auto_charge"))
    if auto and not has_card(inv.client):
        flash("Automatic charging needs a card on file. Request one from the contact page first.", "error")
        return back
    if auto and not _stripe.configured():
        flash(NOT_CONFIGURED + " The plan can still send email reminders; untick automatic charging.", "error")
        return back
    balance = inv.balance_cents
    per = int(math.ceil(balance / n))
    plan = PaymentPlan(invoice_id=inv.id, contact_id=inv.client_id, installment_cents=per, installments=n,
                       paid_installments=0, frequency=freq, next_charge_on=first, auto_charge=auto, status="active",
                       created_by_id=current_user().id)
    db.session.add(plan)
    db.session.flush()
    audit("plan_created", "payment_plan", plan.id,
          f"{inv.number}: {n} x {cents_to_str(per)} {freq} from {first.isoformat()}, "
          f"{'auto-charge' if auto else 'email reminders'}", current_user().id)
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="plan",
                                detail=f"payment plan: {n} {freq} installments of {cents_to_str(per)}"))
    db.session.commit()
    flash(f"Payment plan set up: {n} {FREQUENCY_LABELS[freq].replace('every ', '')} installments of "
          f"{cents_to_str(per)} starting {first:%b %-d, %Y}.", "ok")
    return redirect(url_for("money.plan_detail", plan_id=plan.id))


@bp.route("/money/plans")
@login_required
def plans():
    status = (request.args.get("status") or "active").strip().lower()
    q = PaymentPlan.query
    if status != "all":
        if status not in PLAN_STATUSES:
            status = "active"
        q = q.filter(PaymentPlan.status == status)
    rows = q.order_by(PaymentPlan.next_charge_on.asc().nulls_last(), PaymentPlan.id.desc()).all()
    counts = {s: PaymentPlan.query.filter_by(status=s).count() for s in PLAN_STATUSES}
    return render_template("money/plans.html", plans=rows, status=status, counts=counts, today=date.today(),
                           next_cents=next_installment_cents, labels=FREQUENCY_LABELS)


@bp.route("/money/plans/<int:plan_id>")
@login_required
def plan_detail(plan_id):
    plan = db.session.get(PaymentPlan, plan_id) or abort(404)
    return render_template("money/plan_detail.html", plan=plan, schedule=plan_schedule(plan),
                           payments=plan_payments(plan), today=date.today(), has_card=has_card(plan.contact),
                           card=card_label(plan.contact), next_cents=next_installment_cents(plan),
                           surcharge=surcharge_cents(next_installment_cents(plan)), labels=FREQUENCY_LABELS,
                           stripe_ok=_stripe.configured(), dollars=_dollars)


def _plan_action(plan_id, allowed, new_status, action, msg):
    plan = db.session.get(PaymentPlan, plan_id) or abort(404)
    back = redirect(url_for("money.plan_detail", plan_id=plan.id))
    if plan.status not in allowed:
        flash(f"The plan is {plan.status}; it cannot be {action.replace('plan_', '')}d from there.", "error")
        return back
    plan.status = new_status
    if new_status == "active":
        plan.last_error = ""
        if plan.next_charge_on and plan.next_charge_on < date.today():
            plan.next_charge_on = date.today()
    audit(action, "payment_plan", plan.id, f"{plan.invoice.number if plan.invoice else ''}", current_user().id)
    db.session.commit()
    flash(msg, "ok")
    return back


@bp.route("/money/plans/<int:plan_id>/pause", methods=["POST"])
@login_required
def plan_pause(plan_id):
    return _plan_action(plan_id, ("active", "failed"), "paused", "plan_pause", "Plan paused. Nothing will be charged or sent until you resume it.")


@bp.route("/money/plans/<int:plan_id>/resume", methods=["POST"])
@login_required
def plan_resume(plan_id):
    return _plan_action(plan_id, ("paused", "failed"), "active", "plan_resume", "Plan resumed.")


@bp.route("/money/plans/<int:plan_id>/cancel", methods=["POST"])
@login_required
def plan_cancel(plan_id):
    return _plan_action(plan_id, ("active", "paused", "failed"), "cancelled", "plan_cancel", "Plan cancelled. The invoice balance stays due.")


@bp.route("/money/plans/<int:plan_id>/charge", methods=["POST"])
@login_required
def plan_charge_now(plan_id):
    plan = db.session.get(PaymentPlan, plan_id) or abort(404)
    back = redirect(url_for("money.plan_detail", plan_id=plan.id))
    if plan.status not in ("active", "paused", "failed"):
        flash(f"The plan is {plan.status}.", "error")
        return back
    if not _stripe.configured():
        flash(NOT_CONFIGURED, "error")
        return back
    if not has_card(plan.contact):
        flash("No card on file for this client. Request one from the contact page, or send the reminder instead.", "error")
        return back
    ok, err = charge_installment(plan, user_id=current_user().id, force=True)
    if not ok:
        db.session.rollback()
        flash(err, "error")
        return back
    db.session.commit()
    flash(f"Charged installment {plan.paid_installments} of {plan.installments}."
          + (" The plan is complete." if plan.status == "completed" else ""), "ok")
    return back


@bp.route("/money/plans/<int:plan_id>/remind", methods=["POST"])
@login_required
def plan_remind_now(plan_id):
    plan = db.session.get(PaymentPlan, plan_id) or abort(404)
    back = redirect(url_for("money.plan_detail", plan_id=plan.id))
    if plan.status not in ("active", "paused", "failed"):
        flash(f"The plan is {plan.status}.", "error")
        return back
    to = send_plan_reminder(plan)
    if not to:
        flash("The client has no email address.", "error")
        return back
    audit("plan_reminder_manual", "payment_plan", plan.id, f"to {to}", current_user().id)
    db.session.commit()
    flash(f"Reminder emailed to {to}.", "ok")
    return back


def charge_installment(plan, user_id=None, force=False, today=None):
    """Charge the next installment to the card on file. Returns (ok, error). Writes nothing on failure except,
    when called by the scheduler, the failure state itself (handled by the caller). Caller commits."""
    today = today or date.today()
    inv = plan.invoice
    if not inv:
        return False, "The plan's invoice is missing."
    if _complete_if_done(plan):
        return False, "The plan is already complete."
    amount = next_installment_cents(plan)
    k = (plan.paid_installments or 0) + 1
    p, err = charge_card(inv, amount, user_id=user_id, plan=plan,
                         note=f"Payment plan {plan.id}: installment {k} of {plan.installments}")
    if err:
        return False, err
    plan.paid_installments = k
    plan.next_charge_on = advance_date(plan.next_charge_on or today, plan.frequency)
    plan.last_error = ""
    if plan.status in ("paused", "failed") and force:
        plan.status = "active"
    _complete_if_done(plan)
    audit("plan_charged", "payment_plan", plan.id, today.isoformat(), user_id)
    return True, None


def installment_pay_link(plan):
    return f"{_base()}/pay/plan/{plan.id}/{plan.invoice.public_token}"


def send_plan_reminder(plan):
    """Email the client the next installment amount with the pay link. Returns the address, or '' when none."""
    inv = plan.invoice
    c = plan.contact
    to = (c.email or "").strip() if c else ""
    if not to or not inv:
        return ""
    firm = Firm.get()
    amount = next_installment_cents(plan)
    k = (plan.paid_installments or 0) + 1
    link = installment_pay_link(plan)
    view = f"{_base()}/p/{inv.public_token}"
    due = plan.next_charge_on
    html = (f"<div style='font-family:Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1c2430'>"
            f"<p>Hello {escape(c.first_name or c.display_name)},</p>"
            f"<p>Installment {k} of {plan.installments} on invoice {escape(inv.number or '')} is "
            f"<strong>{cents_to_str(amount)}</strong>{', due ' + due.strftime('%B %-d, %Y') if due else ''}. "
            f"You can pay it online here:</p>"
            f"<p><a href='{link}' style='background:#1f5f8b;color:#fff;padding:10px 18px;border-radius:6px;"
            f"text-decoration:none;display:inline-block'>Pay {cents_to_str(amount)}</a></p>"
            f"<p style='font-size:12px;color:#666'>Pay link: {link}<br>Full invoice: {view}</p>"
            f"<p style='font-size:13px;color:#666'>{escape(firm.name or '')}<br>{escape(firm.phone or '')}</p></div>")
    send_email(to, f"Payment of {cents_to_str(amount)} due on invoice {inv.number}", html,
               text=f"Installment {k} of {plan.installments} on invoice {inv.number} is {cents_to_str(amount)}. "
                    f"Pay: {link}", reply_to=firm.email or None)
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="reminder",
                                detail=f"payment plan installment {k} of {plan.installments}, to {to}"))
    return to


def _firm_failure_email(plan, err):
    firm = Firm.get()
    owner = User.query.filter_by(role="owner", is_active=True).order_by(User.id).first()
    to = (firm.email or (owner.email if owner else "") or "").strip()
    if not to:
        return
    inv = plan.invoice
    url = f"{_base()}/money/plans/{plan.id}"
    html = (f"<div style='font-family:Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;color:#1c2430'>"
            f"<p>The automatic charge for payment plan {plan.id} on invoice {escape(inv.number if inv else '')} "
            f"({escape(plan.contact.display_name if plan.contact else '')}) failed:</p>"
            f"<p><strong>{escape(err)}</strong></p>"
            f"<p>The plan is now marked failed and will not retry on its own. Open it to resume, charge again or "
            f"switch it to email reminders: <a href='{url}'>{url}</a></p></div>")
    send_email(to, f"Payment plan charge failed: invoice {inv.number if inv else plan.id}", html,
               text=f"Payment plan {plan.id} charge failed: {err}. {url}")


def _done_today(plan, action, today_iso):
    return AuditLog.query.filter_by(action=action, entity="payment_plan", entity_id=plan.id, detail=today_iso).first() is not None


def run_payment_plans(today=None):
    """Scheduler entry (python -m app.cli payment_plans). For every active plan due today or earlier: charge the
    card (auto_charge) or email a reminder. Once per plan per day through AuditLog plan_charged / plan_reminded.
    Returns dict(charged, reminded, failed, completed, skipped)."""
    today = today or date.today()
    today_iso = today.isoformat()
    out = {"charged": 0, "reminded": 0, "failed": 0, "completed": 0, "skipped": 0}
    plans = PaymentPlan.query.filter(PaymentPlan.status == "active", PaymentPlan.next_charge_on != None,  # noqa: E711
                                     PaymentPlan.next_charge_on <= today).order_by(PaymentPlan.id).all()
    for plan in plans:
        if _complete_if_done(plan):
            audit("plan_completed", "payment_plan", plan.id, today_iso)
            db.session.commit()
            out["completed"] += 1
            continue
        if plan.auto_charge and has_card(plan.contact):
            if _done_today(plan, "plan_charged", today_iso) or _done_today(plan, "plan_failed", today_iso):
                out["skipped"] += 1
                continue
            ok, err = charge_installment(plan, today=today)
            if ok:
                db.session.commit()
                out["charged"] += 1
                if plan.status == "completed":
                    out["completed"] += 1
                continue
            db.session.rollback()
            plan = db.session.get(PaymentPlan, plan.id)
            plan.last_error = (err or "charge failed")[:300]
            plan.status = "failed"
            audit("plan_failed", "payment_plan", plan.id, today_iso)
            db.session.commit()
            try:
                _firm_failure_email(plan, err or "charge failed")
            except Exception:
                current_app.logger.exception("could not email the firm about plan %s", plan.id)
            out["failed"] += 1
            continue
        if _done_today(plan, "plan_reminded", today_iso):
            out["skipped"] += 1
            continue
        to = send_plan_reminder(plan)
        if not to:
            audit("plan_reminded", "payment_plan", plan.id, today_iso)
            plan.last_error = "client has no email; reminder not sent"
            db.session.commit()
            out["skipped"] += 1
            continue
        plan.next_charge_on = advance_date(plan.next_charge_on or today, plan.frequency)
        plan.last_error = ""
        audit("plan_reminded", "payment_plan", plan.id, today_iso)
        db.session.commit()
        out["reminded"] += 1
    return out


# ---------------------------------------------------------------------------
# Public: pay one installment (linked from the reminder email)
# ---------------------------------------------------------------------------
@bp.route("/pay/plan/<int:plan_id>/<token>", methods=["GET", "POST"])
def plan_pay(plan_id, token):
    plan = db.session.get(PaymentPlan, plan_id) or abort(404)
    inv = plan.invoice
    if not inv or inv.public_token != token:
        abort(404)
    firm = Firm.get()
    method = (request.args.get("method") or "card").lower()
    if method not in ("card", "ach"):
        method = "card"
    if inv.status == "void":
        return render_template("payments/pay_closed.html", inv=inv, reason="void")
    if inv.balance_cents <= 0 or plan.status == "completed":
        return render_template("payments/pay_closed.html", inv=inv, reason="paid")
    amount = next_installment_cents(plan)
    sc = surcharge_cents(amount, firm) if method == "card" else 0
    total = amount + sc
    k = (plan.paid_installments or 0) + 1
    ctx = dict(inv=inv, plan=plan, amount=amount, surcharge=sc, total=total, method=method, k=k,
               pct=_pct(firm.surcharge_bps), token=token)
    if request.method == "GET":
        return render_template("money/plan_pay.html", **ctx)
    if not _stripe.configured():
        return render_template("payments/pay_unconfigured.html", inv=inv, f=firm)
    line_items = [{"price_data": {"currency": "usd", "unit_amount": amount,
                                  "product_data": {"name": f"Invoice {inv.number}, installment {k} of {plan.installments}"}},
                   "quantity": 1}]
    if sc > 0:
        line_items.append({"price_data": {"currency": "usd", "unit_amount": sc,
                                          "product_data": {"name": f"Card processing surcharge {_pct(firm.surcharge_bps)}%"}},
                           "quantity": 1})
    params = dict(
        mode="payment",
        payment_method_types=["card"] if method == "card" else ["us_bank_account"],
        line_items=line_items,
        metadata={"kind": "invoice", "invoice_id": str(inv.id), "surcharge_cents": str(sc), "method": method,
                  "plan_id": str(plan.id)},
        success_url=f"{_base()}/pay/{token}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{_base()}/pay/plan/{plan.id}/{token}",
    )
    if inv.client and inv.client.email:
        params["customer_email"] = inv.client.email
    try:
        sess = _stripe.create_checkout_session(**params)
    except Exception as e:
        current_app.logger.exception("stripe checkout failed for plan %s", plan.id)
        flash(f"We could not start the payment: {e}", "error")
        return render_template("money/plan_pay.html", **ctx)
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="link_clicked",
                                detail=f"installment checkout started, {method}, plan {plan.id}"))
    db.session.commit()
    return redirect(sess["url"], code=303)


# ---------------------------------------------------------------------------
# Template helpers for pages owned by other modules (invoice detail, contact detail, portal home)
# ---------------------------------------------------------------------------
@bp.app_context_processor
def _money_context():
    return dict(money_has_card=has_card, money_card_label=card_label, money_surcharge=surcharge_cents,
                money_plans_for_invoice=plans_for_invoice, money_active_plan=active_plan_for,
                money_plans_for_contact=plans_for_contact, money_next_cents=next_installment_cents,
                money_stripe_ok=_stripe.configured, money_freq_labels=FREQUENCY_LABELS)
