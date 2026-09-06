"""Regression tests for the two high-severity audit findings.

1. money.json / /trust/: trust funds earmarked to one matter were silently spent on another
   matter's invoice, because /trust/apply fell through to the pooled client balance and wrote
   the withdrawal with matter_id=None.
2. docs.json / /pi: compute_worksheet deducted every Expense on the matter, including costs
   already billed to and collected from the client, so the net to client was short by that amount.

Own SQLite file (data/test_audit_fixes_trust.db), own UPLOAD_DIR and PDF_DIR. Never touches
data/practice.db.  Run: .venv/bin/python -m pytest tests/test_audit_fixes_trust.py -q
"""
import os
import shutil
import subprocess
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_audit_fixes_trust.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_audit_fixes_trust")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_audit_fixes_trust")

from tests.helpers import login  # noqa: E402


@pytest.fixture(scope="module")
def app():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    shutil.rmtree(PDF_DIR, ignore_errors=True)
    env = dict(os.environ, DATABASE_URL=DB_URI, STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="", SMTP_HOST="")
    out = subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], env=env, cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    from app import create_app
    return create_app({"SQLALCHEMY_DATABASE_URI": DB_URI, "UPLOAD_DIR": UPLOAD_DIR, "PDF_DIR": PDF_DIR,
                       "TESTING": True, "STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": "", "SMTP_HOST": ""})


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    login(c)
    return c


def csrf(client):
    client.get("/trust/new")
    with client.session_transaction() as s:
        return s["_csrf"]


def _models():
    from app.extensions import db
    from app import models
    return db, models


def _make_client_two_matters(app, tag):
    """One client, two matters, no trust activity yet. Returns (client_id, matter_a_id, matter_b_id)."""
    db, M = _models()
    with app.app_context():
        u = M.User.query.first()
        c = M.Contact(first_name="Cross", last_name=f"Matter{tag}", email=f"cross{tag}@example.test",
                      is_client=True)
        db.session.add(c)
        db.session.flush()
        a = M.Matter(number=f"M-XA{tag}", client_id=c.id, name=f"TEST matter A {tag}", billing_type="hourly",
                     responsible_user_id=u.id, status="open")
        b = M.Matter(number=f"M-XB{tag}", client_id=c.id, name=f"TEST matter B {tag}", billing_type="hourly",
                     responsible_user_id=u.id, status="open")
        db.session.add_all([a, b])
        db.session.commit()
        return c.id, a.id, b.id


def _sent_invoice(app, client_id, matter_id, cents, number):
    db, M = _models()
    with app.app_context():
        inv = M.Invoice(number=number, matter_id=matter_id, client_id=client_id, kind="hourly", status="sent",
                        issued_on=date.today(), due_on=date.today(), subtotal_cents=cents, total_cents=cents)
        inv.lines.append(M.InvoiceLine(kind="flat", description="Services", quantity=1.0, unit_cents=cents,
                                       amount_cents=cents))
        db.session.add(inv)
        db.session.commit()
        return inv.id


def _deposit(app, client_id, matter_id, cents, desc="Deposit"):
    db, M = _models()
    with app.app_context():
        db.session.add(M.TrustTransaction(client_id=client_id, matter_id=matter_id, date=date.today(),
                                          type="deposit", amount_cents=cents, description=desc,
                                          created_by_id=M.User.query.first().id))
        db.session.commit()


# ------------------------------------------------------------------ defect 1


def test_earmarked_matter_funds_cannot_pay_another_matters_invoice(app, client):
    """The audit repro exactly: 100000c held for matter A, an 80000c invoice on matter B."""
    db, M = _models()
    cid, a_id, b_id = _make_client_two_matters(app, "1")
    _deposit(app, cid, a_id, 100000, "Retainer for matter A")
    inv_id = _sent_invoice(app, cid, b_id, 80000, "INV-XM-1")
    tok = csrf(client)

    r = client.post("/trust/apply", data={"_csrf": tok, "invoice_id": inv_id, "amount": "800.00"},
                    follow_redirects=True)
    assert r.status_code == 200
    body = r.data.decode()
    assert "$0.00" in body and "$800.00" in body, body[:400]

    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.paid_cents == 0
        assert inv.status == "sent"
        assert M.TrustTransaction.query.filter_by(invoice_id=inv_id).count() == 0
        assert M.Payment.query.filter_by(invoice_id=inv_id).count() == 0
        # matter A keeps every cent, and the client ledger never went below the matter ledgers
        assert db.session.get(M.Matter, a_id).trust_balance_cents() == 100000
        assert db.session.get(M.Matter, b_id).trust_balance_cents() == 0
        assert db.session.get(M.Contact, cid).trust_balance_cents() == 100000


def test_apply_draws_matter_funds_first_then_unallocated(app, client):
    """A partial draw spanning both sources writes two ledger rows and one payment."""
    db, M = _models()
    cid, a_id, b_id = _make_client_two_matters(app, "2")
    _deposit(app, cid, b_id, 30000, "Retainer for matter B")
    _deposit(app, cid, None, 50000, "General retainer")
    inv_id = _sent_invoice(app, cid, b_id, 70000, "INV-XM-2")
    tok = csrf(client)

    r = client.post("/trust/apply", data={"_csrf": tok, "invoice_id": inv_id, "amount": "700.00"})
    assert r.status_code == 302, r.data[:300]

    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.paid_cents == 70000 and inv.status == "paid"
        txns = M.TrustTransaction.query.filter_by(invoice_id=inv_id, type="to_operating").order_by(
            M.TrustTransaction.id).all()
        assert len(txns) == 2, [(t.matter_id, t.amount_cents) for t in txns]
        by_matter = {t.matter_id: t.amount_cents for t in txns}
        assert by_matter == {b_id: -30000, None: -40000}
        pays = M.Payment.query.filter_by(invoice_id=inv_id).all()
        assert len(pays) == 1 and pays[0].amount_cents == 70000
        assert pays[0].method == "trust" and pays[0].account == "operating"
        assert all(t.payment_id == pays[0].id for t in txns)
        assert db.session.get(M.Matter, b_id).trust_balance_cents() == 0
        assert db.session.get(M.Contact, cid).trust_balance_cents() == 10000
        # no matter ledger exceeds what the client actually holds
        assert db.session.get(M.Matter, a_id).trust_balance_cents() == 0


def test_apply_refusal_names_both_figures_when_short(app, client):
    db, M = _models()
    cid, a_id, b_id = _make_client_two_matters(app, "3")
    _deposit(app, cid, a_id, 90000, "Earmarked to A")
    _deposit(app, cid, b_id, 10000, "Earmarked to B")
    _deposit(app, cid, None, 5000, "Unallocated")
    inv_id = _sent_invoice(app, cid, b_id, 60000, "INV-XM-3")
    tok = csrf(client)

    r = client.post("/trust/apply", data={"_csrf": tok, "invoice_id": inv_id, "amount": "600.00"},
                    follow_redirects=True)
    body = r.data.decode()
    assert "$100.00" in body, body[:600]   # held for the matter
    assert "$50.00" in body, body[:600]    # unallocated for the client
    assert "$150.00" in body, body[:600]   # available in total
    with app.app_context():
        assert db.session.get(M.Invoice, inv_id).paid_cents == 0
        assert M.TrustTransaction.query.filter_by(invoice_id=inv_id).count() == 0

    # the full available amount does go through
    r = client.post("/trust/apply", data={"_csrf": tok, "invoice_id": inv_id, "amount": "150.00"})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(M.Invoice, inv_id).paid_cents == 15000
        assert db.session.get(M.Matter, b_id).trust_balance_cents() == 0
        assert db.session.get(M.Matter, a_id).trust_balance_cents() == 90000
        assert db.session.get(M.Contact, cid).trust_balance_cents() == 90000


def test_reconciliation_still_balances_after_a_split_draw(app, client):
    db, M = _models()
    tok = csrf(client)
    with app.app_context():
        for t in M.TrustTransaction.query.filter_by(cleared=False).all():
            t.cleared = True
            t.cleared_on = date.today()
        db.session.commit()
        book = sum(t.amount_cents for t in M.TrustTransaction.query.all())
    r = client.post("/trust/reconcile", data={"_csrf": tok, "period_end": date.today().isoformat(),
                                              "bank_statement": f"{book / 100:.2f}"})
    assert r.status_code == 302
    with app.app_context():
        rec = M.TrustReconciliation.query.order_by(M.TrustReconciliation.id.desc()).first()
        assert rec.balanced is True
        assert rec.book_balance_cents == book == rec.client_ledgers_cents
        # and the ledgers really are truthful: no client's matter ledgers exceed their own balance
        from app.blueprints.trust import client_balances, matter_balances
        cb, mb = client_balances(), matter_balances()
        by_client = {}
        for m in M.Matter.query.all():
            v = mb.get(m.id, 0)
            if v > 0:
                by_client[m.client_id] = by_client.get(m.client_id, 0) + v
        for client_id, allocated in by_client.items():
            assert allocated <= cb.get(client_id, 0), f"client {client_id} over-allocated"


def test_overview_and_ledger_show_unallocated_and_warn_when_over_allocated(app, client):
    """Imported data can leave matter ledgers above the client total. Both pages must say so."""
    db, M = _models()
    cid, a_id, b_id = _make_client_two_matters(app, "4")
    _deposit(app, cid, a_id, 40000, "Earmarked to A")
    _deposit(app, cid, None, 25000, "Unallocated")

    r = client.get(f"/trust/ledger/{cid}")
    assert r.status_code == 200
    assert "Unallocated" in r.data.decode()
    assert "$250.00" in r.data.decode()

    r = client.get("/trust/")
    assert r.status_code == 200 and "$250.00" in r.data.decode()

    # now force the over-allocated state a bad import produces: a client-level withdrawal
    # that leaves the matter ledgers above the client total.
    with app.app_context():
        db.session.add(M.TrustTransaction(client_id=cid, matter_id=None, date=date.today(), type="disbursement",
                                          amount_cents=-40000, description="Imported adjustment",
                                          created_by_id=M.User.query.first().id))
        db.session.commit()

    r = client.get(f"/trust/ledger/{cid}")
    assert r.status_code == 200
    assert "more than the client's balance" in r.data.decode(), r.data.decode()[:1500]
    r = client.get("/trust/")
    assert r.status_code == 200
    assert "more than the client's balance" in r.data.decode()


def test_matter_tagged_disbursement_cannot_spend_another_matters_money(app, client):
    db, M = _models()
    cid, a_id, b_id = _make_client_two_matters(app, "5")
    _deposit(app, cid, a_id, 70000, "Earmarked to A")
    tok = csrf(client)
    before = None
    with app.app_context():
        before = M.TrustTransaction.query.count()
    r = client.post("/trust/new", data={"_csrf": tok, "type": "disbursement", "client_id": cid, "matter_id": b_id,
                                        "date": date.today().isoformat(), "amount": "300.00",
                                        "description": "Filing fee on B", "payee": "County clerk"})
    assert r.status_code == 200 and b"Rejected" in r.data
    with app.app_context():
        assert M.TrustTransaction.query.count() == before

    # a client-level disbursement may not reach into matter A's earmarked funds either
    r = client.post("/trust/new", data={"_csrf": tok, "type": "disbursement", "client_id": cid, "matter_id": "",
                                        "date": date.today().isoformat(), "amount": "300.00",
                                        "description": "General disbursement", "payee": "County clerk"})
    assert r.status_code == 200 and b"Rejected" in r.data
    with app.app_context():
        assert M.TrustTransaction.query.count() == before
        assert db.session.get(M.Matter, a_id).trust_balance_cents() == 70000


# ------------------------------------------------------------------ defect 2


def _pi_matter(app, tag):
    """A PI matter with one billed expense (125000c) and one unbilled expense (32550c)."""
    db, M = _models()
    with app.app_context():
        u = M.User.query.first()
        c = M.Contact(first_name="Worksheet", last_name=f"Client{tag}", email=f"ws{tag}@example.test",
                      is_client=True, address="9 Test Way\nAustin, TX 78701")
        db.session.add(c)
        db.session.flush()
        m = M.Matter(number=f"M-WS{tag}", client_id=c.id, name=f"TEST hybrid PI {tag}",
                     practice_area="Personal Injury", billing_type="hybrid", contingency_pct=33.33,
                     responsible_user_id=u.id, status="open")
        db.session.add(m)
        db.session.flush()
        inv = M.Invoice(number=f"INV-WS{tag}", matter_id=m.id, client_id=c.id, kind="hourly", status="paid",
                        issued_on=date.today(), due_on=date.today(), subtotal_cents=125000,
                        total_cents=125000, paid_cents=125000)
        db.session.add(inv)
        db.session.flush()
        billed = M.Expense(matter_id=m.id, user_id=u.id, date=date.today(), description="Filing fee (billed)",
                           amount_cents=125000, category="Court", billable=True, invoice_id=inv.id)
        unbilled = M.Expense(matter_id=m.id, user_id=u.id, date=date.today(), description="Records fee (unbilled)",
                             amount_cents=32550, category="Records", billable=True)
        db.session.add_all([billed, unbilled])
        db.session.commit()
        return m.id, c.id, inv.id, inv.number, billed.id, unbilled.id


def test_compute_worksheet_excludes_costs_already_billed(app):
    db, M = _models()
    m_id, c_id, inv_id, inv_number, billed_id, unbilled_id = _pi_matter(app, "1")
    with app.app_context():
        from app.blueprints.pi import compute_worksheet
        m = db.session.get(M.Matter, m_id)
        d = compute_worksheet(m, 1000000, 0)
        counted = [e["id"] for e in d["expenses"]]
        assert counted == [unbilled_id], counted
        assert d["costs"] == 32550
        assert d["expense_ids"] == [unbilled_id]
        excluded = {e["id"]: e for e in d["excluded_expenses"]}
        assert billed_id in excluded
        assert excluded[billed_id]["invoice_number"] == inv_number
        assert d["net"] == 1000000 - 32550
        assert d["balanced"] is True

        # voiding the invoice puts the cost back in the default set
        db.session.get(M.Invoice, inv_id).status = "void"
        db.session.commit()
        d2 = compute_worksheet(m, 1000000, 0)
        assert sorted(e["id"] for e in d2["expenses"]) == sorted([billed_id, unbilled_id])
        assert d2["costs"] == 157550
        db.session.get(M.Invoice, inv_id).status = "paid"
        db.session.commit()


def test_worksheet_form_lists_billed_costs_unticked_and_honours_the_choice(app, client):
    db, M = _models()
    m_id, c_id, inv_id, inv_number, billed_id, unbilled_id = _pi_matter(app, "2")
    tok = csrf(client)
    r = client.get(f"/pi/{m_id}")
    assert r.status_code == 200
    body = r.data.decode()
    assert f'name="expense_id" value="{billed_id}"' in body
    assert f'name="expense_id" value="{unbilled_id}"' in body
    assert f"already billed on invoice {inv_number}" in body
    # the billed one is offered unticked; the unbilled one ticked
    billed_tag = body.split(f'name="expense_id" value="{billed_id}"')[1][:40]
    unbilled_tag = body.split(f'name="expense_id" value="{unbilled_id}"')[1][:40]
    assert "checked" not in billed_tag
    assert "checked" in unbilled_tag

    # default save: only the unbilled cost is deducted
    r = client.post(f"/pi/{m_id}/worksheet", data={"_csrf": tok, "gross": "100,000.00", "fee_pct": "33.33",
                                                   "expenses_listed": "1", "expense_id": [str(unbilled_id)]})
    assert r.status_code == 302
    with app.app_context():
        import json
        ws = M.SettlementWorksheet.query.filter_by(matter_id=m_id, is_current=True).first()
        assert ws.costs_cents == 32550
        d = json.loads(ws.detail_json)
        assert d["expense_ids"] == [unbilled_id]
        parts = (ws.fee_cents + ws.costs_cents + ws.liens_cents + ws.other_deductions_cents
                 + ws.net_to_client_cents)
        assert parts == ws.gross_cents == 10000000

    # ticking the already-billed cost back on includes it
    r = client.post(f"/pi/{m_id}/worksheet", data={"_csrf": tok, "gross": "100,000.00", "fee_pct": "33.33",
                                                   "expenses_listed": "1",
                                                   "expense_id": [str(unbilled_id), str(billed_id)]})
    assert r.status_code == 302
    with app.app_context():
        import json
        ws = M.SettlementWorksheet.query.filter_by(matter_id=m_id, is_current=True).first()
        assert ws.costs_cents == 157550
        d = json.loads(ws.detail_json)
        assert sorted(d["expense_ids"]) == sorted([billed_id, unbilled_id])
        parts = (ws.fee_cents + ws.costs_cents + ws.liens_cents + ws.other_deductions_cents
                 + ws.net_to_client_cents)
        assert parts == ws.gross_cents

    # unticking every cost is allowed too
    r = client.post(f"/pi/{m_id}/worksheet", data={"_csrf": tok, "gross": "100,000.00", "fee_pct": "33.33",
                                                   "expenses_listed": "1"})
    assert r.status_code == 302
    with app.app_context():
        import json
        ws = M.SettlementWorksheet.query.filter_by(matter_id=m_id, is_current=True).first()
        assert ws.costs_cents == 0
        assert json.loads(ws.detail_json)["expense_ids"] == []
