"""Guards on the paths that lose money, found by the Phase 2 adversarial QA pass.

Each of these was accepted silently before:

1. A matter holding client trust money could be closed. A closed matter drops off the
   screens a firm looks at daily, so the balance stops being noticed.
2. An invoice could total less than zero, because a negative adjustment line is legitimate
   on its own but nobody checked the sum.
3. A time entry of 999999 hours, and an invoice of $99,999,999, both went straight through.
   Neither is refused now, because a marathon trial day and a large settlement are real;
   both ask for one confirmation.

Own SQLite file, own UPLOAD_DIR and PDF_DIR. Never touches data/practice.db.
Run: .venv/bin/python -m pytest tests/test_destructive_guards.py -q
"""
import os
import shutil
import subprocess
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_destructive_guards.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_destructive_guards")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_destructive_guards")

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


def _matter_holding_trust(app, cents, tag, billing_type="hourly"):
    """A matter with `cents` sitting in trust against it. Returns the matter id."""
    db, M = _models()
    with app.app_context():
        u = M.User.query.first()
        c = M.Contact(first_name="Trust", last_name=f"Holder{tag}", email=f"holder{tag}@example.test",
                      is_client=True)
        db.session.add(c)
        db.session.flush()
        m = M.Matter(number=f"M-TG{tag}", client_id=c.id, name=f"TEST trust guard {tag}",
                     billing_type=billing_type, responsible_user_id=u.id, status="open")
        db.session.add(m)
        db.session.flush()
        if cents:
            db.session.add(M.TrustTransaction(client_id=c.id, matter_id=m.id, date=date.today(),
                                              type="deposit", amount_cents=cents,
                                              description="TEST opening deposit"))
        db.session.commit()
        return m.id


def _status(app, matter_id):
    db, M = _models()
    with app.app_context():
        return db.session.get(M.Matter, matter_id).status


# --- 1. closing a matter that still holds client money ------------------------------------

def test_close_refuses_while_trust_is_held(app, client):
    mid = _matter_holding_trust(app, 110000, "A")
    r = client.post(f"/matters/{mid}/close", data={"_csrf": csrf(client)}, follow_redirects=True)
    assert r.status_code == 200
    # The refusal has to carry the number, or the user goes hunting through the ledger.
    assert b"$1,100.00" in r.data
    assert b"in trust" in r.data
    assert _status(app, mid) == "open"


def test_close_allowed_once_trust_is_zero(app, client):
    mid = _matter_holding_trust(app, 0, "B")
    r = client.post(f"/matters/{mid}/close", data={"_csrf": csrf(client)}, follow_redirects=True)
    assert r.status_code == 200
    assert _status(app, mid) == "closed"


def test_edit_cannot_sneak_the_status_past_the_guard(app, client):
    """The close button is not the only way to close a matter; the edit form sets status too."""
    mid = _matter_holding_trust(app, 50000, "C")
    db, M = _models()
    with app.app_context():
        m = db.session.get(M.Matter, mid)
        name, client_id = m.name, m.client_id
    r = client.post(f"/matters/{mid}/edit",
                    data={"_csrf": csrf(client), "status": "closed", "name": name,
                          "client_id": str(client_id), "billing_type": "hourly"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b"$500.00" in r.data
    assert _status(app, mid) == "open"


# --- 2 and 3. invoice totals ---------------------------------------------------------------

def _new_invoice(client, matter_id, data):
    payload = {"_csrf": csrf(client), "issued_on": date.today().isoformat()}
    payload.update(data)
    return client.post(f"/invoices/new?matter_id={matter_id}", data=payload, follow_redirects=True)


def test_invoice_cannot_total_below_zero(app, client):
    mid = _matter_holding_trust(app, 0, "D", "flat")
    r = _new_invoice(client, mid, {"flat_amount": "100", "adjustment_amount": "-500",
                                   "adjustment_description": "TEST discount"})
    assert b"cannot total less than zero" in r.data


def test_huge_invoice_asks_once_then_goes_through(app, client):
    mid = _matter_holding_trust(app, 0, "E", "flat")
    r = _new_invoice(client, mid, {"flat_amount": "99999999"})
    assert b"$99,999,999.00" in r.data
    assert b"tick the box" in r.data

    r = _new_invoice(client, mid, {"flat_amount": "99999999", "confirm_unusual": "1"})
    assert b"cannot total less than zero" not in r.data
    assert b"tick the box" not in r.data


def test_ordinary_invoice_is_not_interrupted(app, client):
    mid = _matter_holding_trust(app, 0, "F", "flat")
    r = _new_invoice(client, mid, {"flat_amount": "2500"})
    assert b"tick the box" not in r.data


# --- 3. time entries -----------------------------------------------------------------------

def _log_time(client, matter_id, duration, extra=None):
    data = {"_csrf": csrf(client), "matter_id": str(matter_id), "duration": duration,
            "date": date.today().isoformat(), "description": "TEST entry", "rate": "350.00",
            "billable": "1"}
    data.update(extra or {})
    return client.post("/time/new", data=data, follow_redirects=True)


def test_duration_longer_than_a_day_asks_once(app, client):
    mid = _matter_holding_trust(app, 0, "G")
    r = _log_time(client, mid, "999999")
    assert b"longer than a day" in r.data
    assert b"this duration is correct" in r.data


def test_confirmed_long_duration_is_accepted(app, client):
    mid = _matter_holding_trust(app, 0, "H")
    r = _log_time(client, mid, "30", {"confirm_unusual": "1"})
    assert b"longer than a day" not in r.data
    db, M = _models()
    with app.app_context():
        assert M.TimeEntry.query.filter_by(matter_id=mid).count() == 1


def test_zero_and_negative_durations_say_why(app, client):
    """These were reported as silent. They are not, and the message must stay visible."""
    mid = _matter_holding_trust(app, 0, "I")
    for bad in ("0", "-1"):
        r = _log_time(client, mid, bad)
        assert b"greater than zero" in r.data, f"no message for duration {bad!r}"
    db, M = _models()
    with app.app_context():
        assert M.TimeEntry.query.filter_by(matter_id=mid).count() == 0


def test_ordinary_duration_is_not_interrupted(app, client):
    mid = _matter_holding_trust(app, 0, "J")
    r = _log_time(client, mid, "1.5")
    assert b"longer than a day" not in r.data


# --- 4. backdating into a period that has already been reconciled --------------------------

def _reconcile_through(app, when):
    db, M = _models()
    with app.app_context():
        db.session.add(M.TrustReconciliation(period_end=when, balanced=True))
        db.session.commit()


def _deposit(client, client_id, when, cents="1.00"):
    return client.post("/trust/new", data={
        "_csrf": csrf(client), "client_id": str(client_id), "type": "deposit",
        "amount": cents, "date": when.isoformat(), "description": "TEST deposit",
        "payee": "", "reference": "",
    }, follow_redirects=True)


def _client_of(app, matter_id):
    db, M = _models()
    with app.app_context():
        return db.session.get(M.Matter, matter_id).client_id


def test_backdating_into_a_reconciled_period_is_refused(app, client):
    from datetime import timedelta
    mid = _matter_holding_trust(app, 0, "K")
    cid = _client_of(app, mid)
    _reconcile_through(app, date.today())

    r = _deposit(client, cid, date.today() - timedelta(days=3))
    assert b"already been signed off" in r.data
    db, M = _models()
    with app.app_context():
        assert M.TrustTransaction.query.filter_by(client_id=cid).count() == 0


def test_an_entry_after_the_reconciliation_still_works(app, client):
    from datetime import timedelta
    mid = _matter_holding_trust(app, 0, "L")
    cid = _client_of(app, mid)
    r = _deposit(client, cid, date.today() + timedelta(days=1))
    assert b"already been signed off" not in r.data
    db, M = _models()
    with app.app_context():
        assert M.TrustTransaction.query.filter_by(client_id=cid).count() == 1


# --- 5. two people editing the same draft --------------------------------------------------

def _draft_invoice(app, matter_id, cents, number):
    db, M = _models()
    with app.app_context():
        m = db.session.get(M.Matter, matter_id)
        inv = M.Invoice(number=number, matter_id=m.id, client_id=m.client_id, kind="flat", status="draft",
                        issued_on=date.today(), due_on=date.today(), subtotal_cents=cents, total_cents=cents)
        inv.lines.append(M.InvoiceLine(kind="flat", description="TEST services", quantity=1.0,
                                       unit_cents=cents, amount_cents=cents))
        db.session.add(inv)
        db.session.commit()
        return inv.id


def _save_invoice(client, inv_id, version, notes):
    return client.post(f"/invoices/{inv_id}/edit", data={
        "_csrf": csrf(client), "version": str(version), "notes": notes,
        "issued_on": date.today().isoformat(), "due_on": date.today().isoformat(),
    }, follow_redirects=True)


def test_second_tab_cannot_silently_overwrite_the_first(app, client):
    mid = _matter_holding_trust(app, 0, "M")
    inv_id = _draft_invoice(app, mid, 2900, "INV-LOCK-1")
    db, M = _models()

    # Both tabs rendered at version 0.
    r = _save_invoice(client, inv_id, 0, "DUAL-A")
    assert b"while you had it open" not in r.data
    with app.app_context():
        assert db.session.get(M.Invoice, inv_id).notes == "DUAL-A"

    # The second tab still believes it is version 0, and must be refused.
    r = _save_invoice(client, inv_id, 0, "DUAL-B")
    assert b"while you had it open" in r.data
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        assert inv.notes == "DUAL-A", "the first tab's work was thrown away"
        assert inv.version == 1


def test_saving_from_a_current_page_still_works(app, client):
    mid = _matter_holding_trust(app, 0, "N")
    inv_id = _draft_invoice(app, mid, 2900, "INV-LOCK-2")
    assert b"while you had it open" not in _save_invoice(client, inv_id, 0, "FIRST").data
    assert b"while you had it open" not in _save_invoice(client, inv_id, 1, "SECOND").data
    db, M = _models()
    with app.app_context():
        assert db.session.get(M.Invoice, inv_id).notes == "SECOND"


# --- 6. the matters export that was missing ------------------------------------------------

def test_matters_csv_exports_and_carries_the_numbers(app, client):
    mid = _matter_holding_trust(app, 75000, "O")
    r = client.get("/exports/matters.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["Content-Type"]
    body = r.data.decode()
    header = body.splitlines()[0]
    for col in ("Number", "Client", "Status", "LimitationDate", "TrustBalance", "Outstanding"):
        assert col in header, f"{col} missing from the matters export"
    row = [ln for ln in body.splitlines() if "M-TGO" in ln]
    assert row, "the seeded matter is not in the export"
    assert "750.00" in row[0], "the trust balance on screen is not the one in the CSV"


def test_matters_export_is_linked_from_the_exports_page(app, client):
    r = client.get("/exports")
    assert b"/exports/matters.csv" in r.data


# --- 7. saying why a control is missing ----------------------------------------------------

def test_paid_invoice_says_why_void_is_unavailable(app, client):
    mid = _matter_holding_trust(app, 0, "P")
    inv_id = _draft_invoice(app, mid, 5000, "INV-VOID-1")
    db, M = _models()
    with app.app_context():
        inv = db.session.get(M.Invoice, inv_id)
        inv.status = "sent"
        db.session.add(M.Payment(invoice_id=inv.id, client_id=inv.client_id, received_on=date.today(),
                                 amount_cents=5000, method="check", reference="TEST"))
        db.session.flush()
        inv.recalc()          # paid_cents is a stored column, not a live sum
        db.session.commit()

    r = client.get(f"/invoices/{inv_id}")
    assert b"cannot be voided" in r.data
    assert b"issue a credit" in r.data


def test_unpaid_invoice_still_offers_void(app, client):
    mid = _matter_holding_trust(app, 0, "Q")
    inv_id = _draft_invoice(app, mid, 5000, "INV-VOID-2")
    r = client.get(f"/invoices/{inv_id}")
    assert b"cannot be voided" not in r.data
    assert b"Void" in r.data
