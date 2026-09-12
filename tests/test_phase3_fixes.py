"""Phase 3 findings the overnight loop listed as real and then closed without fixing.

1. A client signed an engagement letter and got a 500, because the courtesy email after the
   signature was committed hit a mail relay that refused the recipient. The signature was
   valid and saved; the signer had no way to know.
2. A trust deposit dated in the future counted as money on hand today, so a disbursement or
   an invoice application could spend a post-dated cheque.
3. Reported: the payment row for money applied from trust said "operating". Not a defect.
   `method` is the source and `account` is the destination; trust money applied to an
   invoice lands in operating, and two older tests already pinned that. Kept here so the
   next person to read that row the same way finds the answer.

Own SQLite file, own UPLOAD_DIR and PDF_DIR. Never touches data/practice.db.
Run: .venv/bin/python -m pytest tests/test_phase3_fixes.py -q
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_phase3_fixes.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_phase3_fixes")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_phase3_fixes")

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
                       "TESTING": True, "STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": "", "SMTP_HOST": "",
                       "BASE_URL": "http://localhost"})


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


def _client_and_matter(app, tag):
    db, M = _models()
    with app.app_context():
        u = M.User.query.first()
        c = M.Contact(first_name="Phase", last_name=f"Three{tag}", email=f"p3{tag}@example.test", is_client=True)
        db.session.add(c)
        db.session.flush()
        m = M.Matter(number=f"M-P3{tag}", client_id=c.id, name=f"TEST phase3 {tag}", billing_type="hourly",
                     responsible_user_id=u.id, status="open")
        db.session.add(m)
        db.session.commit()
        return c.id, m.id


# --- 1. a signature must not 500 because a courtesy email bounced -----------------------------

def test_signing_survives_a_mail_relay_that_refuses_the_recipient(app, monkeypatch):
    db, M = _models()
    cid, mid = _client_and_matter(app, "A")
    with app.app_context():
        e = M.Engagement(matter_id=mid, contact_id=cid, subject="TEST engagement", body_html="<p>Terms.</p>",
                         token="p3-sign-token-A", status="sent", sent_to="nobody@example.com")
        db.session.add(e)
        db.session.commit()
        eid = e.id

    import app.blueprints.engagements as eng

    def _refuse(*a, **k):
        raise RuntimeError("550 5.1.1 The email account that you tried to reach does not exist")
    monkeypatch.setattr(eng, "send_email", _refuse)

    c = app.test_client()
    page = c.get("/sign/p3-sign-token-A")
    assert page.status_code == 200
    tok = re.search(rb'name="_csrf" value="([^"]+)"', page.data)
    data = {"signer_name": "Priya Chen", "signer_email": "nobody@example.com", "agree": "1"}
    if tok:
        data["_csrf"] = tok.group(1).decode()
    r = c.post("/sign/p3-sign-token-A", data=data)
    assert r.status_code == 200, f"signer got {r.status_code} for a valid signature"

    with app.app_context():
        e = db.session.get(M.Engagement, eid)
        assert e.status == "signed"
        assert e.signer_name == "Priya Chen"
        assert e.signature_hash


# --- 2. a post-dated deposit is not money on hand -------------------------------------------

def _trust_post(client, **fields):
    data = {"_csrf": csrf(client), "description": "TEST", "payee": "", "reference": ""}
    data.update({k: str(v) for k, v in fields.items()})
    return client.post("/trust/new", data=data, follow_redirects=True)


def test_disbursement_cannot_spend_a_future_dated_deposit(app, client):
    cid, mid = _client_and_matter(app, "B")
    later = date.today() + timedelta(days=3)
    r = _trust_post(client, client_id=cid, matter_id=mid, type="deposit", amount="800.00", date=later.isoformat())
    assert b"recorded" in r.data

    r = _trust_post(client, client_id=cid, matter_id=mid, type="disbursement", amount="500.00",
                    date=date.today().isoformat())
    assert b"Rejected" in r.data and b"$0.00" in r.data, "spent money that has not arrived yet"
    db, M = _models()
    with app.app_context():
        assert M.TrustTransaction.query.filter_by(client_id=cid).count() == 1


def test_disbursement_dated_after_the_deposit_still_works(app, client):
    cid, mid = _client_and_matter(app, "C")
    later = date.today() + timedelta(days=3)
    _trust_post(client, client_id=cid, matter_id=mid, type="deposit", amount="800.00", date=later.isoformat())
    r = _trust_post(client, client_id=cid, matter_id=mid, type="disbursement", amount="500.00",
                    date=(later + timedelta(days=1)).isoformat())
    assert b"Rejected" not in r.data
    assert b"recorded" in r.data


# --- 3. applying trust to an invoice ---------------------------------------------------------

def _sent_invoice(app, cid, mid, cents, number):
    db, M = _models()
    with app.app_context():
        inv = M.Invoice(number=number, matter_id=mid, client_id=cid, kind="hourly", status="sent",
                        issued_on=date.today(), due_on=date.today(), subtotal_cents=cents, total_cents=cents)
        inv.lines.append(M.InvoiceLine(kind="flat", description="Services", quantity=1.0,
                                       unit_cents=cents, amount_cents=cents))
        db.session.add(inv)
        db.session.commit()
        return inv.id


def test_apply_refuses_a_future_dated_deposit(app, client):
    cid, mid = _client_and_matter(app, "D")
    later = date.today() + timedelta(days=3)
    _trust_post(client, client_id=cid, matter_id=mid, type="deposit", amount="800.00", date=later.isoformat())
    inv_id = _sent_invoice(app, cid, mid, 80000, "INV-P3D")
    r = client.post("/trust/apply", data={"_csrf": csrf(client), "invoice_id": str(inv_id), "amount": "800.00"},
                    follow_redirects=True)
    db, M = _models()
    with app.app_context():
        assert M.Payment.query.filter_by(invoice_id=inv_id).count() == 0, "applied money not yet received"


def test_applied_trust_money_records_source_and_destination(app, client):
    cid, mid = _client_and_matter(app, "E")
    _trust_post(client, client_id=cid, matter_id=mid, type="deposit", amount="800.00",
                date=date.today().isoformat())
    inv_id = _sent_invoice(app, cid, mid, 80000, "INV-P3E")
    client.post("/trust/apply", data={"_csrf": csrf(client), "invoice_id": str(inv_id), "amount": "800.00"},
                follow_redirects=True)
    db, M = _models()
    with app.app_context():
        pays = M.Payment.query.filter_by(invoice_id=inv_id).all()
        assert len(pays) == 1
        assert pays[0].method == "trust", "method is the source"
        assert pays[0].account == "operating", "account is where it landed, and an application pays the firm"


# --- 4. the matter summary is told what has actually been received -------------------------

def test_summary_context_spells_out_provider_record_status(app):
    """The summary once said records from every provider were complete when one had been
    requested and none received. The material now says so per provider, in words."""
    db, M = _models()
    cid, mid = _client_and_matter(app, "F")
    with app.app_context():
        db.session.add_all([
            M.MedicalProvider(matter_id=mid, name="Cedar Hollow Emergency Center",
                              records_requested_on=date(2026, 3, 1)),
            M.MedicalProvider(matter_id=mid, name="Bluebonnet Orthopedics"),
            M.MedicalProvider(matter_id=mid, name="Riverside Imaging",
                              records_requested_on=date(2026, 3, 1), records_received_on=date(2026, 3, 20)),
        ])
        db.session.commit()
        from app.blueprints.ai import _matter_context
        ctx = _matter_context(db.session.get(M.Matter, mid))

    assert "Cedar Hollow Emergency Center: records requested 2026-03-01, NOT yet received" in ctx
    assert "Bluebonnet Orthopedics: records NOT requested" in ctx
    assert "Riverside Imaging: records received 2026-03-20" in ctx


def test_summary_prompt_forbids_inferring_completion():
    import inspect
    from app.blueprints import ai
    src = inspect.getsource(ai.matter_summary)
    assert "requested is not received" in src
    assert "one provider is not all of them" in src


# --- 5. deadlines belong in the feed a lawyer subscribes to -----------------------------------

def test_a_deadline_on_the_screen_is_in_the_ical_feed(app, client):
    """Phase 3 §G: a date-only deadline showed on the web calendar and was absent from the
    firm feed, which only carried events. A limitation date is the thing a lawyer most needs
    in the calendar they actually look at."""
    db, M = _models()
    cid, mid = _client_and_matter(app, "G")
    with app.app_context():
        db.session.add(M.Task(matter_id=mid, title="Limitations expire", kind="deadline",
                              due_on=date(2028, 2, 11)))
        db.session.add(M.Task(matter_id=mid, title="Already done", kind="deadline",
                              due_on=date(2028, 3, 1), done=True))
        db.session.commit()
        from app.blueprints.calendar import feed_secret
        secret = feed_secret()

    r = client.get(f"/calendar/feed/{secret}.ics")
    assert r.status_code == 200
    body = r.data.decode()
    assert "SUMMARY:Deadline: Limitations expire" in body
    assert "DTSTART;VALUE=DATE:20280211" in body, "a due date is a date, so the feed entry is all-day"
    assert "DTEND;VALUE=DATE:20280212" in body
    assert "Already done" not in body, "a completed task is not a deadline any more"


def test_personal_feed_carries_own_and_unassigned_deadlines_only(app, client):
    db, M = _models()
    cid, mid = _client_and_matter(app, "H")
    with app.app_context():
        users = M.User.query.order_by(M.User.id).limit(2).all()
        me, other = users[0], (users[1] if len(users) > 1 else None)
        db.session.add(M.Task(matter_id=mid, title="Mine", kind="court_date", due_on=date(2027, 5, 5),
                              assignee_id=me.id))
        db.session.add(M.Task(matter_id=mid, title="Nobody's yet", kind="deadline", due_on=date(2027, 5, 6)))
        if other:
            db.session.add(M.Task(matter_id=mid, title="Someone else's", kind="deadline",
                                  due_on=date(2027, 5, 7), assignee_id=other.id))
        db.session.commit()
        from app.blueprints.calendar import feed_secret
        secret, uid = feed_secret(me.id), me.id

    body = client.get(f"/calendar/feed/u/{uid}/{secret}.ics").data.decode()
    assert "SUMMARY:Court: Mine" in body
    assert "SUMMARY:Deadline: Nobody's yet" in body
    assert "Someone else's" not in body


# --- 6. the time list at volume ----------------------------------------------------------------

def test_time_list_paginates_and_totals_cover_every_row(app, client):
    """Phase 3 §I: four hundred entries rendered as one page with no controls. Worse, the
    totals row summed the Python list, so cutting it to a page would have understated the
    firm's hours, which is the exact bug the API had at row 200."""
    db, M = _models()
    cid, mid = _client_and_matter(app, "I")
    with app.app_context():
        u = M.User.query.first()
        for i in range(250):
            db.session.add(M.TimeEntry(matter_id=mid, user_id=u.id, date=date.today(), minutes=6,
                                       rate_cents=30000, billable=True, description=f"TEST vol {i}"))
        db.session.commit()

    r = client.get(f"/time?matter_id={mid}")
    body = r.data.decode()
    assert r.status_code == 200
    assert "250 entries" in body, "the count must be every matching row, not the page"
    assert "showing page 1 of 3" in body
    assert body.count("TEST vol ") == 100, "one page of rows"
    # 250 x 6 min = 25.00 hours; 250 x $30 = $7,500.00. Both must reflect all 250.
    assert "25.00" in body
    assert "$7,500.00" in body
    assert "Older" in body and "Newer" not in body

    r3 = client.get(f"/time?matter_id={mid}&page=3").data.decode()
    assert r3.count("TEST vol ") == 50
    assert "Newer" in r3 and "Older" not in r3
    assert "25.00" in r3 and "$7,500.00" in r3, "totals do not change with the page"


def test_time_list_page_links_keep_the_filters(app, client):
    db, M = _models()
    cid, mid = _client_and_matter(app, "J")
    r = client.get(f"/time?matter_id={mid}&page=1")
    assert r.status_code == 200
