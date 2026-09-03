"""Smoke test for trust, payments and portal. Seeds a fresh SQLite DB in a temp dir via seed.py."""
import json
import os
import subprocess
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("modc")
    dbfile = tmp / "test.db"
    uri = f"sqlite:///{dbfile}"
    env = dict(os.environ, DATABASE_URL=uri, STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="", SMTP_HOST="")
    subprocess.run([sys.executable, "seed.py"], cwd=ROOT, env=env, check=True)
    from app import create_app
    application = create_app({"SQLALCHEMY_DATABASE_URI": uri, "TESTING": True, "STRIPE_SECRET_KEY": "",
                              "STRIPE_WEBHOOK_SECRET": "", "SMTP_HOST": "", "UPLOAD_DIR": str(tmp / "uploads")})
    return application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    login(c)
    return c


def csrf(client):
    """auth.login clears the session on success, so mint a fresh token by rendering a page with a form."""
    client.get("/trust/new")
    with client.session_transaction() as s:
        return s["_csrf"]


def test_module_c_flow(app, client):
    from app.extensions import db
    from app.models import (Contact, Matter, Invoice, InvoiceLine, Payment, TrustTransaction,
                            TrustReconciliation, PortalToken, Firm)

    with app.app_context():
        maria = Contact.query.filter_by(last_name="Alvarez").first()
        blue = Contact.query.filter_by(company_name="Bluebonnet Logistics LLC").first()
        m1002 = Matter.query.filter_by(number="M-1002").first()
        maria_id, blue_id, m1002_id = maria.id, blue.id, m1002.id
        assert Firm.get().surcharge_enabled is True

    tok = csrf(client)
    today = date.today().isoformat()

    # 1. Trust deposit for Maria.
    r = client.post("/trust/new", data={"_csrf": tok, "type": "deposit", "client_id": maria_id, "matter_id": "",
                                        "date": today, "amount": "1,000.00", "description": "Retainer"})
    assert r.status_code == 302, r.data[:300]
    with app.app_context():
        assert db.session.get(Contact, maria_id).trust_balance_cents() == 100000
        before = TrustTransaction.query.count()

    # 2. Disbursement larger than her balance is rejected and nothing is written.
    r = client.post("/trust/new", data={"_csrf": tok, "type": "disbursement", "client_id": maria_id, "matter_id": "",
                                        "date": today, "amount": "1,500.00", "description": "Too much",
                                        "payee": "County clerk"})
    assert r.status_code == 200
    assert b"Rejected" in r.data
    with app.app_context():
        assert TrustTransaction.query.count() == before
        assert db.session.get(Contact, maria_id).trust_balance_cents() == 100000

    # 3. Valid disbursement.
    r = client.post("/trust/new", data={"_csrf": tok, "type": "disbursement", "client_id": maria_id, "matter_id": "",
                                        "date": today, "amount": "250.00", "description": "Filing fee",
                                        "payee": "County clerk", "reference": "1001"})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(Contact, maria_id).trust_balance_cents() == 75000

    # 4. Invoice on M-1002, apply 60000 from Bluebonnet's trust.
    with app.app_context():
        inv = Invoice(number="INV-TEST-1", matter_id=m1002_id, client_id=blue_id, kind="hourly", status="sent",
                      issued_on=date.today(), due_on=date.today(), subtotal_cents=100000, total_cents=100000)
        inv.lines.append(InvoiceLine(kind="flat", description="Services", quantity=1.0, unit_cents=100000,
                                     amount_cents=100000))
        db.session.add(inv)
        db.session.commit()
        inv_id = inv.id
    r = client.post("/trust/apply", data={"_csrf": tok, "invoice_id": inv_id, "amount": "600.00"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/invoices/{inv_id}")
    with app.app_context():
        inv = db.session.get(Invoice, inv_id)
        assert inv.paid_cents == 60000
        assert inv.status == "partial"
        t = TrustTransaction.query.filter_by(invoice_id=inv_id, type="to_operating").first()
        assert t is not None and t.amount_cents == -60000
        assert db.session.get(Contact, blue_id).trust_balance_cents() == 440000
        pay = Payment.query.filter_by(invoice_id=inv_id, method="trust").first()
        assert pay and pay.amount_cents == 60000 and pay.account == "operating"
        assert t.payment_id == pay.id
        uncleared_ids = [x.id for x in TrustTransaction.query.filter_by(cleared=False).all()]

    # Over-apply is rejected.
    r = client.post("/trust/apply", data={"_csrf": tok, "invoice_id": inv_id, "amount": "999,999.00"})
    with app.app_context():
        assert db.session.get(Invoice, inv_id).paid_cents == 60000

    # 5. Clear everything, then reconcile with bank == book (balanced) and a wrong figure (not balanced).
    for tid in uncleared_ids:
        r = client.post(f"/trust/{tid}/clear", data={"_csrf": tok})
        assert r.status_code == 302
    with app.app_context():
        assert TrustTransaction.query.filter_by(cleared=False).count() == 0
        book = sum(x.amount_cents for x in TrustTransaction.query.all())
        assert book == 440000 + 75000
    r = client.post("/trust/reconcile", data={"_csrf": tok, "period_end": today, "bank_statement": f"{book/100:.2f}"})
    assert r.status_code == 302
    with app.app_context():
        rec = TrustReconciliation.query.order_by(TrustReconciliation.id.desc()).first()
        assert rec.balanced is True
        assert rec.book_balance_cents == rec.client_ledgers_cents == rec.adjusted_bank_cents == book
        detail = json.loads(rec.detail_json)
        assert {d["balance_cents"] for d in detail["clients"]} == {440000, 75000}
        rec_id = rec.id
    r = client.get(f"/trust/reconcile/{rec_id}")
    assert r.status_code == 200 and b"Balanced" in r.data
    r = client.post("/trust/reconcile", data={"_csrf": tok, "period_end": today, "bank_statement": "5,000.00"})
    assert r.status_code == 302
    with app.app_context():
        rec2 = TrustReconciliation.query.order_by(TrustReconciliation.id.desc()).first()
        assert rec2.balanced is False and rec2.id != rec_id
        rec2_id = rec2.id
    r = client.get(f"/trust/reconcile/{rec2_id}")
    assert b"Out of balance" in r.data
    r = client.get("/trust/")
    assert r.status_code == 200 and b"Bluebonnet" in r.data
    r = client.get(f"/trust/ledger/{maria_id}")
    assert r.status_code == 200 and b"Filing fee" in r.data

    # 6. Manual check for the remaining 40000 -> paid.
    r = client.post("/payments/record", data={"_csrf": tok, "invoice_id": inv_id, "amount": "400.00",
                                              "method": "check", "received_on": today, "reference": "2044"})
    assert r.status_code == 302
    with app.app_context():
        inv = db.session.get(Invoice, inv_id)
        assert inv.status == "paid" and inv.paid_cents == 100000
    r = client.get("/payments")
    assert r.status_code == 200
    r = client.get(f"/payments?month={today[:7]}")
    assert r.status_code == 200 and b"check" in r.data

    # 7. Public pay page: card shows a 3% surcharge of $30.00, ACH shows none.
    with app.app_context():
        inv2 = Invoice(number="INV-TEST-2", matter_id=m1002_id, client_id=blue_id, kind="hourly", status="sent",
                       issued_on=date.today(), due_on=date.today(), subtotal_cents=100000, total_cents=100000)
        inv2.lines.append(InvoiceLine(kind="flat", description="Services", quantity=1.0, unit_cents=100000,
                                      amount_cents=100000))
        db.session.add(inv2)
        db.session.commit()
        inv2_id, token2 = inv2.id, inv2.public_token
    anon = app.test_client()
    r = anon.get(f"/pay/{token2}?method=card")
    assert r.status_code == 200
    assert b"Card processing surcharge 3%" in r.data and b"$30.00" in r.data and b"$1,030.00" in r.data
    r = anon.get(f"/pay/{token2}?method=ach")
    assert r.status_code == 200
    assert b"Card processing surcharge" not in r.data and b"$30.00" not in r.data and b"$1,000.00" in r.data
    # POST with Stripe unset renders the not-configured page rather than erroring.
    r = anon.post(f"/pay/{token2}?method=card")
    assert r.status_code == 200 and b"not set up" in r.data and b"Austin" in r.data
    r = anon.get(f"/pay/{token2}/cancel")
    assert r.status_code == 200 and f"/p/{token2}".encode() in r.data
    r = anon.get(f"/pay/{token2}/success")
    assert r.status_code == 200
    r = anon.get("/pay/deposit/success")
    assert r.status_code == 200

    # 8. Stripe webhook (no secret): idempotent on the checkout session id.
    event = {"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_test_modc_1", "object": "checkout.session", "payment_status": "paid",
        "payment_intent": "pi_test_modc_1", "amount_total": 103000, "payment_method_types": ["card"],
        "metadata": {"kind": "invoice", "invoice_id": str(inv2_id), "surcharge_cents": "3000", "method": "card"}}}}
    for _ in range(2):
        r = anon.post("/webhooks/stripe", data=json.dumps(event), content_type="application/json")
        assert r.status_code == 200
    with app.app_context():
        ps = Payment.query.filter_by(stripe_checkout_session="cs_test_modc_1").all()
        assert len(ps) == 1
        assert ps[0].amount_cents == 100000 and ps[0].surcharge_cents == 3000 and ps[0].method == "card"
        inv2 = db.session.get(Invoice, inv2_id)
        assert inv2.status == "paid" and inv2.paid_cents == 100000
    r = anon.get(f"/pay/{token2}?method=card")
    assert r.status_code == 200 and b"is paid" in r.data

    # Trust deposit via webhook (ACH completed while pending is ignored, then settles).
    dep = {"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_test_modc_dep", "payment_status": "unpaid", "payment_intent": "pi_test_modc_dep",
        "amount_total": 20000, "payment_method_types": ["us_bank_account"],
        "metadata": {"kind": "trust_deposit", "client_id": str(maria_id), "matter_id": "", "amount_cents": "20000"}}}}
    r = anon.post("/webhooks/stripe", data=json.dumps(dep), content_type="application/json")
    assert r.status_code == 200
    with app.app_context():
        assert db.session.get(Contact, maria_id).trust_balance_cents() == 75000
    dep["type"] = "checkout.session.async_payment_succeeded"
    dep["data"]["object"]["payment_status"] = "paid"
    for _ in range(2):
        r = anon.post("/webhooks/stripe", data=json.dumps(dep), content_type="application/json")
        assert r.status_code == 200
    with app.app_context():
        assert db.session.get(Contact, maria_id).trust_balance_cents() == 95000
        p = Payment.query.filter_by(stripe_checkout_session="cs_test_modc_dep").one()
        assert p.account == "trust" and p.method == "ach" and p.invoice_id is None
        t = TrustTransaction.query.filter_by(payment_id=p.id).one()
        assert t.type == "deposit" and t.amount_cents == 20000 and t.cleared is False

    # 9. Portal magic link.
    portal = app.test_client()
    r = portal.post("/portal/login", data={"email": "MARIA@example.com"})
    assert r.status_code == 200 and b"If we have that email on file" in r.data
    r = portal.post("/portal/login", data={"email": "nobody@example.com"})
    assert r.status_code == 200 and b"If we have that email on file" in r.data
    with app.app_context():
        pt = PortalToken.query.filter_by(contact_id=maria_id).order_by(PortalToken.id.desc()).first()
        assert pt is not None
        ptoken = pt.token
        assert PortalToken.query.count() == 1  # unknown email creates nothing
    r = portal.get("/portal")
    assert r.status_code == 302 and "/portal/login" in r.headers["Location"]
    r = portal.get(f"/portal/auth/{ptoken}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/portal")
    r = portal.get("/portal")
    assert r.status_code == 200
    assert b"M-1001" in r.data and b"Maria" in r.data
    assert b"$950.00" in r.data  # trust balance shown
    # Token is single-use.
    r = portal.get(f"/portal/auth/{ptoken}")
    assert r.status_code == 410
    # Invoice belonging to another client is a 404; upload works.
    r = portal.get(f"/portal/invoices/{inv2_id}")
    assert r.status_code == 404
    with app.app_context():
        m1001_id = Matter.query.filter_by(number="M-1001").first().id
    from io import BytesIO
    r = portal.post("/portal/upload", data={"matter_id": m1001_id, "file": (BytesIO(b"hello"), "id-card.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        from app.models import Document
        d = Document.query.filter_by(matter_id=m1001_id, uploaded_by_client=True).first()
        assert d and d.shared_to_portal and d.size == 5
        doc_id = d.id
    r = portal.get(f"/portal/documents/{doc_id}/download")
    assert r.status_code == 200 and r.data == b"hello"
    r = portal.post("/portal/logout")
    assert r.status_code == 302
    r = portal.get("/portal")
    assert r.status_code == 302

    # Rate limit: no more than 3 tokens per contact per 15 minutes.
    for _ in range(5):
        portal.post("/portal/login", data={"email": "maria@example.com"})
    with app.app_context():
        assert PortalToken.query.filter_by(contact_id=maria_id).count() == 3
