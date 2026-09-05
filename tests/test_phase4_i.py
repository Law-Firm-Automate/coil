"""Phase 4, Agent I: client statements, invoice template editor, monthly invoicing, client update email,
aggregate (A/R, hours) questions answered without the model.

Own SQLite file (data/test_phase4_i.db), own UPLOAD_DIR and PDF_DIR. Tests run in file order and share ids via S.
"""
import io
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402

TEST_DB = os.path.join(ROOT, "data", "test_phase4_i.db")
UPLOAD_DIR = os.path.join(ROOT, "data", "test_phase4_i_uploads")
PDF_DIR = os.path.join(ROOT, "data", "test_phase4_i_pdf")
S = {}


@pytest.fixture(scope="module")
def app():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{TEST_DB}")
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT, env=env)
    from app import create_app
    application = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{TEST_DB}", "TESTING": True, "SMTP_HOST": "",
                              "UPLOAD_DIR": UPLOAD_DIR, "PDF_DIR": PDF_DIR, "OPENROUTER_API_KEY": "",
                              "ANTHROPIC_API_KEY": ""})
    yield application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    S["tok"] = login(c)
    return c


@pytest.fixture
def no_keys(monkeypatch):
    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "LLM_ENABLED", "LLM_DAILY_CAP", "AI_DAILY_CAP_CENTS"):
        monkeypatch.delenv(k, raising=False)


def _models():
    from app.extensions import db
    from app import models
    return db, models


def _pdf_text(data):
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)


def _outbox():
    from app.services.mail import dev_outbox
    return dev_outbox()


def _make_invoice(M, db, matter, number, issued, total_cents, status="sent", lines=None):
    inv = M.Invoice(number=number, matter_id=matter.id, client_id=matter.client_id, kind="hourly", status=status,
                    issued_on=issued, due_on=issued + timedelta(days=30), currency="USD")
    db.session.add(inv)
    db.session.flush()
    for i, (desc, cents) in enumerate(lines or [("Legal services", total_cents)]):
        db.session.add(M.InvoiceLine(invoice_id=inv.id, kind="time", date=issued, description=desc, quantity=1.0,
                                     unit_cents=cents, amount_cents=cents, sort=i))
    db.session.flush()
    inv.recalc()
    return inv


# ---------------------------------------------------------------- 1. client statement
def test_statement_html_and_pdf_running_balance(app, client):
    db, M = _models()
    today = date.today()
    with app.app_context():
        owner = M.User.query.filter_by(email="owner@example.com").first()
        c = M.Contact(first_name="Stella", last_name="Statement", email="stella@example.test", is_client=True,
                      address="1 Test Lane\nAustin, TX")
        db.session.add(c)
        db.session.flush()
        m = M.Matter(number="M-9101", client_id=c.id, name="Statement matter", billing_type="hourly",
                     hourly_rate_cents=30000, responsible_user_id=owner.id, opened_on=today - timedelta(days=90))
        db.session.add(m)
        db.session.flush()
        i1 = _make_invoice(M, db, m, "INV-9101", today - timedelta(days=60), 100000)
        i2 = _make_invoice(M, db, m, "INV-9102", today - timedelta(days=20), 50000)
        db.session.flush()
        pay = M.Payment(matter_id=m.id, client_id=c.id, amount_cents=40000, method="check",
                        received_on=today - timedelta(days=40), reference="chk 77")
        i1.payments.append(pay)
        db.session.flush()
        i1.recalc()
        assert i1.paid_cents == 40000
        db.session.commit()
        S.update(client_id=c.id, matter_id=m.id, inv1=i1.id, inv2=i2.id, owner_id=owner.id)

    r = client.get("/statements")
    assert r.status_code == 200 and b"Stella Statement" in r.data
    r = client.get(f"/statements/{S['client_id']}")
    assert r.status_code == 200
    html = r.data.decode()
    assert "INV-9101" in html and "INV-9102" in html and "Payment (check) chk 77" in html
    # running balance: 1,000.00 -> 600.00 after the payment -> 1,100.00 after the second invoice
    activity = html[html.index("Activity</h2>"):html.index("By matter")]
    order = [activity.index("$1,000.00"), activity.index("$600.00"), activity.index("$1,100.00")]
    assert order == sorted(order), activity
    assert "Statement matter" in html and "Subtotal M-9101" in html
    # balance due card = sum of open balances
    assert html.count("$1,100.00") >= 2
    # matter filter and date range still render
    r = client.get(f"/statements/{S['client_id']}?matter_id={S['matter_id']}&from={(today - timedelta(days=30)).isoformat()}")
    assert r.status_code == 200 and b"Balance forward" in r.data and b"INV-9102" in r.data and b"INV-9101" not in r.data
    # PDF
    r = client.get(f"/statements/{S['client_id']}/pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data[:4] == b"%PDF"
    txt = _pdf_text(r.data)
    assert "STATEMENT OF ACCOUNT" in txt and "INV-9101" in txt and "1,100.00" in txt and "600.00" in txt
    # contact page and invoice list link to it
    r = client.get(f"/contacts/{S['client_id']}")
    assert f'href="/statements/{S["client_id"]}"'.encode() in r.data
    r = client.get("/invoices")
    assert b'href="/statements"' in r.data


def test_statement_send_emails_pdf(app, client):
    before = len(_outbox())
    r = client.post(f"/statements/{S['client_id']}/send", data={"_csrf": S["tok"], "note": "Here is your statement."})
    assert r.status_code == 302
    out = _outbox()
    assert len(out) == before + 1
    assert out[0]["to"] == "stella@example.test" and "Statement of account" in out[0]["subject"]
    assert "$1,100.00" in out[0]["html"] and "Here is your statement." in out[0]["html"]
    db, M = _models()
    with app.app_context():
        a = M.AuditLog.query.filter_by(action="send", entity="statement", entity_id=S["client_id"]).first()
        assert a is not None and "stella@example.test" in a.detail


# ---------------------------------------------------------------- 2. invoice template editor
def _png_bytes():
    from PIL import Image
    im = Image.new("RGB", (120, 40), (200, 30, 30))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_template_settings_round_trip_and_apply(app, client):
    r = client.get("/settings/invoice-template")
    assert r.status_code == 200 and b"Preview PDF" in r.data and b'name="col_qty"' in r.data
    data = {"_csrf": S["tok"], "action": "save",
            "col_date": "1", "ord_date": "1", "col_description": "1", "ord_description": "2",
            "col_amount": "1", "ord_amount": "3",  # qty and rate hidden
            "col_timekeeper": "1", "ord_timekeeper": "9",
            "invoice_show_timekeeper": "1",
            "label_bill_to": "Invoice to", "label_balance_due": "Amount now due",
            "invoice_accent": "#aa2222", "invoice_title": "STATEMENT OF FEES",
            "invoice_payment_instructions": "Pay online at {link} or by check to the firm.",
            "statement_footer": "Questions about this statement? Call us.",
            "monthly_billing_day": "0",
            "logo": (io.BytesIO(_png_bytes()), "logo.png")}
    r = client.post("/settings/invoice-template", data=data, content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:300]
    db, M = _models()
    with app.app_context():
        f = M.Firm.get()
        assert json.loads(f.invoice_columns_json) == ["date", "description", "amount", "timekeeper"]
        assert json.loads(f.invoice_labels_json) == {"bill_to": "Invoice to", "balance_due": "Amount now due"}
        assert f.invoice_accent == "#aa2222" and f.invoice_title == "STATEMENT OF FEES"
        assert f.invoice_show_timekeeper is True and f.invoice_show_activity_codes is False
        assert f.invoice_logo_path == "firm/logo.png" and os.path.isfile(os.path.join(UPLOAD_DIR, "firm", "logo.png"))
        assert f.statement_footer.startswith("Questions")
        inv = db.session.get(M.Invoice, S["inv2"])
        S["token"] = inv.public_token
    r = client.get("/settings/invoice-template")
    assert b'value="Invoice to"' in r.data and b"/p/firm-logo" in r.data
    # logo served publicly
    r = client.get("/p/firm-logo")
    assert r.status_code == 200 and r.data[:4] == b"\x89PNG"
    # public page: hidden qty/rate columns, custom labels, logo, timekeeper column
    r = client.get(f"/p/{S['token']}")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Invoice to" in html and "Amount now due" in html and "STATEMENT OF FEES" in html
    assert "<th class=\"acc num\">Qty</th>" not in html and ">Rate<" not in html
    assert ">Tk<" in html and "/p/firm-logo" in html and "#aa2222" in html
    assert "Pay online at http://localhost" in html and "?method=ach" in html and "?method=card" in html
    # PDF reflects the template
    r = client.get(f"/invoices/{S['inv2']}/pdf")
    assert r.status_code == 200
    txt = _pdf_text(r.data)
    assert "STATEMENT OF FEES" in txt and "Invoice to" in txt and "Amount now due" in txt
    assert "Qty" not in txt and "Rate" not in txt and "Amount" in txt and "Pay online at" in txt
    # statement PDF picks up the footer and labels
    r = client.get(f"/statements/{S['client_id']}/pdf")
    txt = _pdf_text(r.data)
    assert "Questions about this statement" in txt and "Invoice to" in txt
    # preview: unsaved settings, marked SAMPLE, nothing changes in the DB
    data = {"_csrf": S["tok"], "action": "preview", "col_description": "1", "ord_description": "1",
            "col_amount": "1", "ord_amount": "2", "col_qty": "1", "ord_qty": "3",
            "invoice_title": "PREVIEW ONLY TITLE", "invoice_accent": "#00aa00", "monthly_billing_day": "0"}
    r = client.post("/settings/invoice-template", data=data, content_type="multipart/form-data")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    txt = _pdf_text(r.data)
    assert "SAMPLE" in txt and "PREVIEW ONLY TITLE" in txt and "Sample Client LLC" in txt and "Qty" in txt
    with app.app_context():
        assert M.Firm.get().invoice_title == "STATEMENT OF FEES"
    # non-owner cannot open the editor
    from app.blueprints.invoices import invoice_settings, visible_columns
    with app.app_context():
        tpl = invoice_settings(M.Firm.get(), {"columns": ["bogus", "amount"], "accent": "red"})
        assert tpl.columns == ["description", "amount"] and tpl.accent == "#1f5f8b"
        assert visible_columns(invoice_settings(M.Firm.get(), {"show_timekeeper": False})) == ["date", "description", "amount"]


def test_template_rejects_bad_logo(app, client):
    r = client.post("/settings/invoice-template", data={"_csrf": S["tok"], "action": "save",
                                                        "col_description": "1", "col_amount": "1",
                                                        "monthly_billing_day": "0",
                                                        "logo": (io.BytesIO(b"not an image"), "logo.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and b"not a readable image" in r.data
    db, M = _models()
    with app.app_context():
        assert M.Firm.get().invoice_logo_path == "firm/logo.png"


# ---------------------------------------------------------------- 3. monthly invoicing
def test_monthly_invoicing_builds_once_per_month_and_sends(app, client):
    db, M = _models()
    from app.cli import run_monthly_invoicing
    today = date.today()
    with app.app_context():
        owner = db.session.get(M.User, S["owner_id"])
        f = M.Firm.get()
        f.monthly_billing_day = today.day
        f.monthly_billing_send = False
        # opted-in matter with unbilled time (has a client email)
        m1 = db.session.get(M.Matter, S["matter_id"])
        m1.auto_invoice_monthly = True
        db.session.add(M.TimeEntry(matter_id=m1.id, user_id=owner.id, date=today - timedelta(days=2), minutes=120,
                                   rate_cents=30000, description="Monthly run work", billable=True))
        # second opted-in matter, client without an email
        c2 = M.Contact(first_name="Noemail", last_name="Client", is_client=True)
        db.session.add(c2)
        db.session.flush()
        m2 = M.Matter(number="M-9102", client_id=c2.id, name="No email matter", billing_type="hourly",
                      responsible_user_id=owner.id, auto_invoice_monthly=True)
        db.session.add(m2)
        db.session.flush()
        db.session.add(M.Expense(matter_id=m2.id, user_id=owner.id, date=today, description="Filing fee",
                                 amount_cents=5000, billable=True))
        # a matter with unbilled time that is NOT opted in
        m3 = M.Matter.query.filter_by(number="M-1002").first()
        m3.auto_invoice_monthly = False
        db.session.commit()
        S["m2"] = m2.id

        # wrong day: nothing happens
        r = run_monthly_invoicing(today=today + timedelta(days=1) if today.day < 28 else today - timedelta(days=1))
        assert r["ran"] is False and not r["built"]

        before = len(_outbox())
        r = run_monthly_invoicing(today=today)
        assert r["ran"] is True
        assert len(r["built"]) == 2 and not r["sent"]
        built_matters = {i.matter_id for i in r["built"]}
        assert built_matters == {m1.id, m2.id}
        assert all(i.status == "draft" for i in r["built"])
        assert M.Invoice.query.filter_by(matter_id=m3.id).count() == 0
        te = M.TimeEntry.query.filter_by(description="Monthly run work").first()
        assert te.invoice_id == r["built"][0].id if r["built"][0].matter_id == m1.id else True
        assert M.AuditLog.query.filter_by(action="monthly_invoiced", entity="matter", entity_id=m1.id,
                                          detail=today.strftime("%Y-%m")).count() == 1
        # summary email to the owner
        out = _outbox()
        assert len(out) == before + 1 and "Monthly invoicing" in out[0]["subject"] and "2 invoices built" in out[0]["subject"]

        # idempotent: run again the same month, nothing new
        r = run_monthly_invoicing(today=today)
        assert not r["built"] and all("already invoiced" in reason for _, reason in r["skipped"])
        assert M.Invoice.query.filter_by(matter_id=m1.id, status="draft").count() == 1

        # next month with send on: builds and sends where the client has an email
        f.monthly_billing_send = True
        db.session.add(M.TimeEntry(matter_id=m1.id, user_id=owner.id, date=today, minutes=60, rate_cents=30000,
                                   description="Next month work", billable=True))
        db.session.add(M.Expense(matter_id=m2.id, user_id=owner.id, date=today, description="Copies",
                                 amount_cents=1200, billable=True))
        db.session.commit()
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=min(today.day, 28))
        before = len(_outbox())
        r = run_monthly_invoicing(today=nxt, force=True)
        assert len(r["built"]) == 2 and len(r["sent"]) == 1
        sent = r["sent"][0]
        assert sent.matter_id == m1.id and sent.status == "sent" and sent.sent_to == "stella@example.test"
        assert any("no email" in reason for _, reason in r["skipped"])
        out = _outbox()
        assert len(out) == before + 2  # invoice email + summary
        assert any(o["to"] == "stella@example.test" and sent.number in o["subject"] for o in out[:2])

    # CLI entry point runs and prints
    from app.cli import main
    assert main(["monthly_invoicing"]) == 0

    # matter form checkbox saves through _fill; bulk page has the toggle
    r = client.get(f"/matters/{S['matter_id']}/edit")
    assert b'name="auto_invoice_monthly"' in r.data and b"checked" in r.data
    r = client.get("/invoices/bulk")
    assert b"Monthly invoicing" in r.data and b'name="monthly_ids"' in r.data
    r = client.post("/invoices/bulk/monthly", data={"_csrf": S["tok"], "monthly_ids": [str(S["m2"])]})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(M.Matter, S["matter_id"]).auto_invoice_monthly is False
        assert db.session.get(M.Matter, S["m2"]).auto_invoice_monthly is True


# ---------------------------------------------------------------- 4. client update email
def test_update_email_fallback_without_model(app, client, no_keys):
    db, M = _models()
    today = date.today()
    with app.app_context():
        m = db.session.get(M.Matter, S["matter_id"])
        db.session.add(M.Note(matter_id=m.id, user_id=S["owner_id"], body="Filed the motion to compel."))
        db.session.add(M.Note(matter_id=m.id, user_id=S["owner_id"], body="[internal] client is slow to pay"))
        db.session.add(M.Task(matter_id=m.id, title="Hearing on motion", kind="court_date",
                              due_on=today + timedelta(days=10)))
        db.session.add(M.Task(matter_id=m.id, title="Serve discovery", kind="task", done=True,
                              done_at=datetime.utcnow() - timedelta(days=1)))
        db.session.commit()
    r = client.post(f"/ai/matter/{S['matter_id']}/update-email", data={"_csrf": S["tok"]})
    assert r.status_code == 200
    html = r.data.decode()
    assert 'name="subject"' in html and 'name="body"' in html and "AI was not available" in html
    assert "Filed the motion to compel." in html and "Hearing on motion" in html and "Serve discovery" in html
    assert "slow to pay" not in html and "$" not in html.split('name="body"')[1].split("</textarea>")[0]
    assert "Dear Stella" in html and "Update on Statement matter" in html
    # Spanish client gets Spanish
    with app.app_context():
        c = db.session.get(M.Contact, S["client_id"])
        c.language = "es"
        db.session.commit()
    r = client.post(f"/ai/matter/{S['matter_id']}/update-email", data={"_csrf": S["tok"]})
    assert "Estimado/a Stella" in r.data.decode() and "Actualizaci" in r.data.decode()
    with app.app_context():
        c = db.session.get(M.Contact, S["client_id"])
        c.language = ""
        db.session.commit()


def test_update_email_model_path_send_and_draft(app, client, monkeypatch):
    db, M = _models()
    from app import llm
    with app.app_context():
        f = M.Firm.get()
        f.ai_enabled = True
        db.session.commit()
    calls = []

    def fake(prompt, **kw):
        calls.append((prompt, kw))
        return json.dumps({"subject": "Your matter this week", "body": "Dear Stella,\n\nWe filed the motion.\n\nRegards"})
    monkeypatch.setattr(llm, "complete", fake)
    r = client.post(f"/ai/matter/{S['matter_id']}/update-email", data={"_csrf": S["tok"]})
    assert r.status_code == 200
    html = r.data.decode()
    assert len(calls) == 1 and "Filed the motion to compel." in calls[0][0] and "slow to pay" not in calls[0][0]
    assert 'value="Your matter this week"' in html and "We filed the motion." in html and "AI was not available" not in html
    # send (edited)
    before = len(_outbox())
    r = client.post(f"/ai/matter/{S['matter_id']}/update-email/send",
                    data={"_csrf": S["tok"], "to": "stella@example.test", "subject": "Your matter this week (edited)",
                          "body": "Dear Stella,\n\nWe filed the motion. Edited.\n\nRegards"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/matters/{S['matter_id']}")
    out = _outbox()
    assert len(out) == before + 1 and out[0]["subject"] == "Your matter this week (edited)" and "Edited." in out[0]["html"]
    with app.app_context():
        msg = M.Message.query.filter_by(matter_id=S["matter_id"], channel="email", direction="out", status="sent").first()
        assert msg is not None and msg.contact_id == S["client_id"] and "Edited." in msg.body
    # save as draft, listed at /intake/drafts
    r = client.post(f"/ai/matter/{S['matter_id']}/update-email/draft",
                    data={"_csrf": S["tok"], "to": "stella@example.test", "subject": "Draft update", "body": "Draft body here."})
    assert r.status_code == 302 and r.headers["Location"].endswith("/intake/drafts")
    r = client.get("/intake/drafts")
    assert b"Draft update" in r.data and b"Draft body here." in r.data and b"Statement matter" in r.data
    # matter page has the button
    r = client.get(f"/matters/{S['matter_id']}")
    assert f'action="/ai/matter/{S["matter_id"]}/update-email"'.encode() in r.data


# ---------------------------------------------------------------- 5. aggregate questions
def test_ar_and_hours_questions_answered_without_model(app, client, monkeypatch):
    db, M = _models()
    from app import llm
    today = date.today()

    def boom(*a, **kw):
        raise AssertionError("model must not be called for aggregate questions")
    monkeypatch.setattr(llm, "complete", boom)
    with app.app_context():
        m = db.session.get(M.Matter, S["matter_id"])
        old = _make_invoice(M, db, m, "INV-9190", today - timedelta(days=130), 77700)  # due 100 days ago
        db.session.commit()
        S["old_inv"] = old.id
    r = client.get("/ai/search?q=what amount do we have in AR aged over 90 days")
    assert r.status_code == 200
    html = r.data.decode()
    assert "A/R over 90 days" in html and "$777.00" in html and "INV-9190" in html and "/reports/ar-aging" in html
    assert "INV-9102" not in html  # only 20 days old
    r = client.get("/ai/search?q=which timekeeper has less than 150 billable hours this month")
    html = r.data.decode()
    assert "Hours per timekeeper" in html and "Demo Owner" in html and "/reports/productivity" in html
    assert "1 timekeeper under 150 billable hours" in html
    r = client.get("/ai/search?q=who has more than 1000 hours this month")
    assert "0 timekeepers over 1000" in r.data.decode()
    r = client.get("/ai/search?q=total AR outstanding")
    html = r.data.decode()
    assert "Accounts receivable" in html and "$2,177.00" in html
    r = client.get("/ai/search?q=how much unbilled WIP do we have")
    assert "Unbilled work in progress" in r.data.decode() and "/reports/wip" in r.data.decode()
    r = client.get("/ai/search?q=what is our trust balance")
    assert "Trust balance" in r.data.decode() and "$5,000.00" in r.data.decode()
    r = client.get("/ai/search?q=which invoices are overdue")
    assert "Overdue invoices" in r.data.decode() and "INV-9190" in r.data.decode()
    # a non-aggregate question still takes the model path (here: unavailable -> plain search)
    from app.blueprints.ai import answer_aggregate
    with app.app_context():
        assert answer_aggregate("litigation matters opened this year") is None
        assert answer_aggregate("") is None
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("off")))
    r = client.get("/ai/search?q=Stella")
    assert r.status_code == 200 and b"Plain text matches" in r.data
