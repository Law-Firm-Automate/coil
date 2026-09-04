"""Phase 3 Agent G: operating accounting, REST API, outgoing webhooks. Own SQLite DB seeded via seed.py."""
import io
import json
import os
import subprocess
import sys
from datetime import date, timedelta, datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("p3g")
    dbfile = tmp / "test.db"
    uri = f"sqlite:///{dbfile}"
    env = dict(os.environ, DATABASE_URL=uri, STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="", SMTP_HOST="")
    subprocess.run([sys.executable, "seed.py"], cwd=ROOT, env=env, check=True)
    from app import create_app
    application = create_app({"SQLALCHEMY_DATABASE_URI": uri, "TESTING": True, "STRIPE_SECRET_KEY": "",
                              "STRIPE_WEBHOOK_SECRET": "", "SMTP_HOST": "", "UPLOAD_DIR": str(tmp / "uploads"),
                              "API_RATE_LIMIT": 30})
    return application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    login(c)
    return c


def csrf(client):
    client.get("/accounting/new")
    with client.session_transaction() as s:
        return s["_csrf"]


def _ids(app):
    from app.models import Matter, User
    with app.app_context():
        m1 = Matter.query.filter_by(number="M-1001").first()
        u = User.query.filter_by(email="owner@example.com").first()
        return m1.id, m1.client_id, u.id


def _code_of(entry):
    return entry.account.code if entry.account else None


# ---------------------------------------------------------------- automatic postings
def test_payment_posts_fee_surcharge_and_merchant_lines(app):
    from app.extensions import db
    from app.models import Invoice, InvoiceLine, Payment, LedgerEntry, Account
    matter_id, client_id, _ = _ids(app)
    with app.app_context():
        inv = Invoice(number="INV-G1", matter_id=matter_id, client_id=client_id, kind="flat", status="sent",
                      issued_on=date.today(), due_on=date.today() + timedelta(days=30), sent_at=datetime.utcnow())
        db.session.add(inv)
        db.session.flush()
        db.session.add(InvoiceLine(invoice_id=inv.id, kind="flat", description="Estate plan", amount_cents=100000))
        inv.recalc()
        db.session.commit()
        p = Payment(invoice_id=inv.id, matter_id=matter_id, client_id=client_id, amount_cents=100000,
                    surcharge_cents=3000, stripe_fee_cents=320, method="card", account="operating",
                    received_on=date.today(), reference="pi_test")
        db.session.add(p)
        inv.payments.append(p)
        db.session.flush()
        inv.recalc()
        db.session.commit()
        pid = p.id
        entries = LedgerEntry.query.filter_by(payment_id=pid).all()
        by_code = {_code_of(e): e.amount_cents for e in entries}
        assert by_code == {"4000": 100000, "4300": 3000, "6100": -320}, by_code
        assert sum(by_code.values()) == 100000 + 3000 - 320
        assert all(e.source == "payment" and e.matter_id == matter_id for e in entries)
        assert entries[0].payee == "Maria Alvarez"
        assert Account.query.filter_by(is_system=True).count() == 15
        # trust-side payments never touch the operating ledger
        tp = Payment(invoice_id=None, matter_id=matter_id, client_id=client_id, amount_cents=5000, method="ach",
                     account="trust")
        db.session.add(tp)
        db.session.commit()
        assert LedgerEntry.query.filter_by(payment_id=tp.id).count() == 0
        db.session.delete(tp)
        db.session.commit()


def test_expense_posts_office_or_client_costs(app):
    from app.extensions import db
    from app.models import Expense, LedgerEntry
    matter_id, _, uid = _ids(app)
    with app.app_context():
        office = Expense(matter_id=matter_id, user_id=uid, date=date.today(), description="Printer toner",
                         category="Office", amount_cents=4599, billable=False)
        adv = Expense(matter_id=matter_id, user_id=uid, date=date.today(), description="Filing fee",
                      category="Court", amount_cents=35000, billable=True)
        db.session.add_all([office, adv])
        db.session.commit()
        e1 = LedgerEntry.query.filter_by(expense_id=office.id).one()
        e2 = LedgerEntry.query.filter_by(expense_id=adv.id).one()
        assert (_code_of(e1), e1.amount_cents, e1.source) == ("6600", -4599, "expense")
        assert (_code_of(e2), e2.amount_cents) == ("1300", -35000)
        # editing the amount updates the line in place; deleting removes it
        office.amount_cents = 4600
        db.session.commit()
        assert LedgerEntry.query.filter_by(expense_id=office.id).one().amount_cents == -4600
        db.session.delete(adv)
        db.session.commit()
        assert LedgerEntry.query.filter_by(expense_id=adv.id).count() == 0


def test_deleting_payment_removes_entries(app):
    from app.extensions import db
    from app.models import Payment, LedgerEntry
    with app.app_context():
        p = Payment.query.filter_by(reference="pi_test").one()
        pid = p.id
        assert LedgerEntry.query.filter_by(payment_id=pid).count() == 3
        db.session.delete(p)
        db.session.commit()
        assert LedgerEntry.query.filter_by(payment_id=pid).count() == 0
        # put an equivalent payment back so the P&L test below has income to show
        p2 = Payment(invoice_id=p.invoice_id, matter_id=p.matter_id, client_id=p.client_id, amount_cents=100000,
                     surcharge_cents=3000, stripe_fee_cents=320, method="card", account="operating",
                     received_on=date.today(), reference="pi_test2")
        db.session.add(p2)
        db.session.commit()
        assert LedgerEntry.query.filter_by(payment_id=p2.id).count() == 3


# ---------------------------------------------------------------- ledger pages, import, categorise
def test_manual_entry_and_ledger_page(app, client):
    from app.models import LedgerEntry, Account
    tok = csrf(client)
    with app.app_context():
        rent = Account.query.filter_by(code="6200").one().id
    r = client.post("/accounting/new", data={"_csrf": tok, "date": date.today().isoformat(), "account_id": rent,
                                              "amount": "1,500.00", "direction": "out", "payee": "Landlord LLC",
                                              "description": "September rent", "reference": "chk 1041"})
    assert r.status_code == 302, r.data[:300]
    with app.app_context():
        e = LedgerEntry.query.filter_by(payee="Landlord LLC").one()
        assert e.amount_cents == -150000 and e.source == "manual" and not e.cleared
    r = client.get(f"/accounting/?month={date.today().strftime('%Y-%m')}")
    assert r.status_code == 200
    assert b"Landlord LLC" in r.data and b"Payment on INV-G1" in r.data
    r = client.get("/accounting/accounts")
    assert r.status_code == 200 and b"Legal Fees" in r.data and b"Merchant Fees" in r.data


def test_bank_csv_import_matches_and_creates_uncategorised(app, client):
    from app.models import LedgerEntry, BankImport
    tok = csrf(client)
    d = date.today()
    csv_text = ("Date,Description,Amount\n"
                f"{(d - timedelta(days=2)).strftime('%m/%d/%Y')},CHECK 1041 LANDLORD,-1500.00\n"
                f"{d.strftime('%m/%d/%Y')},MYSTERY VENDOR 8831,-12.34\n")
    r = client.post("/accounting/import", data={"_csrf": tok, "file": (io.BytesIO(csv_text.encode()), "bank.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.data[:300]
    assert b"matched" in r.data and b"MYSTERY VENDOR" in r.data
    import re
    rows_json = re.search(rb"name=\"rows_json\" value='([^']+)'", r.data).group(1).decode()
    import html
    rows = json.loads(html.unescape(rows_json))
    assert rows[0]["match_id"] and rows[1]["match_id"] is None
    r = client.post("/accounting/import/commit", data={"_csrf": tok, "rows_json": json.dumps(rows), "filename": "bank.csv",
                                                        "action_0": "auto", "action_1": "auto"})
    assert r.status_code == 302
    with app.app_context():
        rent = LedgerEntry.query.filter_by(payee="Landlord LLC").one()
        assert rent.cleared is True and rent.cleared_on == d - timedelta(days=2)
        new = LedgerEntry.query.filter_by(description="MYSTERY VENDOR 8831").one()
        assert new.account_id is None and new.source == "import" and new.amount_cents == -1234 and new.cleared
        imp = BankImport.query.order_by(BankImport.id.desc()).first()
        assert (imp.rows, imp.matched, imp.created) == (2, 1, 1)
    r = client.get("/accounting/uncategorised")
    assert b"MYSTERY VENDOR 8831" in r.data


def test_debit_credit_columns_parse(app):
    from app.blueprints.accounting import parse_bank_csv
    rows, errors = parse_bank_csv("Posting Date,Details,Debit,Credit\n2026-09-01,Deposit,,250.00\n2026-09-02,Fee,5.00,\nbad,x,,\n")
    assert [r["amount_cents"] for r in rows] == [25000, -500]
    assert len(errors) == 1


def test_categorise_uncategorised_entry(app, client):
    from app.models import LedgerEntry, Account
    tok = csrf(client)
    with app.app_context():
        new = LedgerEntry.query.filter_by(description="MYSTERY VENDOR 8831").one()
        office = Account.query.filter_by(code="6600").one()
        eid, oid = new.id, office.id
    r = client.post(f"/accounting/{eid}/categorise", data={"_csrf": tok, "account_id": oid})
    assert r.status_code == 302
    with app.app_context():
        assert LedgerEntry.query.get(eid).account_id == oid
    r = client.get("/accounting/uncategorised")
    assert b"Everything is categorised" in r.data


# ---------------------------------------------------------------- reconciliation, reports
def test_operating_reconciliation_balanced_then_not(app, client):
    from app.models import LedgerEntry, OperatingReconciliation
    from app.extensions import db
    tok = csrf(client)
    today = date.today()
    with app.app_context():
        book = sum(e.amount_cents for e in LedgerEntry.query.filter(LedgerEntry.date <= today).all())
        cleared = sum(e.amount_cents for e in LedgerEntry.query.filter(LedgerEntry.date <= today,
                                                                          LedgerEntry.cleared == True).all())  # noqa: E712
        assert book != cleared  # the payment lines are still uncleared
    r = client.post("/accounting/reconcile", data={"_csrf": tok, "period_end": today.isoformat(),
                                                    "statement_balance": f"{cleared / 100:.2f}"})
    assert r.status_code == 302
    r2 = client.get(r.headers["Location"])
    assert r2.status_code == 200 and b"Balanced." in r2.data
    with app.app_context():
        rec = OperatingReconciliation.query.order_by(OperatingReconciliation.id.desc()).first()
        assert rec.balanced is True and rec.book_balance_cents == book
        assert rec.statement_balance_cents + rec.outstanding_in_cents + rec.outstanding_out_cents == book
    r = client.post("/accounting/reconcile", data={"_csrf": tok, "period_end": today.isoformat(),
                                                    "statement_balance": f"{(cleared - 1000) / 100:.2f}"})
    r2 = client.get(r.headers["Location"])
    assert b"Out of balance by $10.00" in r2.data
    with app.app_context():
        assert OperatingReconciliation.query.order_by(OperatingReconciliation.id.desc()).first().balanced is False


def test_pnl_and_balance(app, client):
    from app.blueprints.accounting import pnl_data
    today = date.today()
    start = today.replace(day=1)
    with app.app_context():
        d = pnl_data(start, today)
        inc = {a.code: v for a, v in d["income"]}
        exp = {a.code: v for a, v in d["expense"]}
        assert inc["4000"] == 100000 and inc["4300"] == 3000
        assert exp["6100"] == -320 and exp["6200"] == -150000
        assert exp["6600"] == -(4600 + 1234)
        assert d["total_income"] == 103000
        assert d["net"] == d["total_income"] + d["total_expense"]
        assert d["uncategorised_count"] == 0
    r = client.get(f"/accounting/pnl?from={start.isoformat()}&to={today.isoformat()}")
    assert r.status_code == 200 and b"$1,030.00" in r.data
    r = client.get(f"/accounting/pnl?from={start.isoformat()}&to={today.isoformat()}&format=csv")
    assert r.status_code == 200 and b"Net income" in r.data and r.mimetype == "text/csv"
    r = client.get("/accounting/balance")
    assert r.status_code == 200 and b"Client trust liability" in r.data
    r = client.get("/accounting/balance?format=csv")
    assert b"Accounts receivable" in r.data
    r = client.get("/reports")
    assert b"/accounting/pnl" in r.data and b"/accounting/balance" in r.data


# ---------------------------------------------------------------- REST API
def _make_token(app, scopes):
    from app.extensions import db
    from app.models import User
    from app.blueprints.api import create_token
    with app.app_context():
        u = User.query.filter_by(email="owner@example.com").first()
        t, raw = create_token(u, f"test {scopes}", scopes)
        db.session.commit()
        return raw


def _h(raw):
    return {"Authorization": f"Bearer {raw}"}


def test_api_token_page_and_auth(app, client):
    from app.models import ApiToken
    tok = csrf(client)
    r = client.post("/settings/api", data={"_csrf": tok, "name": "Laptop", "scopes": "read,write"})
    assert r.status_code == 302
    r = client.get("/settings/api")
    assert r.status_code == 200 and b"Copy this token now" in r.data
    import re
    raw = re.search(rb'value="(coil_[^"]+)"', r.data).group(1).decode()
    assert len(raw) > 20
    r = client.get("/settings/api")
    assert b"Copy this token now" not in r.data  # shown once
    with app.app_context():
        t = ApiToken.query.filter_by(name="Laptop").one()
        assert t.token_hash != raw and t.prefix == raw[:12] and t.scopes == "read,write"
        tid = t.id
    # bearer works, no session needed
    anon = app.test_client()
    r = anon.get("/api/v1/me", headers=_h(raw))
    assert r.status_code == 200 and r.json["user"]["email"] == "owner@example.com"
    assert r.json["token"]["scopes"] == ["read", "write"]
    r = anon.get("/api/v1/me")
    assert r.status_code == 401 and r.json["error"]
    r = anon.get("/api/v1/me", headers=_h("coil_wrong"))
    assert r.status_code == 401
    r = anon.get("/api/v1/nothing-here", headers=_h(raw))
    assert r.status_code == 404 and r.json["status"] == 404
    # revoke
    r = client.post(f"/settings/api/{tid}/revoke", data={"_csrf": tok})
    assert r.status_code == 302
    r = anon.get("/api/v1/me", headers=_h(raw))
    assert r.status_code == 401


def test_api_read_endpoints(app):
    raw = _make_token(app, "read")
    c = app.test_client()
    r = c.get("/api/v1/matters?q=alvarez", headers=_h(raw))
    assert r.status_code == 200 and r.json["matters"][0]["number"] == "M-1001"
    r = c.get("/api/v1/matters?q=zzzz", headers=_h(raw))
    assert r.json["matters"] == []
    mid = _ids(app)[0]
    r = c.get(f"/api/v1/matters/{mid}", headers=_h(raw))
    assert r.json["number"] == "M-1001" and "unbilled_time_cents" in r.json
    r = c.get("/api/v1/matters/999999", headers=_h(raw))
    assert r.status_code == 404
    r = c.get("/api/v1/contacts?q=blue", headers=_h(raw))
    assert any("Bluebonnet" in x["name"] for x in r.json["contacts"])
    r = c.get("/api/v1/invoices?status=all", headers=_h(raw))
    assert any(i["number"] == "INV-G1" for i in r.json["invoices"])
    r = c.get("/api/v1/tasks?due=today", headers=_h(raw))
    assert r.status_code == 200 and "tasks" in r.json
    r = c.get("/api/v1/time", headers=_h(raw))
    assert r.status_code == 200


def test_api_scope_and_time_entry(app):
    from app.models import TimeEntry
    mid = _ids(app)[0]
    ro = _make_token(app, "read")
    rw = _make_token(app, "read,write")
    c = app.test_client()
    r = c.post("/api/v1/time", json={"matter_id": mid, "minutes": 30, "description": "api test"}, headers=_h(ro))
    assert r.status_code == 403 and "scope" in r.json["error"]
    r = c.post("/api/v1/time", json={"matter_id": mid, "hours": "1:30", "description": "Drafted will", "billable": True},
               headers=_h(rw))
    assert r.status_code == 201, r.json
    assert r.json["minutes"] == 90 and r.json["matter_number"] == "M-1001" and r.json["rate_cents"] > 0
    with app.app_context():
        assert TimeEntry.query.filter_by(description="Drafted will").one().minutes == 90
    r = c.post("/api/v1/time", json={"matter_id": mid, "minutes": 0}, headers=_h(rw))
    assert r.status_code == 400
    r = c.post("/api/v1/time", data={"matter_id": mid, "minutes": "12", "description": "form post"}, headers=_h(rw))
    assert r.status_code == 201 and r.json["minutes"] == 12
    r = c.get(f"/api/v1/time?matter_id={mid}&from={date.today().isoformat()}", headers=_h(rw))
    assert r.json["total_minutes"] >= 102


def test_api_timer_start_stop_rounds_up(app):
    from app.models import Timer, TimeEntry
    from app.extensions import db
    mid = _ids(app)[0]
    rw = _make_token(app, "read,write")
    c = app.test_client()
    r = c.post("/api/v1/timer/start", json={"matter_id": mid, "description": "Call with client"}, headers=_h(rw))
    assert r.status_code == 201 and r.json["timer"]["matter_number"] == "M-1001"
    r = c.post("/api/v1/timer/start", json={"matter_id": mid}, headers=_h(rw))
    assert r.status_code == 409
    r = c.get("/api/v1/timer", headers=_h(rw))
    assert r.json["timer"]["description"] == "Call with client"
    with app.app_context():  # pretend 8 minutes passed
        t = Timer.query.one()
        t.started_at = datetime.utcnow() - timedelta(minutes=8)
        db.session.commit()
    r = c.post("/api/v1/timer/stop", json={}, headers=_h(rw))
    assert r.status_code == 201, r.json
    assert r.json["minutes"] == 12 and r.json["description"] == "Call with client"
    with app.app_context():
        assert Timer.query.count() == 0
        assert TimeEntry.query.filter_by(description="Call with client").one().minutes == 12
    r = c.post("/api/v1/timer/stop", json={}, headers=_h(rw))
    assert r.status_code == 404


def test_api_rate_limit(app):
    from app.blueprints.api import reset_rate_limits
    raw = _make_token(app, "read")
    c = app.test_client()
    reset_rate_limits()
    limit = app.config["API_RATE_LIMIT"]
    codes = [c.get("/api/v1/me", headers=_h(raw)).status_code for _ in range(limit + 1)]
    assert codes[:limit] == [200] * limit and codes[-1] == 429
    r = c.get("/api/v1/me", headers=_h(raw))
    assert r.status_code == 429 and r.headers.get("Retry-After") == "60"
    reset_rate_limits()
    assert c.get("/api/v1/me", headers=_h(raw)).status_code == 200


# ---------------------------------------------------------------- outgoing webhooks
class _Resp:
    def __init__(self, code):
        self.status_code = code


def test_webhooks_signed_delivery_and_retry(app, client, monkeypatch):
    import requests
    from app.extensions import db
    from app.models import Webhook, WebhookDelivery, Invoice, Payment
    from app.blueprints.webhooks_out import verify, run_webhooks
    tok = csrf(client)
    r = client.post("/settings/webhooks/new", data={"_csrf": tok, "url": "https://example.test/hook",
                                                     "events": ["invoice.sent", "payment.received"], "secret": "s3cret"})
    assert r.status_code == 302
    with app.app_context():
        h = Webhook.query.one()
        assert h.events == "invoice.sent,payment.received" and h.secret == "s3cret" and h.is_active
        hid = h.id
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append((url, data, headers, timeout))
        return _Resp(200)
    monkeypatch.setattr(requests, "post", fake_post)

    matter_id, client_id, _ = _ids(app)
    with app.app_context():
        inv = Invoice.query.filter_by(number="INV-G1").one()
        inv.sent_at = datetime.utcnow()  # what /invoices/<id>/send does
        db.session.commit()
        p = Payment(invoice_id=inv.id, matter_id=matter_id, client_id=client_id, amount_cents=2500, method="check",
                    account="operating", received_on=date.today(), reference="chk 77")
        db.session.add(p)
        db.session.commit()
        pid = p.id
    assert [c[2]["X-Coil-Event"] for c in calls] == ["invoice.sent", "payment.received"]
    for url, body, headers, timeout in calls:
        assert url == "https://example.test/hook" and timeout == 5
        assert verify("s3cret", body, headers["X-Coil-Signature"])
        assert headers["X-Coil-Signature"].startswith("sha256=")
    payload = json.loads(calls[1][1])
    assert payload["event"] == "payment.received" and payload["data"]["amount_cents"] == 2500
    assert payload["data"]["received_on"] == date.today().isoformat()
    with app.app_context():
        ds = WebhookDelivery.query.filter_by(webhook_id=hid).order_by(WebhookDelivery.id).all()
        assert [d.event for d in ds] == ["invoice.sent", "payment.received"]
        assert all(d.status == "ok" and d.attempts == 1 and d.response_code == 200 for d in ds)
        # unsubscribed events are not queued
        assert WebhookDelivery.query.filter_by(event="matter.created").count() == 0

    # now the endpoint is down: delivery fails and is picked up by the retry command
    def broken_post(url, data=None, headers=None, timeout=None):
        raise requests.ConnectionError("connection refused")
    monkeypatch.setattr(requests, "post", broken_post)
    with app.app_context():
        p2 = Payment(invoice_id=None, matter_id=matter_id, client_id=client_id, amount_cents=100, method="cash",
                     account="operating", received_on=date.today(), reference="fail-me")
        db.session.add(p2)
        db.session.commit()
        d = WebhookDelivery.query.order_by(WebhookDelivery.id.desc()).first()
        assert d.status == "failed" and d.attempts == 1 and "refused" in d.last_error
        did = d.id
        # backoff not elapsed yet: nothing retried
        assert run_webhooks() == (0, 0, 0)
        d = db.session.get(WebhookDelivery, did)
        d.last_at = datetime.utcnow() - timedelta(minutes=10)
        db.session.commit()
        monkeypatch.setattr(requests, "post", fake_post)
        before = len(calls)
        assert run_webhooks() == (1, 1, 0)
        assert len(calls) == before + 1 and calls[-1][2]["X-Coil-Delivery"] == str(did)
        d = db.session.get(WebhookDelivery, did)
        assert d.status == "ok" and d.attempts == 2
        # cleanup so later tests are not surprised by the extra payment
        db.session.delete(db.session.get(Payment, pid))
        db.session.delete(p2)
        db.session.commit()
    r = client.get("/settings/webhooks")
    assert r.status_code == 200 and b"payment.received" in r.data and b"example.test/hook" in r.data


def test_webhook_cli_and_other_events(app, monkeypatch):
    import requests
    from app.extensions import db
    from app.models import Webhook, WebhookDelivery, Task, Matter
    from app.cli import main
    calls = []
    monkeypatch.setattr(requests, "post", lambda url, data=None, headers=None, timeout=None: (calls.append(headers), _Resp(204))[1])
    with app.app_context():
        h = Webhook.query.one()
        h.events = "task.completed,matter.closed,matter.created"
        db.session.commit()
        m = Matter.query.filter_by(number="M-1002").first()
        t = Task(matter_id=m.id, title="Webhook task")
        db.session.add(t)
        db.session.commit()
        assert not [c for c in calls if c["X-Coil-Event"] == "task.completed"]
        t.done = True
        t.done_at = datetime.utcnow()
        db.session.commit()
        assert [c["X-Coil-Event"] for c in calls][-1] == "task.completed"
        m.status = "closed"
        m.closed_on = date.today()
        db.session.commit()
        assert [c["X-Coil-Event"] for c in calls][-1] == "matter.closed"
        m.status = "open"
        m.closed_on = None
        db.session.commit()
        assert [c["X-Coil-Event"] for c in calls][-1] == "matter.closed"  # reopening emits nothing
        assert WebhookDelivery.query.filter_by(status="ok").count() >= 2
    import app as app_pkg
    monkeypatch.setattr(app_pkg, "create_app", lambda *a, **kw: app)  # keep the CLI on the test DB
    assert main(["webhooks"]) == 0
    assert main(["not-a-command"]) == 2


def test_remaining_pages_render(app, client):
    from app.models import Account, LedgerEntry
    with app.app_context():
        aid = Account.query.filter_by(code="6300").one().id
        eid = LedgerEntry.query.filter_by(source="manual").first().id
    for path in ("/accounting/accounts/new", f"/accounting/accounts/{aid}/edit", f"/accounting/{eid}/edit",
                 "/accounting/reconcile", "/accounting/import", "/accounting/?account_id=%d" % aid,
                 "/settings/webhooks", "/settings/api", "/settings"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
    assert b"/settings/api" in client.get("/settings").data
    # system accounts cannot be deleted; payment-sourced entries cannot be edited
    tok = csrf(client)
    with app.app_context():
        legal = Account.query.filter_by(code="4000").one().id
        pe = LedgerEntry.query.filter_by(source="payment").first().id
    client.post(f"/accounting/accounts/{legal}/delete", data={"_csrf": tok})
    with app.app_context():
        assert Account.query.filter_by(code="4000").count() == 1
    r = client.get(f"/accounting/{pe}/edit")
    assert r.status_code == 302
