"""Phase 6, Agent P: fee splits + compensation report (CosmoLex lane), cards on file, charge on invoice and
payment plans (Gravity Legal lane).

Own SQLite file seeded by seed.py. Every Stripe call is monkeypatched at app.blueprints._stripe; the app config
starts with STRIPE_SECRET_KEY blank and tests that need "configured" set a fake key on app.config.
"""
import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402

TEST_DB = os.path.join(ROOT, "data", "test_phase6_p.db")
UPLOAD_DIR = os.path.join(ROOT, "data", "test_phase6_p_uploads")
S = {}
TODAY = date.today()


@pytest.fixture(scope="module")
def app():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{TEST_DB}", STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="",
               SMTP_HOST="")
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT, env=env)
    from app import create_app
    application = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{TEST_DB}", "TESTING": True, "SMTP_HOST": "",
                              "UPLOAD_DIR": UPLOAD_DIR, "STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": "",
                              "BASE_URL": "http://coil.test"})
    with application.app_context():
        _seed(application)
    yield application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    S["tok"] = login(c)
    return c


@pytest.fixture
def stripe_on(app, monkeypatch):
    monkeypatch.setitem(app.config, "STRIPE_SECRET_KEY", "sk_test_fake")
    from app.blueprints import _stripe
    monkeypatch.setattr(_stripe, "fee_cents_for_payment_intent", lambda pi: 30)
    monkeypatch.setattr(_stripe, "detach_payment_method", lambda pm: {"id": pm})
    return _stripe


def _seed(app):
    from app.extensions import db
    from app.models import User, Contact, Matter, Firm
    owner = User.query.filter_by(email="owner@example.com").first()
    ann = User(email="ann@example.com", name="Ann Associate", role="attorney", hourly_rate_cents=25000, initials="AA")
    ann.set_password("password123")
    db.session.add(ann)
    pat = Contact(kind="person", first_name="Pat", last_name="Payer", email="pat@example.com", is_client=True)
    db.session.add(pat)
    db.session.flush()
    m = Matter(number="M-P6", client_id=pat.id, name="Payer contract dispute", billing_type="hourly",
               responsible_user_id=owner.id, originating_user_id=owner.id, status="open", opened_on=TODAY)
    db.session.add(m)
    firm = Firm.get()
    firm.surcharge_enabled = True
    firm.surcharge_bps = 300
    firm.email = "firm@example.com"
    db.session.commit()
    S.update(owner_id=owner.id, ann_id=ann.id, pat_id=pat.id, matter_id=m.id)


def _invoice(app, total, expense=0, number=None):
    """A sent invoice on the seeded matter: one fee line for `total - expense` plus an optional expense line."""
    from app.extensions import db
    from app.models import Invoice, InvoiceLine
    with app.app_context():
        n = Invoice.query.count() + 1
        inv = Invoice(number=number or f"INV-P6-{n}", matter_id=S["matter_id"], client_id=S["pat_id"], kind="hourly",
                      status="sent", issued_on=TODAY, due_on=TODAY + timedelta(days=30))
        inv.lines.append(InvoiceLine(kind="time", description="Work", quantity=1.0, unit_cents=total - expense,
                                     amount_cents=total - expense, sort=1))
        if expense:
            inv.lines.append(InvoiceLine(kind="expense", description="Filing fee", quantity=1.0, unit_cents=expense,
                                         amount_cents=expense, sort=2))
        db.session.add(inv)
        db.session.flush()
        inv.recalc()
        db.session.commit()
        return inv.id, inv.public_token


def _outbox(subject_part=""):
    from app.services.mail import dev_outbox
    return [m for m in dev_outbox() if subject_part in m["subject"]]


def _charge_recorder(monkeypatch, _stripe, fail=False):
    calls = []

    def fake(customer_id, payment_method_id, amount_cents, description="", metadata=None, idempotency_key=None):
        if fail:
            raise RuntimeError("Your card was declined.")
        calls.append({"customer": customer_id, "pm": payment_method_id, "amount": amount_cents, "meta": metadata})
        return {"id": f"pi_p6_{len(calls)}", "status": "succeeded", "object": "payment_intent"}

    monkeypatch.setattr(_stripe, "charge_payment_method", fake)
    return calls


# ---------------------------------------------------------------------------
# 1. Fee splits and compensation
# ---------------------------------------------------------------------------
def test_splits_validation_and_save(app, client):
    from app.models import MatterFeeSplit
    mid = S["matter_id"]
    r = client.get(f"/matters/{mid}")
    assert r.status_code == 200
    assert b"Fee splits" in r.data and b"(default, not saved)" in r.data  # originator 100% computed
    r = client.get(f"/money/splits/{mid}")
    assert r.status_code == 200 and b"Demo Owner" in r.data

    # Working must total 100.
    bad = {"_csrf": S["tok"], "user_id_0": S["owner_id"], "role_0": "working", "percent_0": "60",
           "user_id_1": S["ann_id"], "role_1": "working", "percent_1": "30"}
    r = client.post(f"/money/splits/{mid}", data=bad)
    assert r.status_code == 400 and b"add up to 100" in r.data
    with app.app_context():
        assert MatterFeeSplit.query.filter_by(matter_id=mid).count() == 0

    good = {"_csrf": S["tok"], "user_id_0": S["owner_id"], "role_0": "working", "percent_0": "60",
            "user_id_1": S["ann_id"], "role_1": "working", "percent_1": "40",
            "user_id_2": S["owner_id"], "role_2": "originating", "percent_2": "100"}
    r = client.post(f"/money/splits/{mid}", data=good)
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/matters/{mid}")
    with app.app_context():
        rows = MatterFeeSplit.query.filter_by(matter_id=mid).order_by(MatterFeeSplit.id).all()
        assert [(s.role, s.percent) for s in rows] == [("working", 60.0), ("working", 40.0), ("originating", 100.0)]
    r = client.get(f"/matters/{mid}")
    assert b"Ann Associate" in r.data and b"40%" in r.data

    # Originating and referral are informational: any total up to 100 each, no working rows is fine.
    only_orig = {"_csrf": S["tok"], "user_id_0": S["ann_id"], "role_0": "referral", "percent_0": "10",
                 "user_id_1": S["owner_id"], "role_1": "originating", "percent_1": "100"}
    r = client.post(f"/money/splits/{mid}", data=only_orig)
    assert r.status_code == 302
    # Put the 60/40 back for the report test.
    r = client.post(f"/money/splits/{mid}", data=good)
    assert r.status_code == 302


def test_compensation_report_allocates_fee_share(app, client):
    from app.extensions import db
    from app.models import Invoice, Payment
    from app.blueprints.money import compensation_data
    # 100000 of fees plus a 20000 expense reimbursement, paid in full with a card surcharge on top.
    inv_id, _ = _invoice(app, 120000, expense=20000, number="INV-P6-COMP")
    with app.app_context():
        inv = db.session.get(Invoice, inv_id)
        p = Payment(invoice_id=inv.id, matter_id=inv.matter_id, client_id=inv.client_id, amount_cents=120000,
                    surcharge_cents=3600, method="card", account="operating", received_on=TODAY, note="test")
        inv.payments.append(p)
        db.session.flush()
        inv.recalc()
        db.session.commit()
        assert inv.status == "paid"
        matter_rows, user_rows, totals = compensation_data(TODAY, TODAY)
        row = [r for r in matter_rows if r["matter"].id == S["matter_id"]][0]
        assert row["gross"] == 120000 and row["fee"] == 100000 and row["flagged"] is False
        assert {(u.id, cents) for u, pct, cents in row["working"]} == {(S["owner_id"], 60000), (S["ann_id"], 40000)}
        assert [(u.id, pct, cents) for u, pct, cents in row["originating"]] == [(S["owner_id"], 100.0, 100000)]
        by_user = {r["user"].id: r for r in user_rows}
        assert by_user[S["owner_id"]]["working"] == 60000 and by_user[S["owner_id"]]["originating"] == 100000
        assert by_user[S["ann_id"]]["working"] == 40000 and by_user[S["ann_id"]]["originating"] == 0
        assert totals["working"] == 100000 and totals["fee"] == 100000
    r = client.get(f"/reports/compensation?from={TODAY.isoformat()}&to={TODAY.isoformat()}")
    assert r.status_code == 200
    assert b"$600.00" in r.data and b"$400.00" in r.data and b"$1,000.00" in r.data
    r = client.get(f"/reports/compensation?from={TODAY.isoformat()}&to={TODAY.isoformat()}&format=csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    body = r.data.decode()
    assert "Ann Associate,40,400.00,1000.00,1200.00" in body
    assert "Demo Owner,60,600.00,1000.00,1200.00" in body
    assert "originating,Demo Owner,100,1000.00" in body
    r = client.get("/reports")
    assert b"/reports/compensation" in r.data


# ---------------------------------------------------------------------------
# 2. Cards on file
# ---------------------------------------------------------------------------
def test_card_request_needs_stripe(app, client):
    from app.models import PortalToken
    with app.app_context():
        before = PortalToken.query.filter_by(contact_id=S["pat_id"]).count()
    r = client.post(f"/money/cards/{S['pat_id']}/request", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert r.status_code == 200 and b"Online payments are not configured" in r.data
    with app.app_context():
        assert PortalToken.query.filter_by(contact_id=S["pat_id"]).count() == before
    r = client.get(f"/contacts/{S['pat_id']}")
    assert b"Request card on file" in r.data and b"Online payments are not configured" in r.data


def test_card_request_email_and_consent_flow(app, client, stripe_on, monkeypatch):
    from app.extensions import db
    from app.models import PortalToken, Contact
    r = client.post(f"/money/cards/{S['pat_id']}/request", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert r.status_code == 200 and b"Emailed pat@example.com" in r.data
    mails = _outbox("save a card on file")
    assert mails and mails[0]["to"] == "pat@example.com"
    m = re.search(r"http://coil\.test/pay/card/([A-Za-z0-9_\-]+)", mails[0]["html"])
    assert m, mails[0]["html"]
    token = m.group(1)
    S["card_token"] = token
    with app.app_context():
        tok = PortalToken.query.filter_by(token=token).one()
        assert tok.contact_id == S["pat_id"] and tok.used_at is None
        assert (tok.expires_at - tok.created_at).days >= 6

    anon = app.test_client()
    r = anon.get(f"/pay/card/{token}")
    assert r.status_code == 200
    assert b"I authorise" in r.data and b"3%" in r.data and b"never sees the full card number" in r.data
    # Consent checkbox is required.
    r = anon.post(f"/pay/card/{token}", data={})
    assert r.status_code == 400 and b"tick the box" in r.data

    created = {}
    monkeypatch.setattr(stripe_on, "create_customer", lambda email="", name="", metadata=None:
                        created.setdefault("cust", {"id": "cus_p6", "email": email}))
    sessions = []

    def fake_setup(customer_id, success_url, cancel_url, metadata=None):
        sessions.append(dict(customer=customer_id, success_url=success_url, cancel_url=cancel_url, metadata=metadata))
        return {"id": "cs_setup_p6", "url": "https://checkout.stripe.test/setup", "mode": "setup"}

    monkeypatch.setattr(stripe_on, "create_setup_session", fake_setup)
    r = anon.post(f"/pay/card/{token}", data={"agree": "1"})
    assert r.status_code == 303 and r.headers["Location"] == "https://checkout.stripe.test/setup"
    assert created["cust"]["email"] == "pat@example.com"
    assert sessions[0]["customer"] == "cus_p6" and sessions[0]["metadata"]["contact_id"] == str(S["pat_id"])
    assert sessions[0]["success_url"].startswith(f"http://coil.test/pay/card/{token}/success?session_id=")
    with app.app_context():
        assert db.session.get(Contact, S["pat_id"]).stripe_customer_id == "cus_p6"
    r = anon.get(f"/pay/card/{token}/cancel")
    assert r.status_code == 200 and b"No card was saved" in r.data


def test_setup_completion_stores_card(app, client, stripe_on):
    from app.extensions import db
    from app.models import Contact, PortalToken
    token = S["card_token"]
    event = {"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_setup_p6", "object": "checkout.session", "mode": "setup", "payment_status": "no_payment_required",
        "customer": "cus_p6",
        "setup_intent": {"id": "seti_p6", "payment_method": {"id": "pm_p6", "card": {"brand": "visa", "last4": "4242"}}},
        "metadata": {"kind": "card_setup", "contact_id": str(S["pat_id"]), "token": token}}}}
    anon = app.test_client()
    for _ in range(2):  # idempotent
        r = anon.post("/webhooks/stripe", data=json.dumps(event), content_type="application/json")
        assert r.status_code == 200
    with app.app_context():
        c = db.session.get(Contact, S["pat_id"])
        assert c.stripe_customer_id == "cus_p6" and c.stripe_payment_method_id == "pm_p6"
        assert c.card_brand == "visa" and c.card_last4 == "4242" and c.card_authorised_on == TODAY
        assert PortalToken.query.filter_by(token=token).one().used_at is not None
    # The one-time link is spent.
    r = anon.get(f"/pay/card/{token}")
    assert r.status_code == 410 and b"expired or was already used" in r.data
    # The success page confirms what is on file.
    r = anon.get(f"/pay/card/{token}/success")
    assert r.status_code == 200 and b"Visa ending 4242" in r.data
    r = client.get(f"/contacts/{S['pat_id']}")
    assert b"Visa ending 4242" in r.data and b"Remove card" in r.data


def test_portal_save_card_button(app, client, stripe_on):
    from app.extensions import db
    from app.models import Contact, PortalToken
    portal = app.test_client()
    with portal.session_transaction() as s:
        s["portal_contact_id"] = S["pat_id"]
    r = portal.get("/portal")
    assert r.status_code == 200 and b"Visa ending 4242" in r.data  # card on file shown
    with app.app_context():
        c = db.session.get(Contact, S["pat_id"])
        saved = (c.stripe_payment_method_id, c.card_brand, c.card_last4, c.card_authorised_on)
        c.stripe_payment_method_id = ""
        c.card_brand = c.card_last4 = ""
        c.card_authorised_on = None
        db.session.commit()
    r = portal.get("/portal")
    assert b"Save a card for automatic payments" in r.data
    from tests.helpers import _tok
    r = portal.post("/money/portal/card", data={"_csrf": _tok(r.data)})
    assert r.status_code == 302 and "/pay/card/" in r.headers["Location"]
    tok = r.headers["Location"].rsplit("/", 1)[1]
    with app.app_context():
        assert PortalToken.query.filter_by(token=tok, contact_id=S["pat_id"]).one().used_at is None
        c = db.session.get(Contact, S["pat_id"])
        c.stripe_payment_method_id, c.card_brand, c.card_last4, c.card_authorised_on = saved
        db.session.commit()
    r = portal.get(f"/pay/card/{tok}")
    assert r.status_code == 200 and b"I authorise" in r.data


def test_charge_card_on_file(app, client, stripe_on, monkeypatch):
    from app.extensions import db
    from app.models import Invoice, Payment, Firm, AuditLog
    inv_id, _ = _invoice(app, 100000, number="INV-P6-CHG1")
    r = client.get(f"/invoices/{inv_id}")
    assert r.status_code == 200
    assert b"Charge card on file" in r.data and b"plus a $30.00 card surcharge ($1,030.00 in total)" in r.data
    calls = _charge_recorder(monkeypatch, stripe_on)
    r = client.post(f"/money/charge/{inv_id}", data={"_csrf": S["tok"], "amount": "1,000.00"}, follow_redirects=True)
    assert r.status_code == 200 and b"Charged $1,000.00 to Visa ending 4242 plus a $30.00 surcharge" in r.data
    assert calls[0]["amount"] == 103000 and calls[0]["customer"] == "cus_p6" and calls[0]["pm"] == "pm_p6"
    assert calls[0]["meta"]["invoice_id"] == str(inv_id) and calls[0]["meta"]["surcharge_cents"] == "3000"
    with app.app_context():
        inv = db.session.get(Invoice, inv_id)
        assert inv.status == "paid" and inv.paid_cents == 100000
        p = Payment.query.filter_by(invoice_id=inv_id).one()
        assert p.method == "card" and p.account == "operating" and p.amount_cents == 100000
        assert p.surcharge_cents == 3000 and p.stripe_fee_cents == 30 and p.stripe_payment_intent == "pi_p6_1"
        assert AuditLog.query.filter_by(action="card_charged", entity="invoice", entity_id=inv_id).count() == 1

    # Surcharge off: the card is charged for exactly the amount.
    with app.app_context():
        Firm.get().surcharge_enabled = False
        db.session.commit()
    inv2_id, _ = _invoice(app, 50000, number="INV-P6-CHG2")
    r = client.post(f"/money/charge/{inv2_id}", data={"_csrf": S["tok"], "amount": "200.00"}, follow_redirects=True)
    assert r.status_code == 200 and b"Charged $200.00 to Visa ending 4242." in r.data
    assert calls[-1]["amount"] == 20000 and calls[-1]["meta"]["surcharge_cents"] == "0"
    with app.app_context():
        inv2 = db.session.get(Invoice, inv2_id)
        assert inv2.status == "partial" and inv2.paid_cents == 20000
        assert Payment.query.filter_by(invoice_id=inv2_id).one().surcharge_cents == 0
        Firm.get().surcharge_enabled = True
        db.session.commit()

    # Over the balance is refused before Stripe is called.
    n = len(calls)
    r = client.post(f"/money/charge/{inv2_id}", data={"_csrf": S["tok"], "amount": "999.00"}, follow_redirects=True)
    assert b"more than the balance" in r.data and len(calls) == n

    # A declined card writes nothing.
    _charge_recorder(monkeypatch, stripe_on, fail=True)
    with app.app_context():
        payments_before = Payment.query.count()
        events_before = len(db.session.get(Invoice, inv2_id).events)
        audits_before = AuditLog.query.filter_by(action="card_charged", entity="invoice", entity_id=inv2_id).count()
    r = client.post(f"/money/charge/{inv2_id}", data={"_csrf": S["tok"], "amount": "300.00"}, follow_redirects=True)
    assert r.status_code == 200 and b"The card was not charged: Your card was declined." in r.data
    with app.app_context():
        assert Payment.query.count() == payments_before
        inv2 = db.session.get(Invoice, inv2_id)
        assert inv2.paid_cents == 20000 and inv2.status == "partial" and len(inv2.events) == events_before
        assert AuditLog.query.filter_by(action="card_charged", entity="invoice", entity_id=inv2_id).count() == audits_before


def test_charge_refused_when_stripe_unset(app, client):
    from app.models import Payment
    inv_id, _ = _invoice(app, 40000, number="INV-P6-CHG3")
    with app.app_context():
        before = Payment.query.count()
    r = client.post(f"/money/charge/{inv_id}", data={"_csrf": S["tok"], "amount": "400.00"}, follow_redirects=True)
    assert b"Online payments are not configured" in r.data
    with app.app_context():
        assert Payment.query.count() == before


# ---------------------------------------------------------------------------
# 3. Payment plans
# ---------------------------------------------------------------------------
def test_auto_charge_plan_lifecycle(app, client, stripe_on, monkeypatch):
    from app.extensions import db
    from app.models import Invoice, PaymentPlan, Payment, AuditLog
    from app.blueprints.money import plan_schedule, run_payment_plans, advance_date
    inv_id, token = _invoice(app, 100001, number="INV-P6-PLAN1")
    r = client.get(f"/invoices/{inv_id}")
    assert b"Set up payment plan" in r.data
    # Validation: needs at least 2 installments.
    r = client.post("/money/plans/new", data={"_csrf": S["tok"], "invoice_id": inv_id, "installments": "1",
                                             "frequency": "monthly", "first_charge_on": TODAY.isoformat()},
                    follow_redirects=True)
    assert b"between 2 and 60" in r.data
    r = client.post("/money/plans/new", data={"_csrf": S["tok"], "invoice_id": inv_id, "installments": "3",
                                             "frequency": "monthly", "first_charge_on": TODAY.isoformat(),
                                             "auto_charge": "1"})
    assert r.status_code == 302 and "/money/plans/" in r.headers["Location"]
    plan_id = int(r.headers["Location"].rsplit("/", 1)[1])
    with app.app_context():
        plan = db.session.get(PaymentPlan, plan_id)
        assert plan.installment_cents == 33334 and plan.installments == 3 and plan.auto_charge is True
        assert plan.status == "active" and plan.next_charge_on == TODAY
        assert [amt for _, _, amt, _ in plan_schedule(plan)] == [33334, 33334, 33333]
    r = client.get(f"/money/plans/{plan_id}")
    assert r.status_code == 200 and b"$333.34" in r.data and b"$333.33" in r.data
    r = client.get("/money/plans")
    assert b"INV-P6-PLAN1" in r.data
    # A second plan on the same invoice is refused.
    r = client.post("/money/plans/new", data={"_csrf": S["tok"], "invoice_id": inv_id, "installments": "2",
                                             "frequency": "weekly", "first_charge_on": TODAY.isoformat()},
                    follow_redirects=True)
    assert b"already has a payment plan" in r.data

    calls = _charge_recorder(monkeypatch, stripe_on)
    with app.app_context():
        out = run_payment_plans(TODAY)
        assert out["charged"] == 1 and out["completed"] == 0
        out = run_payment_plans(TODAY)  # same day: idempotent
        assert out["charged"] == 0 and out["skipped"] == 0  # next_charge_on already moved on
        plan = db.session.get(PaymentPlan, plan_id)
        assert plan.paid_installments == 1 and plan.next_charge_on == advance_date(TODAY, "monthly")
        assert len(calls) == 1 and calls[0]["amount"] == 33334 + 1000  # 3% surcharge on 33334 = 1000.02 -> 1000
        inv = db.session.get(Invoice, inv_id)
        assert inv.paid_cents == 33334 and inv.status == "partial"
        assert AuditLog.query.filter_by(action="plan_charged", entity="payment_plan", entity_id=plan_id).count() == 1
        # Pretend the day arrived but a run already happened today: the audit row blocks a second charge.
        second = plan.next_charge_on
        out = run_payment_plans(second)
        assert out["charged"] == 1
        plan = db.session.get(PaymentPlan, plan_id)
        db.session.add(AuditLog(action="plan_charged", entity="payment_plan", entity_id=plan_id,
                                detail=plan.next_charge_on.isoformat()))
        db.session.commit()
        out = run_payment_plans(plan.next_charge_on)
        assert out["charged"] == 0 and out["skipped"] == 1 and len(calls) == 2
        AuditLog.query.filter_by(action="plan_charged", detail=plan.next_charge_on.isoformat()).delete()
        db.session.commit()
        out = run_payment_plans(plan.next_charge_on)
        assert out["charged"] == 1 and out["completed"] == 1
        plan = db.session.get(PaymentPlan, plan_id)
        inv = db.session.get(Invoice, inv_id)
        assert plan.status == "completed" and plan.paid_installments == 3
        assert inv.status == "paid" and inv.balance_cents == 0
        assert [c["amount"] for c in calls] == [34334, 34334, 34333]  # 33334+1000, 33334+1000, 33333+1000
        assert [p.amount_cents for p in Payment.query.filter_by(invoice_id=inv_id).order_by(Payment.id)] == [33334, 33334, 33333]
    r = client.get(f"/money/plans/{plan_id}")
    assert b"completed" in r.data
    r = client.get("/money/plans?status=completed")
    assert b"INV-P6-PLAN1" in r.data


def test_non_auto_plan_reminder_once(app, client, stripe_on):
    from app.extensions import db
    from app.models import PaymentPlan
    from app.blueprints.money import run_payment_plans
    inv_id, token = _invoice(app, 60000, number="INV-P6-PLAN2")
    r = client.post("/money/plans/new", data={"_csrf": S["tok"], "invoice_id": inv_id, "installments": "2",
                                             "frequency": "biweekly", "first_charge_on": TODAY.isoformat()})
    assert r.status_code == 302
    plan_id = int(r.headers["Location"].rsplit("/", 1)[1])
    with app.app_context():
        assert db.session.get(PaymentPlan, plan_id).auto_charge is False
        before = len(_outbox("Payment of $300.00 due"))
        out = run_payment_plans(TODAY)
        assert out["reminded"] == 1 and out["charged"] == 0
        mails = _outbox("Payment of $300.00 due")
        assert len(mails) == before + 1 and mails[0]["to"] == "pat@example.com"
        link = f"http://coil.test/pay/plan/{plan_id}/{token}"
        assert link in mails[0]["html"] and f"/p/{token}" in mails[0]["html"]
        out = run_payment_plans(TODAY)
        assert out["reminded"] == 0 and len(_outbox("Payment of $300.00 due")) == before + 1
        plan = db.session.get(PaymentPlan, plan_id)
        assert plan.next_charge_on == TODAY + timedelta(days=14)
    # The pay link shows the installment with the card surcharge, and ACH without.
    anon = app.test_client()
    r = anon.get(f"/pay/plan/{plan_id}/{token}")
    assert r.status_code == 200 and b"Pay installment 1 of 2" in r.data and b"$300.00" in r.data and b"$309.00" in r.data
    r = anon.get(f"/pay/plan/{plan_id}/{token}?method=ach")
    assert b"$309.00" not in r.data
    r = anon.get(f"/pay/plan/{plan_id}/not-the-token")
    assert r.status_code == 404
    # Portal lists the plan with the next amount and date.
    portal = app.test_client()
    with portal.session_transaction() as s:
        s["portal_contact_id"] = S["pat_id"]
    r = portal.get("/portal")
    assert b"INV-P6-PLAN2" in r.data and b"$300.00" in r.data and b"Pay now" in r.data
    S["plan2_id"] = plan_id


def test_pause_resume_cancel(app, client, stripe_on, monkeypatch):
    from app.extensions import db
    from app.models import PaymentPlan, Payment
    from app.blueprints.money import run_payment_plans
    inv_id, _ = _invoice(app, 90000, number="INV-P6-PLAN3")
    r = client.post("/money/plans/new", data={"_csrf": S["tok"], "invoice_id": inv_id, "installments": "3",
                                             "frequency": "weekly", "first_charge_on": TODAY.isoformat(),
                                             "auto_charge": "1"})
    plan_id = int(r.headers["Location"].rsplit("/", 1)[1])
    calls = _charge_recorder(monkeypatch, stripe_on)
    r = client.post(f"/money/plans/{plan_id}/pause", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert b"Plan paused" in r.data
    with app.app_context():
        assert db.session.get(PaymentPlan, plan_id).status == "paused"
        out = run_payment_plans(TODAY)
        assert calls == [] and Payment.query.filter_by(invoice_id=inv_id).count() == 0
    r = client.post(f"/money/plans/{plan_id}/resume", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert b"Plan resumed" in r.data
    with app.app_context():
        out = run_payment_plans(TODAY)
        assert out["charged"] == 1 and calls[0]["amount"] == 30000 + 900
        assert db.session.get(PaymentPlan, plan_id).paid_installments == 1
    # Charge now from the detail page takes the next installment regardless of the date.
    r = client.post(f"/money/plans/{plan_id}/charge", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert b"Charged installment 2 of 3" in r.data and len(calls) == 2
    r = client.post(f"/money/plans/{plan_id}/cancel", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert b"Plan cancelled" in r.data
    with app.app_context():
        plan = db.session.get(PaymentPlan, plan_id)
        assert plan.status == "cancelled"
        out = run_payment_plans(TODAY + timedelta(days=30))
        assert len(calls) == 2
        assert plan.invoice.balance_cents == 30000  # stays due
    r = client.post(f"/money/plans/{plan_id}/resume", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert b"cannot be resumed" in r.data


def test_failed_auto_charge_marks_plan_and_emails_firm(app, client, stripe_on, monkeypatch):
    from app.extensions import db
    from app.models import PaymentPlan, Payment
    from app.blueprints.money import run_payment_plans
    inv_id, _ = _invoice(app, 20000, number="INV-P6-PLAN4")
    r = client.post("/money/plans/new", data={"_csrf": S["tok"], "invoice_id": inv_id, "installments": "2",
                                             "frequency": "monthly", "first_charge_on": TODAY.isoformat(),
                                             "auto_charge": "1"})
    plan_id = int(r.headers["Location"].rsplit("/", 1)[1])
    _charge_recorder(monkeypatch, stripe_on, fail=True)
    with app.app_context():
        out = run_payment_plans(TODAY)
        assert out["failed"] == 1
        plan = db.session.get(PaymentPlan, plan_id)
        assert plan.status == "failed" and "declined" in plan.last_error
        assert Payment.query.filter_by(invoice_id=inv_id).count() == 0
        mails = _outbox("Payment plan charge failed")
        assert mails and mails[0]["to"] == "firm@example.com" and "declined" in mails[0]["html"]
    r = client.get("/money/plans?status=failed")
    assert b"INV-P6-PLAN4" in r.data and b"declined" in r.data


def test_remove_card_switches_plans_to_reminders(app, client, stripe_on):
    from app.extensions import db
    from app.models import Contact, PaymentPlan, AuditLog
    inv_id, _ = _invoice(app, 30000, number="INV-P6-PLAN5")
    r = client.post("/money/plans/new", data={"_csrf": S["tok"], "invoice_id": inv_id, "installments": "2",
                                             "frequency": "monthly", "first_charge_on": TODAY.isoformat(),
                                             "auto_charge": "1"})
    plan_id = int(r.headers["Location"].rsplit("/", 1)[1])
    r = client.post(f"/money/cards/{S['pat_id']}/remove", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert r.status_code == 200 and b"Removed Visa ending 4242" in r.data
    with app.app_context():
        c = db.session.get(Contact, S["pat_id"])
        assert c.stripe_payment_method_id == "" and c.card_brand == "" and c.card_last4 == ""
        assert c.card_authorised_on is None and c.stripe_customer_id == "cus_p6"  # customer id is kept
        assert db.session.get(PaymentPlan, plan_id).auto_charge is False
        assert AuditLog.query.filter_by(action="card_removed", entity="contact", entity_id=S["pat_id"]).count() == 1
    r = client.get(f"/contacts/{S['pat_id']}")
    assert b"Request card on file" in r.data
    # Auto-charge cannot be chosen without a card.
    inv2_id, _ = _invoice(app, 30000, number="INV-P6-PLAN6")
    r = client.post("/money/plans/new", data={"_csrf": S["tok"], "invoice_id": inv2_id, "installments": "2",
                                             "frequency": "monthly", "first_charge_on": TODAY.isoformat(),
                                             "auto_charge": "1"}, follow_redirects=True)
    assert b"needs a card on file" in r.data


def test_cli_entry_point(app):
    from app import cli
    with app.app_context():
        out = cli.run_payment_plans(TODAY)
        assert set(out) == {"charged", "reminded", "failed", "completed", "skipped"}
    assert cli.main(["nope"]) == 2
