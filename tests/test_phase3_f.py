"""Phase 3, agent F: document versions, folders and tags, full-text search, email filing, PWA files.

Own SQLite file (data/test_phase3_f.db) seeded by seed.py and own UPLOAD_DIR, so nothing touches practice.db.
Run: .venv/bin/python -m pytest tests/test_phase3_f.py -q
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
from email.message import EmailMessage

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_phase3_f.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads-test-phase3-f")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_phase3_f")

from tests.helpers import login  # noqa: E402
from app.extensions import db  # noqa: E402

# Smallest PDF that pypdf will open. Text extraction on it is empty, which is fine: the test checks filing, not OCR.
TINY_PDF = (b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\nxref\n0 4\n0000000000 65535 f \n"
            b"0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n160\n%%EOF\n")


@pytest.fixture(scope="module")
def app():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    shutil.rmtree(PDF_DIR, ignore_errors=True)
    env = dict(os.environ, DATABASE_URL=DB_URI, SMTP_HOST="")
    out = subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], env=env, cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    from app import create_app
    a = create_app({"SQLALCHEMY_DATABASE_URI": DB_URI, "UPLOAD_DIR": UPLOAD_DIR, "PDF_DIR": PDF_DIR, "TESTING": True,
                    "SMTP_HOST": "", "BASE_URL": "http://test.local", "IMAP_HOST": "", "IMAP_USER": ""})
    yield a


@pytest.fixture(scope="module")
def staff(app):
    c = app.test_client()
    tok = login(c)
    return c, tok


def _matter_id(app, number):
    from app.models import Matter
    with app.app_context():
        return Matter.query.filter_by(number=number).first().id


def _id_from(location):
    return int(re.search(r"/documents/(\d+)", location).group(1))


def _upload(c, tok, matter_id, name, data, folder="", tags="", extra=None):
    payload = {"_csrf": tok, "matter_id": str(matter_id), "folder": folder, "tags": tags,
               "file": (io.BytesIO(data), name)}
    payload.update(extra or {})
    r = c.post("/documents/upload", data=payload, content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:300]
    return r


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------
def test_versions_current_only_with_badge_and_history(app, staff):
    c, tok = staff
    from app.models import Document
    mid = _matter_id(app, "M-1002")
    _upload(c, tok, mid, "brief-v1.txt", b"First draft of the appellate brief, zebra paragraph one.")
    with app.app_context():
        v1 = Document.query.filter_by(name="brief-v1.txt").first()
        v1_id = v1.id
        assert v1.version == 1 and v1.is_current and v1.version_of_id is None
    r = c.post(f"/documents/{v1_id}/new-version", data={"_csrf": tok, "file": (io.BytesIO(b"Second draft, tightened."), "brief-v2.txt")},
               content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:300]
    v2_id = _id_from(r.headers["Location"])
    with app.app_context():
        v1 = db.session.get(Document, v1_id)
        v2 = db.session.get(Document, v2_id)
        assert v2.version == 2 and v2.version_of_id == v1_id and v2.is_current
        assert not v1.is_current
        assert v2.matter_id == mid
    r = c.get(f"/documents?matter_id={mid}")
    assert r.status_code == 200
    html = r.data.decode()
    assert f'href="/documents/{v2_id}/download"' in html and f'href="/documents/{v1_id}/download"' not in html
    assert f'href="/documents/{v2_id}/versions"' in html and ">v2<" in html
    r = c.get(f"/documents/{v2_id}/versions")
    assert r.status_code == 200
    assert b"brief-v1.txt" in r.data and b"brief-v2.txt" in r.data
    # history reachable from either version id, and the old file still downloads
    assert c.get(f"/documents/{v1_id}/versions").status_code == 200
    r = c.get(f"/documents/{v1_id}/download")
    assert r.status_code == 200 and b"First draft" in r.data
    r = c.get(f"/documents/{v2_id}/download")
    assert r.status_code == 200 and b"Second draft" in r.data
    app.v1_id, app.v2_id = v1_id, v2_id


# ---------------------------------------------------------------------------
# folders, tags, bulk
# ---------------------------------------------------------------------------
def test_bulk_move_and_tag(app, staff):
    c, tok = staff
    from app.models import Document
    mid = _matter_id(app, "M-1002")
    _upload(c, tok, mid, "motion-to-dismiss.txt", b"Motion to dismiss for want of jurisdiction.")
    _upload(c, tok, mid, "exhibit-a.txt", b"Exhibit A: the signed contract.", tags="Exhibit")
    with app.app_context():
        ids = [d.id for d in Document.query.filter(Document.name.in_(["motion-to-dismiss.txt", "exhibit-a.txt"])).all()]
        assert len(ids) == 2
    r = c.post("/documents/bulk", data={"_csrf": tok, "doc_id": [str(i) for i in ids], "set_folder": "1",
                                        "folder": " Pleadings / Motions ", "tags": "Urgent, filed", "tag_mode": "add"})
    assert r.status_code == 302, r.data[:300]
    with app.app_context():
        docs = Document.query.filter(Document.id.in_(ids)).all()
        for d in docs:
            assert d.folder == "Pleadings/Motions"
            assert "urgent" in d.tags and "filed" in d.tags
        ex = Document.query.filter_by(name="exhibit-a.txt").first()
        assert ex.tags == "exhibit, urgent, filed"  # existing tag kept, new ones appended, lowercased
    # folder filter is a prefix match, tag filter is exact
    r = c.get("/documents?folder=Pleadings")
    assert b"motion-to-dismiss.txt" in r.data and b"exhibit-a.txt" in r.data and b"brief-v2.txt" not in r.data
    r = c.get("/documents?tag=urgent")
    assert b"motion-to-dismiss.txt" in r.data and b"brief-v2.txt" not in r.data
    r = c.get("/documents?tag=exhibit")
    assert b"exhibit-a.txt" in r.data and b"motion-to-dismiss.txt" not in r.data
    # bulk on a versioned document applies to the whole family
    r = c.post("/documents/bulk", data={"_csrf": tok, "doc_id": [str(app.v2_id)], "set_folder": "1", "folder": "Briefs"})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(Document, app.v1_id).folder == "Briefs" and db.session.get(Document, app.v2_id).folder == "Briefs"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def test_search_by_text_snippet_and_by_tag(app, staff):
    c, tok = staff
    mid = _matter_id(app, "M-1001")
    body = ("Lorem ipsum filler text " * 20) + "the Colorado river zebra crossing was observed at dawn " + ("more filler text " * 20)
    _upload(c, tok, mid, "field-notes.txt", body.encode())
    r = c.get("/documents/search?q=zebra")
    assert r.status_code == 200
    html = r.data.decode()
    assert "field-notes.txt" in html
    assert "zebra crossing" in html and "..." in html  # snippet around the hit, not the whole file
    assert "brief-v1.txt" not in html  # old versions never surface
    # by tag
    r = c.get("/documents/search?q=urgent")
    html = r.data.decode()
    assert "motion-to-dismiss.txt" in html and "exhibit-a.txt" in html and ">tag<" in html
    # scoped to a matter
    r = c.get(f"/documents/search?q=zebra&matter_id={_matter_id(app, 'M-1002')}")
    assert b"field-notes.txt" not in r.data
    # nothing
    r = c.get("/documents/search?q=nonexistentwordxyz")
    assert r.status_code == 200 and b"Nothing matches" in r.data
    # the documents page carries the search box
    r = c.get("/documents")
    assert b'action="/documents/search"' in r.data


# ---------------------------------------------------------------------------
# email filing
# ---------------------------------------------------------------------------
def _fake_messages():
    return [
        dict(message_id="<one@test.local>", from_addr="opposing@counsel.test", to_addr="files@demolaw.test",
             subject="Re: demand letter [M-1002]", body="Please see the attached response.",
             attachments=[("response.pdf", TINY_PDF, "application/pdf")]),
        dict(message_id="<two@test.local>", from_addr="maria@example.com", to_addr="files@demolaw.test",
             subject="Question about the signing", body="Can we move the appointment to Friday?", attachments=[]),
        dict(message_id="<three@test.local>", from_addr="stranger@nowhere.test", to_addr="files@demolaw.test",
             subject="Hello", body="I found your firm online.",
             attachments=[("notes.txt", b"stranger notes about a zoning dispute", "text/plain")]),
    ]


def test_parse_email_reads_mime():
    from app.blueprints.emailin import parse_email
    m = EmailMessage()
    m["From"] = "Maria Alvarez <Maria@Example.com>"
    m["To"] = "files@demolaw.test"
    m["Subject"] = "Signing [M-1001]"
    m["Message-ID"] = "<mime-1@test.local>"
    m.set_content("Plain text body here.")
    m.add_alternative("<p>HTML <b>body</b> here.</p>", subtype="html")
    m.add_attachment(b"hello", maintype="text", subtype="plain", filename="hi.txt")
    p = parse_email(m.as_bytes())
    assert p["from_addr"] == "maria@example.com" and p["subject"] == "Signing [M-1001]"
    assert p["message_id"] == "<mime-1@test.local>" and p["body"].startswith("Plain text body")
    assert p["attachments"] == [("hi.txt", b"hello", "text/plain")]
    # html-only mail is stripped to text
    h = EmailMessage()
    h["From"] = "x@y.test"
    h.set_content("<p>Only <i>html</i> here</p>", subtype="html")
    assert parse_email(h.as_bytes())["body"] == "Only html here"


def test_email_filing_matches_and_is_idempotent(app, staff, monkeypatch):
    c, tok = staff
    from app.blueprints import emailin
    from app.models import Message, Document
    from app import cli
    monkeypatch.setattr(emailin, "fetch_unseen", _fake_messages)
    m1002 = _matter_id(app, "M-1002")
    m1001 = _matter_id(app, "M-1001")
    with app.app_context():
        before_docs = Document.query.count()
        counts = emailin.run_emailin()
        assert counts == dict(filed=2, unfiled=1, skipped=0)
        one = Message.query.filter_by(message_id="<one@test.local>").one()
        assert one.channel == "email" and one.direction == "in" and one.matter_id == m1002
        assert one.subject == "Re: demand letter [M-1002]" and one.has_attachments and one.body.startswith("Please see")
        assert one.contact_id == one.matter.client_id
        pdf = Document.query.filter_by(matter_id=m1002, name="response.pdf").one()
        assert pdf.folder == "Email" and pdf.is_current and pdf.size == len(TINY_PDF)
        assert os.path.isfile(os.path.join(UPLOAD_DIR, pdf.path))
        two = Message.query.filter_by(message_id="<two@test.local>").one()
        assert two.matter_id == m1001 and two.contact.email == "maria@example.com"
        three = Message.query.filter_by(message_id="<three@test.local>").one()
        assert three.matter_id is None and three.contact_id is None and three.status == "unfiled"
        assert Document.query.count() == before_docs + 1
        # second run: nothing new, nothing duplicated
        counts = emailin.run_emailin()
        assert counts == dict(filed=0, unfiled=0, skipped=3)
        assert Message.query.filter(Message.channel == "email").count() == 3
        assert Document.query.count() == before_docs + 1
        # the cli wrapper refuses to touch IMAP when it is not configured
        assert cli.run_emailin() == dict(filed=0, unfiled=0, skipped=0)
        three_id = three.id
    # review list shows the stranger with the parked attachment, then staff file it
    r = c.get("/messages/unfiled")
    assert r.status_code == 200
    assert b"stranger@nowhere.test" in r.data and b"notes.txt" in r.data and b"opposing@counsel.test" not in r.data
    r = c.post(f"/messages/unfiled/{three_id}/file", data={"_csrf": tok, "matter_id": str(m1002)})
    assert r.status_code == 302
    with app.app_context():
        three = db.session.get(Message, three_id)
        assert three.matter_id == m1002 and three.contact_id == three.matter.client_id and three.status == "received"
        notes = Document.query.filter_by(matter_id=m1002, name="notes.txt").one()
        assert notes.folder == "Email" and "zoning" in notes.extracted_text
        assert not os.path.exists(os.path.join(UPLOAD_DIR, "unfiled", str(three_id)))
    r = c.get("/messages/unfiled")
    assert b"stranger@nowhere.test" not in r.data
    # the filed email is searchable through the documents index too
    r = c.get("/documents/search?q=zoning")
    assert b"notes.txt" in r.data


def test_sender_with_two_open_matters_is_not_guessed(app):
    from app.blueprints.emailin import find_matter, file_email
    from app.models import Matter, Contact, Message
    with app.app_context():
        maria = Contact.query.filter_by(email="maria@example.com").first()
        extra = Matter(number="M-1999", client_id=maria.id, name="Alvarez second matter", status="open")
        db.session.add(extra)
        db.session.commit()
        msg = dict(message_id="<four@test.local>", from_addr="maria@example.com", subject="hi", body="no number", attachments=[])
        assert find_matter(msg) is None
        # a bracketed number still wins, in any case, and a lowercase one is fine
        assert find_matter(dict(subject="re: [m-1999] update", body="", from_addr="")).id == extra.id
        row, created = file_email(msg)
        assert created and row.matter_id is None
        db.session.delete(extra)
        db.session.delete(db.session.get(Message, row.id))
        db.session.commit()


# ---------------------------------------------------------------------------
# PWA
# ---------------------------------------------------------------------------
def test_pwa_manifest_service_worker_and_registration(app, staff):
    c, tok = staff
    r = c.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.content_type.startswith("application/manifest+json")
    m = json.loads(r.data)
    assert m["name"] == "Coil" and m["short_name"] == "Coil" and m["display"] == "standalone"
    assert m["theme_color"] == "#1f5f8b" and any(i["sizes"] == "512x512" for i in m["icons"])
    for icon in m["icons"]:
        ri = c.get(icon["src"])
        assert ri.status_code == 200, icon
        assert ri.content_type.startswith(icon["type"])
    r = c.get("/sw.js")
    assert r.status_code == 200 and r.content_type.startswith("text/javascript")
    assert r.headers.get("Service-Worker-Allowed") == "/" and b"offline.html" in r.data
    r = c.get("/offline")
    assert r.status_code == 200 and b"offline" in r.data.lower()
    assert c.get("/static/offline.html").status_code == 200
    # registration lines in the head of staff and public layouts
    r = c.get("/documents")
    head = r.data.decode().split("</head>")[0]
    assert 'rel="manifest" href="/manifest.webmanifest"' in head and 'name="theme-color"' in head
    assert "serviceWorker.register('/sw.js')" in head
    anon = app.test_client()
    r = anon.get("/portal/login")
    head = r.data.decode().split("</head>")[0]
    assert 'rel="manifest" href="/manifest.webmanifest"' in head and "serviceWorker.register('/sw.js')" in head
    # the unauthenticated PWA files never redirect to login
    assert anon.get("/manifest.webmanifest").status_code == 200 and anon.get("/sw.js").status_code == 200


def test_integrations_page_shows_filing_instructions(app, staff):
    c, tok = staff
    r = c.get("/settings/integrations")
    assert r.status_code == 200
    assert b"Email filing" in r.data and b"[M-1002]" in r.data and b"IMAP_HOST" in r.data
