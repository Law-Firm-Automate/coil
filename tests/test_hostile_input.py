"""Guards on hostile and awkward input, found by the Phase 2 §D QA pass.

1. A spreadsheet formula in a contact name exported raw. Excel, LibreOffice and Sheets all
   evaluate a cell beginning with = + @ or -, so the firm that opens its own export runs
   whatever was typed into the name field.
2. A file could claim any extension it liked. HTML bytes uploaded as .pdf were stored,
   listed and served under that name.
3. Any character outside cp1252 was flattened to a question mark on the way into a PDF, so
   a client with a name in Arabic, Greek, Cyrillic or Chinese received a corrupted invoice.

Own SQLite file, own UPLOAD_DIR and PDF_DIR. Never touches data/practice.db.
Run: .venv/bin/python -m pytest tests/test_hostile_input.py -q
"""
import os
import shutil
import subprocess
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_hostile_input.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_hostile_input")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_hostile_input")

from tests.helpers import login  # noqa: E402

ARABIC = "نادية"          # Nadia
GREEK = "Θεοδώρα"  # Theodora


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


def _models():
    from app.extensions import db
    from app import models
    return db, models


# --- 1. spreadsheet formula injection -------------------------------------------------------

def test_csv_safe_neutralises_formulas_but_not_money():
    from app.helpers import csv_safe
    for payload in ("=cmd|'/c calc'!A1", "+1+1", "@SUM(A1)", "-2+3+cmd", "\tSUM(A1)"):
        assert csv_safe(payload).startswith("'"), f"{payload!r} left live"
    # Negative money is not a formula. Quoting it would turn the trust and discount columns
    # into text that no spreadsheet will add up.
    for safe in ("-500.00", "-1", "1250.00", "0", "Marchetti", "O=Brien", ""):
        assert csv_safe(safe) == safe, f"{safe!r} was mangled"


def test_contact_export_does_not_carry_a_live_formula(app, client):
    db, M = _models()
    with app.app_context():
        db.session.add(M.Contact(first_name="=cmd|'/c calc'!A1", last_name="QA", email="f@e.test",
                                 is_client=False))
        db.session.commit()
    body = client.get("/exports/contacts.csv").data.decode()
    assert "=cmd" in body, "the value should still be exported, just not as a formula"
    for line in body.splitlines():
        for cell in line.split(","):
            bare = cell.strip('"')
            assert not bare.startswith("=cmd"), f"live formula in export: {cell!r}"


# --- 2. a file that is not what it claims ---------------------------------------------------

def _matter(app, tag):
    db, M = _models()
    with app.app_context():
        u = M.User.query.first()
        c = M.Contact(first_name="Host", last_name=f"Ile{tag}", email=f"h{tag}@e.test", is_client=True)
        db.session.add(c)
        db.session.flush()
        m = M.Matter(number=f"M-HI{tag}", client_id=c.id, name=f"TEST hostile {tag}", billing_type="flat",
                     responsible_user_id=u.id, status="open")
        db.session.add(m)
        db.session.commit()
        return m.id


def test_html_dressed_as_a_pdf_is_refused(app):
    from app.blueprints.documents import store_bytes
    mid = _matter(app, "A")
    with app.app_context():
        doc, err = store_bytes(mid, "not-a-pdf.pdf", b"<html><body>hello</body></html>")
        assert doc is None
        assert err and "contents are a web page" in err


def test_a_real_pdf_is_still_accepted(app):
    from app.blueprints.documents import store_bytes
    mid = _matter(app, "B")
    with app.app_context():
        doc, err = store_bytes(mid, "real.pdf", b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n")
        assert err is None, err
        assert doc is not None


def test_formats_without_a_signature_are_left_alone(app):
    """A .txt, .csv or .eml has no magic bytes. Guessing at those would refuse real files."""
    from app.blueprints.documents import store_bytes
    mid = _matter(app, "C")
    with app.app_context():
        for name, data in (("notes.txt", b"just text"),
                           ("rows.csv", b"a,b,c\n1,2,3"),
                           ("mail.eml", b"From: a@b.c\nSubject: hi\n\nbody")):
            doc, err = store_bytes(mid, name, data)
            assert err is None, f"{name} refused: {err}"


def test_download_tells_the_browser_not_to_sniff(app, client):
    from app.blueprints.documents import store_bytes
    from app.extensions import db
    mid = _matter(app, "D")
    with app.app_context():
        doc, err = store_bytes(mid, "real2.pdf", b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n")
        assert err is None
        db.session.commit()
        did = doc.id
    r = client.get(f"/documents/{did}/download")
    assert r.status_code == 200
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


# --- 3. non-Latin text in PDFs ---------------------------------------------------------------

def test_needs_unicode_only_fires_when_it_has_to():
    from app.services.pdf import needs_unicode
    assert needs_unicode(ARABIC)
    assert needs_unicode(GREEK)
    assert needs_unicode("\U0001f600")
    # cp1252 covers these, so an ordinary invoice keeps the metrics it has always had.
    assert not needs_unicode("Jane Client")
    assert not needs_unicode("José Café")
    assert not needs_unicode("£100 and €50")
    assert not needs_unicode(None, "", "plain")


def _invoice_for(app, client_name):
    db, M = _models()
    with app.app_context():
        u = M.User.query.first()
        c = M.Contact(first_name=client_name, last_name="", company_name="", email="u@e.test", is_client=True)
        db.session.add(c)
        db.session.flush()
        m = M.Matter(number=f"M-UNI{abs(hash(client_name)) % 9999}", client_id=c.id, name="TEST unicode",
                     billing_type="flat", responsible_user_id=u.id, status="open")
        db.session.add(m)
        db.session.flush()
        inv = M.Invoice(number=f"INV-U{abs(hash(client_name)) % 9999}", matter_id=m.id, client_id=c.id,
                        kind="flat", status="draft", issued_on=date.today(), due_on=date.today(),
                        subtotal_cents=10000, total_cents=10000)
        inv.lines.append(M.InvoiceLine(kind="flat", description="Legal services", quantity=1.0,
                                       unit_cents=10000, amount_cents=10000))
        db.session.add(inv)
        db.session.commit()
        return inv.id


def test_a_non_latin_client_name_survives_into_the_pdf(app):
    from app.blueprints.invoices import render_invoice_pdf
    from app.services.pdf import unicode_on, reset_unicode
    from app.extensions import db
    from app import models as M
    inv_id = _invoice_for(app, ARABIC)
    with app.app_context():
        reset_unicode()
        inv = db.session.get(M.Invoice, inv_id)
        pdf = render_invoice_pdf(inv)
        assert unicode_on(), "the document should have switched to the bundled Unicode font"
        out = bytes(pdf.output())
        assert out.startswith(b"%PDF"), "not a PDF"
        assert b"DejaVu" in out, "the Unicode font was not embedded"

        # Check per character, not as a substring: Arabic is right-to-left, so the extracted
        # order is not the logical order and an `in` test on the whole word fails even when
        # every glyph is present. The old behaviour turned all of them into question marks.
        import io as _io
        import pypdf
        text = pypdf.PdfReader(_io.BytesIO(out)).pages[0].extract_text()
        missing = [ch for ch in ARABIC if ch not in text]
        assert not missing, f"dropped from the PDF: {missing}"
        assert "?" not in text, "characters were still being flattened"


def test_an_ordinary_invoice_does_not_switch_fonts(app):
    """The common case must keep the exact typography firms already send to clients."""
    from app.blueprints.invoices import render_invoice_pdf
    from app.services.pdf import unicode_on, reset_unicode
    from app.extensions import db
    from app import models as M
    inv_id = _invoice_for(app, "Jane")
    with app.app_context():
        reset_unicode()
        inv = db.session.get(M.Invoice, inv_id)
        pdf = render_invoice_pdf(inv)
        assert not unicode_on(), "a plain ASCII invoice should stay on the core font"
        out = bytes(pdf.output())
        assert out.startswith(b"%PDF")
