"""Phase 2, Agent C: origination, realization and profitability reports plus the customizable dashboard.

Seeds a throwaway SQLite file (data/test_phase2_c.db) via seed.py, then builds a small known dataset dated in
January 2025 so the seeded demo rows (dated today) stay out of every date-filtered assertion.
"""
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402

TEST_DB = os.path.join(ROOT, "data", "test_phase2_c.db")
RANGE = {"from": "2025-01-01", "to": "2025-01-31"}
JAN = date(2025, 1, 10)
S = {}


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{TEST_DB}", SMTP_HOST="", STRIPE_SECRET_KEY="")
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT, env=env)
    from app import create_app
    tmp = tmp_path_factory.mktemp("phase2c")
    application = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{TEST_DB}", "TESTING": True,
                              "SMTP_HOST": "", "STRIPE_SECRET_KEY": "", "UPLOAD_DIR": str(tmp / "uploads")})
    yield application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    login(c)
    return c


def csrf(c):
    c.get("/dashboard/customize")
    with c.session_transaction() as s:
        return s["_csrf"]


def _models():
    from app.extensions import db
    from app import models
    return db, models


def _cards(html):
    return re.findall(r'data-card="([a-z_]+)"', html)


# ---------------------------------------------------------------- fixture data
def test_seed_known_dataset(app):
    db, M = _models()
    with app.app_context():
        owner = M.User.query.filter_by(email="owner@example.com").first()
        owner.cost_rate_cents = 10000
        ann = M.User(email="ann@example.com", name="Ann Attorney", role="attorney", hourly_rate_cents=30000,
                     cost_rate_cents=5000, initials="AA")
        ann.set_password("password123")
        bob = M.User(email="bob@example.com", name="Bob Paralegal", role="paralegal", hourly_rate_cents=0,
                     cost_rate_cents=0, initials="BP")  # no cost rate on purpose
        bob.set_password("password123")
        db.session.add_all([ann, bob])
        db.session.commit()
        maria = M.Contact.query.filter_by(last_name="Alvarez").first()
        blue = M.Contact.query.filter_by(company_name="Bluebonnet Logistics LLC").first()
        ma = M.Matter(number="M-C001", client_id=maria.id, name="Alvarez lease dispute", billing_type="hourly",
                      hourly_rate_cents=30000, responsible_user_id=owner.id, originating_user_id=ann.id,
                      opened_on=JAN, status="open")
        mb = M.Matter(number="M-C002", client_id=blue.id, name="Bluebonnet vendor contract", billing_type="hourly",
                      hourly_rate_cents=30000, responsible_user_id=owner.id, originating_user_id=None,
                      opened_on=JAN, status="open")
        mc = M.Matter(number="M-C003", client_id=maria.id, name="Alvarez will update", billing_type="flat",
                      flat_fee_cents=50000, responsible_user_id=owner.id, originating_user_id=ann.id,
                      opened_on=JAN, status="closed", closed_on=JAN)
        db.session.add_all([ma, mb, mc])
        db.session.commit()
        # MA: Ann works 2.0 hours ($600), billed at $450 (a $150 write-down), plus a $50 billable expense on the
        # same invoice, so the invoice totals $500. Client pays $250. A $20 non-billable expense is a firm cost.
        t1 = M.TimeEntry(matter_id=ma.id, user_id=ann.id, date=JAN, minutes=120, rate_cents=30000,
                         description="Draft lease demand", billable=True)
        x1 = M.Expense(matter_id=ma.id, user_id=ann.id, date=JAN, description="Filing fee", amount_cents=5000,
                       billable=True)
        x2 = M.Expense(matter_id=ma.id, user_id=ann.id, date=JAN, description="Courier (not billed)",
                       amount_cents=2000, billable=False)
        db.session.add_all([t1, x1, x2])
        db.session.flush()
        inv = M.Invoice(number="INV-C001", matter_id=ma.id, client_id=maria.id, kind="hourly", status="sent",
                        issued_on=JAN, due_on=date(2025, 2, 9), sent_at=datetime(2025, 1, 10, 12, 0))
        db.session.add(inv)
        db.session.flush()
        db.session.add_all([
            M.InvoiceLine(invoice_id=inv.id, kind="time", date=JAN, description="Draft lease demand", quantity=1.5,
                          unit_cents=30000, amount_cents=45000, time_entry_id=t1.id, sort=0),
            M.InvoiceLine(invoice_id=inv.id, kind="expense", date=JAN, description="Filing fee", quantity=1,
                          unit_cents=5000, amount_cents=5000, expense_id=x1.id, sort=1),
        ])
        t1.invoice_id = inv.id
        x1.invoice_id = inv.id
        db.session.flush()
        pay = M.Payment(invoice_id=inv.id, matter_id=ma.id, client_id=maria.id, amount_cents=25000, method="check",
                        account="operating", received_on=date(2025, 1, 20), reference="1001")
        db.session.add(pay)
        db.session.flush()
        inv.recalc()
        assert inv.total_cents == 50000 and inv.paid_cents == 25000 and inv.status == "partial"
        # MB: no originator. Ann logs 1.0 hour unbilled ($300). A $100 payment arrives with no invoice attached.
        db.session.add(M.TimeEntry(matter_id=mb.id, user_id=ann.id, date=JAN, minutes=60, rate_cents=30000,
                                   description="Contract review", billable=True))
        db.session.add(M.Payment(matter_id=mb.id, client_id=blue.id, amount_cents=10000, method="cash",
                                 account="operating", received_on=date(2025, 1, 25)))
        # MC: Bob (no cost rate) logs 0.5 hour. Also a trust payment outside the range that must not count.
        db.session.add(M.TimeEntry(matter_id=mc.id, user_id=bob.id, date=JAN, minutes=30, rate_cents=0,
                                   description="Assemble will packet", billable=False))
        db.session.add(M.Payment(matter_id=mc.id, client_id=maria.id, amount_cents=99900, method="check",
                                 account="operating", received_on=date(2025, 3, 1)))
        db.session.commit()
        S.update(owner=owner.id, ann=ann.id, bob=bob.id, ma=ma.id, mb=mb.id, mc=mc.id, inv=inv.id)


# ---------------------------------------------------------------- origination
def test_origination_totals_by_attorney(app, client):
    from app.blueprints.reports import origination_data
    with app.app_context():
        rows, totals = origination_data(date(2025, 1, 1), date(2025, 1, 31))
    by_name = {r["user"].name: r for r in rows}
    assert by_name["Ann Attorney"]["cents"] == 25000
    assert by_name["Ann Attorney"]["matter_count"] == 1
    assert by_name["Ann Attorney"]["flagged_count"] == 0
    # M-C002 has no originator, so its $100 falls to the responsible attorney (the owner) and is flagged.
    assert by_name["Demo Owner"]["cents"] == 10000
    assert by_name["Demo Owner"]["flagged_count"] == 1
    assert totals["cents"] == 35000 and totals["count"] == 2 and totals["flagged_count"] == 1
    r = client.get("/reports/origination", query_string=RANGE)
    assert r.status_code == 200
    assert b"Ann Attorney" in r.data and b"$250.00" in r.data
    assert b"no originator" in r.data
    assert b"$999.00" not in r.data  # March payment is outside the range


def test_origination_csv(client):
    r = client.get("/reports/origination", query_string=dict(RANGE, format="csv"))
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "origination.csv" in r.headers["Content-Disposition"]
    lines = r.data.decode().splitlines()
    assert lines[0] == "Attorney,Matter,Name,Client,Payments,Collected,Flag"
    assert any(l.startswith("Demo Owner,M-C002,") and "responsible attorney used" in l for l in lines)
    assert lines[-1].startswith("ALL,2025-01-01 to 2025-01-31,") and lines[-1].split(",")[5] == "350.00"


# ---------------------------------------------------------------- realization
def test_realization_known_writedown(app, client):
    from app.blueprints.reports import realization_data
    with app.app_context():
        user_rows, matter_rows, totals = realization_data(date(2025, 1, 1), date(2025, 1, 31))
        ann = next(r for r in user_rows if r["user"].id == S["ann"])
        ma = next(r for r in matter_rows if r["matter"].id == S["ma"])
        mb = next(r for r in matter_rows if r["matter"].id == S["mb"])
    # Matter A: worked $600, billed $450, collected = $250 paid * (450 / 500 invoice total) = $225.
    assert ma["worked"] == 60000 and ma["billed"] == 45000 and ma["collected"] == 22500
    assert ma["billing_pct"] == 75.0 and ma["collection_pct"] == 50.0
    assert ma["writedown"] == 15000 and ma["invoiced"] is True
    # Matter B has time but no invoice: nothing billed, and unbilled WIP is not a write-down.
    assert mb["worked"] == 30000 and mb["billed"] == 0 and mb["collected"] == 0
    assert mb["billing_pct"] == 0.0 and mb["collection_pct"] is None
    assert mb["writedown"] == 0 and mb["invoiced"] is False
    # Ann across both matters.
    assert ann["worked"] == 90000 and ann["billed"] == 45000 and ann["collected"] == 22500
    assert ann["billing_pct"] == 50.0 and ann["collection_pct"] == 50.0 and ann["writedown"] == 15000
    assert totals["worked"] == 90000 and totals["billed"] == 45000 and totals["writedown"] == 15000
    r = client.get("/reports/realization", query_string=RANGE)
    assert r.status_code == 200
    assert b"75.0%" in r.data and b"$150.00" in r.data and b"not yet invoiced" in r.data


def test_realization_csv(client):
    r = client.get("/reports/realization", query_string=dict(RANGE, format="csv"))
    assert r.status_code == 200 and r.mimetype == "text/csv"
    lines = r.data.decode().splitlines()
    assert lines[0] == ("Group,Key,Name,Hours,Worked,Billed,Collected,Billing realization %,"
                        "Collection realization %,Write-downs,Flag")
    ma = next(l for l in lines if l.startswith("matter,M-C001,"))
    assert ma.split(",")[3:10] == ["2.00", "600.00", "450.00", "225.00", "75.0", "50.0", "150.00"]
    assert lines[-1].startswith("total,")


# ---------------------------------------------------------------- profitability
def test_profitability_margin_and_missing_rate_flag(app, client):
    from app.blueprints.reports import profitability_data
    with app.app_context():
        rows, totals = profitability_data(date(2025, 1, 1), date(2025, 1, 31))
        by_id = {r["matter"].id: r for r in rows}
        ma, mb, mc = by_id[S["ma"]], by_id[S["mb"]], by_id[S["mc"]]
    # Matter A: revenue $250; cost = 2h * $50 cost rate = $100 plus $20 non-billable expense = $120; margin $130.
    assert ma["revenue"] == 25000 and ma["time_cost"] == 10000 and ma["expense_cost"] == 2000
    assert ma["cost"] == 12000 and ma["margin"] == 13000 and ma["margin_pct"] == 52.0
    assert ma["cost_rate_missing"] is False
    # Matter B: revenue $100, cost 1h * $50 = $50, margin $50 (50%).
    assert mb["revenue"] == 10000 and mb["cost"] == 5000 and mb["margin"] == 5000 and mb["margin_pct"] == 50.0
    # Matter C: Bob has no cost rate, so the matter is flagged rather than shown as free.
    assert mc["cost_rate_missing"] is True and "Bob Paralegal" in mc["missing_rate_users"]
    assert mc["revenue"] == 0 and mc["margin_pct"] is None
    assert totals["revenue"] == 35000 and totals["cost"] == 17000 and totals["margin"] == 18000
    assert totals["flagged"] == 1
    r = client.get("/reports/profitability", query_string=RANGE)
    assert r.status_code == 200
    assert b"cost rate not set" in r.data and b"Bob Paralegal" in r.data
    assert b"52.0%" in r.data
    # Status filter narrows to closed matters only.
    r = client.get("/reports/profitability", query_string=dict(RANGE, status="closed"))
    assert b"M-C003" in r.data and b"M-C001" not in r.data


def test_profitability_csv(client):
    r = client.get("/reports/profitability", query_string=dict(RANGE, format="csv"))
    assert r.status_code == 200 and r.mimetype == "text/csv"
    lines = r.data.decode().splitlines()
    assert lines[0] == ("Matter,Name,Client,Status,Revenue,Hours,Time cost,Non-billable expenses,Total cost,"
                        "Margin,Margin %,Flag")
    mc = next(l for l in lines if l.startswith("M-C003,"))
    assert "cost rate not set: Bob Paralegal" in mc
    ma = next(l for l in lines if l.startswith("M-C001,"))
    assert ma.split(",")[4:11] == ["250.00", "2.00", "100.00", "20.00", "120.00", "130.00", "52.0"]
    assert lines[-1].startswith("TOTAL,")


def test_reports_index_links_new_reports(client):
    r = client.get("/reports")
    assert r.status_code == 200
    for path in ("/reports/origination", "/reports/realization", "/reports/profitability"):
        assert path.encode() in r.data


# ---------------------------------------------------------------- dashboard
def test_dashboard_defaults_when_dashboard_json_empty(app, client):
    from app.blueprints.dashboard import DEFAULT_CARDS
    db, M = _models()
    with app.app_context():
        u = M.User.query.filter_by(email="owner@example.com").first()
        assert not u.dashboard_json
    r = client.get("/")
    assert r.status_code == 200
    assert _cards(r.data.decode()) == DEFAULT_CARDS
    assert b"timerbar" in r.data or b"Start timer" in r.data  # timer bar or start link still in the top bar
    assert b'href="/time/new"' in r.data and b'href="/matters/new"' in r.data  # quick actions kept
    assert b"/dashboard/customize" in r.data


def test_dashboard_customize_saves_subset_and_order(app, client):
    db, M = _models()
    r = client.get("/dashboard/customize")
    assert r.status_code == 200 and b'name="card_portal_messages"' in r.data
    tok = csrf(client)
    r = client.post("/dashboard/customize", data={"_csrf": tok,
                                                  "card_trust": "1", "order_trust": "2",
                                                  "card_tasks": "1", "order_tasks": "1",
                                                  "card_portal_messages": "1", "order_portal_messages": "3",
                                                  "order_ar": "1"})  # ar has an order but is not ticked
    assert r.status_code == 302
    with app.app_context():
        u = M.User.query.filter_by(email="owner@example.com").first()
        assert json.loads(u.dashboard_json) == ["tasks", "trust", "portal_messages"]
    r = client.get("/")
    assert r.status_code == 200
    assert _cards(r.data.decode()) == ["tasks", "trust", "portal_messages"]
    assert b"Showing your own card selection" in r.data


def test_dashboard_new_cards_render_live_data(app, client):
    db, M = _models()
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        inv.approval_status = "pending"
        maria = M.Contact.query.filter_by(last_name="Alvarez").first()
        doc = M.Document(matter_id=S["ma"], name="Settlement agreement.pdf", path="x/settlement.pdf", size=10,
                         mime="application/pdf")
        db.session.add(doc)
        db.session.flush()
        db.session.add(M.DocumentSignature(document_id=doc.id, contact_id=maria.id, title="Settlement agreement",
                                           status="sent", sent_at=datetime.utcnow(), sent_to=maria.email))
        db.session.add(M.Message(contact_id=maria.id, matter_id=S["ma"], direction="in", channel="portal",
                                 body="Hello from the portal, is the lease signed?", status="received"))
        m = db.session.get(M.Matter, S["mb"])
        m.trust_minimum_cents = 100000
        m.trust_replenish_to_cents = 250000
        db.session.commit()
    tok = csrf(client)
    keys = ["pending_approvals", "evergreen", "unsigned_documents", "portal_messages", "open_matters"]
    data = {"_csrf": tok}
    for i, k in enumerate(keys):
        data[f"card_{k}"] = "1"
        data[f"order_{k}"] = str(i + 1)
    assert client.post("/dashboard/customize", data=data).status_code == 302
    r = client.get("/")
    html = r.data.decode()
    assert _cards(html) == keys
    assert "INV-C001" in html  # pending approval
    assert "Settlement agreement" in html  # unsigned document
    assert "Hello from the portal" in html  # unread portal message
    assert "M-C002" in html and "$2,500.00" in html  # evergreen shortfall: replenish to $2,500 from $0


def test_dashboard_evergreen_shows_no_data_when_helper_missing(app, client, monkeypatch):
    import app.blueprints.dashboard as dash
    import app.blueprints.trust as trust
    monkeypatch.delattr(trust, "evergreen_shortfalls")
    assert dash._evergreen() is None
    r = client.get("/")
    assert r.status_code == 200 and b"No data" in r.data


def test_dashboard_reset_restores_defaults(app, client):
    from app.blueprints.dashboard import DEFAULT_CARDS
    db, M = _models()
    tok = csrf(client)
    assert client.post("/dashboard/customize", data={"_csrf": tok, "reset": "1"}).status_code == 302
    with app.app_context():
        u = M.User.query.filter_by(email="owner@example.com").first()
        assert u.dashboard_json == ""
    r = client.get("/")
    assert _cards(r.data.decode()) == DEFAULT_CARDS
    # Empty selection is refused rather than saved as nothing.
    r = client.post("/dashboard/customize", data={"_csrf": tok})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard/customize")
