"""Voice line: the Coil side of the phone agent. Own SQLite DB via seed.py.

Covers the voice API (config, lookup, verify, pin, note, time, calls), outbound reminder calls with Twilio
monkeypatched, the staff test-call route, and the settings pages (voice line + voice PIN on the user form).
"""
import os
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402

OWNER_PHONE = "+15125550100"
MARIA_PHONE = "+15125550111"  # seeded contact Maria Alvarez, client on M-1001


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("voice")
    dbfile = tmp / "test.db"
    uri = f"sqlite:///{dbfile}"
    env = dict(os.environ, DATABASE_URL=uri, STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="", SMTP_HOST="")
    subprocess.run([sys.executable, "seed.py"], cwd=ROOT, env=env, check=True)
    from app import create_app
    application = create_app({"SQLALCHEMY_DATABASE_URI": uri, "TESTING": True, "STRIPE_SECRET_KEY": "",
                              "STRIPE_WEBHOOK_SECRET": "", "SMTP_HOST": "", "UPLOAD_DIR": str(tmp / "uploads"),
                              "PDF_DIR": str(tmp / "pdf"), "API_RATE_LIMIT": 1000,
                              "TWILIO_ACCOUNT_SID": "", "TWILIO_AUTH_TOKEN": "", "TWILIO_FROM_NUMBER": ""})
    with application.app_context():
        from app.extensions import db
        from app.models import Firm, Matter, Contact, Invoice
        f = Firm.get()
        f.voice_enabled = False
        f.voice_client_status = False
        f.voice_reminders = False
        f.timezone = "America/Chicago"
        maria = Contact.query.filter_by(phone=MARIA_PHONE).first()
        assert maria is not None, "seed changed: Maria Alvarez with +15125550111 expected"
        m1 = Matter.query.filter_by(number="M-1001").first()
        m1.stage = "Drafting"
        m1.stage_changed_at = datetime.utcnow() - timedelta(days=1)
        # An outstanding invoice so verify has a balance to read back
        db.session.add(Invoice(number="INV-9001", matter_id=m1.id, client_id=maria.id, status="sent",
                               subtotal_cents=50000, total_cents=50000, paid_cents=0,
                               due_on=date.today() + timedelta(days=20)))
        # Two matters with near-identical names for the ambiguous hint test
        db.session.add(Matter(number="M-1101", client_id=maria.id, name="Alvarez Trust Amendment", status="open"))
        db.session.add(Matter(number="M-1102", client_id=maria.id, name="Alvarez Trust Litigation", status="open"))
        db.session.commit()
    return application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    login(c)
    return c


def csrf(client):
    client.get("/settings")
    with client.session_transaction() as s:
        return s["_csrf"]


def db_get(model, id):
    from app.extensions import db
    return db.session.get(model, id)


def _make_token(app, scopes):
    from app.extensions import db
    from app.models import User
    from app.blueprints.api import create_token
    with app.app_context():
        u = User.query.filter_by(email="owner@example.com").first()
        t, raw = create_token(u, f"voice test {scopes}", scopes)
        db.session.commit()
        return raw


def _h(raw):
    return {"Authorization": f"Bearer {raw}"}


def _set_firm(app, **kw):
    from app.extensions import db
    from app.models import Firm
    with app.app_context():
        f = Firm.get()
        for k, v in kw.items():
            setattr(f, k, v)
        db.session.commit()


@pytest.fixture(scope="module")
def rw(app):
    return _make_token(app, "read,write")


@pytest.fixture(scope="module")
def ro(app):
    return _make_token(app, "read")


# ---------------------------------------------------------------- config
def test_config_off_then_on(app, rw, ro):
    c = app.test_client()
    r = c.get("/api/v1/voice/config")
    assert r.status_code == 401  # no bearer token
    r = c.get("/api/v1/voice/config", headers=_h(ro))
    assert r.status_code == 403 and r.json["ok"] is False and r.json["reason"] == "voice_disabled"
    _set_firm(app, voice_enabled=True, voice_greeting_name="Harbor Defense", voice_practice_areas="dui,criminal",
              voice_approved_lines_json='{"fees": "Flat fee quoted by the attorney only."}')
    r = c.get("/api/v1/voice/config", headers=_h(ro))
    assert r.status_code == 200, r.json
    j = r.json
    assert j["ok"] is True and j["greeting_name"] == "Harbor Defense" and j["practice_areas"] == ["dui", "criminal"]
    assert j["approved_lines"]["fees"] == "Flat fee quoted by the attorney only."
    assert "no_advice" in j["approved_lines"] and "do_not_discuss" in j["approved_lines"]
    assert j["callback_tiers"]["urgent_minutes"] == 15 and j["voice_client_status"] is False
    assert j["timezone"] == "America/Chicago" and any("/api/v1/voice/verify" in k for k in j["endpoints"])


# ---------------------------------------------------------------- lookup
def test_lookup_never_leaks(app, ro):
    from app.models import VoiceCall
    c = app.test_client()
    r = c.post("/api/v1/voice/lookup", json={"phone": "(512) 555-0111", "call_id": "CA-lookup-1"}, headers=_h(ro))
    assert r.status_code == 200, r.json
    assert r.json == {"ok": True, "found": True, "verification_required": True,
                      "hint": "ask for full name and the matter"}
    assert "Alvarez" not in r.data.decode() and "Maria" not in r.data.decode()
    r = c.post("/api/v1/voice/lookup", json={"phone": "+15125559999"}, headers=_h(ro))
    assert r.status_code == 200 and r.json["found"] is False and set(r.json) == {"ok", "found", "verification_required", "hint"}
    with app.app_context():
        vc = VoiceCall.query.filter_by(call_id="CA-lookup-1").first()
        assert vc and vc.kind == "status" and vc.contact_id is None and vc.from_number == "(512) 555-0111"


# ---------------------------------------------------------------- verify
def test_verify_refused_when_toggle_off(app, ro):
    c = app.test_client()
    r = c.post("/api/v1/voice/verify", json={"phone": MARIA_PHONE, "name": "Maria Alvarez", "call_id": "CA-v0"},
               headers=_h(ro))
    assert r.status_code == 403 and r.json["ok"] is False and r.json["reason"] == "client_status_off"
    assert "matters" not in r.json


def test_verify_right_name_returns_matters_wrong_name_refuses(app, ro):
    from app.models import VoiceCall, Contact
    _set_firm(app, voice_client_status=True)
    c = app.test_client()
    # wrong name
    r = c.post("/api/v1/voice/verify", json={"phone": MARIA_PHONE, "name": "Derek Holloway", "call_id": "CA-v1"},
               headers=_h(ro))
    assert r.status_code == 403 and r.json["reason"] == "name_mismatch" and "matters" not in r.json
    with app.app_context():
        vc = VoiceCall.query.filter_by(call_id="CA-v1").first()
        assert vc.outcome == "unverified" and vc.contact_id is None
    # unknown number
    r = c.post("/api/v1/voice/verify", json={"phone": "+15125559999", "name": "Maria Alvarez"}, headers=_h(ro))
    assert r.status_code == 403 and r.json["reason"] == "no_match"
    # right name, slightly misheard ("Maria Alvares")
    r = c.post("/api/v1/voice/verify", json={"phone": "512-555-0111", "name": "Maria Alvares", "call_id": "CA-v2"},
               headers=_h(ro))
    assert r.status_code == 200, r.json
    j = r.json
    assert j["ok"] is True and j["name"] == "Maria Alvarez"
    numbers = {m["number"]: m for m in j["matters"]}
    assert "M-1001" in numbers and "M-1101" in numbers
    m1 = numbers["M-1001"]
    assert m1["stage"] == "Drafting" and m1["balance_cents"] == 50000
    assert m1["next_event"]["title"] == "Alvarez signing" and " at " in m1["next_event"]["when"]
    assert m1["next_task_due"]["title"] == "Signing appointment"
    assert m1["last_update"].endswith(": stage changed to Drafting")
    body = r.data.decode()
    assert "Bluebonnet" not in body and "trust_balance" not in body and "documents" not in body
    for m in j["matters"]:
        assert set(m) == {"id", "number", "name", "stage", "status", "next_event", "next_task_due", "balance_cents", "last_update"}
    with app.app_context():
        vc = VoiceCall.query.filter_by(call_id="CA-v2").first()
        maria = Contact.query.filter_by(phone=MARIA_PHONE).first()
        assert vc.outcome == "verified" and vc.contact_id == maria.id
    # matter hint narrows to one; an alias also verifies
    r = c.post("/api/v1/voice/verify", json={"phone": MARIA_PHONE, "name": "Maria Alvarez", "matter_hint": "M-1001"},
               headers=_h(ro))
    assert r.status_code == 200 and [m["number"] for m in r.json["matters"]] == ["M-1001"]
    with app.app_context():
        from app.extensions import db
        maria = Contact.query.filter_by(phone=MARIA_PHONE).first()
        maria.aliases = "Maria Gonzalez"
        db.session.commit()
    r = c.post("/api/v1/voice/verify", json={"phone": MARIA_PHONE, "name": "Maria Gonzales"}, headers=_h(ro))
    assert r.status_code == 200 and r.json["ok"] is True


# ---------------------------------------------------------------- PIN, note, time
def test_user_form_sets_voice_phone_and_pin(app, client):
    from app.models import User
    from werkzeug.security import check_password_hash
    with app.app_context():
        u = User.query.filter_by(email="owner@example.com").first()
        uid = u.id
    tok = csrf(client)
    base = {"_csrf": tok, "name": "Demo Owner", "email": "owner@example.com", "initials": "DO", "role": "owner",
            "hourly_rate": "350", "cost_rate": "0", "office_id": "", "is_active": "1", "password": "",
            "voice_phone": OWNER_PHONE}
    r = client.post(f"/settings/users/{uid}/edit", data=dict(base, voice_pin="12"), follow_redirects=True)
    assert b"4 to 6 digits" in r.data
    r = client.post(f"/settings/users/{uid}/edit", data=dict(base, voice_pin="4321"), follow_redirects=True)
    assert r.status_code == 200 and b"User saved" in r.data
    with app.app_context():
        u = db_get(User, uid)
        assert u.voice_phone == OWNER_PHONE and u.voice_pin_hash and "4321" not in u.voice_pin_hash
        assert check_password_hash(u.voice_pin_hash, "4321")
    r = client.get(f"/settings/users/{uid}/edit")
    assert b"A PIN is set" in r.data and b"4321" not in r.data


def test_note_and_time_refused_without_pin(app, rw):
    from app.blueprints.voice import reset_voice_state
    reset_voice_state()
    c = app.test_client()
    r = c.post("/api/v1/voice/note", json={"user_id": 1, "matter_id": 1, "body": "x", "call_id": "CA-nopin"}, headers=_h(rw))
    assert r.status_code == 403 and r.json["reason"] == "pin_required"
    r = c.post("/api/v1/voice/time", json={"user_id": 1, "matter_id": 1, "minutes": 10, "description": "x", "call_id": "CA-nopin"},
               headers=_h(rw))
    assert r.status_code == 403 and r.json["reason"] == "pin_required"


def test_pin_flow_and_lockout(app, rw, ro):
    from app.blueprints.voice import reset_voice_state
    from app.models import VoiceCall
    reset_voice_state()
    c = app.test_client()
    # write scope needed
    r = c.post("/api/v1/voice/pin", json={"phone": OWNER_PHONE, "pin": "4321"}, headers=_h(ro))
    assert r.status_code == 403 and "scope" in r.json["error"]
    # good pin
    r = c.post("/api/v1/voice/pin", json={"phone": "512 555 0100", "pin": "4321", "call_id": "CA-pin-1"}, headers=_h(rw))
    assert r.status_code == 200, r.json
    assert r.json["ok"] is True and r.json["name"] == "Demo Owner" and r.json["pin_session"] == "CA-pin-1"
    assert {m["number"] for m in r.json["matters"]} >= {"M-1001", "M-1002"}
    assert all(set(m) == {"id", "number", "name"} for m in r.json["matters"])
    with app.app_context():
        vc = VoiceCall.query.filter_by(call_id="CA-pin-1").first()
        assert vc.kind == "memo" and vc.outcome == "verified" and vc.user_id == r.json["user_id"]
    # wrong number with the right pin
    r = c.post("/api/v1/voice/pin", json={"phone": "+15125550999", "pin": "4321"}, headers=_h(rw))
    assert r.status_code == 403 and r.json["reason"] == "bad_pin"
    # five failures lock the phone
    for i in range(5):
        r = c.post("/api/v1/voice/pin", json={"phone": OWNER_PHONE, "pin": "0000", "call_id": f"CA-bad-{i}"}, headers=_h(rw))
        assert r.status_code == 403 and r.json["reason"] == "bad_pin" and r.json["attempts_left"] == 4 - i
    r = c.post("/api/v1/voice/pin", json={"phone": OWNER_PHONE, "pin": "4321", "call_id": "CA-locked"}, headers=_h(rw))
    assert r.status_code == 423 and r.json["reason"] == "locked"
    with app.app_context():
        assert VoiceCall.query.filter_by(call_id="CA-locked").first().outcome == "unverified"
    reset_voice_state()


def test_note_and_time_saved_with_rounding(app, rw):
    from app.blueprints.voice import reset_voice_state
    from app.models import Note, TimeEntry, Matter, VoiceCall
    reset_voice_state()
    c = app.test_client()
    r = c.post("/api/v1/voice/pin", json={"phone": OWNER_PHONE, "pin": "4321", "call_id": "CA-memo"}, headers=_h(rw))
    assert r.status_code == 200
    uid = r.json["user_id"]
    # wrong user_id for this call
    r = c.post("/api/v1/voice/note", json={"user_id": uid + 99, "matter_hint": "M-1002", "body": "x", "call_id": "CA-memo"},
               headers=_h(rw))
    assert r.status_code == 403 and r.json["reason"] == "user_mismatch"
    # note by number
    r = c.post("/api/v1/voice/note", json={"user_id": uid, "matter_hint": "M 1002",
                                           "body": "Called opposing counsel, they will produce the contract Friday.",
                                           "call_id": "CA-memo"}, headers=_h(rw))
    assert r.status_code == 200, r.json
    assert r.json["ok"] is True and r.json["matter"]["number"] == "M-1002" and r.json["read_back"].startswith("Saved a note on M-1002")
    with app.app_context():
        n = db_get(Note, r.json["note_id"])
        m2 = Matter.query.filter_by(number="M-1002").first()
        assert n.matter_id == m2.id and n.user_id == uid and "opposing counsel" in n.body and n.body.startswith("[by phone]")
    # note by description (client name)
    r = c.post("/api/v1/voice/note", json={"user_id": uid, "matter_hint": "the Bluebonnet contract case", "body": "Second note.",
                                           "call_id": "CA-memo"}, headers=_h(rw))
    assert r.status_code == 200 and r.json["matter"]["number"] == "M-1002"
    # time: 20 minutes -> 24, at the matter rate
    r = c.post("/api/v1/voice/time", json={"user_id": uid, "matter_id": m2.id, "minutes": 20,
                                           "description": "Call with opposing counsel", "call_id": "CA-memo"}, headers=_h(rw))
    assert r.status_code == 200, r.json
    assert r.json["minutes"] == 24 and r.json["hours"] == 0.4 and r.json["rate_cents"] > 0
    assert r.json["amount_cents"] == int(round(24 * r.json["rate_cents"] / 60))
    with app.app_context():
        te = db_get(TimeEntry, r.json["time_entry_id"])
        assert te.minutes == 24 and te.user_id == uid and te.matter_id == m2.id and te.billable and te.date == date.today()
        vc = VoiceCall.query.filter_by(call_id="CA-memo").first()
        assert vc.outcome == "time_saved" and vc.matter_id == m2.id and vc.user_id == uid
    # 6 minutes stays 6, 7 becomes 12, no minutes refused
    r = c.post("/api/v1/voice/time", json={"user_id": uid, "matter_id": m2.id, "minutes": 7, "description": "x", "call_id": "CA-memo"},
               headers=_h(rw))
    assert r.status_code == 200 and r.json["minutes"] == 12
    r = c.post("/api/v1/voice/time", json={"user_id": uid, "matter_id": m2.id, "minutes": 0, "description": "x", "call_id": "CA-memo"},
               headers=_h(rw))
    assert r.status_code == 400 and r.json["reason"] == "minutes_required"
    # pin_session works without call_id
    r = c.post("/api/v1/voice/time", json={"user_id": uid, "pin_session": "CA-memo", "matter_id": m2.id, "minutes": 6,
                                           "description": "y"}, headers=_h(rw))
    assert r.status_code == 200 and r.json["minutes"] == 6


def test_ambiguous_matter_hint(app, rw):
    from app.blueprints.voice import reset_voice_state
    reset_voice_state()
    c = app.test_client()
    r = c.post("/api/v1/voice/pin", json={"phone": OWNER_PHONE, "pin": "4321", "call_id": "CA-amb"}, headers=_h(rw))
    uid = r.json["user_id"]
    r = c.post("/api/v1/voice/note", json={"user_id": uid, "matter_hint": "Alvarez trust", "body": "x", "call_id": "CA-amb"},
               headers=_h(rw))
    assert r.status_code == 409, r.json
    assert r.json["ok"] is False and r.json["reason"] == "ambiguous"
    assert {m["number"] for m in r.json["ambiguous"]} == {"M-1101", "M-1102"}
    r = c.post("/api/v1/voice/note", json={"user_id": uid, "matter_hint": "Alvarez trust litigation", "body": "x", "call_id": "CA-amb"},
               headers=_h(rw))
    assert r.status_code == 200 and r.json["matter"]["number"] == "M-1102"
    r = c.post("/api/v1/voice/note", json={"user_id": uid, "matter_hint": "zzz nothing like this", "body": "x", "call_id": "CA-amb"},
               headers=_h(rw))
    assert r.status_code == 404 and r.json["reason"] == "no_matter"


# ---------------------------------------------------------------- calls
def test_calls_finalise_a_voice_call(app, rw, client):
    from app.models import VoiceCall
    c = app.test_client()
    # a lead the intake agent filed first
    r = c.post("/api/v1/leads", json={"name": "Jordan Reyes", "phone": "+15125550177", "matter_type": "Criminal defense",
                                      "description": "Arrested last night.", "external_id": "HD-0905-AAAA"}, headers=_h(rw))
    lead_id = r.json["id"]
    body = {"call_id": "CA-final-1", "kind": "intake", "from": "+15125550177", "to": "+15125550000",
            "summary": "Wife called; husband arrested for DWI in Travis County.",
            "transcript": [{"role": "caller", "text": "My husband was arrested."}, {"role": "agent", "text": "I can help."}],
            "duration_seconds": 254, "outcome": "filed", "lead_id": lead_id}
    r = c.post("/api/v1/voice/calls", json=body, headers=_h(rw))
    assert r.status_code == 200, r.json
    assert r.json["ok"] is True and r.json["url"].endswith(f"/voice/{r.json['id']}")
    with app.app_context():
        vc = VoiceCall.query.filter_by(call_id="CA-final-1").first()
        assert vc.kind == "intake" and vc.outcome == "filed" and vc.duration_seconds == 254 and vc.lead_id == lead_id
        assert "caller: My husband was arrested." in vc.transcript and vc.to_number == "+15125550000"
        assert VoiceCall.query.filter_by(call_id="CA-final-1").count() == 1
    # posting again for the same call updates, no duplicate; earlier per-call rows are finalised too
    r = c.post("/api/v1/voice/calls", json=dict(body, duration_seconds=300), headers=_h(rw))
    assert r.status_code == 200
    r = c.post("/api/v1/voice/calls", json={"call_id": "CA-memo", "kind": "memo", "duration_seconds": 90, "summary": "Memo call."},
               headers=_h(rw))
    with app.app_context():
        assert VoiceCall.query.filter_by(call_id="CA-final-1").count() == 1
        assert VoiceCall.query.filter_by(call_id="CA-final-1").first().duration_seconds == 300
        memo = VoiceCall.query.filter_by(call_id="CA-memo").first()
        assert memo.duration_seconds == 90 and memo.outcome == "time_saved" and memo.matter_id  # links kept
    r = c.post("/api/v1/voice/calls", json={"kind": "intake"}, headers=_h(rw))
    assert r.status_code == 400
    # staff pages
    r = client.get("/voice")
    assert r.status_code == 200 and b"Wife called" in r.data and b"Jordan Reyes" in r.data
    r = client.get("/voice?kind=memo")
    assert r.status_code == 200 and b"Wife called" not in r.data and b"Memo call." in r.data
    with app.app_context():
        vid = VoiceCall.query.filter_by(call_id="CA-final-1").first().id
    r = client.get(f"/voice/{vid}")
    assert r.status_code == 200 and b"My husband was arrested." in r.data and b"Transcript" in r.data
    assert client.get("/voice/999999").status_code == 404
    # matter page links the voice calls
    from app.models import Matter
    with app.app_context():
        m2 = Matter.query.filter_by(number="M-1002").first().id
    r = client.get(f"/matters/{m2}")
    assert f"/voice?matter_id={m2}".encode() in r.data
    r = client.get(f"/voice?matter_id={m2}")
    assert r.status_code == 200 and b"Memo call." in r.data and b"Wife called" not in r.data


# ---------------------------------------------------------------- outbound reminders
class _FakeResp:
    def __init__(self, sid):
        self.status_code = 201
        self._sid = sid

    def json(self):
        return {"sid": self._sid, "status": "queued"}


def _fake_twilio(monkeypatch, app, calls):
    import app.blueprints.voice as voice

    def fake_post(url, auth=None, data=None, timeout=None):
        calls.append({"url": url, "auth": auth, "data": data})
        return _FakeResp(f"CA{len(calls):032d}")

    monkeypatch.setattr(voice.requests, "post", fake_post)
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setitem(app.config, "TWILIO_FROM_NUMBER", "+15125550000")


def test_reminders(app, monkeypatch):
    from app.extensions import db
    from app.models import Matter, Task, VoiceCall, Message, AuditLog, User
    from app.cli import run_voice_reminders
    with app.app_context():
        m1 = Matter.query.filter_by(number="M-1001").first()
        u = User.query.filter_by(email="owner@example.com").first()
        db.session.add(Task(matter_id=m1.id, title="Arraignment, Travis County Court 5", kind="court_date",
                            due_on=date.today() + timedelta(days=1), assignee_id=u.id))
        db.session.commit()
        m1_id, m1_client_id = m1.id, m1.client_id
    # toggle off: nothing, even with Twilio
    calls = []
    _fake_twilio(monkeypatch, app, calls)
    _set_firm(app, voice_reminders=False, voice_reminder_days=10)
    with app.app_context():
        r = run_voice_reminders()
    assert r["placed"] == [] and calls == [] and "off" in r["reason"]
    # toggle on, no Twilio: logs only, records nothing
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "")
    _set_firm(app, voice_reminders=True)
    with app.app_context():
        r = run_voice_reminders()
        assert r["placed"] == [] and len(r["would"]) == 2 and "not configured" in r["reason"]
        assert VoiceCall.query.filter_by(kind="reminder").count() == 0
        assert AuditLog.query.filter_by(action="voice_reminded").count() == 0
    # Twilio on: one call per event per client with the right TwiML
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "ACtest")
    with app.app_context():
        r = run_voice_reminders()
        assert len(r["placed"]) == 2 and r["failed"] == 0, r
        assert len(calls) == 2
        for call in calls:
            assert call["url"] == "https://api.twilio.com/2010-04-01/Accounts/ACtest/Calls.json"
            assert call["auth"] == ("ACtest", "tok") and call["data"]["To"] == MARIA_PHONE and call["data"]["From"] == "+15125550000"
            assert call["data"]["Twiml"].startswith("<?xml") and "<Say" in call["data"]["Twiml"]
            assert "This is a reminder from Harbor Defense." in call["data"]["Twiml"]
            assert "Please call the office with any questions" in call["data"]["Twiml"]
            assert "Estate Plan" not in call["data"]["Twiml"]  # no case detail
        texts = [c["data"]["Twiml"] for c in calls]
        assert any("Alvarez signing is scheduled for" in t and " at " in t and ("CDT" in t or "CST" in t) for t in texts)
        assert any("Arraignment, Travis County Court 5 is due on" in t for t in texts)
        vcs = VoiceCall.query.filter_by(kind="reminder").all()
        assert len(vcs) == 2 and all(v.direction == "out" and v.outcome == "reminded" and v.to_number == MARIA_PHONE for v in vcs)
        assert all(v.matter_id == m1_id and v.contact_id == m1_client_id and v.call_id.startswith("CA") for v in vcs)
        msgs = Message.query.filter_by(channel="voice").all()
        assert len(msgs) == 2 and all(m.direction == "out" and m.to_addr == MARIA_PHONE for m in msgs)
        assert AuditLog.query.filter_by(action="voice_reminded").count() == 2
        # idempotent
        r = run_voice_reminders()
        assert r["placed"] == [] and r["skipped"] == 2 and len(calls) == 2
        assert VoiceCall.query.filter_by(kind="reminder").count() == 2
    # the cli entry point prints a summary
    from app.cli import main
    monkeypatch.setattr("app.create_app", lambda *a, **k: app)
    assert main(["voice_reminders"]) == 0
    assert len(calls) == 2


def test_reminder_test_call_route(app, client, monkeypatch):
    from app.models import VoiceCall
    calls = []
    tok = csrf(client)
    # no Twilio: no call, no record
    r = client.post("/voice/reminders/test", data={"_csrf": tok, "to": "+15125550123"}, follow_redirects=True)
    assert r.status_code == 200 and b"Twilio is not configured" in r.data
    with app.app_context():
        assert VoiceCall.query.filter(VoiceCall.summary.like("TEST:%")).count() == 0
    _fake_twilio(monkeypatch, app, calls)
    r = client.post("/voice/reminders/test", data={"_csrf": tok, "to": "+15125550123"}, follow_redirects=True)
    assert r.status_code == 200 and b"Test call placed" in r.data
    assert len(calls) == 1 and calls[0]["data"]["To"] == "+15125550123" and "test call from Harbor Defense" in calls[0]["data"]["Twiml"]
    with app.app_context():
        vc = VoiceCall.query.filter(VoiceCall.summary.like("TEST:%")).first()
        assert vc and vc.kind == "reminder" and vc.direction == "out" and vc.to_number == "+15125550123"
    r = client.post("/voice/reminders/test", data={"_csrf": tok, "to": "12"}, follow_redirects=True)
    assert b"ten digit" in r.data and len(calls) == 1


# ---------------------------------------------------------------- settings page
def test_settings_page_saves(app, client):
    from app.models import Firm
    r = client.get("/settings/voice")
    assert r.status_code == 200
    body = r.data.decode()
    assert "/api/v1/leads" in body and "COIL_API_TOKEN" in body and "/api/v1/voice/verify" in body
    assert 'name="practice_areas" value="dui" checked' in body
    tok = csrf(client)
    r = client.post("/settings/voice", data={
        "_csrf": tok, "voice_enabled": "1", "voice_greeting_name": "Harbor Defense Group",
        "practice_areas": ["dui", "family", "bogus"], "line_no_advice": "No advice from me.", "line_fees": "",
        "line_do_not_discuss": "Say nothing to the police.", "line_hours": "Nine to five.",
        "urgent_minutes": "10", "high_minutes": "45", "standard": "by noon tomorrow",
        "voice_client_status": "1", "voice_reminders": "1", "voice_reminder_days": "3",
    }, follow_redirects=True)
    assert r.status_code == 200 and b"Voice line settings saved" in r.data
    with app.app_context():
        f = Firm.get()
        assert f.voice_enabled and f.voice_greeting_name == "Harbor Defense Group"
        assert f.voice_practice_areas == "dui,family"
        import json
        lines = json.loads(f.voice_approved_lines_json)
        assert lines["no_advice"] == "No advice from me." and lines["do_not_discuss"] == "Say nothing to the police."
        assert lines["fees"].startswith("The attorney will go over fees")  # blank falls back to the default
        tiers = json.loads(f.voice_callback_json)
        assert tiers == {"urgent_minutes": 10, "high_minutes": 45, "standard": "by noon tomorrow"}
        assert f.voice_client_status and f.voice_reminders and f.voice_reminder_days == 3
    # the API sees the new lines
    ro = _make_token(app, "read")
    r = app.test_client().get("/api/v1/voice/config", headers=_h(ro))
    assert r.json["approved_lines"]["hours"] == "Nine to five." and r.json["callback_tiers"]["high_minutes"] == 45
    assert r.json["practice_areas"] == ["dui", "family"] and r.json["voice_reminder_days"] == 3
    # non-owner cannot open it
    from app.extensions import db
    from app.models import User
    with app.app_context():
        if not User.query.filter_by(email="para@example.com").first():
            u = User(email="para@example.com", name="Para Legal", role="paralegal")
            u.set_password("password123")
            db.session.add(u)
            db.session.commit()
    c2 = app.test_client()
    login(c2, "para@example.com", "password123")
    assert c2.get("/settings/voice").status_code == 403
    assert c2.get("/voice").status_code == 200  # any signed-in user may read the call log
