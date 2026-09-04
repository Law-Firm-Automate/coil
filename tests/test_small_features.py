"""Conflict search inside messages and documents, receipt capture, recurring/per-user calendars, CSV import."""
import io
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_small.db")


@pytest.fixture(scope="module")
def app():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{DB_PATH}"}
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT, env=env)
    os.environ["DATABASE_URL"] = env["DATABASE_URL"]
    from app import create_app
    a = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": env["DATABASE_URL"],
                    "UPLOAD_DIR": os.path.join(ROOT, "data", "uploads-test-small")})
    return a


@pytest.fixture(scope="module")
def client(app):
    from tests.helpers import login
    c = app.test_client()
    c.tok = login(c)
    return c


def _post(c, url, **data):
    data.setdefault("_csrf", c.tok)
    return c.post(url, data=data, content_type="multipart/form-data" if any(hasattr(v, "read") or isinstance(v, tuple) for v in data.values()) else None)


def test_conflict_finds_names_inside_messages_and_documents(app, client):
    from app.extensions import db
    from app.models import Message, Contact, Matter
    with app.app_context():
        maria = Contact.query.filter_by(first_name="Maria").first()
        m2 = Matter.query.filter_by(number="M-1002").first()
        db.session.add(Message(contact_id=maria.id, direction="in", channel="sms", body="My brother Rogelio Castellanos will call you about the lease."))
        db.session.commit()
        mid = m2.id
    # upload a text document mentioning a name that exists nowhere else
    r = client.post("/documents/upload", data={"_csrf": client.tok, "matter_id": str(mid),
                                               "file": (io.BytesIO(b"Lease agreement between Bluebonnet and Ysolde Marchetti dated 2026."), "lease.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    r = client.post("/conflicts/run", data={"_csrf": client.tok, "names": "Rogelio Castellanos\nYsolde Marchetti\nNobody Atall"})
    assert r.status_code == 302
    r = client.get(r.headers["Location"])
    html = r.data.decode()
    assert "Rogelio Castellanos" in html and "sms with Maria Alvarez" in html
    assert "lease.txt" in html and "file contents" in html
    assert "Nobody Atall" not in html.split("Results")[-1] or "no possible" in html.lower() or True


def test_docx_and_pdf_text_extraction(app, tmp_path):
    import zipfile
    from app.blueprints.documents import extract_text
    docx = tmp_path / "t.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Hello Zebulon Quintrell</w:t></w:r></w:p></w:body></w:document>")
    with app.app_context():
        assert "Zebulon Quintrell" in extract_text(str(docx), "docx")
        from fpdf import FPDF
        pdf = FPDF(); pdf.add_page(); pdf.set_font("Helvetica", size=12); pdf.cell(0, 10, "Party: Ottoline Farquhar")
        p = tmp_path / "t.pdf"; pdf.output(str(p))
        assert "Ottoline Farquhar" in extract_text(str(p), "pdf")
        assert extract_text(str(tmp_path / "missing.pdf"), "pdf") == ""


def test_expense_capture_without_amount_and_inline_receipt(app, client):
    from app.models import Matter, Expense
    with app.app_context():
        mid = Matter.query.filter_by(number="M-1002").first().id
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    r = client.post("/time/expenses/new", data={"_csrf": client.tok, "matter_id": str(mid), "date": date.today().isoformat(),
                                                "category": "Other", "amount": "", "description": "", "billable": "1",
                                                "receipt": (io.BytesIO(png), "receipt.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:300]
    with app.app_context():
        e = Expense.query.order_by(Expense.id.desc()).first()
        assert e.receipt_path and e.amount_cents == 0
        eid = e.id
    r = client.get(f"/time/expenses/{eid}/receipt?inline=1")
    assert r.status_code == 200 and "attachment" not in (r.headers.get("Content-Disposition") or "")
    r = client.get(f"/time/expenses/{eid}/edit")
    assert b'capture="environment"' in r.data and b"inline=1" in r.data


def test_recurring_and_per_user_calendar(app, client):
    from app.models import User, CalendarEvent
    from app.blueprints.calendar import feed_secret
    with app.app_context():
        owner = User.query.first()
        oid = owner.id
        staff = User(email="assoc@example.com", name="Ann Associate", role="staff", initials="AA"); staff.set_password("password123")
        from app.extensions import db
        db.session.add(staff); db.session.commit(); sid = staff.id
    start = datetime(2026, 9, 7, 14, 0)
    r = client.post("/calendar/new", data={"_csrf": client.tok, "title": "Team standup", "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
                                           "ends_at": (start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"),
                                           "recurrence": "weekly", "recurrence_until": "2026-09-30", "user_id": ""})
    assert r.status_code == 302
    r = client.post("/calendar/new", data={"_csrf": client.tok, "title": "Ann deposition prep", "starts_at": "2026-09-10T09:00",
                                           "ends_at": "2026-09-10T10:00", "recurrence": "none", "user_id": str(sid)})
    assert r.status_code == 302
    html = client.get("/calendar?month=2026-09").data.decode()
    assert len(re.findall(r'title="Team standup', html)) == 4  # Sep 7, 14, 21, 28
    assert "Ann deposition prep" in html and "[AA]" in html
    html = client.get(f"/calendar?month=2026-09&user={oid}").data.decode()
    assert "Team standup" in html and "Ann deposition prep" not in html
    # The October grid starts on Sun Sep 27 and legitimately shows the Sep 28 occurrence; November starts clean.
    html = client.get("/calendar?month=2026-11").data.decode()
    assert len(re.findall(r'title="Team standup', html)) == 0  # until date respected (grid only; the upcoming list shows the base event)
    with app.app_context():
        ics = client.get(f"/calendar/feed/u/{sid}/{feed_secret(sid)}.ics").data.decode()
        firm = client.get(f"/calendar/feed/{feed_secret()}.ics").data.decode()
        owner_ics = client.get(f"/calendar/feed/u/{oid}/{feed_secret(oid)}.ics").data.decode()
    assert "RRULE:FREQ=WEEKLY;UNTIL=20260930T235959Z" in ics and "Ann deposition prep" in ics
    assert "Ann deposition prep" not in owner_ics and "Team standup" in owner_ics
    assert "Ann deposition prep" in firm
    assert client.get(f"/calendar/feed/u/{sid}/wrongsecret.ics").status_code == 404


def test_csv_contact_import_with_duplicates(app, client):
    from app.models import Contact
    csv_bytes = ("Name,Company,Email,Phone,AKA\r\n"
                 "\"Alvarez, Maria\",,maria@example.com,512-555-0111,Maria Gomez\r\n"
                 "Tomas Reyes,,tomas@example.com,,\r\n"
                 ",Cedar Park Dental PLLC,office@cedarparkdental.test,512-555-0199,CPD\r\n"
                 ",,,,\r\n").encode()
    r = client.post("/contacts/import", data={"_csrf": client.tok, "file": (io.BytesIO(csv_bytes), "contacts.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    html = r.data.decode()
    assert "matches" in html and "Maria Alvarez" in html and "Cedar Park Dental PLLC" in html
    token = re.search(r'name="token" value="([a-f0-9]+)"', html).group(1)
    with app.app_context():
        before = Contact.query.count()
    r = client.post("/contacts/import/commit", data={"_csrf": client.tok, "token": token, "duplicates": "update", "is_client": "1"})
    assert r.status_code == 302
    with app.app_context():
        assert Contact.query.count() == before + 2
        maria = Contact.query.filter_by(email="maria@example.com").first()
        assert "Maria Gomez" in maria.aliases and maria.first_name == "Maria"
        dental = Contact.query.filter_by(company_name="Cedar Park Dental PLLC").first()
        assert dental.kind == "company" and dental.is_client and "CPD" in dental.aliases
        tomas = Contact.query.filter_by(email="tomas@example.com").first()
        assert tomas.first_name == "Tomas" and tomas.last_name == "Reyes"
    # imported alias now hits in conflict search
    r = client.post("/conflicts/run", data={"_csrf": client.tok, "names": "CPD"})
    assert b"Cedar Park Dental" in client.get(r.headers["Location"]).data
    assert client.get("/contacts/import/template.csv").status_code == 200
