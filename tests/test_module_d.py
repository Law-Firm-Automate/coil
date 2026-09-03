"""Smoke test for module D: intake, engagements, messages, settings, exports, CLI.

Uses its own SQLite file (data/test_module_d.db) seeded by running seed.py, so it never touches data/practice.db.
Run: .venv/bin/python -m pytest tests/test_module_d.py -q
"""
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_module_d.db")
DB_URI = f"sqlite:///{DB_PATH}"
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_module_d")

from tests.helpers import login  # noqa: E402


@pytest.fixture(scope="module")
def app():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    env = dict(os.environ, DATABASE_URL=DB_URI)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], env=env, cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "seed ok" in out.stdout
    from app import create_app
    a = create_app({"SQLALCHEMY_DATABASE_URI": DB_URI, "PDF_DIR": PDF_DIR, "TESTING": True})
    yield a


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def staff(client):
    tok = login(client)
    return client, tok


def test_public_intake_submit_creates_lead(app, client):
    from app.models import IntakeLead
    r = client.get("/intake/form")
    assert r.status_code == 200 and b'name="website"' in r.data
    r = client.post("/intake/submit", data={"name": "Test Lead Walker", "email": "walker@example.test",
                                            "phone": "512-555-0142", "matter_type": "Family law",
                                            "description": "Custody question.", "adverse_party": "Jordan Walker",
                                            "website": "", "source": "test"})
    assert r.status_code == 200 and b"Thank you" in r.data
    with app.app_context():
        lead = IntakeLead.query.filter_by(email="walker@example.test").first()
        assert lead and lead.status == "new" and lead.adverse_party == "Jordan Walker" and lead.source == "test"
    # honeypot filled: nothing stored
    r = client.post("/intake/submit", data={"name": "Bot", "website": "http://spam", "matter_type": "Other"})
    assert r.status_code == 200
    with app.app_context():
        assert IntakeLead.query.filter_by(name="Bot").count() == 0


def test_convert_lead_sign_flow(app, staff):
    client, tok = staff
    from app.models import IntakeLead, Contact, Matter, ConflictCheck, Engagement, EngagementEvent
    from app.extensions import db
    with app.app_context():
        lead = IntakeLead.query.filter_by(name="Priya Natarajan").first()
        assert lead and lead.status == "new"
        lead_id = lead.id
        matter_count = Matter.query.count()
    r = client.get(f"/intake/{lead_id}")
    assert r.status_code == 200 and b"Conflict preview" in r.data
    r = client.post(f"/intake/{lead_id}/convert", data={
        "_csrf": tok, "contact_mode": "new", "first_name": "Priya", "last_name": "Natarajan",
        "email": "priya@example.com", "phone": "+15125550199", "adverse_party": "",
        "matter_name": "Natarajan - Business formation", "practice_area": "Business formation",
        "billing_type": "flat", "flat_fee": "3,000.00", "split_milestones": "1",
        "milestone1_desc": "Retainer on signing", "milestone1_amount": "1,500.00",
        "milestone2_desc": "Balance", "milestone2_amount": "1,500.00",
        "send_engagement": "1", "scope": "Form a Texas LLC and draft the company agreement.",
    })
    assert r.status_code == 302, r.data[:500]
    assert re.match(r".*/matters/\d+$", r.headers["Location"])
    with app.app_context():
        lead = db.session.get(IntakeLead, lead_id)
        assert lead.status == "converted" and lead.contact_id and lead.matter_id and lead.conflict_check_id
        contact = db.session.get(Contact, lead.contact_id)
        assert contact.is_client and contact.display_name == "Priya Natarajan"
        matter = db.session.get(Matter, lead.matter_id)
        assert Matter.query.count() == matter_count + 1
        assert matter.billing_type == "flat" and matter.flat_fee_cents == 300000
        assert len(matter.milestones) == 2 and [m.amount_cents for m in matter.milestones] == [150000, 150000]
        assert matter.number.startswith("M-")
        check = db.session.get(ConflictCheck, lead.conflict_check_id)
        assert check.outcome in ("clear", "unresolved") and "Priya Natarajan" in check.query
        eng = Engagement.query.filter_by(matter_id=matter.id).first()
        assert eng and eng.status == "sent" and len(eng.document_hash) == 64
        assert eng.sent_to == "priya@example.com"
        assert "A flat fee of $3,000.00" in eng.body_html and "Retainer on signing $1,500.00" in eng.body_html
        assert "Form a Texas LLC" in eng.body_html
        assert EngagementEvent.query.filter_by(engagement_id=eng.id, event="sent").count() == 1
        token, eng_id = eng.token, eng.id

    pub = app.test_client()
    r = pub.get(f"/sign/{token}")
    assert r.status_code == 200 and b"Sign this letter" in r.data
    with app.app_context():
        eng = db.session.get(Engagement, eng_id)
        assert eng.view_count == 1 and eng.status == "viewed" and eng.first_viewed_at is not None
    r = pub.get(f"/track/engagement/{token}.gif")
    assert r.status_code == 200 and r.mimetype == "image/gif" and "no-store" in r.headers["Cache-Control"]
    with app.app_context():
        assert db.session.get(Engagement, eng_id).view_count == 1  # deduped inside 60s

    # missing checkbox is refused
    r = pub.post(f"/sign/{token}", data={"signer_name": "Priya Natarajan", "signer_email": "priya@example.com"})
    assert r.status_code == 400
    r = pub.post(f"/sign/{token}", data={"signer_name": "Priya Natarajan", "signer_email": "priya@example.com",
                                         "agree": "1"}, headers={"User-Agent": "pytest-browser/1.0"})
    assert r.status_code == 200 and b"Thank you, Priya Natarajan" in r.data
    with app.app_context():
        eng = db.session.get(Engagement, eng_id)
        assert eng.status == "signed" and len(eng.signature_hash) == 64
        assert eng.signer_name == "Priya Natarajan" and eng.signer_ua == "pytest-browser/1.0" and eng.signed_at
        assert eng.pdf_path and os.path.exists(eng.pdf_path)
        assert EngagementEvent.query.filter_by(engagement_id=eng.id, event="signed").count() == 1
    # signed page no longer shows the form; PDF is downloadable publicly and by staff
    r = pub.get(f"/sign/{token}")
    assert r.status_code == 200 and b"Sign this letter" not in r.data and b"signed by" in r.data
    r = pub.get(f"/sign/{token}/pdf")
    assert r.status_code == 200 and r.data[:4] == b"%PDF"
    r = client.get(f"/engagements/{eng_id}/pdf")
    assert r.status_code == 200 and r.data[:4] == b"%PDF"
    r = client.get(f"/engagements/{eng_id}")
    assert r.status_code == 200 and b"pytest-browser" in r.data
    r = client.get("/engagements?status=signed")
    assert r.status_code == 200 and b"Priya Natarajan" in r.data


def test_engagement_new_draft_pdf_and_templates(app, staff):
    client, tok = staff
    from app.models import Matter, Engagement, LetterTemplate
    with app.app_context():
        m = Matter.query.filter_by(number="M-1002").first()
        mid = m.id
    r = client.get(f"/engagements/new?matter_id={mid}")
    assert r.status_code == 200 and b"per hour billed in 0.1 hour increments" in r.data
    r = client.post("/engagements/new", data={"_csrf": tok, "matter_id": mid, "action": "draft",
                                              "scope": "Defend the contract claim.", "body_html": "<p>Custom body</p>"})
    assert r.status_code == 302
    with app.app_context():
        e = Engagement.query.filter_by(matter_id=mid).order_by(Engagement.id.desc()).first()
        assert e.status == "draft" and e.body_html == "<p>Custom body</p>"
        eid = e.id
    r = client.get(f"/engagements/{eid}/pdf")
    assert r.status_code == 200 and r.data[:4] == b"%PDF"
    r = client.post(f"/engagements/{eid}/void", data={"_csrf": tok})
    assert r.status_code == 302
    # templates CRUD
    r = client.post("/engagements/templates/new", data={"_csrf": tok, "name": "Short letter", "kind": "engagement",
                                                        "subject": "Letter: {{ matter_name }}",
                                                        "body_html": "<p>{{ client_name }} {{ fee_summary }}</p>",
                                                        "is_default": "0"})
    assert r.status_code == 302
    r = client.post("/engagements/templates/new", data={"_csrf": tok, "name": "Broken", "body_html": "{% if %}"})
    assert r.status_code == 200 and b"syntax error" in r.data
    with app.app_context():
        t = LetterTemplate.query.filter_by(name="Short letter").first()
        assert t and not t.is_default
        tid = t.id
    r = client.get("/engagements/templates")
    assert r.status_code == 200 and b"Short letter" in r.data
    r = client.post(f"/engagements/templates/{tid}/delete", data={"_csrf": tok})
    assert r.status_code == 302


def test_messages_send_and_twilio_inbound(app, staff):
    client, tok = staff
    from app.models import Contact, Message
    with app.app_context():
        maria = Contact.query.filter_by(last_name="Alvarez").first()
        mid = maria.id
    r = client.post("/messages/send", data={"_csrf": tok, "contact_id": mid, "body": "Hi Maria, your documents are ready."})
    assert r.status_code == 302
    with app.app_context():
        m = Message.query.filter_by(contact_id=mid, direction="out").order_by(Message.id.desc()).first()
        assert m and m.status == "unconfigured" and m.to_addr == "+15125550111"
    r = app.test_client().post("/webhooks/twilio", data={"From": "+15125550111", "To": "+15125550100",
                                                          "Body": "Great, thanks!", "MessageSid": "SMtest0001"})
    assert r.status_code == 200 and r.mimetype == "text/xml" and b"<Response></Response>" in r.data
    with app.app_context():
        m = Message.query.filter_by(provider_id="SMtest0001").first()
        assert m and m.direction == "in" and m.contact_id == mid and m.contact.display_name == "Maria Alvarez"
    r = client.get("/messages")
    assert r.status_code == 200 and b"Maria Alvarez" in r.data and b"1 new" in r.data
    r = client.get(f"/messages/{mid}")
    assert r.status_code == 200 and b"Great, thanks!" in r.data and b"M-1001" in r.data


def test_settings_surcharge_and_users(app, staff):
    client, tok = staff
    from app.models import Firm, User
    r = client.get("/settings")
    assert r.status_code == 200
    r = client.post("/settings", data={"_csrf": tok, "_form": "1", "name": "Demo Law PLLC", "surcharge_pct": "2.5",
                                       "surcharge_enabled": "1", "daily_agenda_email": "1", "default_rate": "350.00",
                                       "invoice_terms_days": "30", "next_invoice_number": "1001",
                                       "next_matter_number": "1005"})
    assert r.status_code == 302
    with app.app_context():
        f = Firm.get()
        assert f.surcharge_bps == 250 and f.surcharge_enabled and f.default_rate_cents == 35000
    r = client.post("/settings/users/new", data={"_csrf": tok, "name": "Sam Staff", "email": "sam@example.test",
                                                 "role": "staff", "hourly_rate": "200", "password": "password123"})
    assert r.status_code == 302
    with app.app_context():
        u = User.query.filter_by(email="sam@example.test").first()
        assert u and u.role == "staff" and u.hourly_rate_cents == 20000 and u.initials == "SS" and u.check_password("password123")
        uid = u.id
    r = client.post(f"/settings/users/{uid}/edit", data={"_csrf": tok, "name": "Sam Staff", "email": "sam@example.test",
                                                         "role": "staff", "hourly_rate": "210", "initials": "SS", "is_active": "1"})
    assert r.status_code == 302
    r = client.get("/settings/users")
    assert r.status_code == 200 and b"Sam Staff" in r.data
    r = client.get("/settings/integrations")
    assert r.status_code == 200 and b"/webhooks/stripe" in r.data and b"/webhooks/twilio" in r.data
    r = client.get("/dev/outbox")
    assert r.status_code == 200 and b"Engagement letter" in r.data


def test_exports_headers(staff):
    client, _ = staff
    expected = {
        "/exports/quickbooks/invoices.csv": "InvoiceNo,Customer,InvoiceDate,DueDate,Item(Product/Service),ItemDescription,ItemQuantity,ItemRate,ItemAmount",
        "/exports/quickbooks/payments.csv": "PaymentDate,Customer,InvoiceNo,Amount,Method,Reference",
        "/exports/quickbooks/customers.csv": "Name,Company,Email,Phone,Billing Address",
        "/exports/time.csv": "Id,Date,MatterNumber,Matter,Client,User,Hours,Minutes,Rate,Amount,Billable,InvoiceNo,ActivityCode,Description",
        "/exports/trust.csv": "Id,Date,Type,Client,MatterNumber,Matter,Amount,Description,Payee,Reference,InvoiceNo,Cleared,ClearedOn,CreatedBy,CreatedAt",
        "/exports/contacts.csv": "Id,Kind,FirstName,LastName,Company,Email,Phone,Address,Tags,IsClient,Aliases,CreatedAt",
    }
    r = client.get("/exports")
    assert r.status_code == 200 and b"QuickBooks" in r.data
    for path, header in expected.items():
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
        assert r.data.decode().splitlines()[0] == header, path
    body = client.get("/exports/time.csv").data.decode()
    assert "M-1002" in body and "1.50" in body


def test_cli_agenda_and_reminders_idempotent(app):
    from app.cli import run_agenda, run_reminders
    from app.models import AuditLog, User, Invoice, Engagement, Matter, InvoiceEvent, EngagementEvent
    from app.extensions import db
    from app.blueprints.engagements import build_engagement, send_engagement
    with app.app_context():
        m2 = Matter.query.filter_by(number="M-1002").first()
        inv = Invoice(number="INV-TEST-7", matter_id=m2.id, client_id=m2.client_id, kind="hourly", status="sent",
                      issued_on=date.today() - timedelta(days=37), due_on=date.today() - timedelta(days=7),
                      subtotal_cents=70000, total_cents=70000, sent_at=datetime.utcnow() - timedelta(days=37),
                      sent_to="ap@bluebonnet.test")
        db.session.add(inv)
        e = build_engagement(m2, scope="Reminder test")
        send_engagement(e)
        e.sent_at = datetime.utcnow() - timedelta(days=3)
        db.session.commit()
        inv_id, eng_id = inv.id, e.id
        active_users = User.query.filter_by(is_active=True).count()
        assert active_users >= 2

        assert run_agenda() == active_users
        assert run_agenda() == 0
        assert AuditLog.query.filter_by(action="agenda_sent").count() == active_users

        assert run_reminders() == (1, 1)
        assert run_reminders() == (0, 0)
        assert AuditLog.query.filter_by(action="reminder_sent").count() == 2
        assert InvoiceEvent.query.filter_by(invoice_id=inv_id, event="reminder").count() == 1
        assert EngagementEvent.query.filter_by(engagement_id=eng_id, event="reminder").count() == 1
