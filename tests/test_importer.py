"""Switch-to-Coil importer: Clio-style CSVs for every entity through preview and commit, cross-file links via
ExternalRef, re-import updating instead of duplicating, hand mapping of a generic CSV, failed-rows CSV."""
import io
import os
import re
import subprocess
import sys
import zipfile
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_importer.db")
UPLOADS = os.path.join(ROOT, "data", "uploads-test-importer")


@pytest.fixture(scope="module")
def app():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{DB_PATH}"}
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT, env=env)
    os.environ["DATABASE_URL"] = env["DATABASE_URL"]
    from app import create_app
    return create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": env["DATABASE_URL"], "UPLOAD_DIR": UPLOADS})


@pytest.fixture(scope="module")
def client(app):
    from tests.helpers import login
    c = app.test_client()
    c.tok = login(c)
    return c


def _upload(c, entity, body, filename, source="clio"):
    data = {"_csrf": c.tok, "source": source, "file": (io.BytesIO(body if isinstance(body, bytes) else body.encode()), filename)}
    r = c.post(f"/import/{entity}/upload", data=data, content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:300]
    loc = r.headers["Location"]
    assert "/import/preview/" in loc, loc
    return loc.rsplit("/", 1)[-1]


def _commit(c, token, **extra):
    data = {"_csrf": c.tok, "do": "commit", **extra}
    r = c.post(f"/import/preview/{token}", data=data)
    assert r.status_code == 302, r.data[:500]
    loc = r.headers["Location"]
    assert "/import/jobs/" in loc, loc
    return int(loc.rsplit("/", 1)[-1])


def _job(app, job_id):
    from app.models import ImportJob
    with app.app_context():
        j = ImportJob.query.get(job_id)
        return {"created": j.created, "updated": j.updated, "skipped": j.skipped, "rows": j.rows, "errors": j.errors}


def _ref(app, entity, ext, source="clio"):
    from app.models import ExternalRef
    with app.app_context():
        r = ExternalRef.query.filter_by(source=source, entity=entity, external_id=ext).first()
        return r.coil_id if r else None


CONTACTS = ("Id,Name,Type,Company,Email Address (Work),Email Address (Home),Phone Number (Work),Phone Number (Mobile),Primary Address,Tags\r\n"
            "c101,Yolanda Quist,Person,,,yolanda@example.test,,512-555-0101,\"1 Elm St, Austin TX\",estate\r\n"
            "c102,Pemberton Freight LLC,Company,Pemberton Freight LLC,ap@pemberton.test,,512-555-0102,,9 Dock Rd,\r\n")


def test_hub_and_guide_render(client):
    r = client.get("/import")
    assert r.status_code == 200
    html = r.data.decode()
    assert "contacts, matters, time and expenses, bills, trust" in html and "PracticePanther" in html
    assert "Nothing imported yet" in html
    r = client.get("/import/guide")
    assert r.status_code == 200 and b"Full Data Backup" in r.data and b"Activities" in r.data
    assert b"/import/guide" in client.get("/settings").data


def test_contacts_preview_and_commit(app, client):
    from app.models import Contact
    token = _upload(client, "contacts", CONTACTS, "contacts.csv")
    html = client.get(f"/import/preview/{token}").data.decode()
    assert "create 2" in html.replace("<strong>", "").replace("</strong>", "")
    assert 'name="map_email"' in html and "Email Address (Work) + Email Address (Home)" in html
    assert "yolanda@example.test" in html  # first non-empty email column
    job = _commit(client, token)
    j = _job(app, job)
    assert j["created"] == 2 and j["updated"] == 0 and not j["errors"]
    with app.app_context():
        y = Contact.query.filter_by(email="yolanda@example.test").first()
        assert y and y.kind == "person" and y.first_name == "Yolanda" and y.last_name == "Quist"
        assert y.phone == "512-555-0101" and y.is_client and "estate" in y.tags
        p = Contact.query.filter_by(company_name="Pemberton Freight LLC").first()
        assert p and p.kind == "company" and p.email == "ap@pemberton.test" and p.phone == "512-555-0102"
        assert _ref(app, "contact", "c101") == y.id and _ref(app, "contact", "c102") == p.id


def test_reimport_contacts_updates_not_duplicates(app, client):
    from app.models import Contact
    changed = CONTACTS.replace("512-555-0101", "512-555-0199")
    with app.app_context():
        before = Contact.query.count()
    job = _commit(client, _upload(client, "contacts", changed, "contacts2.csv"))
    j = _job(app, job)
    assert j["updated"] == 2 and j["created"] == 0
    with app.app_context():
        assert Contact.query.count() == before
        assert Contact.query.filter_by(email="yolanda@example.test").first().phone == "512-555-0199"


MATTERS = ("Unique ID,Display Number,Custom Number,Description,Client ID,Client Name,Status,Practice Area,Responsible Attorney,Open Date,Close Date\r\n"
           "m201,00201-Quist,,Quist Trust,c101,Yolanda Quist,Open,Estate Planning,Demo Owner,03/01/2026,\r\n"
           "m202,00202-Pemberton,PF-7,Pemberton lease dispute,,Pemberton Freight LLC,Closed,Litigation,Nobody Here,01/15/2026,06/30/2026\r\n"
           "m203,00203-Ghost,,Ghost matter,,Nobody Known,Open,,,,\r\n")


def test_matters_link_by_client_id_and_name(app, client):
    from app.models import Matter, Contact
    token = _upload(client, "matters", MATTERS, "matters.csv")
    html = client.get(f"/import/preview/{token}").data.decode()
    assert "Nobody Known" in html and "not found" in html
    job = _commit(client, token)
    j = _job(app, job)
    assert j["created"] == 2 and len(j["errors"]) == 1 and "Nobody Known" in j["errors"][0]["message"]
    with app.app_context():
        y = Contact.query.filter_by(email="yolanda@example.test").first()
        m1 = Matter.query.filter_by(number="00201-Quist").first()
        assert m1 and m1.client_id == y.id and m1.status == "open" and m1.opened_on == date(2026, 3, 1)
        assert m1.responsible.email == "owner@example.com" and m1.practice_area == "Estate Planning"
        m2 = Matter.query.filter_by(number="PF-7").first()
        assert m2 and m2.client.company_name == "Pemberton Freight LLC" and m2.status == "closed"
        assert m2.closed_on == date(2026, 6, 30) and m2.responsible.email == "owner@example.com"
        assert _ref(app, "matter", "m201") == m1.id and _ref(app, "matter", "m202") == m2.id
    html = client.get(f"/import/jobs/{job}").data.decode()
    assert "Nobody Known" in html and "failed.csv" in html


ACTIVITIES = ("ID,Type,Date,User,Matter,Client,Activity Description,Description,Quantity,Rate,Total,Billable,Billed\r\n"
              "a301,TimeEntry,03/02/2026,Demo Owner,00201-Quist,Yolanda Quist,Drafting,Draft trust,1.50,300.00,450.00,Yes,No\r\n"
              "a302,ExpenseEntry,03/03/2026,Demo Owner,00201-Quist,Yolanda Quist,Filing fee,County filing,1,85.00,85.00,Yes,No\r\n"
              "a303,TimeEntry,02/01/2026,Demo Owner,PF-7,Pemberton Freight LLC,Review,Review lease,0.5,300.00,150.00,Yes,Yes\r\n")


def test_activities_time_expense_and_billed(app, client):
    from app.models import TimeEntry, Expense, Matter
    job = _commit(client, _upload(client, "activities", ACTIVITIES, "activities.csv"))
    j = _job(app, job)
    assert j["created"] == 3 and not j["errors"]
    with app.app_context():
        m1 = Matter.query.filter_by(number="00201-Quist").first()
        m2 = Matter.query.filter_by(number="PF-7").first()
        t = TimeEntry.query.get(_ref(app, "time", "a301"))
        assert t.matter_id == m1.id and t.minutes == 90 and t.rate_cents == 30000 and t.billable and t.invoice_id is None
        assert t.description == "Drafting: Draft trust" and t.user.email == "owner@example.com"
        e = Expense.query.get(_ref(app, "expense", "a302"))
        assert e.matter_id == m1.id and e.amount_cents == 8500 and e.category == "Filing fee" and e.billable
        b = TimeEntry.query.get(_ref(app, "time", "a303"))
        assert b.matter_id == m2.id and b.billable is False and b.invoice_id is None
        assert b.description.startswith("[billed in previous system]")
    # second pass updates, no duplicates
    job = _commit(client, _upload(client, "activities", ACTIVITIES, "activities.csv"))
    j = _job(app, job)
    assert j["updated"] == 3 and j["created"] == 0


BILLS = ("ID,Bill #,Client,Matter,Issued,Due,Total,Paid,Balance,State\r\n"
         "b401,7001,Yolanda Quist,00201-Quist,03/10/2026,04/09/2026,\"1,200.00\",\"1,200.00\",0.00,Paid\r\n"
         "b402,7002,Pemberton Freight LLC,PF-7,03/12/2026,04/11/2026,500.00,100.00,400.00,Unpaid\r\n")


def test_bills_balance_and_payment(app, client):
    from app.models import Invoice, Payment
    job = _commit(client, _upload(client, "bills", BILLS, "bills.csv"))
    j = _job(app, job)
    assert j["created"] == 2 and not j["errors"], j
    with app.app_context():
        paid = Invoice.query.get(_ref(app, "invoice", "b401"))
        assert paid.number == "7001" and paid.total_cents == 120000 and paid.paid_cents == 120000
        assert paid.status == "paid" and paid.balance_cents == 0 and paid.issued_on == date(2026, 3, 10)
        assert len(paid.lines) == 1 and paid.lines[0].description == "Imported balance from Clio bill 7001"
        assert len(paid.payments) == 1 and paid.payments[0].amount_cents == 120000 and paid.payments[0].received_on == date(2026, 3, 10)
        open_inv = Invoice.query.get(_ref(app, "invoice", "b402"))
        assert open_inv.total_cents == 50000 and open_inv.paid_cents == 10000 and open_inv.balance_cents == 40000
        assert open_inv.status == "partial" and open_inv.due_on == date(2026, 4, 11)
        assert open_inv.matter.number == "PF-7" and open_inv.client.company_name == "Pemberton Freight LLC"
    # re-import keeps one invoice and one payment each
    _commit(client, _upload(client, "bills", BILLS, "bills.csv"))
    with app.app_context():
        assert Invoice.query.filter(Invoice.number.in_(["7001", "7002"])).count() == 2
        assert Payment.query.filter_by(invoice_id=_ref(app, "invoice", "b401")).count() == 1


TRUST = ("ID,Date,Type,Source/Destination,Client,Matter,Description,Check or reference no.,Funds In,Funds Out,Cleared\r\n"
         "t502,03/15/2026,Disbursement,County Clerk,Yolanda Quist,00201-Quist,Filing fee,2001,,85.00,No\r\n"
         "t501,03/01/2026,Deposit,Yolanda Quist,Yolanda Quist,00201-Quist,Retainer,1001,\"2,000.00\",,Yes\r\n"
         "t503,03/20/2026,Disbursement,Vendor,Pemberton Freight LLC,PF-7,Expert fee,2002,,300.00,No\r\n")


def test_trust_date_order_refuses_negative_and_failed_csv_round_trip(app, client):
    from app.models import Contact, TrustTransaction, Matter
    job = _commit(client, _upload(client, "trust", TRUST, "trust.csv"))
    j = _job(app, job)
    assert j["created"] == 2 and len(j["errors"]) == 1
    assert "Refused" in j["errors"][0]["message"] and "Pemberton" in j["errors"][0]["message"]
    with app.app_context():
        y = Contact.query.filter_by(email="yolanda@example.test").first()
        p = Contact.query.filter_by(company_name="Pemberton Freight LLC").first()
        assert y.trust_balance_cents() == 191500 and p.trust_balance_cents() == 0
        dep = TrustTransaction.query.get(_ref(app, "trust", "t501"))
        assert dep.type == "deposit" and dep.amount_cents == 200000 and dep.cleared and dep.reference == "1001"
        dis = TrustTransaction.query.get(_ref(app, "trust", "t502"))
        assert dis.type == "disbursement" and dis.amount_cents == -8500 and dis.payee == "County Clerk"
        assert _ref(app, "trust", "t503") is None
        pid, mid = p.id, Matter.query.filter_by(number="PF-7").first().id
    # failed rows CSV keeps the original columns plus the reason
    r = client.get(f"/import/jobs/{job}/failed.csv")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    failed = r.data.decode()
    lines = failed.strip().splitlines()
    assert lines[0].startswith("ID,Date,Type") and lines[0].endswith("Import error") and len(lines) == 2 and "t503" in lines[1]
    # opening balance, then re-upload just the failed rows
    r = client.post("/import/trust/opening", data={"_csrf": client.tok, "client_id": str(pid), "matter_id": str(mid),
                                                   "amount": "500.00", "date": "2026-03-01"})
    assert r.status_code == 302
    job2 = _commit(client, _upload(client, "trust", failed, "failed.csv"))
    j2 = _job(app, job2)
    assert j2["created"] == 1 and not j2["errors"], j2
    with app.app_context():
        p = Contact.query.filter_by(company_name="Pemberton Freight LLC").first()
        assert p.trust_balance_cents() == 20000
        assert TrustTransaction.query.get(_ref(app, "trust", "t503")).amount_cents == -30000


def test_tasks_calendar_notes(app, client):
    from app.models import Task, CalendarEvent, Note, Matter
    tasks = ("ID,Title,Matter,Due Date,Assigned To,Completed,Notes,Priority\r\n"
             "k601,Send draft trust,00201-Quist,03/20/2026,Demo Owner,No,Client wants by end of month,High\r\n"
             "k602,Close file,PF-7,07/01/2026,owner@example.com,Yes,,\r\n")
    j = _job(app, _commit(client, _upload(client, "tasks", tasks, "tasks.csv")))
    assert j["created"] == 2 and not j["errors"]
    cal = ("ID,Title,Start,End,All Day,Matter,Location,Description\r\n"
           "e701,Signing meeting,03/25/2026 10:00 AM,03/25/2026 11:00 AM,No,00201-Quist,Office,Bring ids\r\n"
           "e702,Lease deadline,2026-04-01,,Yes,PF-7,,\r\n")
    j = _job(app, _commit(client, _upload(client, "calendar", cal, "calendar.csv")))
    assert j["created"] == 2 and not j["errors"]
    notes = ("ID,Matter,Date,Note,Author\r\n"
             "n801,00201-Quist,03/04/2026 09:15,Called client about trustee choice.,Demo Owner\r\n")
    j = _job(app, _commit(client, _upload(client, "notes", notes, "notes.csv")))
    assert j["created"] == 1 and not j["errors"]
    with app.app_context():
        m1 = Matter.query.filter_by(number="00201-Quist").first()
        t = Task.query.get(_ref(app, "task", "k601"))
        assert t.matter_id == m1.id and t.due_on == date(2026, 3, 20) and t.assignee.email == "owner@example.com"
        assert not t.done and t.priority == "high" and "end of month" in t.notes
        t2 = Task.query.get(_ref(app, "task", "k602"))
        assert t2.done and t2.done_at and t2.matter.number == "PF-7"
        e = CalendarEvent.query.get(_ref(app, "event", "e701"))
        assert e.matter_id == m1.id and e.starts_at.hour == 10 and e.ends_at.hour == 11 and not e.all_day and e.location == "Office"
        e2 = CalendarEvent.query.get(_ref(app, "event", "e702"))
        assert e2.all_day and e2.starts_at.date() == date(2026, 4, 1) and e2.matter.number == "PF-7"
        n = Note.query.get(_ref(app, "note", "n801"))
        assert n.matter_id == m1.id and n.created_at.date() == date(2026, 3, 4) and n.user.email == "owner@example.com"


def test_documents_zip_by_matter_folder(app, client):
    from app.models import Document, Matter
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("00201-Quist/trust-draft.txt", "Draft trust for Yolanda Quist")
        z.writestr("00201-Quist/bad.exe", "MZ")
        z.writestr("PF-7/lease/lease.txt", "Lease between Pemberton and landlord")
        z.writestr("Unknown Folder/x.txt", "nothing")
        z.writestr("__MACOSX/00201-Quist/._trust-draft.txt", "junk")
    token = _upload(client, "documents", buf.getvalue(), "docs.zip")
    html = client.get(f"/import/preview/{token}").data.decode()
    assert "Unknown Folder" in html and "no match" in html and "bad.exe" in html
    job = _commit(client, token)
    j = _job(app, job)
    assert j["created"] == 2 and len(j["errors"]) == 1 and "Unknown Folder" in j["errors"][0]["message"]
    with app.app_context():
        m1 = Matter.query.filter_by(number="00201-Quist").first()
        m2 = Matter.query.filter_by(number="PF-7").first()
        d1 = Document.query.filter_by(matter_id=m1.id, name="trust-draft.txt").first()
        assert d1 and d1.folder == "Imported" and "Yolanda" in d1.extracted_text
        d2 = Document.query.filter_by(matter_id=m2.id, name="lease.txt").first()
        assert d2 and d2.folder == "Imported/lease"
        assert Document.query.filter_by(name="bad.exe").count() == 0
    # second upload of the same ZIP skips what is already there
    j = _job(app, _commit(client, _upload(client, "documents", buf.getvalue(), "docs.zip")))
    assert j["created"] == 0 and j["skipped"] >= 2


def test_generic_csv_mapped_by_hand(app, client):
    from app.models import Contact
    body = "Person,Mail,Tel\r\nOttoline Farquhar,ottoline@example.test,512-555-0177\r\n"
    token = _upload(client, "contacts", body, "weird.csv", source="generic")
    html = client.get(f"/import/preview/{token}").data.decode()
    assert "Not imported: Person, Mail, Tel" in html
    # re-check with a hand mapping, then commit with it
    r = client.post(f"/import/preview/{token}", data={"_csrf": client.tok, "do": "recheck", "map_name": "Person",
                                                      "map_email": "Mail", "map_phone": "Tel", "duplicates": "update",
                                                      "mark_clients": "1"})
    assert r.status_code == 200
    html = r.data.decode()
    assert "ottoline@example.test" in html and 'value="Person" selected' in html
    job = _commit(client, token, map_name="Person", map_email="Mail", map_phone="Tel", duplicates="update")
    assert _job(app, job)["created"] == 1
    with app.app_context():
        c = Contact.query.filter_by(email="ottoline@example.test").first()
        assert c and c.first_name == "Ottoline" and c.last_name == "Farquhar" and c.phone == "512-555-0177"
    html = client.get("/import").data.decode()
    assert "weird.csv" in html and "Generic CSV" in html


def test_bad_uploads_are_refused(client):
    r = client.post("/import/contacts/upload", data={"_csrf": client.tok, "source": "clio"}, content_type="multipart/form-data")
    assert r.status_code == 302
    r = client.post("/import/documents/upload", data={"_csrf": client.tok, "source": "clio",
                                                      "file": (io.BytesIO(b"not a zip"), "x.zip")}, content_type="multipart/form-data")
    assert r.status_code == 302 and "/import/preview/" not in r.headers["Location"]
    assert client.get("/import/preview/deadbeef").status_code == 302
    assert client.get("/import/nothing/upload").status_code in (404, 405)
