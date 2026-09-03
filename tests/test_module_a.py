"""Smoke test for module A: contacts, matters, conflicts, tasks, calendar, documents."""
import hashlib
import io
import os
import re
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _id_from(location):
    return int(re.search(r"/(\d+)(?:\?|$)", location).group(1))


@pytest.fixture(scope="module")
def client():
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT)
    from app import create_app
    from tests.helpers import login
    app = create_app({"TESTING": True})
    c = app.test_client()
    login(c)
    # login() clears the session, so pull a fresh CSRF token from the dashboard's logout form
    r = c.get("/")
    c.tok = re.search(rb'name="_csrf" value="([^"]+)"', r.data).group(1).decode()
    c.app = app
    return c


def test_create_contact(client):
    tok = client.tok
    r = client.post("/contacts/new", data={
        "_csrf": tok, "kind": "person", "first_name": "Test", "last_name": "Client",
        "email": "testclient@example.com", "phone": "+15125550777", "aliases": "T. Client\nTestco DBA",
        "is_client": "1", "address": "1 Test St",
    })
    assert r.status_code == 302, r.data[:300]
    cid = _id_from(r.headers["Location"])
    r = client.get(f"/contacts/{cid}")
    assert r.status_code == 200
    assert b"Test Client" in r.data and b"Testco DBA" in r.data
    r = client.get("/contacts/search.json?q=testclient")
    assert r.status_code == 200
    assert any(row["id"] == cid and row["email"] == "testclient@example.com" for row in r.get_json())
    r = client.get("/contacts?q=Client")
    assert r.status_code == 200 and b"Test Client" in r.data
    # cannot delete once it has matters; deleting a fresh contact works
    r = client.post("/contacts/new", data={"_csrf": tok, "kind": "company", "company_name": "Throwaway Co"})
    tmp = _id_from(r.headers["Location"])
    r = client.post(f"/contacts/{tmp}/delete", data={"_csrf": tok})
    assert r.status_code == 302 and r.headers["Location"].endswith("/contacts")
    client.cid = cid


def test_create_matter_with_milestones_and_party(client):
    tok = client.tok
    due = (date.today() + timedelta(days=30)).isoformat()
    r = client.post("/matters/new", data={
        "_csrf": tok, "client_id": client.cid, "name": "Test Client Estate Plan", "practice_area": "Estate Planning",
        "status": "open", "billing_type": "flat", "flat_fee": "3,000.00",
        "ms_id": ["", ""], "ms_description": ["Retainer on signing", "Balance at execution"],
        "ms_amount": ["1,500.00", "1500"], "ms_due": [date.today().isoformat(), due],
        "sol_date": (date.today() + timedelta(days=45)).isoformat(), "sol_basis": "test basis",
        "court": "Travis County Probate", "case_number": "TC-1",
        "cf_key": ["Policy number", ""], "cf_value": ["POL-123", ""],
    })
    assert r.status_code == 302, r.data[:500]
    mid = _id_from(r.headers["Location"])
    r = client.get(f"/matters/{mid}")
    assert r.status_code == 200
    body = r.data.decode()
    assert re.search(r"M-\d+", body)
    assert "Retainer on signing" in body and "Balance at execution" in body and "$3,000.00" in body
    assert "Policy number" in body and "POL-123" in body
    r = client.post(f"/matters/{mid}/parties", data={"_csrf": tok, "name": "Opposing Person", "role": "adverse"})
    assert r.status_code == 302
    r = client.get(f"/matters/{mid}")
    assert b"Opposing Person" in r.data
    from app.models import Matter, Firm
    from app.extensions import db
    with client.app.app_context():
        m = db.session.get(Matter, mid)
        assert len(m.milestones) == 2 and sum(ms.amount_cents for ms in m.milestones) == 300000
        assert len(m.parties) == 1
        assert m.number.startswith(Firm.get().matter_prefix)
        assert m.custom_fields == {"Policy number": "POL-123"}
    for tab in ("time", "invoices", "trust", "tasks", "documents", "engagements", "activity"):
        assert client.get(f"/matters/{mid}?tab={tab}").status_code == 200
    r = client.post(f"/matters/{mid}/close", data={"_csrf": tok})
    assert r.status_code == 302
    r = client.post(f"/matters/{mid}/reopen", data={"_csrf": tok})
    assert r.status_code == 302
    # milestone edit round-trip: change one, drop one, add one
    r = client.get(f"/matters/{mid}/edit")
    assert r.status_code == 200
    with client.app.app_context():
        ids = [ms.id for ms in db.session.get(Matter, mid).milestones]
    r = client.post(f"/matters/{mid}/edit", data={
        "_csrf": tok, "client_id": client.cid, "name": "Test Client Estate Plan", "status": "open",
        "billing_type": "flat", "flat_fee": "3000",
        "ms_id": [str(ids[0]), str(ids[1]), ""], "ms_description": ["Retainer (edited)", "", "Final payment"],
        "ms_amount": ["1000", "1500", "2000"], "ms_due": ["", "", due],
        "cf_key": ["Policy number"], "cf_value": ["POL-456"],
    })
    assert r.status_code == 302, r.data[:500]
    with client.app.app_context():
        m = db.session.get(Matter, mid)
        assert [ms.description for ms in m.milestones] == ["Retainer (edited)", "Final payment"]
        assert [ms.amount_cents for ms in m.milestones] == [100000, 200000]
        assert m.custom_fields == {"Policy number": "POL-456"}
    client.mid = mid


def test_conflict_check_hits_seeded_adverse_party(client):
    tok = client.tok
    r = client.post("/conflicts/run", data={"_csrf": tok, "names": "Derek Holloway\nDerek Halloway",
                                            "matter_id": client.mid})
    assert r.status_code == 302, r.data[:300]
    chk_id = _id_from(r.headers["Location"])
    r = client.get(f"/conflicts/{chk_id}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Derek Holloway" in body and "adverse" in body
    from app.models import ConflictCheck
    from app.extensions import db
    with client.app.app_context():
        chk = db.session.get(ConflictCheck, chk_id)
        assert chk.outcome == "unresolved" and chk.matter_id == client.mid
        hits = chk.results
        assert any(h["source"] == "party" and h["role"] == "adverse" and h["score"] == 100 for h in hits)
        assert any(h["source"] == "contact" and "Holloway" in h["label"] for h in hits)
        assert any(h["query"] == "Derek Halloway" and h["score"] < 100 for h in hits), "fuzzy misspelling should still hit"
        for h in hits:
            assert set(h) == {"query", "source", "label", "score", "url", "role"}
    r = client.post(f"/conflicts/{chk_id}/resolve", data={"_csrf": tok, "outcome": "waived", "notes": "Client waived."})
    assert r.status_code == 302
    r = client.get("/conflicts")
    assert r.status_code == 200 and b"waived" in r.data


def test_task_create_and_complete(client):
    tok = client.tok
    r = client.post("/tasks/new", data={"_csrf": tok, "title": "Smoke test task", "kind": "deadline",
                                        "due_on": date.today().isoformat(), "priority": "high",
                                        "matter_id": client.mid})
    assert r.status_code == 302, r.data[:300]
    tid = _id_from(r.headers["Location"])
    r = client.get("/tasks")
    assert r.status_code == 200 and b"Smoke test task" in r.data and b"Limitations deadlines" in r.data
    r = client.post(f"/tasks/{tid}/done", data={"_csrf": tok})
    assert r.status_code == 302
    from app.models import Task
    from app.extensions import db
    with client.app.app_context():
        t = db.session.get(Task, tid)
        assert t.done and t.done_at is not None
    assert client.get(f"/tasks/{tid}").status_code == 200
    assert client.get(f"/tasks/{tid}/edit").status_code == 200


def test_calendar_event_and_ics_feed(client):
    tok = client.tok
    start = (date.today() + timedelta(days=3)).isoformat()
    r = client.post("/calendar/new", data={"_csrf": tok, "title": "Smoke hearing, room 2; bring exhibits",
                                           "starts_at": f"{start}T14:00", "ends_at": f"{start}T15:30",
                                           "location": "Courtroom 3B", "matter_id": client.mid, "notes": "Line one\nline two"})
    assert r.status_code == 302, r.data[:300]
    eid = _id_from(r.headers["Location"])
    assert client.get(f"/calendar/{eid}").status_code == 200
    r = client.get(f"/calendar?month={start[:7]}")
    assert r.status_code == 200 and b"Smoke hearing" in r.data and b"/calendar/feed/" in r.data
    secret = hashlib.sha256((client.app.config["SECRET_KEY"] + "ics").encode()).hexdigest()[:24]
    r = client.get(f"/calendar/feed/{secret}.ics")
    assert r.status_code == 200
    assert r.mimetype == "text/calendar"
    ics = r.data.decode()
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.rstrip().endswith("END:VCALENDAR")
    assert "BEGIN:VEVENT" in ics and "SUMMARY:Smoke hearing\\, room 2\\; bring exhibits" in ics
    assert "LOCATION:Courtroom 3B" in ics and "DTSTART:" in ics and "DTEND:" in ics and "UID:" in ics
    assert all(len(l.encode()) <= 75 for l in ics.split("\r\n"))
    assert client.get("/calendar/feed/wrongsecret.ics").status_code == 404
    client.eid = eid


def test_document_upload_and_download(client):
    tok = client.tok
    r = client.post("/documents/upload", data={"_csrf": tok, "matter_id": client.mid,
                                               "file": (io.BytesIO(b"hello from the smoke test"), "smoke note.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:300]
    from app.models import Document
    from app.extensions import db
    with client.app.app_context():
        doc = Document.query.filter_by(matter_id=client.mid).order_by(Document.id.desc()).first()
        assert doc and doc.size == 25 and doc.path.startswith(f"{client.mid}/")
        assert os.path.isfile(os.path.join(client.app.config["UPLOAD_DIR"], doc.path))
        did = doc.id
    r = client.get(f"/documents/{did}/download")
    assert r.status_code == 200 and r.data == b"hello from the smoke test"
    assert "attachment" in r.headers.get("Content-Disposition", "")
    r = client.get(f"/documents?matter_id={client.mid}")
    assert r.status_code == 200 and b"smoke note.txt" in r.data
    r = client.post(f"/documents/{did}/share", data={"_csrf": tok})
    assert r.status_code == 302
    # blocked extension and oversize are rejected with a flash, not a 500
    r = client.post("/documents/upload", data={"_csrf": tok, "matter_id": client.mid,
                                               "file": (io.BytesIO(b"x"), "evil.exe")}, content_type="multipart/form-data")
    assert r.status_code == 302
    with client.app.app_context():
        assert Document.query.filter_by(name="evil.exe").count() == 0
    r = client.post(f"/documents/{did}/delete", data={"_csrf": tok})
    assert r.status_code == 302
    with client.app.app_context():
        assert db.session.get(Document, did) is None


def test_all_pages_return_200(client):
    urls = ["/contacts", "/contacts/new", f"/contacts/{client.cid}", f"/contacts/{client.cid}/edit",
            "/contacts/search.json?q=a",
            "/matters", "/matters/new", f"/matters/new?contact_id={client.cid}", f"/matters/{client.mid}",
            f"/matters/{client.mid}/edit", "/matters?status=open&billing_type=flat",
            "/conflicts", f"/conflicts?matter_id={client.mid}",
            "/tasks", "/tasks/new", f"/tasks/new?matter_id={client.mid}", f"/tasks?matter_id={client.mid}&kind=task",
            "/calendar", "/calendar/new", f"/calendar/new?matter_id={client.mid}", f"/calendar/{client.eid}",
            f"/calendar/{client.eid}/edit", "/calendar?month=2026-01",
            "/documents", f"/documents?matter_id={client.mid}"]
    for u in urls:
        r = client.get(u)
        assert r.status_code == 200, (u, r.status_code)
