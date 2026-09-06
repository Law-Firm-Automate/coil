"""Smoke test for module B: time, invoices, reports.

Runs seed.py against a throwaway SQLite file (data/test_module_b.db) so it never touches the dev DB,
then drives the app with test_client(). Tests run in file order and share state through S.
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

TEST_DB = os.path.join(ROOT, "data", "test_module_b.db")
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
                              "SMTP_HOST": ""})
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


# ---------------------------------------------------------------- time
def test_log_time_with_colon_duration(app, client):
    db, M = _models()
    with app.app_context():
        m = M.Matter.query.filter_by(number="M-1002").first()
        S["m1002"] = m.id
        S["m1001"] = M.Matter.query.filter_by(number="M-1001").first().id
    r = client.get(f"/time/new?matter_id={S['m1002']}")
    assert r.status_code == 200
    assert b'value="350.00"' in r.data  # rate prefilled from matter.effective_rate_cents
    tok = csrf(client)
    r = client.post("/time/new", data={"_csrf": tok, "matter_id": S["m1002"], "date": date.today().isoformat(),
                                       "duration": "1:30", "description": "Smoke test entry (1:30)",
                                       "rate": "350.00", "billable": "1", "activity_code": "A103"})
    assert r.status_code == 302, r.data[:300]
    with app.app_context():
        e = M.TimeEntry.query.filter_by(description="Smoke test entry (1:30)").first()
        assert e is not None
        assert e.minutes == 90
        assert e.rate_cents == 35000
        assert e.amount_cents == 52500
        S["entry_130"] = e.id
    r = client.get(f"/time?matter_id={S['m1002']}")
    assert r.status_code == 200 and b"Smoke test entry (1:30)" in r.data


def test_timer_start_stop_rounds_up_to_six_minutes(app, client):
    db, M = _models()
    tok = csrf(client)
    r = client.post("/time/timer/start", data={"_csrf": tok, "matter_id": S["m1002"],
                                               "description": "Timer smoke test"})
    assert r.status_code == 302
    r = client.get("/time/timer")
    assert r.status_code == 200 and b"data-timer-seconds" in r.data
    # pause / resume round trip
    assert client.post("/time/timer/pause", data={"_csrf": tok}).status_code == 302
    assert client.post("/time/timer/resume", data={"_csrf": tok}).status_code == 302
    with app.app_context():
        t = M.Timer.query.first()
        assert t is not None and not t.paused
        # pretend it has been running for 7 minutes 10 seconds
        t.started_at = M.now() - timedelta(minutes=7, seconds=10)
        t.accumulated_seconds = 0
        db.session.commit()
    r = client.post("/time/timer/stop", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        assert M.Timer.query.count() == 0
        e = M.TimeEntry.query.filter_by(description="Timer smoke test").first()
        assert e is not None
        assert e.minutes == 12, e.minutes  # 7:10 rounds up to 12
        assert e.rate_cents == 35000
        S["entry_timer"] = e.id
    # minimum is one 6-minute increment
    from app.blueprints.time import round_up_minutes
    assert round_up_minutes(0) == 6
    assert round_up_minutes(1) == 6
    assert round_up_minutes(360) == 6
    assert round_up_minutes(361) == 12
    assert round_up_minutes(3600) == 60


def test_expense_create(app, client):
    db, M = _models()
    tok = csrf(client)
    r = client.post("/time/expenses/new", data={"_csrf": tok, "matter_id": S["m1002"],
                                                "date": date.today().isoformat(), "category": "Filing fee",
                                                "amount": "402.00", "description": "Smoke test filing fee",
                                                "billable": "1"})
    assert r.status_code == 302, r.data[:300]
    with app.app_context():
        e = M.Expense.query.filter_by(description="Smoke test filing fee").first()
        assert e and e.amount_cents == 40200 and e.category == "Filing fee"
        S["expense"] = e.id
    assert client.get("/time/expenses").status_code == 200


# ---------------------------------------------------------------- invoices
def test_build_hourly_invoice_from_unbilled(app, client):
    db, M = _models()
    r = client.get(f"/invoices/new?matter_id={S['m1002']}")
    assert r.status_code == 200
    assert b"Unbilled time" in r.data and b"Unbilled expenses" in r.data
    with app.app_context():
        m = db.session.get(M.Matter, S["m1002"])
        times = [t for t in m.time_entries if t.billable and t.invoice_id is None]
        exps = [e for e in m.expenses if e.billable and e.invoice_id is None]
        assert len(times) >= 4  # 2 seeded + 1:30 + timer
        expected = sum(t.amount_cents for t in times) + sum(e.amount_cents for e in exps)
        time_ids = [t.id for t in times]
        exp_ids = [e.id for e in exps]
        expected_hours = sum(t.minutes for t in times) / 60.0
        firm = M.Firm.get()
        next_no = firm.next_invoice_number
        prefix = firm.invoice_prefix
    tok = csrf(client)
    r = client.post("/invoices/new", data={"_csrf": tok, "matter_id": S["m1002"],
                                           "issued_on": date.today().isoformat(),
                                           "due_on": (date.today() + timedelta(days=30)).isoformat(),
                                           "time_ids": [str(i) for i in time_ids],
                                           "expense_ids": [str(i) for i in exp_ids],
                                           "adjustment_description": "Courtesy discount",
                                           "adjustment_amount": "-50.00", "notes": "Thanks for your business."})
    assert r.status_code == 302, r.data[:500]
    inv_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    S["hourly_inv"] = inv_id
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.number == f"{prefix}{next_no}"
        assert M.Firm.get().next_invoice_number == next_no + 1
        assert inv.kind == "hourly" and inv.status == "draft"
        assert inv.subtotal_cents == expected - 5000
        assert inv.total_cents == expected - 5000
        assert inv.balance_cents == expected - 5000
        kinds = {l.kind for l in inv.lines}
        assert kinds == {"time", "expense", "discount"}
        time_lines = [l for l in inv.lines if l.kind == "time"]
        assert abs(sum(l.quantity for l in time_lines) - expected_hours) < 0.01
        assert all(t.invoice_id == inv_id for t in M.TimeEntry.query.filter(M.TimeEntry.id.in_(time_ids)))
        assert all(e.invoice_id == inv_id for e in M.Expense.query.filter(M.Expense.id.in_(exp_ids)))
        S["hourly_token"] = inv.public_token
        S["hourly_number"] = inv.number
    r = client.get(f"/invoices/{inv_id}")
    assert r.status_code == 200 and b"Courtesy discount" in r.data and S["hourly_number"].encode() in r.data
    # locked entry shows the lock and a link to the invoice
    r = client.get(f"/time/{S['entry_130']}/edit")
    assert r.status_code == 200 and b"read-only" in r.data and f"/invoices/{inv_id}".encode() in r.data
    # editing while draft: change a description and add an adjustment
    tok = csrf(client)
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        first = inv.lines[0]
        data = {"_csrf": tok, f"desc_{first.id}": "Edited description", "adj_description": "Rush fee",
                "adj_amount": "25.00", "issued_on": inv.issued_on.isoformat(), "due_on": inv.due_on.isoformat(),
                "notes": inv.notes}
        before = inv.total_cents
    r = client.post(f"/invoices/{inv_id}/edit", data=data)
    assert r.status_code == 302
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.total_cents == before + 2500
        assert inv.lines[0].description == "Edited description"


def test_build_flat_fee_invoice_first_milestone(app, client):
    db, M = _models()
    r = client.get(f"/invoices/new?matter_id={S['m1001']}")
    assert r.status_code == 200 and b"Flat fee" in r.data and b"Retainer on signing" in r.data
    with app.app_context():
        m = db.session.get(M.Matter, S["m1001"])
        open_ms = [x for x in m.milestones if x.invoice_id is None]
        assert len(open_ms) == 2
        first = open_ms[0]
        S["milestone"] = first.id
        amt = first.amount_cents
    # the earliest uninvoiced milestone is pre-checked, the rest are not
    assert re.search(rb'name="milestone_ids" value="%d"[^>]*checked' % first.id, r.data)
    tok = csrf(client)
    r = client.post("/invoices/new", data={"_csrf": tok, "matter_id": S["m1001"],
                                           "issued_on": date.today().isoformat(),
                                           "milestone_ids": [str(S["milestone"])]})
    assert r.status_code == 302, r.data[:500]
    inv_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    S["flat_inv"] = inv_id
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.kind == "flat" and inv.status == "draft"
        assert inv.total_cents == amt == 125000
        assert len(inv.lines) == 1 and inv.lines[0].kind == "flat" and inv.lines[0].milestone_id == S["milestone"]
        ms = db.session.get(M.FlatFeeMilestone, S["milestone"])
        assert ms.invoice_id == inv_id and ms.invoiced
        # default due date = issue + firm terms
        assert inv.due_on == inv.issued_on + timedelta(days=M.Firm.get().invoice_terms_days)
    # the builder no longer offers that milestone
    r = client.get(f"/invoices/new?matter_id={S['m1001']}")
    assert b'name="milestone_ids" value="%d"' % S["milestone"] not in r.data
    assert b"Already invoiced" in r.data


def test_send_invoice_logs_email_and_builds_pdf(app, client):
    db, M = _models()
    from app.services.mail import dev_outbox
    inv_id = S["hourly_inv"]
    tok = csrf(client)
    r = client.post(f"/invoices/{inv_id}/send", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.status == "sent"
        assert inv.sent_at is not None
        assert inv.sent_to == "ap@bluebonnet.test"
        assert inv.pdf_path and os.path.isfile(inv.pdf_path), inv.pdf_path
        with open(inv.pdf_path, "rb") as fh:
            assert fh.read(5) == b"%PDF-"
        assert [e.event for e in inv.events] == ["sent"]
        outbox = dev_outbox()
        assert outbox and outbox[0]["to"] == "ap@bluebonnet.test"
        assert inv.number in outbox[0]["subject"]
        html = outbox[0]["html"]
        assert f"/p/{inv.public_token}" in html
        assert f"/track/invoice/{inv.public_token}.gif" in html
        assert "View and pay" in html
    # reminder
    r = client.post(f"/invoices/{inv_id}/remind", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert [e.event for e in inv.events] == ["sent", "reminder"]
        assert "Reminder" in dev_outbox()[0]["subject"]
    # PDF download for staff
    r = client.get(f"/invoices/{inv_id}/pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data[:5] == b"%PDF-"
    # edit is refused once sent
    r = client.get(f"/invoices/{inv_id}/edit")
    assert r.status_code == 302


def test_detail_shows_payment_and_trust_forms(app, client):
    """Bluebonnet has $5,000 in trust, so a sent invoice offers both manual payment and apply-from-trust."""
    r = client.get(f"/invoices/{S['hourly_inv']}")
    assert r.status_code == 200
    assert b'action="/payments/record"' in r.data and b'name="method"' in r.data
    assert b'action="/trust/apply"' in r.data
    # Wording changed when the trust figure became "what this invoice may draw" rather than the
    # pooled client balance (audit fix). Same $5,000 here, since it is all earmarked to this matter.
    assert b"$5,000.00 of Bluebonnet Logistics LLC's trust money can be applied" in r.data
    assert f"/p/{S['hourly_token']}".encode() in r.data and b"data-copy=" in r.data
    assert b"Send reminder" in r.data and b"Download PDF" in r.data
    # locked expense view links back to the invoice
    r = client.get(f"/time/expenses/{S['expense']}/edit")
    assert r.status_code == 200 and b"read-only" in r.data


def test_builder_hybrid_and_contingency(app, client):
    db, M = _models()
    with app.app_context():
        c = M.Contact.query.filter_by(last_name="Alvarez").first()
        u = M.User.query.first()
        hyb = M.Matter(number="T-HYB", client_id=c.id, name="Smoke hybrid matter", billing_type="hybrid",
                       flat_fee_cents=100000, hourly_rate_cents=20000, responsible_user_id=u.id)
        con = M.Matter(number="T-CON", client_id=c.id, name="Smoke contingency matter", billing_type="contingency",
                       contingency_pct=33.3, responsible_user_id=u.id)
        db.session.add_all([hyb, con])
        db.session.commit()
        db.session.add(M.TimeEntry(matter_id=hyb.id, user_id=u.id, minutes=60, rate_cents=20000,
                                   description="Hybrid hour"))
        db.session.add(M.Expense(matter_id=con.id, user_id=u.id, amount_cents=5000, category="Copies",
                                 description="Contingency copies"))
        db.session.commit()
        hyb_id, con_id = hyb.id, con.id
        hyb_time = M.TimeEntry.query.filter_by(matter_id=hyb_id).first().id
        con_exp = M.Expense.query.filter_by(matter_id=con_id).first().id
    # hybrid with no milestones: free flat amount line defaults to the matter flat fee, plus unbilled time
    r = client.get(f"/invoices/new?matter_id={hyb_id}")
    assert r.status_code == 200
    assert b'name="flat_amount"' in r.data and b'value="1000.00"' in r.data and b"Hybrid hour" in r.data
    tok = csrf(client)
    r = client.post("/invoices/new", data={"_csrf": tok, "matter_id": hyb_id, "flat_amount": "1000.00",
                                           "flat_description": "Flat fee: smoke hybrid",
                                           "time_ids": [str(hyb_time)]})
    assert r.status_code == 302, r.data[:300]
    inv_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.kind == "hybrid" and inv.total_cents == 100000 + 20000
        assert {l.kind for l in inv.lines} == {"flat", "time"}
    # contingency: settlement helper on the page, free fee amount plus expenses
    r = client.get(f"/invoices/new?matter_id={con_id}")
    assert r.status_code == 200
    assert b'id="settlement"' in r.data and b'value="33.3"' in r.data and b"Contingency copies" in r.data
    r = client.post("/invoices/new", data={"_csrf": tok, "matter_id": con_id, "fee_amount": "33,300.00",
                                           "fee_description": "Contingency fee: 33.3% of $100,000.00",
                                           "expense_ids": [str(con_exp)]})
    assert r.status_code == 302, r.data[:300]
    inv_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.kind == "contingency" and inv.total_cents == 3330000 + 5000
        assert db.session.get(M.Expense, con_exp).invoice_id == inv_id
    # an empty builder POST is refused
    r = client.post("/invoices/new", data={"_csrf": tok, "matter_id": con_id})
    assert r.status_code == 400 and b"Pick at least one item" in r.data
    # /invoices/new without a matter shows the picker
    r = client.get("/invoices/new")
    assert r.status_code == 200 and b"Which matter" in r.data


def test_public_view_and_tracking_pixel(app):
    db, M = _models()
    anon = app.test_client()  # no login
    token = S["hourly_token"]
    r = anon.get(f"/p/{token}")
    assert r.status_code == 200
    assert b"Pay by bank transfer (ACH), no fee" in r.data
    assert f"/pay/{token}?method=ach".encode() in r.data
    assert f"/pay/{token}?method=card".encode() in r.data
    assert b"3% card surcharge applies" in r.data  # seed sets 300 bps
    assert b"on deposit in our trust account" in r.data  # Bluebonnet has $5,000 in trust
    assert b"Download PDF" in r.data
    with app.app_context():
        inv = M.Invoice.query.filter_by(public_token=token).first()
        assert inv.view_count == 1
        assert inv.first_viewed_at is not None
        assert inv.status == "viewed"
        first_viewed = inv.first_viewed_at
    r = anon.get(f"/track/invoice/{token}.gif")
    assert r.status_code == 200
    assert r.mimetype == "image/gif"
    assert r.data.startswith(b"GIF89a") and len(r.data) == 43
    assert "no-store" in r.headers["Cache-Control"]
    with app.app_context():
        inv = M.Invoice.query.filter_by(public_token=token).first()
        assert inv.view_count == 1  # deduped: within 60 seconds of the page view
        assert inv.first_viewed_at == first_viewed
        viewed = [e for e in inv.events if e.event == "viewed"]
        assert len(viewed) == 1 and viewed[0].ip and viewed[0].detail == "page"
        # age the last view past the window and the pixel counts again
        viewed[0].created_at = M.now() - timedelta(seconds=61)
        db.session.commit()
    r = anon.get(f"/track/invoice/{token}.gif")
    assert r.status_code == 200
    with app.app_context():
        inv = M.Invoice.query.filter_by(public_token=token).first()
        assert inv.view_count == 2
    # unknown token still returns the pixel, and the public PDF works without login
    assert anon.get("/track/invoice/nope.gif").status_code == 200
    r = anon.get(f"/p/{token}/pdf")
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"
    assert anon.get("/p/nope").status_code == 404


def test_void_draft_unlinks_sources(app, client):
    db, M = _models()
    inv_id = S["flat_inv"]
    tok = csrf(client)
    r = client.post(f"/invoices/{inv_id}/void", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.status == "void"
        ms = db.session.get(M.FlatFeeMilestone, S["milestone"])
        assert ms.invoice_id is None
        assert M.AuditLog.query.filter_by(action="void", entity="invoice", entity_id=inv_id).count() == 1
    # the milestone is offered again
    r = client.get(f"/invoices/new?matter_id={S['m1001']}")
    assert b'name="milestone_ids" value="%d"' % S["milestone"] in r.data
    # void a sent invoice with no payments also unlinks time and expenses
    r = client.post(f"/invoices/{S['hourly_inv']}/void", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(M.TimeEntry, S["entry_130"]).invoice_id is None
        assert db.session.get(M.TimeEntry, S["entry_timer"]).invoice_id is None
        assert db.session.get(M.Expense, S["expense"]).invoice_id is None
        assert db.session.get(M.Invoice, S["hourly_inv"]).status == "void"
    # sending a void invoice is refused
    r = client.post(f"/invoices/{S['hourly_inv']}/send", data={"_csrf": tok}, follow_redirects=True)
    assert b"Cannot send a void invoice" in r.data


def test_invoice_list_tabs(client):
    for status in ("all", "draft", "sent", "viewed", "partial", "paid", "overdue", "void"):
        r = client.get(f"/invoices?status={status}")
        assert r.status_code == 200, status
    r = client.get("/invoices?status=void")
    assert S["hourly_number"].encode() in r.data


# ---------------------------------------------------------------- reports
@pytest.mark.parametrize("path", ["/reports", "/reports/ar-aging", "/reports/wip", "/reports/revenue",
                                  "/reports/trust-balances", "/reports/productivity"])
def test_report_pages(client, path):
    r = client.get(path)
    assert r.status_code == 200, path
    if path != "/reports":
        r = client.get(path + "?format=csv")
        assert r.status_code == 200, path
        assert r.mimetype == "text/csv"
        assert "attachment" in r.headers.get("Content-Disposition", "")
        assert len(r.data.splitlines()) >= 1


def test_wip_and_trust_report_contents(client):
    r = client.get("/reports/wip")
    assert b"M-1002" in r.data  # voided invoice released the entries back to WIP
    r = client.get("/reports/trust-balances?format=csv")
    assert b"Bluebonnet Logistics LLC" in r.data and b"5000.00" in r.data
    r = client.get("/reports/productivity?format=csv")
    assert b"Demo Owner" in r.data
    r = client.get(f"/reports/revenue?from={date.today().isoformat()}&to={date.today().isoformat()}")
    assert r.status_code == 200
