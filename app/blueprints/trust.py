"""Trust (IOLTA) ledger: client balances, deposits and disbursements, applying trust to invoices,
three-way reconciliation, and online deposit requests. Every write is one transaction and is validated
before anything is added to the session."""
import json
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, abort
from sqlalchemy import func, or_, and_
from ..extensions import db
from ..models import (Contact, Matter, Invoice, InvoiceEvent, Payment, TrustTransaction,
                      TrustReconciliation, Firm, audit)
from ..helpers import login_required, current_user, parse_money, parse_date, cents_to_str
from ..services.mail import send_email
from . import _stripe

bp = Blueprint("trust", __name__, url_prefix="/trust")

FORM_TYPES = ("deposit", "disbursement", "refund", "bank_fee", "interest")
POSITIVE_TYPES = ("deposit", "interest")
TYPE_LABELS = {"deposit": "Deposit", "disbursement": "Disbursement", "refund": "Refund to client",
               "bank_fee": "Bank fee", "interest": "Interest", "to_operating": "Applied to invoice"}


# ==== balance helpers (grouped queries, independent of the model convenience methods) ====
def client_balances(as_of=None):
    q = db.session.query(TrustTransaction.client_id, func.coalesce(func.sum(TrustTransaction.amount_cents), 0))
    if as_of:
        q = q.filter(TrustTransaction.date <= as_of)
    return {cid: int(v or 0) for cid, v in q.group_by(TrustTransaction.client_id).all()}


def matter_balances(as_of=None):
    q = db.session.query(TrustTransaction.matter_id, func.coalesce(func.sum(TrustTransaction.amount_cents), 0)).filter(
        TrustTransaction.matter_id != None)  # noqa: E711
    if as_of:
        q = q.filter(TrustTransaction.date <= as_of)
    return {mid: int(v or 0) for mid, v in q.group_by(TrustTransaction.matter_id).all()}


def book_total(as_of=None):
    q = db.session.query(func.coalesce(func.sum(TrustTransaction.amount_cents), 0))
    if as_of:
        q = q.filter(TrustTransaction.date <= as_of)
    return int(q.scalar() or 0)


def allocation(client, as_of=None):
    """How a client's trust money is earmarked.

    Returns (total, per_matter, allocated, unallocated) where per_matter maps matter id to that
    matter's own sub-ledger balance, allocated is the sum of the POSITIVE per-matter balances, and
    unallocated = total - allocated. Unallocated is what may be spent without a matter tag.

    Unallocated goes negative only when the matter sub-ledgers claim more than the client actually
    holds, which imported data can do. Callers must treat that as a shortfall, not as spendable.
    """
    mb = matter_balances(as_of)
    per = {m.id: mb.get(m.id, 0) for m in client.matters}
    total = int(client_balances(as_of).get(client.id, 0))
    allocated = sum(v for v in per.values() if v > 0)
    return total, per, allocated, total - allocated


def available_for_matter(client, matter, as_of=None):
    """(own, unallocated, available) in cents that may be spent on this matter.

    A matter may spend its own earmarked balance plus the client's unallocated balance, and nothing
    else. Another matter's earmarked funds are never available, so `available` is capped at what the
    client actually holds when the sub-ledgers are over-allocated.
    """
    total, per, allocated, unallocated = allocation(client, as_of)
    own = max(0, per.get(matter.id, 0)) if matter else 0
    return own, unallocated, max(0, own + unallocated)


def outstanding_filter(period_end):
    """Items not cleared as of period_end: never cleared, or cleared after the period."""
    return or_(TrustTransaction.cleared == False,  # noqa: E712
               and_(TrustTransaction.cleared_on != None, TrustTransaction.cleared_on > period_end))  # noqa: E711


# ==== overview ====
@bp.route("/")
@login_required
def index():
    cbal = client_balances()
    mbal = matter_balances()
    ids = [cid for cid, v in cbal.items() if v != 0]
    clients = Contact.query.filter(Contact.id.in_(ids)).all() if ids else []
    clients.sort(key=lambda c: c.sort_name.lower())
    rows = []
    negatives = []
    over_allocated = []
    for c in clients:
        matters = [(m, mbal.get(m.id, 0)) for m in c.matters if mbal.get(m.id, 0) != 0]
        allocated = sum(v for _, v in matters if v > 0)
        unallocated = cbal[c.id] - allocated
        rows.append({"client": c, "balance": cbal[c.id], "matters": matters, "allocated": allocated,
                     "unallocated": unallocated})
        if cbal[c.id] < 0:
            negatives.append(f"{c.display_name} ({cents_to_str(cbal[c.id])})")
        for m, v in matters:
            if v < 0:
                negatives.append(f"{m.label} ({cents_to_str(v)})")
        if unallocated < 0:
            over_allocated.append(f"{c.display_name}: matters claim {cents_to_str(allocated)} but the client "
                                  f"holds {cents_to_str(cbal[c.id])}, short {cents_to_str(-unallocated)}")
    uncleared = TrustTransaction.query.filter_by(cleared=False).count()
    last_recon = TrustReconciliation.query.order_by(TrustReconciliation.period_end.desc(),
                                                    TrustReconciliation.id.desc()).first()
    card_fees = db.session.query(func.coalesce(func.sum(Payment.stripe_fee_cents), 0)).join(
        TrustTransaction, TrustTransaction.payment_id == Payment.id).filter(
        TrustTransaction.type == "deposit",
        or_(Payment.stripe_checkout_session != "", Payment.stripe_payment_intent != "")).scalar() or 0
    all_clients = Contact.query.filter_by(is_client=True).all()
    all_clients.sort(key=lambda c: c.sort_name.lower())
    open_matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    return render_template("trust/index.html", rows=rows, bank_total=book_total(), uncleared=uncleared,
                           last_recon=last_recon, negatives=negatives, over_allocated=over_allocated,
                           card_fees=int(card_fees),
                           all_clients=all_clients, open_matters=open_matters, stripe_on=_stripe.configured())


# ==== ledger ====
@bp.route("/ledger/<int:client_id>")
@login_required
def ledger(client_id):
    client = db.session.get(Contact, client_id) or abort(404)
    txns = TrustTransaction.query.filter_by(client_id=client_id).order_by(
        TrustTransaction.date, TrustTransaction.id).all()
    running = 0
    rows = []
    for t in txns:
        running += t.amount_cents
        rows.append((t, running))
    mbal = matter_balances()
    matters = [(m, mbal.get(m.id, 0)) for m in client.matters if m.id in mbal]
    allocated = sum(v for _, v in matters if v > 0)
    unallocated = running - allocated
    return render_template("trust/ledger.html", client=client, rows=rows, balance=running, matters=matters,
                           allocated=allocated, unallocated=unallocated, labels=TYPE_LABELS)


@bp.route("/<int:txn_id>/clear", methods=["POST"])
@login_required
def clear(txn_id):
    t = db.session.get(TrustTransaction, txn_id) or abort(404)
    t.cleared = not t.cleared
    t.cleared_on = date.today() if t.cleared else None
    db.session.commit()
    nxt = request.form.get("next") or ""
    if nxt.startswith("/"):
        return redirect(nxt)
    return redirect(url_for("trust.ledger", client_id=t.client_id))


# ==== new transaction ====
@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    clients = Contact.query.filter_by(is_client=True).all()
    clients.sort(key=lambda c: c.sort_name.lower())
    matters = Matter.query.order_by(Matter.number).all()
    form = {"type": request.args.get("type", "deposit"), "client_id": request.args.get("client_id", ""),
            "matter_id": request.args.get("matter_id", ""), "date": date.today().isoformat(), "amount": "",
            "description": "", "payee": "", "reference": ""}
    if request.method == "POST":
        form.update({k: (request.form.get(k) or "").strip() for k in form})
        ttype = form["type"]
        client = db.session.get(Contact, int(form["client_id"])) if form["client_id"].isdigit() else None
        matter = db.session.get(Matter, int(form["matter_id"])) if form["matter_id"].isdigit() else None
        when = parse_date(form["date"])
        amount = parse_money(form["amount"])
        errors = []
        if ttype not in FORM_TYPES:
            errors.append("Pick a transaction type.")
        if not client:
            errors.append("Pick a client.")
        if matter and client and matter.client_id != client.id:
            errors.append("That matter does not belong to the selected client.")
        if not when:
            errors.append("Enter a date as YYYY-MM-DD.")
        if amount <= 0:
            errors.append("Enter a positive amount. The sign is set by the transaction type.")
        if not errors:
            delta = amount if ttype in POSITIVE_TYPES else -amount
            if delta < 0:
                total, per, allocated, unallocated = allocation(client)
                label = TYPE_LABELS[ttype].lower()
                if total + delta < 0:
                    errors.append(f"Rejected: {client.display_name} holds {cents_to_str(total)} in trust. "
                                  f"A {label} of {cents_to_str(amount)} would overdraw the client.")
                elif matter:
                    # A matter-tagged withdrawal may only spend that matter's own earmarked funds.
                    # Unallocated money and other matters' funds are off limits: tag it to the right
                    # matter, or move the money first.
                    own = max(0, per.get(matter.id, 0))
                    if amount > own:
                        errors.append(f"Rejected: {matter.label} holds {cents_to_str(own)} in trust. "
                                      f"A {label} of {cents_to_str(amount)} would overdraw the matter. "
                                      f"{cents_to_str(max(0, unallocated))} of {client.display_name}'s money is "
                                      f"unallocated; deposit it to this matter first if it belongs here.")
                elif amount > unallocated:
                    # Untagged withdrawal: only the client's unallocated money can go out this way,
                    # otherwise it silently spends another matter's retainer.
                    errors.append(f"Rejected: only {cents_to_str(max(0, unallocated))} of "
                                  f"{client.display_name}'s {cents_to_str(total)} trust balance is unallocated. "
                                  f"The rest is earmarked to specific matters, so a {label} of "
                                  f"{cents_to_str(amount)} with no matter selected is refused. Pick the matter "
                                  f"the money is coming from.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("trust/new.html", clients=clients, matters=matters, form=form, types=FORM_TYPES,
                                   labels=TYPE_LABELS)
        t = TrustTransaction(client_id=client.id, matter_id=matter.id if matter else None, date=when, type=ttype,
                             amount_cents=delta, description=form["description"], payee=form["payee"],
                             reference=form["reference"], cleared=False, created_by_id=current_user().id)
        db.session.add(t)
        db.session.flush()
        audit("trust_" + ttype, "trust_transaction", t.id,
              f"{client.display_name} {cents_to_str(delta)} {form['description']}", current_user().id)
        db.session.commit()
        flash(f"{TYPE_LABELS[ttype]} of {cents_to_str(amount)} recorded for {client.display_name}.", "ok")
        return redirect(url_for("trust.ledger", client_id=client.id))
    return render_template("trust/new.html", clients=clients, matters=matters, form=form, types=FORM_TYPES,
                           labels=TYPE_LABELS)


# ==== apply trust funds to an invoice ====
@bp.route("/apply", methods=["POST"])
@login_required
def apply():
    inv_id = request.form.get("invoice_id", "")
    inv = db.session.get(Invoice, int(inv_id)) if inv_id.isdigit() else None
    if not inv:
        abort(404)
    back = redirect(f"/invoices/{inv.id}")
    amount = parse_money(request.form.get("amount"))
    if inv.status in ("void", "draft"):
        flash("Trust funds can only be applied to a sent invoice.", "error")
        return back
    if amount <= 0:
        flash("Enter a positive amount to apply.", "error")
        return back
    if amount > inv.balance_cents:
        flash(f"That is more than the invoice balance of {cents_to_str(inv.balance_cents)}.", "error")
        return back
    # An invoice may only be paid from its own matter's earmarked funds plus the client's
    # unallocated balance. Another matter's money is never available, whatever the pooled client
    # balance says.
    own, unallocated, available = available_for_matter(inv.client, inv.matter)
    if amount > available:
        mlabel = inv.matter.label if inv.matter else inv.client.display_name
        flash(f"Only {cents_to_str(available)} can be applied to this invoice: {cents_to_str(own)} held in "
              f"trust for {mlabel} and {cents_to_str(unallocated)} unallocated for "
              f"{inv.client.display_name}. Any other trust money this client holds is earmarked to a "
              f"different matter and cannot pay this one.", "error")
        return back
    from_matter = min(amount, own) if inv.matter_id else 0
    from_unallocated = amount - from_matter
    parts = [(inv.matter_id, from_matter), (None, from_unallocated)]
    source = inv.matter.label if inv.matter else inv.client.display_name
    if from_matter and from_unallocated:
        source = f"{inv.matter.label} {cents_to_str(from_matter)} + unallocated {cents_to_str(from_unallocated)}"
    elif not from_matter:
        source = f"{inv.client.display_name} (unallocated)"
    today = date.today()
    uid = current_user().id
    pay = Payment(invoice_id=inv.id, matter_id=inv.matter_id, client_id=inv.client_id, amount_cents=amount,
                  method="trust", account="operating", received_on=today,
                  note=f"Applied from trust ({source})")
    inv.payments.append(pay)
    db.session.flush()
    # One Payment, but one ledger row per source so each sub-ledger stays truthful.
    for matter_id, part in parts:
        if part <= 0:
            continue
        db.session.add(TrustTransaction(
            client_id=inv.client_id, matter_id=matter_id, date=today, type="to_operating",
            amount_cents=-part, description=f"Applied to invoice {inv.number}"
                                            f"{'' if matter_id else ' (unallocated funds)'}",
            payee=Firm.get().name, invoice_id=inv.id, payment_id=pay.id, cleared=False, created_by_id=uid))
    inv.recalc()
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="paid",
                                detail=f"{cents_to_str(amount)} applied from trust"))
    audit("trust_apply", "invoice", inv.id, f"{cents_to_str(amount)} from trust ({source})", uid)
    db.session.commit()
    flash(f"Applied {cents_to_str(amount)} from trust to invoice {inv.number}.", "ok")
    return back


# ==== reconciliation ====
@bp.route("/reconcile", methods=["GET", "POST"])
@login_required
def reconcile():
    if request.method == "POST":
        period_end = parse_date(request.form.get("period_end"))
        bank = parse_money(request.form.get("bank_statement"))
        if not period_end:
            flash("Enter the statement period end date.", "error")
            return redirect(url_for("trust.reconcile"))
        book = book_total(period_end)
        per_client = client_balances(period_end)
        client_ledgers = sum(per_client.values())
        base = TrustTransaction.query.filter(TrustTransaction.date <= period_end, outstanding_filter(period_end))
        od = base.filter(TrustTransaction.amount_cents > 0).all()
        odis = base.filter(TrustTransaction.amount_cents < 0).all()
        od_cents = sum(t.amount_cents for t in od)
        odis_cents = sum(t.amount_cents for t in odis)  # negative
        adjusted = bank + od_cents - abs(odis_cents)
        balanced = (adjusted == book == client_ledgers)
        names = {c.id: c.display_name for c in Contact.query.filter(Contact.id.in_(list(per_client) or [0])).all()}
        detail = {
            "clients": [{"client_id": cid, "name": names.get(cid, f"#{cid}"), "balance_cents": v}
                        for cid, v in sorted(per_client.items(), key=lambda kv: names.get(kv[0], "").lower())],
            "outstanding_deposit_ids": [t.id for t in od],
            "outstanding_disbursement_ids": [t.id for t in odis],
            "bank_vs_book_cents": adjusted - book,
            "ledgers_vs_book_cents": client_ledgers - book,
        }
        r = TrustReconciliation(period_end=period_end, bank_statement_cents=bank, book_balance_cents=book,
                                client_ledgers_cents=client_ledgers, outstanding_deposits_cents=od_cents,
                                outstanding_disbursements_cents=odis_cents, adjusted_bank_cents=adjusted,
                                balanced=balanced, detail_json=json.dumps(detail),
                                notes=(request.form.get("notes") or "").strip(), created_by_id=current_user().id)
        db.session.add(r)
        db.session.flush()
        audit("trust_reconcile", "trust_reconciliation", r.id,
              f"period {period_end.isoformat()} {'balanced' if balanced else 'out of balance'}", current_user().id)
        db.session.commit()
        return redirect(url_for("trust.reconcile_report", recon_id=r.id))
    past = TrustReconciliation.query.order_by(TrustReconciliation.period_end.desc(),
                                              TrustReconciliation.id.desc()).all()
    return render_template("trust/reconcile.html", past=past, today=date.today(), book=book_total())


@bp.route("/reconcile/<int:recon_id>")
@login_required
def reconcile_report(recon_id):
    r = db.session.get(TrustReconciliation, recon_id) or abort(404)
    try:
        detail = json.loads(r.detail_json or "{}")
    except Exception:
        detail = {}
    ids = list(detail.get("outstanding_deposit_ids", [])) + list(detail.get("outstanding_disbursement_ids", []))
    txns = {t.id: t for t in TrustTransaction.query.filter(TrustTransaction.id.in_(ids or [0])).all()}
    deposits = [txns[i] for i in detail.get("outstanding_deposit_ids", []) if i in txns]
    disbursements = [txns[i] for i in detail.get("outstanding_disbursement_ids", []) if i in txns]
    diff_bank = r.adjusted_bank_cents - r.book_balance_cents
    diff_ledgers = r.client_ledgers_cents - r.book_balance_cents
    out_by = max(abs(diff_bank), abs(diff_ledgers))
    return render_template("trust/reconcile_report.html", r=r, detail=detail, deposits=deposits,
                           disbursements=disbursements, diff_bank=diff_bank, diff_ledgers=diff_ledgers,
                           out_by=out_by, labels=TYPE_LABELS)


# ==== request an online deposit ====
def send_deposit_request(client, matter, amount_cents, user_id=None):
    """Email the client a trust deposit request for amount_cents: a Stripe checkout link when Stripe is
    configured, otherwise check and mailing instructions. Adds an audit row; the caller commits.

    Returns "stripe" or "mail" on success. Raises ValueError with a plain message when nothing was sent.
    Shared by the /trust/request-deposit route and the evergreen retainer reminder in app/cli.py.
    """
    if not client:
        raise ValueError("Pick a client.")
    if not client.email:
        raise ValueError(f"{client.display_name} has no email address on file.")
    if matter and matter.client_id != client.id:
        raise ValueError("That matter does not belong to the selected client.")
    amount = int(amount_cents or 0)
    if amount <= 0:
        raise ValueError("Enter a positive amount.")
    firm = Firm.get()
    base_url = current_app.config["BASE_URL"]
    purpose = f"{matter.label}" if matter else "your matters with us"
    amt = cents_to_str(amount)
    if _stripe.configured():
        try:
            sess = _stripe.create_checkout_session(
                mode="payment",
                payment_method_types=["us_bank_account", "card"],
                line_items=[{"price_data": {"currency": "usd", "unit_amount": amount,
                                            "product_data": {"name": f"Trust deposit: {purpose}"}},
                             "quantity": 1}],
                metadata={"kind": "trust_deposit", "client_id": str(client.id),
                          "matter_id": str(matter.id) if matter else "", "amount_cents": str(amount)},
                customer_email=client.email,
                success_url=f"{base_url}/pay/deposit/success",
                cancel_url=f"{base_url}/pay/deposit/cancel",
            )
        except Exception as e:  # network or Stripe error; nothing was written
            current_app.logger.exception("stripe checkout for trust deposit failed")
            raise ValueError(f"Stripe could not create a checkout link: {e}")
        url = sess["url"] if hasattr(sess, "__getitem__") else sess.url
        html = (f"<p>{firm.name} is asking for a deposit of <strong>{amt}</strong> to our client trust account "
                f"for {purpose}.</p>"
                f"<p>Use this secure link to pay by bank transfer or card:<br><a href=\"{url}\">{url}</a></p>"
                f"<p>Money you deposit stays in the trust account and belongs to you until it is applied to an "
                f"invoice for work already done. Any unused balance is returned to you.</p>"
                f"<p>Questions? Reply to this email or call {firm.phone}.</p>")
        text = (f"{firm.name} is asking for a deposit of {amt} to our client trust account for {purpose}.\n\n"
                f"Pay securely here: {url}\n\nDeposited money stays in trust and belongs to you until it is applied "
                f"to an invoice for completed work. Unused funds are returned.")
        send_email(client.email, f"Trust deposit request from {firm.name}: {amt}", html, text)
        audit("trust_request_deposit", "contact", client.id, f"{amt} stripe link emailed to {client.email}", user_id)
        return "stripe"
    acct = firm.trust_bank_name or "our client trust account"
    last4 = f" (account ending {firm.trust_account_last4})" if firm.trust_account_last4 else ""
    memo = matter.number if matter else client.display_name
    addr = (firm.address or "").replace("\n", "<br>")
    html = (f"<p>{firm.name} is asking for a deposit of <strong>{amt}</strong> to our client trust account "
            f"for {purpose}.</p>"
            f"<p>Please make a check payable to <strong>{firm.name}, {acct}</strong>{last4} and write "
            f"<strong>{memo}</strong> in the memo line. Mail or bring it to:</p><p>{addr}</p>"
            f"<p>If you prefer to send a wire, reply to this email and we will send the routing details "
            f"by phone.</p>"
            f"<p>Money you deposit stays in the trust account and belongs to you until it is applied to an "
            f"invoice for work already done. Any unused balance is returned to you.</p>")
    text = (f"{firm.name} is asking for a deposit of {amt} to our client trust account for {purpose}.\n\n"
            f"Make a check payable to {firm.name}, {acct}{last4}, memo: {memo}.\nMail to:\n{firm.address}\n\n"
            f"Deposited money stays in trust and belongs to you until it is applied to an invoice for "
            f"completed work. Unused funds are returned.")
    send_email(client.email, f"Trust deposit request from {firm.name}: {amt}", html, text)
    audit("trust_request_deposit", "contact", client.id, f"{amt} mailing instructions emailed to {client.email}",
          user_id)
    return "mail"


def evergreen_shortfalls():
    """Open matters whose trust balance is below their evergreen minimum.

    Returns [(matter, balance_cents, shortfall_cents)] where shortfall = replenish_to - balance (or
    minimum - balance when no replenish target is set). Used by the reminders CLI and the dashboard card.

    balance_cents is what the matter can actually spend: its own earmarked funds plus the client's
    unallocated balance. Another matter's earmarked funds are not counted, because they cannot pay
    this matter's invoices.
    """
    out = []
    cbal, mbal = client_balances(), matter_balances()   # two queries, not two per matter
    allocated_by_client = {}
    for m in Matter.query.all():
        v = mbal.get(m.id, 0)
        if v > 0:
            allocated_by_client[m.client_id] = allocated_by_client.get(m.client_id, 0) + v
    q = Matter.query.filter(Matter.status != "closed", Matter.trust_minimum_cents > 0).order_by(Matter.number)
    for m in q.all():
        unallocated = cbal.get(m.client_id, 0) - allocated_by_client.get(m.client_id, 0)
        bal = max(0, max(0, mbal.get(m.id, 0)) + unallocated)
        if bal < (m.trust_minimum_cents or 0):
            target = m.trust_replenish_to_cents or m.trust_minimum_cents or 0
            shortfall = max(0, target - bal)
            if shortfall > 0:
                out.append((m, bal, shortfall))
    return out


@bp.route("/request-deposit", methods=["POST"])
@login_required
def request_deposit():
    cid = request.form.get("client_id", "")
    mid = request.form.get("matter_id", "")
    client = db.session.get(Contact, int(cid)) if cid.isdigit() else None
    matter = db.session.get(Matter, int(mid)) if mid.isdigit() else None
    amount = parse_money(request.form.get("amount"))
    try:
        mode = send_deposit_request(client, matter, amount, user_id=current_user().id)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("trust.index"))
    db.session.commit()
    amt = cents_to_str(amount)
    if mode == "stripe":
        flash(f"Emailed {client.email} a secure Stripe link for a {amt} trust deposit.", "ok")
    else:
        flash(f"Stripe is not configured, so {client.email} was emailed check and mailing instructions for a "
              f"{amt} trust deposit.", "ok")
    return redirect(url_for("trust.index"))
