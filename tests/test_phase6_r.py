"""Phase 6 Agent R: phone intake API, time capture suggestions, criminal defense module. Own SQLite DB via seed.py."""
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
    tmp = tmp_path_factory.mktemp("p6r")
    dbfile = tmp / "test.db"
    uri = f"sqlite:///{dbfile}"
    env = dict(os.environ, DATABASE_URL=uri, STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="", SMTP_HOST="")
    subprocess.run([sys.executable, "seed.py"], cwd=ROOT, env=env, check=True)
    from app import create_app
    application = create_app({"SQLALCHEMY_DATABASE_URI": uri, "TESTING": True, "STRIPE_SECRET_KEY": "",
                              "STRIPE_WEBHOOK_SECRET": "", "SMTP_HOST": "", "UPLOAD_DIR": str(tmp / "uploads"),
                              "PDF_DIR": str(tmp / "pdf"), "API_RATE_LIMIT": 500})
    return application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    login(c)
    return c


def csrf(client):
    client.get("/time/suggestions")
    with client.session_transaction() as s:
        return s["_csrf"]


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


def _matter_id(app, number):
    from app.models import Matter
    with app.app_context():
        return Matter.query.filter_by(number=number).first().id


# ---------------------------------------------------------------- 1. phone intake API
def test_lead_create_scores_and_is_idempotent(app):
    from app.models import IntakeLead
    rw = _make_token(app, "read,write")
    ro = _make_token(app, "read")
    c = app.test_client()
    body = {"name": "Jordan Reyes", "phone": "+15125550199", "matter_type": "Criminal defense",
            "description": "Arrested last night in Travis County, still in custody.", "adverse_party": "State of Texas",
            "call_summary": "Wife called at 2am; husband arrested for DWI.",
            "transcript": [{"role": "caller", "text": "My husband just got arrested."},
                           {"role": "agent", "text": "I can help with that."}],
            "external_id": "HD-20260905-0001"}
    r = c.post("/api/v1/leads", json=body, headers=_h(ro))
    assert r.status_code == 403
    r = c.post("/api/v1/leads", json=body, headers=_h(rw))
    assert r.status_code == 201, r.json
    lid = r.json["id"]
    assert r.json["created"] is True and r.json["url"].endswith(f"/intake/{lid}")
    assert r.json["score"] is not None and r.json["score"] > 0
    with app.app_context():
        l = db_get(IntakeLead, lid)
        assert l.source == "phone" and l.matter_type == "Criminal defense" and l.phone == "+15125550199"
        assert "Call summary:" in l.description and "caller: My husband just got arrested." in l.description
        assert l.description.rstrip().endswith("[ref: HD-20260905-0001]")
        assert l.score == r.json["score"] and '"factors"' in l.score_json
    # retry with the same external_id returns the same lead, no duplicate
    r2 = c.post("/api/v1/leads", json=body, headers=_h(rw))
    assert r2.status_code == 200 and r2.json["id"] == lid and r2.json["created"] is False
    with app.app_context():
        assert IntakeLead.query.filter_by(name="Jordan Reyes").count() == 1
    # missing name is a 400; form-encoded works too; default source is phone
    r = c.post("/api/v1/leads", json={"phone": "555"}, headers=_h(rw))
    assert r.status_code == 400
    r = c.post("/api/v1/leads", data={"name": "Form Caller", "email": "f@example.com"}, headers=_h(rw))
    assert r.status_code == 201 and r.json["source"] == "phone"


# ---------------------------------------------------------------- 2. time capture
def test_capture_segments_guess_merge_and_ignore(app):
    from app.models import TimeSuggestion
    rw = _make_token(app, "read,write")
    c = app.test_client()
    m2 = _matter_id(app, "M-1002")
    base = datetime(2026, 9, 4, 14, 0, 0)
    segs = [
        {"started_at": base.isoformat() + "Z", "minutes": 1, "title": "Quick glance", "url": "https://example.com/a"},
        {"started_at": base.isoformat(), "minutes": 12, "title": "Contract research", "url": "https://law.example/x"},
        {"started_at": (base + timedelta(minutes=20)).isoformat(), "minutes": 8, "title": "Contract research",
         "url": "https://law.example/x"},
        {"started_at": (base + timedelta(hours=2)).isoformat(), "minutes": 30,
         "title": "M-1002 Holloway deposition outline - Google Docs", "url": "https://docs.google.com/d/1"},
    ]
    r = c.post("/api/v1/capture", json=segs, headers=_h(rw))
    assert r.status_code == 201, r.json
    assert r.json["created"] == 2 and r.json["merged"] == 1 and r.json["ignored"] == 1 and r.json["pending"] == 2
    with app.app_context():
        rows = TimeSuggestion.query.filter_by(status="pending").order_by(TimeSuggestion.started_at).all()
        assert len(rows) == 2
        merged, numbered = rows
        assert merged.title == "Contract research" and merged.minutes == 20 and merged.started_at == base
        assert numbered.matter_id == m2 and numbered.minutes == 30
        # client-name guess: Bluebonnet is M-1002's client
    r = c.post("/api/v1/capture", json={"segments": [
        {"started_at": (base + timedelta(days=1)).isoformat(), "minutes": 5,
         "title": "Re: Bluebonnet Logistics invoice question", "url": "https://mail.example/1"}]}, headers=_h(rw))
    assert r.status_code == 201 and r.json["created"] == 1
    with app.app_context():
        s = TimeSuggestion.query.filter_by(title="Re: Bluebonnet Logistics invoice question").one()
        assert s.matter_id == m2
    r = c.post("/api/v1/capture", json={"nope": 1}, headers=_h(rw))
    assert r.status_code == 400
    r = c.get("/api/v1/capture/pending", headers=_h(rw))
    assert r.status_code == 200 and r.json["pending"] == 3 and r.json["minutes"] == 55
    assert r.json["url"].endswith("/time/suggestions")


def test_suggestions_page_accept_dismiss(app, client):
    from app.models import TimeSuggestion, TimeEntry
    tok = csrf(client)
    m1 = _matter_id(app, "M-1001")
    m2 = _matter_id(app, "M-1002")
    r = client.get("/time/suggestions")
    assert r.status_code == 200
    assert b"Contract research" in r.data and b"Holloway deposition outline" in r.data and b"Accept all" in r.data
    with app.app_context():
        merged = TimeSuggestion.query.filter_by(title="Contract research").one()
        numbered = TimeSuggestion.query.filter(TimeSuggestion.title.like("M-1002%")).one()
        mail = TimeSuggestion.query.filter_by(title="Re: Bluebonnet Logistics invoice question").one()
        merged_id, numbered_id, mail_id = merged.id, numbered.id, mail.id
    # accept: 20 minutes rounds up to 24, description editable, matter from the select
    r = client.post(f"/time/suggestions/{merged_id}/accept",
                    data={"_csrf": tok, "matter_id": m1, "description": "Research on the contract question"})
    assert r.status_code == 302
    with app.app_context():
        s = db_get(TimeSuggestion, merged_id)
        assert s.status == "accepted" and s.time_entry_id and s.matter_id == m1
        e = db_get(TimeEntry, s.time_entry_id)
        assert e.minutes == 24 and e.matter_id == m1 and e.description == "Research on the contract question"
        assert e.rate_cents == 35000 and e.date == date(2026, 9, 4)
    # accept without a matter refuses
    r = client.post(f"/time/suggestions/{mail_id}/accept", data={"_csrf": tok, "matter_id": ""}, follow_redirects=True)
    assert b"Pick a matter" in r.data
    # dismiss
    r = client.post(f"/time/suggestions/{mail_id}/dismiss", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        assert db_get(TimeSuggestion, mail_id).status == "dismissed"
    # accept all with a matter: the M-1002 one (30 -> 30, already a 6 multiple)
    r = client.post("/time/suggestions/accept-all", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        s = db_get(TimeSuggestion, numbered_id)
        assert s.status == "accepted" and db_get(TimeEntry, s.time_entry_id).minutes == 30
        assert db_get(TimeEntry, s.time_entry_id).matter_id == m2
        assert TimeSuggestion.query.filter_by(status="pending").count() == 0
    r = client.get("/time/suggestions")
    assert r.status_code == 200 and b"Nothing pending" in r.data and b"Recently handled" in r.data


def db_get(model, id):
    from app.extensions import db
    return db.session.get(model, id)


def test_agenda_mentions_pending_suggestions(app):
    from app.extensions import db
    from app.models import TimeSuggestion, User
    from app.cli import build_agenda
    with app.app_context():
        u = User.query.filter_by(email="owner@example.com").first()
        db.session.add(TimeSuggestion(user_id=u.id, started_at=datetime.utcnow(), minutes=15, title="Agenda check"))
        db.session.commit()
        sections = dict(build_agenda(u))
        items = sections["Time capture suggestions"]
        assert len(items) == 1 and "1 pending suggestion" in items[0] and "/time/suggestions" in items[0]
        TimeSuggestion.query.filter_by(title="Agenda check").delete()
        db.session.commit()
        assert dict(build_agenda(u))["Time capture suggestions"] == []


# ---------------------------------------------------------------- 3. criminal defense
def test_criminal_start_facts_and_board(app, client):
    from app.models import CriminalCase, Matter
    tok = csrf(client)
    m2 = _matter_id(app, "M-1002")
    r = client.get("/criminal")
    assert r.status_code == 200 and b"No criminal cases yet" in r.data
    r = client.post("/criminal/start", data={"_csrf": tok, "matter_id": m2})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/criminal/{m2}")
    with app.app_context():
        c = CriminalCase.query.filter_by(matter_id=m2).one()
        assert c.stage == "arrest"
    arrest = date.today() - timedelta(days=10)
    setting = date.today() + timedelta(days=21)
    r = client.post(f"/criminal/{m2}/facts", data={
        "_csrf": tok, "court": "County Court at Law No. 2", "cause_number": "C-2026-1234", "arrest_on": arrest.isoformat(),
        "bond": "5,000.00", "bond_status": "posted", "custody_status": "in", "prosecutor": "ADA Lee",
        "prosecutor_email": "lee@da.example", "judge": "Judge Park", "next_setting_on": setting.isoformat(),
        "next_setting_type": "arraignment", "discovery_received_on": "", "plea_offer": "", "stage": "charged",
        "notes": "Client says the stop was pretextual."})
    assert r.status_code == 302
    with app.app_context():
        c = CriminalCase.query.filter_by(matter_id=m2).one()
        assert c.bond_cents == 500000 and c.custody_status == "in" and c.stage == "charged"
        assert c.next_setting_on == setting and c.next_setting_type == "arraignment" and c.arrest_on == arrest
        m = db_get(Matter, m2)
        assert m.court == "County Court at Law No. 2" and m.case_number == "C-2026-1234"
    r = client.get(f"/criminal/{m2}")
    assert r.status_code == 200
    assert b"in custody" in r.data and b"charged stage" in r.data and b"Coil ships no statute tables" in r.data
    assert b"Next court setting: Arraignment" in r.data
    r = client.get("/criminal")
    assert r.status_code == 200 and b"in custody" in r.data and b"M-1002" in r.data
    assert b"Every open matter already has a criminal case" not in r.data  # M-1001 is still available
    # matter page carries the button; GET on a matter without a case starts one
    r = client.get(f"/matters/{m2}")
    assert b'href="/criminal/' in r.data
    m1 = _matter_id(app, "M-1001")
    r = client.get(f"/criminal/{m1}")
    assert r.status_code == 200
    with app.app_context():
        assert CriminalCase.query.filter_by(matter_id=m1).count() == 1


def test_charges_crud(app, client):
    from app.models import Charge
    tok = csrf(client)
    m2 = _matter_id(app, "M-1002")
    r = client.get(f"/criminal/{m2}/charges/new")
    assert r.status_code == 200 and b"entered by the attorney" in r.data
    r = client.post(f"/criminal/{m2}/charges/new", data={
        "_csrf": tok, "statute": "PC 49.04", "description": "Driving while intoxicated", "degree": "Class B misdemeanor",
        "range_text": "72 hours to 180 days", "fine_max": "2000", "enhancement": "", "disposition": "pending",
        "disposition_on": "", "sentence": ""})
    assert r.status_code == 302
    r = client.post(f"/criminal/{m2}/charges/new", data={"_csrf": tok, "description": ""})
    assert r.status_code == 400
    with app.app_context():
        ch = Charge.query.filter_by(matter_id=m2).one()
        assert ch.fine_max_cents == 200000 and ch.range_text == "72 hours to 180 days"
        cid = ch.id
    r = client.post(f"/criminal/{m2}/charges/{cid}/edit", data={
        "_csrf": tok, "statute": "PC 49.04", "description": "Driving while intoxicated", "degree": "Class B misdemeanor",
        "range_text": "72 hours to 180 days", "fine_max": "2000", "enhancement": "", "disposition": "plea",
        "disposition_on": date.today().isoformat(), "sentence": "12 months probation, DWI class"})
    assert r.status_code == 302
    with app.app_context():
        ch = db_get(Charge, cid)
        assert ch.disposition == "plea" and ch.sentence.startswith("12 months") and ch.disposition_on == date.today()
    r = client.get(f"/criminal/{m2}")
    assert b"PC 49.04" in r.data and b"12 months probation" in r.data
    # add a second one, then delete it
    r = client.post(f"/criminal/{m2}/charges/new", data={"_csrf": tok, "description": "Open container", "disposition": "pending"})
    assert r.status_code == 302
    with app.app_context():
        other = Charge.query.filter_by(matter_id=m2, description="Open container").one().id
    r = client.post(f"/criminal/{m2}/charges/{other}/delete", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        assert Charge.query.filter_by(matter_id=m2).count() == 1


def test_court_date_chain_and_speedy_trial(app, client):
    from app.models import Task, CriminalCase
    tok = csrf(client)
    m2 = _matter_id(app, "M-1002")
    with app.app_context():
        before = Task.query.filter_by(matter_id=m2).count()
        c = CriminalCase.query.filter_by(matter_id=m2).one()
        setting, arrest = c.next_setting_on, c.arrest_on
    r = client.post(f"/criminal/{m2}/court-chain", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        tasks = Task.query.filter(Task.matter_id == m2, Task.id > 0).order_by(Task.id).all()
        new = tasks[before:]
        assert len(new) == 4
        by_title = {t.title: t for t in new}
        court = by_title["Arraignment (County Court at Law No. 2)"]
        assert court.kind == "court_date" and court.due_on == setting and court.priority == "high"
        assert by_title["Prepare for arraignment"].due_on == setting - timedelta(days=7)
        assert by_title["Confirm client appearance"].due_on == setting - timedelta(days=2)
        disc = by_title["Request discovery"]
        assert disc.due_on == setting + timedelta(days=14) and "ADA Lee" in disc.notes
        assert all(t.assignee_id for t in new)
    # second click: nothing new
    r = client.post(f"/criminal/{m2}/court-chain", data={"_csrf": tok}, follow_redirects=True)
    assert b"already exist" in r.data
    with app.app_context():
        assert Task.query.filter_by(matter_id=m2).count() == before + 4
    # speedy trial deadline at arrest + 180, once
    r = client.post(f"/criminal/{m2}/speedy-trial", data={"_csrf": tok})
    assert r.status_code == 302
    r = client.post(f"/criminal/{m2}/speedy-trial", data={"_csrf": tok}, follow_redirects=True)
    assert b"already exists" in r.data
    with app.app_context():
        st = Task.query.filter_by(matter_id=m2, title="Speedy trial / limitations check").all()
        assert len(st) == 1 and st[0].kind == "deadline" and st[0].due_on == arrest + timedelta(days=180)
        assert "jurisdiction" in st[0].notes
    # once discovery is recorded, the chain for a new setting adds three, not four
    with app.app_context():
        c = CriminalCase.query.filter_by(matter_id=m2).one()
        c.discovery_received_on = date.today()
        c.next_setting_on = setting + timedelta(days=30)
        c.next_setting_type = "pretrial"
        from app.extensions import db
        db.session.commit()
        n = Task.query.filter_by(matter_id=m2).count()
    r = client.post(f"/criminal/{m2}/court-chain", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        assert Task.query.filter_by(matter_id=m2).count() == n + 3
        assert Task.query.filter_by(matter_id=m2, title="Prepare for pretrial hearing").count() == 1


def test_disposition_pdf_document(app, client):
    from app.models import Document
    tok = csrf(client)
    m2 = _matter_id(app, "M-1002")
    r = client.post(f"/criminal/{m2}/disposition-pdf", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        d = Document.query.filter_by(matter_id=m2, folder="Criminal").one()
        assert d.mime == "application/pdf" and d.name.startswith("Disposition summary M-1002") and d.size > 500
        path = os.path.join(app.config["UPLOAD_DIR"], d.path)
        assert os.path.exists(path)
        with open(path, "rb") as fh:
            assert fh.read(5) == b"%PDF-"
        did = d.id
    r = client.get(f"/documents/{did}/download")
    assert r.status_code == 200
    r = client.get(f"/criminal/{m2}")
    assert b"Disposition summary M-1002" in r.data


def test_stage_hook_mirrors_to_matter_when_stage_set_matches(app, client):
    from app.extensions import db
    from app.models import StageSet, Matter, CriminalCase
    import json
    tok = csrf(client)
    m2 = _matter_id(app, "M-1002")
    with app.app_context():
        ss = StageSet(name="Criminal", practice_area="Criminal defense", stages_json=json.dumps([
            {"key": "charged", "label": "Charged"}, {"key": "negotiation", "label": "Negotiating"},
            {"key": "disposed", "label": "Done"}]))
        db.session.add(ss)
        db.session.flush()
        m = db_get(Matter, m2)
        m.stage_set_id = ss.id
        db.session.commit()
        c = CriminalCase.query.filter_by(matter_id=m2).one()
        assert m.stage == ""
    r = client.post(f"/criminal/{m2}/facts", data={"_csrf": tok, "stage": "negotiation", "custody_status": "out"})
    assert r.status_code == 302
    with app.app_context():
        m = db_get(Matter, m2)
        assert m.stage == "negotiation" and m.stage_changed_at is not None
    # a criminal stage with no matching key leaves matter.stage alone
    r = client.post(f"/criminal/{m2}/facts", data={"_csrf": tok, "stage": "pretrial", "custody_status": "out"})
    with app.app_context():
        assert db_get(Matter, m2).stage == "negotiation"
        assert CriminalCase.query.filter_by(matter_id=m2).one().stage == "pretrial"
    r = client.get("/criminal")
    assert r.status_code == 200
