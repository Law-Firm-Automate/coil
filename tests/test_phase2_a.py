"""Phase 2, Agent A (billing): UTBMS codes, bulk invoicing, approval workflow, split billing, interest,
evergreen retainer reminders, LEDES 1998B export, multi-currency.

Runs seed.py against its own SQLite file (data/test_phase2_a.db). Tests run in file order and share state via S.
"""
import os
import re
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402

TEST_DB = os.path.join(ROOT, "data", "test_phase2_a.db")
UPLOAD_DIR = os.path.join(ROOT, "data", "test_phase2_a_uploads")
S = {}


def csrf(c):
    r = c.get("/time/new")
    assert r.status_code == 200, r.data[:300]
    return re.search(rb'name="_csrf" value="([^"]+)"', r.data).group(1).decode()


@pytest.fixture(scope="module")
def app():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{TEST_DB}")
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT, env=env)
    from app import create_app
    application = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{TEST_DB}", "TESTING": True,
                              "SMTP_HOST": "", "UPLOAD_DIR": UPLOAD_DIR})
    yield application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    login(c)
    return c


def _models():
    from app.extensions import db
    from app import models
    return db, models


def _redirect_id(r):
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


# ---------------------------------------------------------------- UTBMS codes
def test_utbms_codes_saved_on_time_entry_and_expense(app, client):
    db, M = _models()
    with app.app_context():
        S["m1002"] = M.Matter.query.filter_by(number="M-1002").first().id
        S["m1001"] = M.Matter.query.filter_by(number="M-1001").first().id
    r = client.get(f"/time/new?matter_id={S['m1002']}")
    assert r.status_code == 200
    assert b'name="task_code"' in r.data and b'value="L110"' in r.data and b'value="A103"' in r.data
    tok = csrf(client)
    r = client.post("/time/new", data={"_csrf": tok, "matter_id": S["m1002"], "date": date.today().isoformat(),
                                       "duration": "1.5", "description": "UTBMS coded entry", "rate": "350.00",
                                       "billable": "1", "task_code": "L110", "activity_code": "A103"})
    assert r.status_code == 302, r.data[:300]
    with app.app_context():
        e = M.TimeEntry.query.filter_by(description="UTBMS coded entry").first()
        assert e.task_code == "L110" and e.activity_code == "A103"
        S["coded_entry"] = e.id
    r = client.get(f"/time/{S['coded_entry']}/edit")
    assert re.search(rb'<option value="L110" selected', r.data)
    # a bogus code is dropped rather than stored
    r = client.post("/time/new", data={"_csrf": tok, "matter_id": S["m1002"], "date": date.today().isoformat(),
                                       "duration": "0.5", "description": "Bad code entry", "rate": "350.00",
                                       "billable": "1", "task_code": "ZZZ9", "activity_code": "A999"})
    assert r.status_code == 302
    with app.app_context():
        e = M.TimeEntry.query.filter_by(description="Bad code entry").first()
        assert e.task_code == "" and e.activity_code == ""
    # expense code
    r = client.get(f"/time/expenses/new?matter_id={S['m1002']}")
    assert b'name="expense_code"' in r.data and b'value="E112"' in r.data
    r = client.post("/time/expenses/new", data={"_csrf": tok, "matter_id": S["m1002"],
                                                "date": date.today().isoformat(), "category": "Filing fee",
                                                "amount": "402.00", "description": "Coded filing fee",
                                                "billable": "1", "expense_code": "E112"})
    assert r.status_code == 302, r.data[:300]
    with app.app_context():
        x = M.Expense.query.filter_by(description="Coded filing fee").first()
        assert x.expense_code == "E112"
        S["coded_expense"] = x.id


# ---------------------------------------------------------------- bulk invoicing
def test_bulk_build_one_draft_per_selected_matter(app, client):
    db, M = _models()
    with app.app_context():
        m1 = db.session.get(M.Matter, S["m1001"])
        # seeded milestones have no due date; give the first one a due date in the past so it qualifies
        ms = [x for x in m1.milestones if x.invoice_id is None][0]
        ms.due_on = date.today() - timedelta(days=1)
        db.session.commit()
        S["due_milestone"] = ms.id
    r = client.get("/invoices/bulk")
    assert r.status_code == 200
    assert b"M-1001" in r.data and b"M-1002" in r.data
    assert re.search(rb'name="matter_ids" value="%d"' % S["m1001"], r.data)
    tok = csrf(client)
    with app.app_context():
        before = M.Invoice.query.count()
    r = client.post("/invoices/bulk", data={"_csrf": tok, "matter_ids": [str(S["m1001"]), str(S["m1002"])],
                                            "issued_on": date.today().isoformat()})
    assert r.status_code == 302 and r.headers["Location"].endswith("/invoices?status=draft")
    r = client.get("/invoices?status=draft")
    assert b"Built 2 draft invoices for 2 matters" in r.data
    with app.app_context():
        assert M.Invoice.query.count() == before + 2
        i1 = M.Invoice.query.filter_by(matter_id=S["m1001"]).order_by(M.Invoice.id.desc()).first()
        i2 = M.Invoice.query.filter_by(matter_id=S["m1002"]).order_by(M.Invoice.id.desc()).first()
        assert i1.status == "draft" and i2.status == "draft"
        assert i1.approval_status == "none"  # owner built it, approval is off
        assert [l.kind for l in i1.lines] == ["flat"] and i1.lines[0].milestone_id == S["due_milestone"]
        assert {l.kind for l in i2.lines} == {"time", "expense"}
        assert all(t.invoice_id == i2.id for t in db.session.get(M.Matter, S["m1002"]).time_entries if t.billable)
        assert i1.currency == "USD" and i2.currency == "USD"
        S["bulk_inv_1002"] = i2.id
    # nothing left to bill, so the page is empty and a second POST builds nothing
    r = client.get("/invoices/bulk")
    assert b"Nothing to bill" in r.data
    r = client.post("/invoices/bulk", data={"_csrf": tok, "matter_ids": [str(S["m1002"])]}, follow_redirects=True)
    assert b"No invoices were built" in r.data


# ---------------------------------------------------------------- approval workflow
def test_approval_flow_pending_refuse_send_approve_send(app, client):
    """Firm requires approval. An attorney (not owner/billing) builds an invoice: it is pending, the attorney
    cannot send it or approve it, the owner approves, then the attorney sends. Under the Phase 2 permission
    matrix a paralegal has no billing permission at all, so the non-approver here is an attorney."""
    db, M = _models()
    from app.services.mail import dev_outbox
    with app.app_context():
        firm = M.Firm.get()
        firm.require_invoice_approval = True
        atty = M.User(email="atty@example.com", name="Test Attorney", role="attorney", hourly_rate_cents=30000,
                      initials="TA", is_active=True)
        atty.set_password("password123")
        db.session.add(atty)
        maria = M.Contact.query.filter_by(last_name="Alvarez").first()
        m = M.Matter(number="T-APPR", client_id=maria.id, name="Approval test matter", billing_type="hourly",
                     hourly_rate_cents=30000)
        db.session.add(m)
        db.session.flush()
        db.session.add(M.TimeEntry(matter_id=m.id, user_id=atty.id, minutes=60, rate_cents=30000,
                                   description="Approval test hour"))
        db.session.commit()
        S["appr_matter"] = m.id
        S["atty"] = atty.id
        t_id = M.TimeEntry.query.filter_by(description="Approval test hour").first().id
    atty_client = app.test_client()
    login(atty_client, "atty@example.com", "password123")
    tok = csrf(atty_client)
    r = atty_client.get(f"/invoices/new?matter_id={S['appr_matter']}")
    assert r.status_code == 200 and b"requires invoice approval" in r.data
    r = atty_client.post("/invoices/new", data={"_csrf": tok, "matter_id": S["appr_matter"],
                                                "issued_on": date.today().isoformat(), "time_ids": [str(t_id)]})
    assert r.status_code == 302, r.data[:300]
    inv_id = _redirect_id(r)
    S["appr_inv"] = inv_id
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.status == "draft" and inv.approval_status == "pending"
        assert inv.created_by_id == S["atty"]
    # pending tab lists it
    r = client.get("/invoices?status=pending")
    assert r.status_code == 200 and inv_number(app, inv_id).encode() in r.data and b"pending approval" in r.data
    # attorney cannot send
    outbox_before = len(dev_outbox())
    r = atty_client.post(f"/invoices/{inv_id}/send", data={"_csrf": tok}, follow_redirects=True)
    assert b"waiting for approval" in r.data
    with app.app_context():
        assert db.session.get(M.Invoice, inv_id).status == "draft"
    assert len(dev_outbox()) == outbox_before
    # attorney cannot approve
    r = atty_client.post(f"/invoices/{inv_id}/approve", data={"_csrf": tok})
    assert r.status_code == 403
    # owner approves
    otok = csrf(client)
    r = client.post(f"/invoices/{inv_id}/approve", data={"_csrf": otok}, follow_redirects=True)
    assert b"approved" in r.data
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.approval_status == "approved" and inv.approved_by_id is not None and inv.approved_at is not None
    # attorney sends
    r = atty_client.post(f"/invoices/{inv_id}/send", data={"_csrf": tok}, follow_redirects=True)
    assert b"sent to maria@example.com" in r.data
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.status == "sent" and inv.sent_to == "maria@example.com"
    assert len(dev_outbox()) == outbox_before + 1
    # reject path: a second draft, rejected with a note, stays a draft and can be resubmitted
    with app.app_context():
        m = db.session.get(M.Matter, S["appr_matter"])
        db.session.add(M.TimeEntry(matter_id=m.id, user_id=S["atty"], minutes=30, rate_cents=30000,
                                   description="Second approval hour"))
        db.session.commit()
        t2 = M.TimeEntry.query.filter_by(description="Second approval hour").first().id
    r = atty_client.post("/invoices/new", data={"_csrf": tok, "matter_id": S["appr_matter"],
                                                "issued_on": date.today().isoformat(), "time_ids": [str(t2)]})
    inv2 = _redirect_id(r)
    r = client.post(f"/invoices/{inv2}/reject", data={"_csrf": otok, "note": "Wrong rate"}, follow_redirects=True)
    assert b"sent back" in r.data
    with app.app_context():
        inv = db.session.get(M.Invoice, inv2)
        assert inv.status == "draft" and inv.approval_status == "rejected" and inv.approval_note == "Wrong rate"
    r = atty_client.post(f"/invoices/{inv2}/send", data={"_csrf": tok}, follow_redirects=True)
    assert b"was rejected" in r.data
    r = atty_client.post(f"/invoices/{inv2}/submit", data={"_csrf": tok}, follow_redirects=True)
    with app.app_context():
        assert db.session.get(M.Invoice, inv2).approval_status == "pending"
        # owner-built invoices bypass approval entirely
        M.Firm.get().require_invoice_approval = False
        db.session.commit()


def inv_number(app, inv_id):
    db, M = _models()
    with app.app_context():
        return db.session.get(M.Invoice, inv_id).number


# ---------------------------------------------------------------- split billing
def test_split_build_sums_exactly_and_group_void(app, client):
    db, M = _models()
    with app.app_context():
        maria = M.Contact.query.filter_by(last_name="Alvarez").first()
        insurer = M.Contact(kind="company", company_name="Split Test Insurance Co", email="claims@split.test",
                            is_client=True)
        db.session.add(insurer)
        db.session.flush()
        m = M.Matter(number="T-SPLIT", client_id=maria.id, name="Split billing matter", billing_type="hourly",
                     hourly_rate_cents=30000)
        db.session.add(m)
        db.session.flush()
        db.session.add(M.Expense(matter_id=m.id, user_id=1, amount_cents=100001, category="Expert",
                                 description="Split test expert fee", billable=True))
        db.session.commit()
        S["split_matter"], S["insurer"], S["maria"] = m.id, insurer.id, maria.id
        exp_id = M.Expense.query.filter_by(description="Split test expert fee").first().id
    tok = csrf(client)
    # add payers through the matter routes: 60% client, 40% insurer
    r = client.post(f"/matters/{S['split_matter']}/payers", data={"_csrf": tok, "contact_id": S["maria"],
                                                                  "percent": "60", "label": "Client"})
    assert r.status_code == 302
    # builder refuses while the split is incomplete
    r = client.post("/invoices/new", data={"_csrf": tok, "matter_id": S["split_matter"], "expense_ids": [str(exp_id)]})
    assert r.status_code == 400 and b"do not add up to 100%" in r.data
    r = client.post(f"/matters/{S['split_matter']}/payers", data={"_csrf": tok, "contact_id": S["insurer"],
                                                                  "percent": "40", "label": "Insurer"})
    assert r.status_code == 302
    r = client.get(f"/matters/{S['split_matter']}")
    assert b"Split Test Insurance Co" in r.data and b"40.0%" in r.data
    r = client.post("/invoices/new", data={"_csrf": tok, "matter_id": S["split_matter"], "expense_ids": [str(exp_id)]})
    assert r.status_code == 302, r.data[:300]
    first_id = _redirect_id(r)
    with app.app_context():
        first = db.session.get(M.Invoice, first_id)
        assert first.split_group and first.split_pct == 60.0 and first.client_id == S["maria"]
        assert first.payer_contact_id == S["maria"]
        group = M.Invoice.query.filter_by(split_group=first.split_group).order_by(M.Invoice.id).all()
        assert len(group) == 2
        second = group[1]
        assert second.client_id == S["insurer"] and second.split_pct == 40.0
        assert first.total_cents == 60001  # 60000.6 rounds half up
        assert second.total_cents == 40000  # remainder so the group sums exactly
        assert first.total_cents + second.total_cents == 100001
        assert first.lines[0].expense_id == exp_id
        assert second.lines[0].expense_id is None and second.lines[0].time_entry_id is None
        assert db.session.get(M.Expense, exp_id).invoice_id == first.id
        assert first.number != second.number
        S["split_first"], S["split_second"] = first.id, second.id
        second_number = second.number
    # detail shows the sibling
    r = client.get(f"/invoices/{first_id}")
    assert r.status_code == 200 and second_number.encode() in r.data and b"Split billing group" in r.data
    # rounding helper: negative discounts and three-way splits keep the sum exact
    from app.blueprints.invoices import split_cents
    assert split_cents(100001, [60, 40]) == [60001, 40000]
    assert sum(split_cents(100, [33.33, 33.33, 33.34])) == 100
    assert sum(split_cents(-5001, [50, 50])) == -5001
    # voiding the second voids the whole group and releases the expense
    r = client.post(f"/invoices/{S['split_second']}/void", data={"_csrf": tok}, follow_redirects=True)
    assert b"Voided the split group" in r.data
    with app.app_context():
        assert db.session.get(M.Invoice, S["split_first"]).status == "void"
        assert db.session.get(M.Invoice, S["split_second"]).status == "void"
        assert db.session.get(M.Expense, exp_id).invoice_id is None
    # payer delete route
    with app.app_context():
        pid = M.MatterPayer.query.filter_by(matter_id=S["split_matter"], contact_id=S["insurer"]).first().id
    r = client.post(f"/matters/{S['split_matter']}/payers/{pid}/delete", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        assert M.MatterPayer.query.filter_by(matter_id=S["split_matter"]).count() == 1


# ---------------------------------------------------------------- interest
def test_interest_once_per_month_idempotent(app, client):
    db, M = _models()
    from app.cli import run_interest
    with app.app_context():
        firm = M.Firm.get()
        firm.interest_apr_bps = 1200  # 12% APR -> 1% per month
        firm.interest_grace_days = 30
        m2 = db.session.get(M.Matter, S["m1002"])
        inv = M.Invoice(number="INV-INT-1", matter_id=m2.id, client_id=m2.client_id, kind="hourly", status="sent",
                        issued_on=date.today() - timedelta(days=91), due_on=date.today() - timedelta(days=61),
                        currency="USD")
        db.session.add(inv)
        db.session.flush()
        db.session.add(M.InvoiceLine(invoice_id=inv.id, kind="flat", description="Old work", quantity=1.0,
                                     unit_cents=100000, amount_cents=100000, sort=0))
        # inside the grace period: must not be charged
        inv2 = M.Invoice(number="INV-INT-2", matter_id=m2.id, client_id=m2.client_id, kind="hourly", status="sent",
                         issued_on=date.today() - timedelta(days=40), due_on=date.today() - timedelta(days=10))
        db.session.add(inv2)
        db.session.flush()
        db.session.add(M.InvoiceLine(invoice_id=inv2.id, kind="flat", description="Recent", quantity=1.0,
                                     unit_cents=50000, amount_cents=50000, sort=0))
        db.session.flush()
        inv.recalc()
        inv2.recalc()
        db.session.commit()
        S["int_inv"], S["int_inv2"] = inv.id, inv2.id

        assert run_interest() == (1, 1000)
        inv = db.session.get(M.Invoice, S["int_inv"])
        assert inv.interest_cents == 1000 and inv.total_cents == 101000 and inv.last_interest_on == date.today()
        lines = [l for l in inv.lines if l.kind == "interest"]
        assert len(lines) == 1 and lines[0].amount_cents == 1000
        assert db.session.get(M.Invoice, S["int_inv2"]).interest_cents == 0
        assert M.AuditLog.query.filter_by(action="interest", entity="invoice", entity_id=inv.id).count() == 1
        # same month again: nothing
        assert run_interest() == (0, 0)
        assert len([l for l in db.session.get(M.Invoice, S["int_inv"]).lines if l.kind == "interest"]) == 1
    # manual route refuses in the same month
    tok = csrf(client)
    r = client.post(f"/invoices/{S['int_inv']}/interest", data={"_csrf": tok}, follow_redirects=True)
    assert b"No interest to add" in r.data
    # pretend last month was charged instead: the manual route adds this month's on the new balance
    with app.app_context():
        inv = db.session.get(M.Invoice, S["int_inv"])
        first = date.today().replace(day=1)
        inv.last_interest_on = first - timedelta(days=1)
        db.session.commit()
    r = client.post(f"/invoices/{S['int_inv']}/interest", data={"_csrf": tok}, follow_redirects=True)
    assert b"Added $10.10 interest" in r.data
    with app.app_context():
        inv = db.session.get(M.Invoice, S["int_inv"])
        assert inv.interest_cents == 2010 and inv.total_cents == 102010
        M.Firm.get().interest_apr_bps = 0
        db.session.commit()
    r = client.get(f"/invoices/{S['int_inv']}")
    assert r.status_code == 200 and b"Interest so far" in r.data


# ---------------------------------------------------------------- evergreen retainer
def test_evergreen_reminder_fires_once_per_14_days(app, client):
    db, M = _models()
    from app.cli import run_evergreen, run_reminders
    from app.blueprints.trust import evergreen_shortfalls
    from app.services.mail import dev_outbox
    with app.app_context():
        c = M.Contact(first_name="Evie", last_name="Green", email="evie@example.com", is_client=True)
        db.session.add(c)
        db.session.flush()
        m = M.Matter(number="T-EVER", client_id=c.id, name="Evergreen matter", billing_type="hourly",
                     trust_minimum_cents=500000, trust_replenish_to_cents=700000)
        db.session.add(m)
        db.session.flush()
        db.session.add(M.TrustTransaction(client_id=c.id, matter_id=m.id, type="deposit", amount_cents=100000,
                                          description="Initial", created_by_id=1))
        db.session.commit()
        S["ever_matter"] = m.id
        rows = evergreen_shortfalls()
        assert [(r[0].id, r[1], r[2]) for r in rows] == [(m.id, 100000, 600000)]
        before = len(dev_outbox())
        assert run_evergreen() == 1
        assert run_evergreen() == 0  # within 14 days
        out = dev_outbox()
        assert len(out) == before + 1
        assert "Trust deposit request" in out[0]["subject"] and "$6,000.00" in out[0]["subject"]
        assert out[0]["to"] == "evie@example.com"
        logs = M.AuditLog.query.filter_by(action="evergreen_sent", entity="matter", entity_id=m.id).all()
        assert len(logs) == 1
        # the reminders CLI path keeps its (invoice, engagement) return and is followed by run_evergreen
        assert run_reminders() == (0, 0)
        # age the audit row past the window and it fires again
        logs[0].created_at = M.now() - timedelta(days=15)
        db.session.commit()
        assert run_evergreen() == 1
        # topping up the trust clears the shortfall
        db.session.add(M.TrustTransaction(client_id=c.id, matter_id=m.id, type="deposit", amount_cents=600000,
                                          description="Top up", created_by_id=1))
        db.session.commit()
        assert evergreen_shortfalls() == []
    r = client.get(f"/matters/{S['ever_matter']}")
    assert r.status_code == 200 and b"evergreen retainer" in r.data
    # the matter form saves the fields
    tok = csrf(client)
    r = client.get(f"/matters/{S['ever_matter']}/edit")
    assert b'name="trust_minimum"' in r.data and b'value="5000.0"' in r.data
    r = client.post(f"/matters/{S['ever_matter']}/edit", data={
        "_csrf": tok, "client_id": str(S.get("maria") or 1), "name": "Evergreen matter", "status": "open",
        "billing_type": "hourly", "opened_on": date.today().isoformat(), "trust_minimum": "2,500.00",
        "trust_replenish_to": "4000", "currency": "CAD", "ledes_matter_id": "CM-77"})
    assert r.status_code == 302, r.data[:300]
    with app.app_context():
        m = db.session.get(M.Matter, S["ever_matter"])
        assert m.trust_minimum_cents == 250000 and m.trust_replenish_to_cents == 400000
        assert m.currency == "CAD" and m.currency_code == "CAD" and m.ledes_matter_id == "CM-77"


# ---------------------------------------------------------------- LEDES
def test_ledes_export_refuses_then_produces_valid_file(app, client):
    db, M = _models()
    from app.blueprints.ledes import LEDES_FIELDS
    with app.app_context():
        c = M.Contact(kind="company", company_name="LEDES Carrier Inc", email="ebill@carrier.test", is_client=True)
        db.session.add(c)
        db.session.flush()
        u = M.User.query.first()
        m = M.Matter(number="T-LEDES", client_id=c.id, name="LEDES matter", billing_type="hourly",
                     hourly_rate_cents=25000)
        db.session.add(m)
        db.session.flush()
        db.session.add(M.TimeEntry(matter_id=m.id, user_id=u.id, minutes=90, rate_cents=25000,
                                   description="Draft motion | with a pipe", task_code="L210", activity_code="A103",
                                   date=date.today()))
        db.session.add(M.Expense(matter_id=m.id, user_id=u.id, amount_cents=35000, category="Filing fee",
                                 description="Filing", expense_code="E112", date=date.today()))
        db.session.commit()
        S["ledes_matter"], S["ledes_client"] = m.id, c.id
        t_id = M.TimeEntry.query.filter_by(matter_id=m.id).first().id
        e_id = M.Expense.query.filter_by(matter_id=m.id).first().id
    tok = csrf(client)
    r = client.post("/invoices/new", data={"_csrf": tok, "matter_id": S["ledes_matter"],
                                           "issued_on": date.today().isoformat(),
                                           "time_ids": [str(t_id)], "expense_ids": [str(e_id)]})
    inv_id = _redirect_id(r)
    assert client.post(f"/invoices/{inv_id}/send", data={"_csrf": tok}).status_code == 302
    today = date.today().isoformat()
    url = f"/exports/ledes?from={today}&to={today}&client_id={S['ledes_client']}"
    r = client.get(url)
    assert r.status_code == 400
    assert b"LEDES export refused" in r.data
    assert b"Firm LEDES id" in r.data and b"LEDES Carrier Inc has no LEDES client id" in r.data
    assert b"Matter T-LEDES has no LEDES matter id" in r.data
    with app.app_context():
        assert db.session.get(M.Invoice, inv_id).ledes_exported_at is None
        M.Firm.get().ledes_firm_id = "74-1234567"
        db.session.get(M.Contact, S["ledes_client"]).ledes_client_id = "CARRIER01"
        db.session.get(M.Matter, S["ledes_matter"]).ledes_matter_id = "CLM-2026-001"
        db.session.commit()
    r = client.get(url)
    assert r.status_code == 200, r.data[:500]
    assert "attachment" in r.headers["Content-Disposition"]
    lines = r.data.decode().splitlines()
    assert lines[0] == "LEDES1998B[]"
    assert lines[1].endswith("[]") and lines[1][:-2].split("|") == LEDES_FIELDS and len(LEDES_FIELDS) == 24
    records = lines[2:]
    assert len(records) == 2
    fee = records[0][:-2].split("|")
    exp = records[1][:-2].split("|")
    assert records[0].endswith("[]") and len(fee) == 24 and len(exp) == 24
    with app.app_context():
        number = db.session.get(M.Invoice, inv_id).number
        assert db.session.get(M.Invoice, inv_id).ledes_exported_at is not None
    assert fee[1] == number and fee[2] == "CARRIER01" and fee[3] == "T-LEDES" and fee[23] == "CLM-2026-001"
    assert fee[0] == date.today().strftime("%Y%m%d")
    assert fee[4] == "725.00"  # invoice total 375 + 350
    assert fee[9] == "F" and fee[10] == "1.50" and fee[11] == "0.00" and fee[12] == "375.00"
    assert fee[14] == "L210" and fee[15] == "" and fee[16] == "A103"
    assert fee[17] == "DO" and fee[21] == "Demo Owner" and fee[22] == "PT"
    assert "|" not in fee[18] and "Draft motion / with a pipe" == fee[18]
    assert fee[19] == "74-1234567" and fee[20] == "250.00"
    assert exp[9] == "E" and exp[12] == "350.00" and exp[15] == "E112" and exp[14] == "" and exp[16] == ""
    assert exp[8] == "2" and exp[20] == "350.00"
    # the exports page links and explains it
    r = client.get("/exports")
    assert r.status_code == 200 and b"/exports/ledes" in r.data and b"LEDES 1998B" in r.data


# ---------------------------------------------------------------- multi-currency
def test_gbp_matter_invoice_renders_pound_sign(app, client):
    db, M = _models()
    with app.app_context():
        maria = db.session.get(M.Contact, S["maria"])
        u = M.User.query.first()
        m = M.Matter(number="T-GBP", client_id=maria.id, name="London matter", billing_type="hourly",
                     hourly_rate_cents=40000, currency="GBP")
        db.session.add(m)
        db.session.flush()
        db.session.add(M.TimeEntry(matter_id=m.id, user_id=u.id, minutes=120, rate_cents=40000,
                                   description="Advice on UK lease"))
        db.session.commit()
        S["gbp_matter"] = m.id
        t_id = M.TimeEntry.query.filter_by(matter_id=m.id).first().id
    tok = csrf(client)
    r = client.get(f"/invoices/new?matter_id={S['gbp_matter']}")
    assert b"Currency <strong>GBP</strong>" in r.data
    r = client.post("/invoices/new", data={"_csrf": tok, "matter_id": S["gbp_matter"],
                                           "issued_on": date.today().isoformat(), "time_ids": [str(t_id)]})
    inv_id = _redirect_id(r)
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.currency == "GBP" and inv.total_cents == 80000
    r = client.get(f"/invoices/{inv_id}")
    assert r.status_code == 200
    assert "£800.00".encode("utf-8") in r.data
    assert b"Amounts are in GBP" in r.data
    r = client.get("/invoices?status=draft")
    assert "£800.00".encode("utf-8") in r.data
    # PDF and email render with the symbol too
    r = client.get(f"/invoices/{inv_id}/pdf")
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"
    from app.services.mail import dev_outbox
    r = client.post(f"/invoices/{inv_id}/send", data={"_csrf": tok})
    assert r.status_code == 302
    assert "£800.00" in dev_outbox()[0]["html"] and "Amounts are in GBP" in dev_outbox()[0]["html"]
    # matter form offers the currency select with GBP selected
    r = client.get(f"/matters/{S['gbp_matter']}/edit")
    assert re.search(rb'<option value="GBP" selected', r.data)


def test_office_address_on_invoice_pdf(app, client):
    db, M = _models()
    with app.app_context():
        o = M.Office(name="Dallas office", address="500 Elm St\nDallas, TX 75201", phone="(214) 555-0100")
        db.session.add(o)
        db.session.flush()
        m = db.session.get(M.Matter, S["gbp_matter"])
        m.office_id = o.id
        db.session.commit()
        inv = M.Invoice.query.filter_by(matter_id=m.id).first()
        from app.blueprints.invoices import _letterhead
        head = _letterhead(M.Firm.get(), inv)
        assert head.address.startswith("500 Elm St") and head.phone == "(214) 555-0100"
        assert head.name == M.Firm.get().name
        inv_id = inv.id
    r = client.get(f"/invoices/{inv_id}/pdf")
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"
