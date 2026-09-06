"""Regression tests for the billing defects recorded in audit/results/money.json.

One test per audited repro:
  1. Invoice template editor (fail): timekeeper/UTBMS options 500 the PDF and the public page.
  2. Send path (partial): a PDF failure was swallowed and the invoice was still marked sent.
  3. Multi-currency (partial): client-facing pages printed every amount in USD.
  4. LEDES 1998B (partial): interest lines were dropped, so records did not sum to INVOICE_TOTAL.
  5. Invoice line quantity (partial): a 7-minute entry printed 0.12 x $333.33 = $38.89.
  6. Client pages declared <html lang="en"> for Spanish clients.

Own SQLite file (data/test_audit_fixes_billing.db), own UPLOAD_DIR and PDF_DIR.
Tests run in file order and share ids via S.
"""
import io
import os
import re
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402

TEST_DB = os.path.join(ROOT, "data", "test_audit_fixes_billing.db")
UPLOAD_DIR = os.path.join(ROOT, "data", "test_audit_fixes_billing_uploads")
PDF_DIR = os.path.join(ROOT, "data", "test_audit_fixes_billing_pdf")
S = {}


@pytest.fixture(scope="module")
def app():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{TEST_DB}")
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT, env=env)
    from app import create_app
    application = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{TEST_DB}", "TESTING": True, "SMTP_HOST": "",
                              "UPLOAD_DIR": UPLOAD_DIR, "PDF_DIR": PDF_DIR, "STRIPE_SECRET_KEY": ""})
    yield application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    S["tok"] = login(c)
    return c


def _models():
    from app.extensions import db
    from app import models
    return db, models


def _pdf_text(data):
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)


def _redirect_id(r):
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def _build_invoice(app, client, matter_id, time_ids=(), expense_ids=()):
    data = {"_csrf": S["tok"], "matter_id": matter_id, "issued_on": date.today().isoformat(),
            "time_ids": [str(i) for i in time_ids], "expense_ids": [str(i) for i in expense_ids]}
    r = client.post("/invoices/new", data=data)
    assert r.status_code == 302, r.data[:400]
    return _redirect_id(r)


# ---------------------------------------------------------------- shared fixture data
def test_setup_matter_with_time_and_expense(app, client):
    """A GBP matter, a 7-minute entry at $333.33/hr and an expense whose Expense.user_id is set."""
    db, M = _models()
    with app.app_context():
        u = M.User.query.filter_by(email="owner@example.com").first()
        c = M.Contact(kind="company", company_name="Audit Fixtures Ltd", email="billing@audit.test",
                      is_client=True, language="es")
        db.session.add(c)
        db.session.flush()
        m = M.Matter(number="T-AUDITBILL", client_id=c.id, name="Audit billing matter", billing_type="hourly",
                     hourly_rate_cents=33333, currency="GBP", ledes_matter_id="CLM-AUDIT-1")
        db.session.add(m)
        db.session.flush()
        t = M.TimeEntry(matter_id=m.id, user_id=u.id, minutes=7, rate_cents=33333, date=date.today(),
                        description="Short call with the client", task_code="L120", activity_code="A106")
        e = M.Expense(matter_id=m.id, user_id=u.id, amount_cents=1245, category="Postage", date=date.today(),
                      description="Certified mail to the court", expense_code="E108")
        db.session.add_all([t, e])
        db.session.commit()
        S["client_id"], S["matter_id"], S["user_id"] = c.id, m.id, u.id
        S["time_id"], S["expense_id"] = t.id, e.id
        assert t.amount_cents == 3889  # round(7 * 33333 / 60)
    S["inv"] = _build_invoice(app, client, S["matter_id"], [S["time_id"]], [S["expense_id"]])
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        S["token"] = inv.public_token
        S["number"] = inv.number
        assert inv.currency == "GBP"
        assert inv.total_cents == 3889 + 1245


# ---------------------------------------------------------------- 1. template editor 500
def test_timekeeper_and_code_options_do_not_500_the_pdf_or_public_page(app, client):
    """audit repro: POST /settings/invoice-template with invoice_show_timekeeper=1, then GET the PDF and /p/<token>.

    Before the fix both raised AttributeError: 'Expense' object has no attribute 'user'."""
    data = {"_csrf": S["tok"], "action": "save",
            "col_date": "1", "ord_date": "1", "col_description": "1", "ord_description": "2",
            "col_timekeeper": "1", "ord_timekeeper": "3", "col_code": "1", "ord_code": "4",
            "col_qty": "1", "ord_qty": "5", "col_rate": "1", "ord_rate": "6",
            "col_amount": "1", "ord_amount": "7",
            "invoice_show_timekeeper": "1", "invoice_show_activity_codes": "1"}
    r = client.post("/settings/invoice-template", data=data, content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:400]
    r = client.get(f"/invoices/{S['inv']}/pdf")
    assert r.status_code == 200, r.data[:400]
    text = _pdf_text(r.data)
    assert "DO" in text and "E108" in text  # timekeeper initials and the expense UTBMS code
    r = client.get(f"/p/{S['token']}")
    assert r.status_code == 200, r.data[:400]
    html = r.data.decode()
    assert "E108" in html and "A106" in html

    # A deleted timekeeper must not bring the pages down either.
    db, M = _models()
    with app.app_context():
        e = db.session.get(M.Expense, S["expense_id"])
        e.user_id = 99999  # user row is gone
        db.session.commit()
    assert client.get(f"/invoices/{S['inv']}/pdf").status_code == 200
    assert client.get(f"/p/{S['token']}").status_code == 200
    with app.app_context():
        e = db.session.get(M.Expense, S["expense_id"])
        e.user_id = S["user_id"]
        db.session.commit()


# ---------------------------------------------------------------- 2. send swallows a PDF failure
def test_send_reports_a_pdf_failure_and_does_not_mark_the_invoice_sent(app, client, monkeypatch):
    db, M = _models()
    from app.blueprints import invoices as inv_mod
    from app.services import mail

    def boom(inv):
        raise RuntimeError("fpdf blew up")

    sent_before = len(mail.dev_outbox())
    monkeypatch.setattr(inv_mod, "build_pdf", boom)
    r = client.post(f"/invoices/{S['inv']}/send", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert r.status_code == 200
    html = r.data.decode()
    assert "could not be built" in html and "not sent" in html.lower()
    assert f"{S['number']} sent to" not in html
    assert len(mail.dev_outbox()) == sent_before, "no email should go out without the invoice PDF"
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        assert inv.status == "draft", "a failed send must leave the invoice sendable again"
        assert inv.sent_at is None and not inv.sent_to
        assert not [e for e in inv.events if e.event == "sent"]
    monkeypatch.undo()
    # and the same invoice sends cleanly once the PDF works again
    r = client.post(f"/invoices/{S['inv']}/send", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert r.status_code == 200 and "sent to billing@audit.test" in r.data.decode()
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        assert inv.status == "sent" and inv.sent_to == "billing@audit.test"
        assert inv.pdf_path and os.path.isfile(inv.pdf_path)
    assert len(mail.dev_outbox()) == sent_before + 1
    assert S["number"] in mail.dev_outbox()[0]["subject"]


def test_a_failed_reminder_keeps_the_sent_status(app, client, monkeypatch):
    db, M = _models()
    from app.blueprints import invoices as inv_mod

    def boom(inv):
        raise RuntimeError("fpdf blew up")

    monkeypatch.setattr(inv_mod, "build_pdf", boom)
    r = client.post(f"/invoices/{S['inv']}/remind", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert r.status_code == 200 and "could not be built" in r.data.decode()
    monkeypatch.undo()
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        assert inv.status in ("sent", "viewed"), "an already sent invoice must not lose its status"
        assert inv.sent_to == "billing@audit.test"


# ---------------------------------------------------------------- 3. multi-currency client pages
def test_public_invoice_page_shows_the_invoice_currency(app, client):
    """audit repro: GBP invoice at /p/<token> showed $400.00 with no mention of GBP."""
    r = client.get(f"/p/{S['token']}")
    assert r.status_code == 200
    html = r.data.decode()
    assert "£51.34" in html, "amounts must be formatted in the invoice currency"
    assert "$51.34" not in html and "$38.89" not in html and "$12.45" not in html
    assert "GBP" in html, "the currency code must appear on the page"


def test_pay_page_shows_the_invoice_currency_and_refuses_a_non_usd_checkout(app, client):
    r = client.get(f"/pay/{S['token']}?method=card")
    assert r.status_code == 200
    html = r.data.decode()
    assert "£51.34" in html and "$51.34" not in html
    assert "GBP" in html
    # Online payment is refused for a non-USD invoice rather than charging the client USD.
    assert "Continue to secure payment" not in html
    assert re.search(r"transferencia\s+bancaria", html), "the refusal has to reach the client in their language"
    # the invoice page does not offer the online buttons either
    inv_html = client.get(f"/p/{S['token']}").data.decode()
    assert f"/pay/{S['token']}?method=card" not in inv_html
    # and neither the PDF nor the emailed copy promises a payment page that will turn the client away
    pdf = _pdf_text(client.get(f"/invoices/{S['inv']}/pdf").data)
    assert "Pay online" not in pdf and "GBP" in pdf


def test_usd_invoice_still_offers_online_payment(app, client):
    """The refusal is scoped to the currency: a USD invoice for an English-speaking client is unchanged."""
    db, M = _models()
    with app.app_context():
        u = db.session.get(M.User, S["user_id"])
        c = M.Contact(kind="person", first_name="Dollar", last_name="Client", email="usd@audit.test",
                      is_client=True)
        db.session.add(c)
        db.session.flush()
        m = M.Matter(number="T-AUDITUSD", client_id=c.id, name="USD matter", billing_type="hourly",
                     hourly_rate_cents=30000)
        db.session.add(m)
        db.session.flush()
        t = M.TimeEntry(matter_id=m.id, user_id=u.id, minutes=60, rate_cents=30000, date=date.today(),
                        description="Drafting")
        db.session.add(t)
        db.session.commit()
        usd_matter, usd_time = m.id, t.id
    usd_inv = _build_invoice(app, client, usd_matter, [usd_time])
    assert client.post(f"/invoices/{usd_inv}/send", data={"_csrf": S["tok"]}).status_code == 302
    with app.app_context():
        inv = db.session.get(M.Invoice, usd_inv)
        assert (inv.currency or "USD") == "USD" and inv.balance_cents == 30000
        S["usd_token"] = inv.public_token
    html = client.get(f"/p/{S['usd_token']}").data.decode()
    assert f"/pay/{S['usd_token']}?method=card" in html and f"/pay/{S['usd_token']}?method=ach" in html
    pay = client.get(f"/pay/{S['usd_token']}?method=card")
    assert pay.status_code == 200 and "Continue to secure payment" in pay.data.decode()


# ---------------------------------------------------------------- 4. LEDES must balance
def test_ledes_records_sum_to_the_invoice_total_with_an_interest_line(app, client):
    """audit repro: INVOICE_TOTAL 756.35 while the LINE_ITEM_TOTAL column summed to 751.34."""
    db, M = _models()
    with app.app_context():
        f = M.Firm.get()
        f.ledes_firm_id = "74-1234567"
        f.interest_apr_bps = 1200
        f.interest_grace_days = 0
        c = db.session.get(M.Contact, S["client_id"])
        c.ledes_client_id = "AUDIT01"
        inv = db.session.get(M.Invoice, S["inv"])
        inv.issued_on = date.today()
        inv.due_on = date.today() - timedelta(days=45)
        db.session.commit()
    r = client.post(f"/invoices/{S['inv']}/interest", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        assert inv.interest_cents > 0 and any(l.kind == "interest" for l in inv.lines)
        S["total_cents"] = inv.total_cents
        # a discount line too: adjustments were dropped from the file the same way
        inv.lines.append(M.InvoiceLine(kind="discount", date=date.today(), description="Courtesy discount",
                                       quantity=1.0, unit_cents=-500, amount_cents=-500,
                                       sort=max(l.sort or 0 for l in inv.lines) + 1))
        db.session.flush()
        inv.recalc()
        db.session.commit()
        S["total_cents"] = inv.total_cents
    today = date.today().isoformat()
    r = client.get(f"/exports/ledes?from={today}&to={today}&client_id={S['client_id']}")
    assert r.status_code == 200, r.data[:500]
    records = [ln[:-2].split("|") for ln in r.data.decode().splitlines()[2:]]
    mine = [f for f in records if f[1] == S["number"]]
    assert len(mine) == 4, "every line on the invoice needs a record: time, expense, interest, discount"
    header_total = {f[4] for f in mine}
    assert header_total == {f"{S['total_cents'] / 100:.2f}"}
    line_sum = sum(round(float(f[12]) * 100) for f in mine)
    assert line_sum == S["total_cents"], "LINE_ITEM_TOTAL must sum to INVOICE_TOTAL"
    types = [f[9] for f in mine]
    assert types == ["F", "E", "IF", "IF"]
    # every record's own arithmetic holds: units x unit cost + adjustment = line total
    for f in mine:
        gross = round(float(f[10]) * float(f[20]) * 100)
        assert gross + round(float(f[11]) * 100) == round(float(f[12]) * 100), f


def test_ledes_export_refuses_an_invoice_that_does_not_balance(app, client):
    db, M = _models()
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        inv.tax_cents = 999  # nothing in the UI sets tax, but the file could not represent it
        inv.total_cents = inv.subtotal_cents + 999
        db.session.commit()
    today = date.today().isoformat()
    r = client.get(f"/exports/ledes?from={today}&to={today}&client_id={S['client_id']}")
    assert r.status_code == 400, r.data[:400]
    html = r.data.decode()
    assert "LEDES export refused" in html and S["number"] in html
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        inv.tax_cents = 0
        inv.total_cents = inv.subtotal_cents
        db.session.commit()


def test_build_1998b_refuses_to_write_an_unbalanced_file(app):
    """The writer itself will not emit a file whose records do not reconcile, and it names the invoice."""
    db, M = _models()
    from app.blueprints.ledes import build_1998b, LedesImbalance
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        inv.total_cents = inv.subtotal_cents + 4242  # corrupt the header total only
        with pytest.raises(LedesImbalance) as err:
            build_1998b(M.Firm.get(), [inv])
        assert S["number"] in str(err.value)
        db.session.rollback()


# ---------------------------------------------------------------- 5. quantity multiplies out
def test_time_line_quantity_multiplies_out(app, client):
    """audit repro: a 7-minute entry at $333.33/hr printed '0.12 x $333.33 = $38.89'."""
    html = client.get(f"/p/{S['token']}").data.decode()
    qty = re.search(r'<td class="num">(\d+\.\d+)</td>\s*<td class="num">£333\.33</td>', html)
    assert qty, html[:2000]
    printed = float(qty.group(1))
    assert round(printed * 33333) == 3889, f"{printed} x 333.33 does not come to 38.89"
    assert "0.12" not in html
    r = client.get(f"/invoices/{S['inv']}/pdf")
    assert r.status_code == 200
    text = _pdf_text(r.data)
    assert "0.11667" in text and "0.12 " not in text
    # the total is unchanged by the display fix
    db, M = _models()
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        assert inv.subtotal_cents == sum(l.amount_cents for l in inv.lines)
        line = [l for l in inv.lines if l.kind == "time"][0]
        assert line.amount_cents == 3889


def test_legacy_two_decimal_quantity_still_renders(app, client):
    """Invoices built before the fix stored quantity rounded to 0.12. They must still render, and still
    multiply out when the source time entry is available."""
    db, M = _models()
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        line = [l for l in inv.lines if l.kind == "time"][0]
        line.quantity = 0.12  # as an older build would have stored it
        db.session.commit()
    r = client.get(f"/p/{S['token']}")
    assert r.status_code == 200
    assert "£38.89" in r.data.decode()
    assert client.get(f"/invoices/{S['inv']}/pdf").status_code == 200
    assert client.get(f"/invoices/{S['inv']}").status_code == 200
    # a hand-typed line with no source entry and no rate still renders
    with app.app_context():
        inv = db.session.get(M.Invoice, S["inv"])
        line = [l for l in inv.lines if l.kind == "time"][0]
        line.time_entry_id = None
        line.unit_cents = 0
        db.session.commit()
    assert client.get(f"/p/{S['token']}").status_code == 200
    assert client.get(f"/invoices/{S['inv']}/pdf").status_code == 200


# ---------------------------------------------------------------- 6. lang attribute
def test_public_pages_declare_the_language_they_are_written_in(app, client):
    """The client on this matter is Spanish-speaking, so the page must not claim to be English."""
    html = client.get(f"/p/{S['token']}").data.decode()
    assert 'Factura' in html, "sanity: the page really is rendered in Spanish"
    assert '<html lang="es">' in html
    assert '<html lang="en">' not in html
    pay = client.get(f"/pay/{S['token']}?method=ach").data.decode()
    assert '<html lang="es">' in pay
    # an English client's page still says en
    en = client.get(f"/p/{S['usd_token']}").data.decode()
    assert '<html lang="en">' in en
