"""Payments: staff list and manual recording, the public pay page (Stripe Checkout with optional card
surcharge), and the Stripe webhook. Recording from the webhook and from the success page share one
idempotent helper keyed on the Checkout Session id."""
import json
from calendar import monthrange
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, abort
from sqlalchemy import func
from ..extensions import db
from ..models import Invoice, InvoiceEvent, Payment, TrustTransaction, Contact, Firm, audit
from ..helpers import login_required, current_user, parse_money, parse_date, cents_to_str, client_ip
from . import _stripe

bp = Blueprint("payments", __name__)

MANUAL_METHODS = ("check", "cash", "wire", "other")


def _pct(bps):
    return f"{bps / 100:g}"


def surcharge_for(invoice, method):
    firm = Firm.get()
    if method == "card" and firm.surcharge_enabled and (firm.surcharge_bps or 0) > 0:
        return int(round(invoice.balance_cents * firm.surcharge_bps / 10000))
    return 0


# ==== staff ====
@bp.route("/payments")
@login_required
def index():
    month = (request.args.get("month") or date.today().strftime("%Y-%m")).strip()
    q = Payment.query
    label = "All time"
    if month != "all":
        try:
            y, m = int(month[:4]), int(month[5:7])
            start, end = date(y, m, 1), date(y, m, monthrange(y, m)[1])
            q = q.filter(Payment.received_on >= start, Payment.received_on <= end)
            label = start.strftime("%B %Y")
        except ValueError:
            month = "all"
    payments = q.order_by(Payment.received_on.desc(), Payment.id.desc()).all()
    totals = {"amount": sum(p.amount_cents or 0 for p in payments),
              "surcharge": sum(p.surcharge_cents or 0 for p in payments),
              "fee": sum(p.stripe_fee_cents or 0 for p in payments)}
    return render_template("payments/index.html", payments=payments, totals=totals, month=month, label=label)


@bp.route("/payments/<int:payment_id>")
@login_required
def detail(payment_id):
    p = db.session.get(Payment, payment_id) or abort(404)
    txn = TrustTransaction.query.filter_by(payment_id=p.id).first()
    return render_template("payments/detail.html", pay=p, txn=txn)


@bp.route("/payments/record", methods=["POST"])
@login_required
def record():
    inv_id = request.form.get("invoice_id", "")
    inv = db.session.get(Invoice, int(inv_id)) if inv_id.isdigit() else None
    if not inv:
        abort(404)
    back = redirect(f"/invoices/{inv.id}")
    amount = parse_money(request.form.get("amount"))
    method = (request.form.get("method") or "check").strip().lower()
    received = parse_date(request.form.get("received_on"), date.today())
    if inv.status == "void":
        flash("This invoice is void. Payments cannot be recorded against it.", "error")
        return back
    if amount <= 0:
        flash("Enter a positive amount.", "error")
        return back
    if method not in MANUAL_METHODS:
        flash("Method must be check, cash, wire or other. Card and bank payments come in through Stripe.", "error")
        return back
    if amount > inv.balance_cents:
        flash(f"That is more than the invoice balance of {cents_to_str(inv.balance_cents)}. "
              f"Record the balance and note the overpayment separately.", "error")
        return back
    uid = current_user().id
    p = Payment(invoice_id=inv.id, matter_id=inv.matter_id, client_id=inv.client_id, amount_cents=amount,
                method=method, account="operating", received_on=received,
                reference=(request.form.get("reference") or "").strip()[:120],
                note=(request.form.get("note") or "").strip()[:300])
    inv.payments.append(p)
    db.session.flush()
    inv.recalc()
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="paid", detail=f"{cents_to_str(amount)} by {method}"))
    audit("payment_record", "invoice", inv.id, f"{cents_to_str(amount)} {method} {p.reference}", uid)
    db.session.commit()
    flash(f"Recorded {cents_to_str(amount)} {method} payment on {inv.number}.", "ok")
    return back


# ==== public pay page ====
def _invoice_by_token(token):
    return Invoice.query.filter_by(public_token=token).first() or abort(404)


@bp.route("/pay/<token>", methods=["GET", "POST"])
def pay(token):
    inv = _invoice_by_token(token)
    firm = Firm.get()
    method = (request.args.get("method") or "card").lower()
    if method not in ("card", "ach"):
        method = "card"
    if inv.status == "void":
        return render_template("payments/pay_closed.html", inv=inv, reason="void")
    if inv.status == "paid" or inv.balance_cents <= 0:
        return render_template("payments/pay_closed.html", inv=inv, reason="paid")
    if inv.status == "draft":
        abort(404)
    surcharge = surcharge_for(inv, method)
    total = inv.balance_cents + surcharge
    if request.method == "GET":
        db.session.add(InvoiceEvent(invoice_id=inv.id, event="link_clicked", ip=client_ip(),
                                    ua=request.user_agent.string[:300], detail=f"pay page, {method}"))
        db.session.commit()
        return render_template("payments/pay_confirm.html", inv=inv, method=method, surcharge=surcharge,
                               total=total, pct=_pct(firm.surcharge_bps or 0))
    # POST is where the charge is created. Stripe settles in the account's currency and every amount
    # here is invoice cents with no conversion, so a non-USD invoice would be charged the right
    # number in the wrong currency. The confirm page already withholds the button and explains this
    # in the client's language; this closes a direct POST past it.
    if (inv.currency or "USD").upper() != "USD":
        return render_template("payments/pay_unconfigured.html", inv=inv, f=firm, reason="currency")
    if not _stripe.configured():
        return render_template("payments/pay_unconfigured.html", inv=inv, f=firm)
    base_url = current_app.config["BASE_URL"]
    line_items = [{"price_data": {"currency": "usd", "unit_amount": inv.balance_cents,
                                  "product_data": {"name": f"Invoice {inv.number}"}}, "quantity": 1}]
    if surcharge > 0:
        line_items.append({"price_data": {"currency": "usd", "unit_amount": surcharge,
                                          "product_data": {"name": f"Card processing surcharge {_pct(firm.surcharge_bps)}%"}},
                           "quantity": 1})
    params = dict(
        mode="payment",
        payment_method_types=["card"] if method == "card" else ["us_bank_account"],
        line_items=line_items,
        metadata={"kind": "invoice", "invoice_id": str(inv.id), "surcharge_cents": str(surcharge), "method": method},
        success_url=f"{base_url}/pay/{token}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/pay/{token}/cancel",
    )
    if inv.client and inv.client.email:
        params["customer_email"] = inv.client.email
    try:
        sess = _stripe.create_checkout_session(**params)
    except Exception as e:
        current_app.logger.exception("stripe checkout failed for invoice %s", inv.id)
        flash(f"We could not start the payment: {e}", "error")
        return render_template("payments/pay_confirm.html", inv=inv, method=method, surcharge=surcharge,
                               total=total, pct=_pct(firm.surcharge_bps or 0))
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="link_clicked", ip=client_ip(),
                                ua=request.user_agent.string[:300], detail=f"checkout started, {method}"))
    db.session.commit()
    return redirect(sess["url"], code=303)


@bp.route("/pay/<token>/success")
def pay_success(token):
    inv = _invoice_by_token(token)
    sid = request.args.get("session_id", "")
    recorded = None
    if sid and _stripe.configured():
        try:
            sess = _stripe.retrieve_checkout_session(sid)
            if sess.get("payment_status") == "paid":
                recorded = record_from_session(sess)
        except Exception:
            current_app.logger.exception("could not verify checkout session %s", sid)
    db.session.refresh(inv)
    return render_template("payments/pay_success.html", inv=inv, recorded=recorded)


@bp.route("/pay/<token>/cancel")
def pay_cancel(token):
    inv = _invoice_by_token(token)
    return render_template("payments/pay_cancel.html", inv=inv)


@bp.route("/pay/deposit/success")
def deposit_success():
    return render_template("payments/deposit_result.html", ok=True)


@bp.route("/pay/deposit/cancel")
def deposit_cancel():
    return render_template("payments/deposit_result.html", ok=False)


# ==== recording a Stripe checkout (shared by webhook and success page) ====
def _meta(sess):
    m = sess.get("metadata") or {}
    return {k: (m.get(k) if hasattr(m, "get") else getattr(m, k, None)) for k in
            ("kind", "invoice_id", "surcharge_cents", "method", "client_id", "matter_id", "amount_cents")}


def _method_from(sess, meta):
    if meta.get("method") in ("card", "ach"):
        return meta["method"]
    types = sess.get("payment_method_types") or []
    return "ach" if "us_bank_account" in types else "card"


def record_from_session(sess):
    """Idempotent: returns the Payment for this Checkout Session, creating it once. Returns None if the
    session carries nothing we know how to record. One transaction per call."""
    sid = sess.get("id") or ""
    if not sid:
        return None
    existing = Payment.query.filter_by(stripe_checkout_session=sid).first()
    if existing:
        return existing
    meta = _meta(sess)
    kind = meta.get("kind") or ""
    pi = sess.get("payment_intent") or ""
    if not isinstance(pi, str):
        pi = pi.get("id", "") if hasattr(pi, "get") else str(pi)
    amount_total = int(sess.get("amount_total") or 0)
    method = _method_from(sess, meta)
    fee = None
    if pi and _stripe.configured():
        try:
            fee = _stripe.fee_cents_for_payment_intent(pi)
        except Exception:
            current_app.logger.warning("fee lookup failed for %s", pi)
    today = date.today()

    if kind == "invoice":
        inv_id = meta.get("invoice_id") or ""
        inv = db.session.get(Invoice, int(inv_id)) if str(inv_id).isdigit() else None
        if not inv:
            current_app.logger.error("stripe session %s references unknown invoice %r", sid, inv_id)
            return None
        surcharge = int(meta.get("surcharge_cents") or 0)
        amount = max(0, amount_total - surcharge)
        p = Payment(invoice_id=inv.id, matter_id=inv.matter_id, client_id=inv.client_id, amount_cents=amount,
                    surcharge_cents=surcharge, stripe_fee_cents=fee or 0, method=method, account="operating",
                    stripe_payment_intent=pi, stripe_checkout_session=sid, received_on=today,
                    reference=pi, note="Paid online via Stripe")
        inv.payments.append(p)
        db.session.flush()
        inv.recalc()
        db.session.add(InvoiceEvent(invoice_id=inv.id, event="paid", detail=f"{cents_to_str(amount)} by {method} via Stripe"
                                    + (f" plus {cents_to_str(surcharge)} surcharge" if surcharge else "")))
        audit("payment_stripe", "invoice", inv.id, f"{cents_to_str(amount)} {method} session {sid}")
        db.session.commit()
        return p

    if kind == "trust_deposit":
        cid = meta.get("client_id") or ""
        client = db.session.get(Contact, int(cid)) if str(cid).isdigit() else None
        if not client:
            current_app.logger.error("stripe session %s references unknown client %r", sid, cid)
            return None
        mid = meta.get("matter_id") or ""
        matter_id = int(mid) if str(mid).isdigit() else None
        if matter_id is not None:
            from ..models import Matter
            m = db.session.get(Matter, matter_id)
            if not m or m.client_id != client.id:
                matter_id = None
        amount = int(meta.get("amount_cents") or 0) or amount_total
        p = Payment(invoice_id=None, matter_id=matter_id, client_id=client.id, amount_cents=amount,
                    stripe_fee_cents=fee or 0, method=method, account="trust", stripe_payment_intent=pi,
                    stripe_checkout_session=sid, received_on=today, reference=pi, note="Online trust deposit")
        db.session.add(p)
        db.session.flush()
        db.session.add(TrustTransaction(client_id=client.id, matter_id=matter_id, date=today, type="deposit",
                                        amount_cents=amount, description="Online trust deposit",
                                        reference=pi or sid, payment_id=p.id, cleared=False))
        audit("trust_deposit_stripe", "contact", client.id, f"{cents_to_str(amount)} {method} session {sid}")
        db.session.commit()
        return p

    current_app.logger.info("stripe session %s has no recognised kind (%r); ignored", sid, kind)
    return None


# ==== webhook ====
@bp.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    secret = current_app.config.get("STRIPE_WEBHOOK_SECRET") or ""
    if secret:
        try:
            event = _stripe.construct_event(payload, request.headers.get("Stripe-Signature", ""), secret)
        except Exception as e:
            current_app.logger.warning("stripe webhook signature rejected: %s", e)
            return ("bad signature", 400)
    else:
        current_app.logger.warning("STRIPE_WEBHOOK_SECRET is not set; accepting webhook without signature check")
        try:
            event = json.loads(payload or b"{}")
        except ValueError:
            return ("bad json", 400)
    etype = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    if etype == "checkout.session.completed" and (obj.get("mode") or "") == "setup":
        # Card on file (money.py): a setup-mode session carries no payment; store the card on the contact.
        from .money import store_card_from_session
        try:
            store_card_from_session(obj)
        except Exception:
            db.session.rollback()
            current_app.logger.exception("failed to store card from setup session %s", obj.get("id"))
            return ("error", 500)
        return ("ok", 200)
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status") != "paid":
            # ACH: completed fires while the debit is pending; async_payment_succeeded follows once it settles.
            current_app.logger.info("stripe %s for %s not paid yet (%s)", etype, obj.get("id"), obj.get("payment_status"))
            return ("pending", 200)
        try:
            record_from_session(obj)
        except Exception:
            db.session.rollback()
            current_app.logger.exception("failed to record stripe session %s", obj.get("id"))
            return ("error", 500)
    elif etype == "checkout.session.async_payment_failed":
        current_app.logger.warning("stripe ACH payment failed for session %s (%s)", obj.get("id"),
                                   (obj.get("metadata") or {}).get("kind"))
    return ("ok", 200)
