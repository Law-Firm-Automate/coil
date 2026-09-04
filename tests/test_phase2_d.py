"""Phase 2, agent D: portal secure messaging, document e-signature, Spanish client pages.

Own SQLite file (data/test_phase2_d.db) seeded by seed.py, own UPLOAD_DIR and PDF_DIR, so nothing touches practice.db.
Run: .venv/bin/python -m pytest tests/test_phase2_d.py -q
"""
import io
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_phase2_d.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads-test-phase2-d")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_phase2_d")

from tests.helpers import login  # noqa: E402


@pytest.fixture(scope="module")
def app():
    for p in (DB_PATH,):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    shutil.rmtree(PDF_DIR, ignore_errors=True)
    env = dict(os.environ, DATABASE_URL=DB_URI, SMTP_HOST="")
    out = subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], env=env, cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    from app import create_app
    a = create_app({"SQLALCHEMY_DATABASE_URI": DB_URI, "UPLOAD_DIR": UPLOAD_DIR, "PDF_DIR": PDF_DIR,
                    "TESTING": True, "SMTP_HOST": "", "BASE_URL": "http://test.local"})
    yield a


@pytest.fixture
def staff(app):
    c = app.test_client()
    tok = login(c)
    return c, tok


def portal_login(app, contact_id):
    """Mint a magic link for the contact and use it, returning a signed-in portal client."""
    from app.extensions import db
    from app.models import PortalToken
    with app.app_context():
        tok = PortalToken(contact_id=contact_id, expires_at=datetime.utcnow() + timedelta(minutes=30))
        db.session.add(tok)
        db.session.commit()
        token = tok.token
    c = app.test_client()
    r = c.get(f"/portal/auth/{token}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/portal")
    return c


def maria_id(app):
    from app.models import Contact
    with app.app_context():
        return Contact.query.filter_by(last_name="Alvarez").first().id


def pdf_bytes(text="Agreement between the parties. Please sign."):
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "", 12)
    pdf.multi_cell(0, 6, text)
    return bytes(pdf.output())


def pdf_text(path):
    from pypdf import PdfReader
    return "".join((p.extract_text() or "") for p in PdfReader(path).pages)


# ---------------------------------------------------------------------------
# Portal secure messaging
# ---------------------------------------------------------------------------
def test_portal_message_to_staff_and_reply(app, staff):
    from app.extensions import db
    from app.models import Message, Matter
    from app.services.mail import dev_outbox
    client, tok = staff
    mid = maria_id(app)
    with app.app_context():
        matter = Matter.query.filter_by(number="M-1001").first()
        matter_id = matter.id

    portal = portal_login(app, mid)
    r = portal.get("/portal/messages")
    assert r.status_code == 200 and b"No messages yet" in r.data
    r = portal.post("/portal/messages/send", data={"body": "When is my signing appointment?", "matter_id": matter_id})
    assert r.status_code == 302
    with app.app_context():
        m = Message.query.filter_by(contact_id=mid, channel="portal", direction="in").first()
        assert m and m.matter_id == matter_id and m.read_at is None and m.body == "When is my signing appointment?"
    notice = dev_outbox()[0]
    assert notice["subject"] == "New portal message from Maria Alvarez" and notice["to"] == "owner@example.com"
    assert "/messages/" in notice["html"]

    # staff threads list: portal badge + unread count, then the thread shows the message with a portal badge
    r = client.get("/messages")
    assert r.status_code == 200 and b"1 portal unread" in r.data and b"1 unread portal" in r.data
    r = client.get(f"/messages/{mid}")
    assert r.status_code == 200 and b"When is my signing appointment?" in r.data
    assert b'<span class="badge new">portal</span>' in r.data and b"Reply in portal" in r.data
    with app.app_context():
        m = Message.query.filter_by(contact_id=mid, channel="portal", direction="in").first()
        assert m.read_at is not None  # staff opened it
    r = client.get("/messages")
    assert b"portal unread" not in r.data

    # staff reply in the portal: client gets a notice with a login link and never the body
    body = "Your signing appointment is Tuesday at 10am. SECRET-BODY-42"
    r = client.post("/messages/portal-send", data={"_csrf": tok, "contact_id": mid, "body": body, "matter_id": matter_id})
    assert r.status_code == 302
    with app.app_context():
        out = Message.query.filter_by(contact_id=mid, channel="portal", direction="out").first()
        assert out and out.user_id and out.read_at is None and out.body == body
        out_id = out.id
    notice = dev_outbox()[0]
    assert notice["to"] == "maria@example.com"
    assert "new secure message" in notice["subject"]
    assert "SECRET-BODY-42" not in notice["html"] and "Tuesday" not in notice["html"]
    assert "/portal/login" in notice["html"]

    # client sees the unread count on portal home, then reading the thread marks read_at
    r = portal.get("/portal")
    assert r.status_code == 200 and b"1 unread" in r.data and b"Secure messages" in r.data
    r = portal.get(f"/portal/messages?thread={matter_id}")
    assert r.status_code == 200 and b"SECRET-BODY-42" in r.data
    with app.app_context():
        assert db.session.get(Message, out_id).read_at is not None
    r = portal.get("/portal")
    assert b"No unread messages" in r.data
    r = client.get(f"/messages/{mid}")
    assert b"read " in r.data  # staff sees the read receipt

    # empty body refused, wrong matter falls back to general
    r = portal.post("/portal/messages/send", data={"body": "   "})
    assert r.status_code == 302
    with app.app_context():
        assert Message.query.filter_by(contact_id=mid, channel="portal", direction="in").count() == 1


# ---------------------------------------------------------------------------
# Document e-signature
# ---------------------------------------------------------------------------
def _upload_pdf(app, client, tok, name="retainer.pdf"):
    from app.models import Matter, Document
    with app.app_context():
        matter_id = Matter.query.filter_by(number="M-1001").first().id
    r = client.post("/documents/upload", data={"_csrf": tok, "matter_id": matter_id,
                                               "file": (io.BytesIO(pdf_bytes()), name)},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        d = Document.query.filter_by(name=name).order_by(Document.id.desc()).first()
        assert d
        return d.id


def _request_and_send(app, client, tok, doc_id, title="Retainer agreement"):
    from app.models import DocumentSignature
    r = client.get(f"/signatures/new?document_id={doc_id}")
    assert r.status_code == 200 and b"Maria Alvarez" in r.data
    r = client.post("/signatures/new", data={"_csrf": tok, "document_id": doc_id, "contact_id": maria_id(app),
                                             "title": title, "message": "Please sign by Friday.", "action": "send"})
    assert r.status_code == 302, r.data[:300]
    sid = int(r.headers["Location"].rsplit("/", 1)[1])
    with app.app_context():
        s = __import__("app.extensions", fromlist=["db"]).db.session.get(DocumentSignature, sid)
        assert s.status == "sent" and len(s.document_hash) == 64 and s.sent_to == "maria@example.com"
        return sid, s.token, s.document_hash


def test_document_signature_flow(app, staff):
    import hashlib
    from app.extensions import db
    from app.models import DocumentSignature, DocumentSignatureEvent, Document
    from app.services.mail import dev_outbox
    client, tok = staff
    r = client.get("/documents")
    assert r.status_code == 200
    doc_id = _upload_pdf(app, client, tok)
    r = client.get("/documents")
    assert f"/signatures/new?document_id={doc_id}".encode() in r.data and b"Request signature" in r.data

    sid, token, doc_hash = _request_and_send(app, client, tok, doc_id)
    with app.app_context():
        d = db.session.get(Document, doc_id)
        with open(os.path.join(UPLOAD_DIR, d.path), "rb") as fh:
            assert hashlib.sha256(fh.read()).hexdigest() == doc_hash
    mail = dev_outbox()[0]
    assert mail["to"] == "maria@example.com" and "Please sign: Retainer agreement" == mail["subject"]
    assert f"/sign/doc/{token}" in mail["html"] and "Please sign by Friday." in mail["html"]
    assert f"/track/docsign/{token}.gif" in mail["html"]

    # staff list and detail
    r = client.get("/signatures?status=open")
    assert r.status_code == 200 and b"Retainer agreement" in r.data
    r = client.get(f"/signatures/{sid}")
    assert r.status_code == 200 and doc_hash.encode() in r.data and f"/sign/doc/{token}".encode() in r.data

    # portal home lists it
    portal = portal_login(app, maria_id(app))
    r = portal.get("/portal")
    assert b"Documents awaiting your signature" in r.data and f"/sign/doc/{token}".encode() in r.data

    # public page: inline PDF, one view, gif deduped inside 60s
    pub = app.test_client()
    r = pub.get(f"/sign/doc/{token}")
    assert r.status_code == 200 and b"Sign this document" in r.data and b"Please sign by Friday." in r.data
    assert f'<iframe src="/sign/doc/{token}/file"'.encode() in r.data
    r = pub.get(f"/sign/doc/{token}/file")
    assert r.status_code == 200 and r.data[:4] == b"%PDF" and "attachment" not in r.headers.get("Content-Disposition", "")
    r = pub.get(f"/track/docsign/{token}.gif")
    assert r.status_code == 200 and r.mimetype == "image/gif" and "no-store" in r.headers["Cache-Control"]
    with app.app_context():
        s = db.session.get(DocumentSignature, sid)
        assert s.view_count == 1 and s.status == "viewed" and s.first_viewed_at is not None
        assert DocumentSignatureEvent.query.filter_by(signature_id=sid, event="viewed").count() == 1

    # validation
    r = pub.post(f"/sign/doc/{token}", data={"signer_name": "Maria Alvarez"})
    assert r.status_code == 400 and b"tick the box" in r.data
    r = pub.post(f"/sign/doc/{token}", data={"signer_name": "", "agree": "1"})
    assert r.status_code == 400

    # sign
    r = pub.post(f"/sign/doc/{token}", data={"signer_name": "Maria Alvarez", "signer_email": "maria@example.com",
                                             "agree": "1"}, headers={"User-Agent": "pytest-browser/2.0"})
    assert r.status_code == 200 and b"Thank you, Maria Alvarez" in r.data and doc_hash.encode() in r.data
    with app.app_context():
        s = db.session.get(DocumentSignature, sid)
        assert s.status == "signed" and len(s.signature_hash) == 64 and s.signed_at
        expected = hashlib.sha256(f"{doc_hash}Maria Alvarez{s.signer_ip}{s.signed_at.isoformat()}".encode()).hexdigest()
        assert s.signature_hash == expected
        assert s.signer_ua == "pytest-browser/2.0" and s.signer_email == "maria@example.com"
        assert s.certificate_pdf_path and os.path.exists(s.certificate_pdf_path)
        assert s.certificate_pdf_path.startswith(PDF_DIR)
        text = re.sub(r"\s+", "", pdf_text(s.certificate_pdf_path))
        assert doc_hash in text and s.signature_hash in text and "MariaAlvarez" in text and "pytest-browser/2.0" in text
        assert "retainer.pdf" in text and "viewed" in text and "signed" in text
        assert DocumentSignatureEvent.query.filter_by(signature_id=sid, event="signed").count() == 1
        sig_hash = s.signature_hash
    # emails: signer and firm each get certificate + original
    mails = dev_outbox()[:2]
    tos = {m["to"] for m in mails}
    assert tos == {"maria@example.com", "billing@demolaw.test"}
    assert all("Signed: Retainer agreement" == m["subject"] for m in mails)

    # certificate downloads, public and staff
    r = pub.get(f"/sign/doc/{token}/certificate")
    assert r.status_code == 200 and r.data[:4] == b"%PDF"
    r = client.get(f"/signatures/{sid}/certificate")
    assert r.status_code == 200 and r.data[:4] == b"%PDF"
    r = client.get(f"/signatures/{sid}")
    assert b"pytest-browser/2.0" in r.data and b"Certificate PDF" in r.data

    # second attempt refused: no form, hash unchanged
    r = pub.get(f"/sign/doc/{token}")
    assert r.status_code == 200 and b"Sign this document" not in r.data and b"was signed by Maria Alvarez" in r.data
    r = pub.post(f"/sign/doc/{token}", data={"signer_name": "Someone Else", "agree": "1"})
    assert r.status_code == 200 and b"was signed by Maria Alvarez" in r.data
    with app.app_context():
        s = db.session.get(DocumentSignature, sid)
        assert s.signature_hash == sig_hash and s.signer_name == "Maria Alvarez"
        assert DocumentSignatureEvent.query.filter_by(signature_id=sid, event="signed").count() == 1
    # void refused once signed; gif no longer counts
    r = client.post(f"/signatures/{sid}/void", data={"_csrf": tok})
    assert r.status_code == 302
    pub.get(f"/track/docsign/{token}.gif")
    with app.app_context():
        s = db.session.get(DocumentSignature, sid)
        assert s.status == "signed" and s.view_count == 1
    r = client.get("/signatures?status=signed")
    assert b"Maria Alvarez" in r.data
    # portal home no longer lists it
    r = portal.get("/portal")
    assert f"/sign/doc/{token}".encode() not in r.data


def test_document_signature_decline_remind_void(app, staff):
    from app.extensions import db
    from app.models import DocumentSignature, DocumentSignatureEvent
    from app.services.mail import dev_outbox
    client, tok = staff
    doc_id = _upload_pdf(app, client, tok, name="waiver.pdf")
    sid, token, _ = _request_and_send(app, client, tok, doc_id, title="Waiver")
    r = client.post(f"/signatures/{sid}/remind", data={"_csrf": tok})
    assert r.status_code == 302 and dev_outbox()[0]["subject"] == "Reminder: Waiver"
    pub = app.test_client()
    r = pub.post(f"/sign/doc/{token}/decline", data={"reason": "Wrong version"})
    assert r.status_code == 200 and b"declined to sign" in r.data
    with app.app_context():
        s = db.session.get(DocumentSignature, sid)
        assert s.status == "declined"
        ev = DocumentSignatureEvent.query.filter_by(signature_id=sid, event="declined").first()
        assert ev and ev.detail == "Wrong version"
    assert dev_outbox()[0]["subject"] == "Declined: Waiver" and "Wrong version" in dev_outbox()[0]["html"]
    r = pub.get(f"/sign/doc/{token}")
    assert r.status_code == 200 and b"Sign this document" not in r.data
    r = pub.post(f"/sign/doc/{token}", data={"signer_name": "Maria Alvarez", "agree": "1"})
    assert r.status_code == 200 and b"declined" in r.data
    r = pub.get(f"/sign/doc/{token}/certificate")
    assert r.status_code == 404
    # reminders stop; void works on a declined request; draft flow and void of a draft
    r = client.post(f"/signatures/{sid}/remind", data={"_csrf": tok})
    assert r.status_code == 302
    r = client.post(f"/signatures/{sid}/void", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(DocumentSignature, sid).status == "void"
    r = pub.get(f"/sign/doc/{token}")
    assert b"no longer available" in r.data
    r = pub.get(f"/sign/doc/{token}/file")
    assert r.status_code == 404
    r = client.post("/signatures/new", data={"_csrf": tok, "document_id": doc_id, "contact_id": maria_id(app),
                                             "title": "Draft only", "action": "draft"})
    assert r.status_code == 302
    with app.app_context():
        d = DocumentSignature.query.filter_by(title="Draft only").first()
        assert d.status == "draft" and not d.document_hash
        dtoken = d.token
    r = pub.get(f"/sign/doc/{dtoken}")
    assert r.status_code == 200 and b"not been sent" in r.data
    # a signer outside the matter is refused
    from app.models import Contact
    with app.app_context():
        other = Contact.query.filter_by(company_name="Bluebonnet Logistics LLC").first().id
    r = client.post("/signatures/new", data={"_csrf": tok, "document_id": doc_id, "contact_id": other,
                                             "title": "Bad signer", "action": "send"})
    assert r.status_code == 200 and b"Pick a signer" in r.data
    r = client.get("/signatures/new")
    assert r.status_code == 200 and b"waiver.pdf" in r.data


# ---------------------------------------------------------------------------
# Spanish for es contacts, English for en contacts
# ---------------------------------------------------------------------------
def test_language_on_public_pages(app, staff):
    from app.extensions import db
    from app.models import Contact, Matter, Invoice, InvoiceLine, Engagement
    from app.blueprints.engagements import build_engagement, send_engagement
    from app.services.mail import dev_outbox
    client, tok = staff
    mid = maria_id(app)
    with app.app_context():
        m = Matter.query.filter_by(number="M-1001").first()
        inv = Invoice(number="INV-ES-1", matter_id=m.id, client_id=mid, kind="flat", status="sent",
                      issued_on=date.today(), due_on=date.today() + timedelta(days=30), sent_at=datetime.utcnow())
        inv.lines.append(InvoiceLine(kind="flat", description="Retainer", quantity=1, unit_cents=125000,
                                     amount_cents=125000))
        db.session.add(inv)
        inv.recalc()
        e = build_engagement(m, scope="Estate plan")
        send_engagement(e)
        db.session.commit()
        inv_token, eng_token = inv.public_token, e.token
    doc_id = _upload_pdf(app, client, tok, name="poder.pdf")
    sid, sig_token, _ = _request_and_send(app, client, tok, doc_id, title="Poder notarial")

    def set_lang(v):
        with app.app_context():
            db.session.get(Contact, mid).language = v
            db.session.commit()

    pub = app.test_client()
    set_lang("es")
    r = pub.get(f"/p/{inv_token}")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Factura INV-ES-1" in html and "Saldo pendiente" in html and "Pagar con tarjeta" in html
    assert "Pagar por transferencia bancaria" in html and f"/pay/{inv_token}?method=card" in html \
        and f"/pay/{inv_token}?method=ach" in html
    assert "Balance due" not in html
    r = pub.get(f"/sign/doc/{sig_token}")
    html = r.data.decode()
    assert "Firmar este documento" in html and "Su nombre completo" in html and "No deseo firmar" in html
    assert "Sign this document" not in html
    r = pub.post(f"/sign/doc/{sig_token}", data={"signer_name": "Maria Alvarez"})
    assert r.status_code == 400 and "marque la casilla" in r.data.decode().lower()
    r = pub.get(f"/sign/{eng_token}")
    html = r.data.decode()
    assert "Firmar esta carta" in html and "Sign this letter" not in html
    # portal in Spanish, including the login email and the new-message notice
    portal = portal_login(app, mid)
    r = portal.get("/portal")
    html = r.data.decode()
    assert "Le damos la bienvenida, Maria" in html and "Sus asuntos" in html and "Cerrar sesión" in html
    assert "Documentos pendientes de su firma" in html and "Revisar y firmar" in html
    r = portal.get("/portal/messages")
    assert "Escriba su mensaje" in r.data.decode()
    with app.app_context():  # earlier portal_login() calls used up the 3-per-15-minutes link allowance
        from app.models import PortalToken
        PortalToken.query.filter_by(contact_id=mid).delete()
        db.session.commit()
    r = pub.post("/portal/login", data={"email": "maria@example.com"})
    assert r.status_code == 200
    mail = dev_outbox()[0]
    assert mail["subject"].startswith("Su enlace de acceso") and "portal de cliente" in mail["html"]
    r = client.post("/messages/portal-send", data={"_csrf": tok, "contact_id": mid, "body": "Hola"})
    assert r.status_code == 302
    assert dev_outbox()[0]["subject"].startswith("Tiene un nuevo mensaje seguro")
    r = client.post(f"/signatures/{sid}/remind", data={"_csrf": tok})
    assert dev_outbox()[0]["subject"] == "Recordatorio: Poder notarial"

    set_lang("en")
    r = pub.get(f"/p/{inv_token}")
    html = r.data.decode()
    assert "Invoice INV-ES-1" in html and "Balance due" in html and "Pay by card" in html and "Factura" not in html
    r = pub.get(f"/sign/doc/{sig_token}")
    html = r.data.decode()
    assert "Sign this document" in html and "Firmar" not in html
    r = pub.get(f"/sign/{eng_token}")
    assert b"Sign this letter" in r.data
    r = portal.get("/portal")
    assert b"Welcome, Maria" in r.data and b"Log out" in r.data

    # firm default applies when the contact has no language set
    from app.models import Firm
    set_lang("")
    with app.app_context():
        Firm.get().default_language = "es"
        db.session.commit()
    r = pub.get(f"/p/{inv_token}")
    assert b"Factura INV-ES-1" in r.data
    r = pub.get("/portal/login")
    assert "Portal del cliente" in r.data.decode()
    with app.app_context():
        Firm.get().default_language = "en"
        db.session.commit()
    r = pub.get("/portal/login")
    assert b"Client portal" in r.data
