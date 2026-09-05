"""Phase 5, Agent M: discovery drafting and deposition summaries (app/blueprints/discovery.py, templates discovery/).

Own SQLite file (data/test_phase5_m.db) seeded by seed.py, own UPLOAD_DIR and PDF_DIR. No network: the model is
monkeypatched at app.llm.complete for the AI tests, and the fixture blanks both API keys in app.config (plus the
no_keys fixture clears the environment) so the fallback tests hit the real gate.
Run: .venv/bin/python -m pytest tests/test_phase5_m.py -q
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402

DB_PATH = os.path.join(ROOT, "data", "test_phase5_m.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_phase5_m")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_phase5_m")
MATTER_NUMBER = "M-DISC1"
SERVED_ON = date(2026, 8, 1)
S = {}

SERVED_ROGS = (
    "IN THE DISTRICT COURT OF TRAVIS COUNTY, TEXAS. Cause No. TEST-2026-1. DEFENDANT'S FIRST SET OF "
    "INTERROGATORIES TO PLAINTIFF. INTERROGATORY NO. 1: State your full name, date of birth and every address "
    "at which you have lived in the last ten years. ANSWER: INTERROGATORY NO. 2: Identify every person who "
    "witnessed the collision on May 1, 2026 and summarize what each saw. ANSWER: INTERROGATORY NO. 3: Describe "
    "every injury you claim resulted from the collision and identify each treating provider. ANSWER: "
    "Respectfully submitted, /s/ Test Counsel. CERTIFICATE OF SERVICE. Served on all parties."
)

TRANSCRIPT = (
    "DEPOSITION OF JANE HOLLOWAY taken August 20, 2026. Page 12 12:1 Q. Where were you at 8 a.m. on May 1? "
    "12:2 A. I was already at the intersection of Lamar and Fifth. 12:5 Q. Were you on your phone? "
    "12:6 A. I was not on my phone at any point that morning. Page 13 13:2 Q. When did the plaintiff first "
    "see a doctor? 13:4 A. He told me he did not see anyone for two weeks. Page 14 14:1 Q. Anything else? "
    "14:2 A. No."
)


@pytest.fixture(scope="module")
def app():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    shutil.rmtree(PDF_DIR, ignore_errors=True)
    env = dict(os.environ, DATABASE_URL=DB_URI)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], env=env, cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    from app import create_app
    a = create_app({"SQLALCHEMY_DATABASE_URI": DB_URI, "UPLOAD_DIR": UPLOAD_DIR, "PDF_DIR": PDF_DIR, "TESTING": True,
                    "SMTP_HOST": "", "OPENROUTER_API_KEY": "", "ANTHROPIC_API_KEY": ""})
    with a.app_context():
        from app.extensions import db
        from app.models import Contact, Matter, User, PiCase, ChronologyEntry, Firm
        from app.blueprints.documents import store_bytes
        u = User.query.first()
        c = Contact(first_name="Test", last_name="Plaintiff", email="plaintiff@example.test", is_client=True)
        db.session.add(c)
        db.session.flush()
        m = Matter(number=MATTER_NUMBER, client_id=c.id, name="TEST Plaintiff v. Holloway", practice_area="Personal Injury",
                   billing_type="contingency", responsible_user_id=u.id, status="open",
                   court="District Court of Travis County, Texas", case_number="TEST-2026-1",
                   description="Rear-end collision at Lamar and Fifth on May 1, 2026.")
        db.session.add(m)
        db.session.flush()
        db.session.add(PiCase(matter_id=m.id, date_of_loss=date(2026, 5, 1), incident_type="auto",
                              incident_description="Client rear-ended by Holloway at a red light.",
                              injuries="Cervical strain."))
        db.session.add(ChronologyEntry(matter_id=m.id, date=date(2026, 5, 1), provider_name="Austin ER",
                                       visit_type="ER", diagnosis="Cervical strain", confirmed=True))
        doc, err = store_bytes(m.id, "served interrogatories.txt", SERVED_ROGS.encode(), user_id=u.id)
        assert err is None
        tr, err = store_bytes(m.id, "holloway transcript.txt", TRANSCRIPT.encode(), user_id=u.id)
        assert err is None
        f = Firm.get()
        f.ai_enabled = True  # the gate then fails on the missing key, which is the fallback path under test
        db.session.commit()
        S.update(mid=m.id, served_doc=doc.id, transcript_doc=tr.id, uid=u.id)
    yield a


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    S["tok"] = login(c)
    return c


@pytest.fixture
def no_keys(monkeypatch):
    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "LLM_ENABLED", "LLM_DAILY_CAP", "AI_DAILY_CAP_CENTS"):
        monkeypatch.delenv(k, raising=False)


def _fake_complete(monkeypatch, payload):
    from app import llm
    calls = []

    def fake(prompt, **kw):
        calls.append((prompt, kw))
        return json.dumps(payload) if not isinstance(payload, str) else payload
    monkeypatch.setattr(llm, "complete", fake)
    return calls


def _set(app, sid):
    from app.models import DiscoverySet
    with app.app_context():
        return DiscoverySet.query.get(sid)


def _pdf_text(app, doc_id):
    from pypdf import PdfReader
    from app.models import Document
    with app.app_context():
        d = Document.query.get(doc_id)
        path = os.path.join(UPLOAD_DIR, d.path)
    return " ".join((p.extract_text() or "") for p in PdfReader(path).pages), d


# ---------------------------------------------------------------- propound
def test_propound_starts_from_pi_starter_set(app, client):
    from app.blueprints.discovery import STARTER_SETS, area_for
    from app.models import Matter, DiscoverySet
    with app.app_context():
        assert area_for(Matter.query.get(S["mid"])) == "personal_injury"
    r = client.post(f"/discovery/new?matter_id={S['mid']}", data={
        "_csrf": S["tok"], "direction": "propound", "kind": "interrogatories", "party": "Defendant Holloway",
        "served_on": SERVED_ON.isoformat()})
    assert r.status_code == 302, r.data[:300]
    sid = int(r.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    S["prop"] = sid
    with app.app_context():
        ds = DiscoverySet.query.get(sid)
        starter = STARTER_SETS["personal_injury"]["interrogatories"]
        assert 15 <= len(starter) <= 25
        assert [it["request"] for it in ds.items] == starter
        assert ds.title == "Interrogatories to Defendant Holloway"
        assert ds.due_on == SERVED_ON + timedelta(days=30)
        assert all(it["response"] == "" and it["objections"] == [] for it in ds.items)
    r = client.get(f"/discovery/{sid}")
    assert r.status_code == 200
    assert b"Interrogatory No. 1" in r.data and starter[0].encode() in r.data
    assert b"draft for attorney review" in r.data
    assert b"Tailor with AI" in r.data
    r = client.get(f"/discovery?matter_id={S['mid']}")
    assert b"Interrogatories to Defendant Holloway" in r.data and b"Propound" in r.data


def test_starter_sets_are_complete():
    from app.blueprints.discovery import STARTER_SETS
    for area in ("personal_injury", "contract", "general"):
        for kind in ("interrogatories", "rfp", "rfa"):
            items = STARTER_SETS[area][kind]
            assert 15 <= len(items) <= 25, (area, kind, len(items))
            assert all("\u2014" not in x for x in items)


def test_tailor_with_ai_replaces_items(app, client, monkeypatch):
    canned = {"items": [{"request": "Identify every person in the Holloway truck on May 1, 2026."},
                        {"request": "State whether you saw the plaintiff's brake lights before the collision."},
                        {"request": "Identify every provider who treated you for the cervical strain."}]}
    calls = _fake_complete(monkeypatch, canned)
    r = client.post(f"/discovery/{S['prop']}/tailor", data={"_csrf": S["tok"]})
    assert r.status_code == 302
    assert len(calls) == 1 and calls[0][1]["kind"] == "discovery_tailor" and calls[0][1]["schema"]
    assert "Rear-end collision" in calls[0][0] and "Austin ER" in calls[0][0] and "Cervical strain" in calls[0][0]
    ds = _set(app, S["prop"])
    assert [it["request"] for it in ds.items] == [c["request"] for c in canned["items"]]
    assert [it["n"] for it in ds.items] == [1, 2, 3]
    r = client.get(f"/discovery/{S['prop']}")
    assert b"Holloway truck" in r.data


def test_editor_edits_reorder_add_remove_persist(app, client):
    sid = S["prop"]
    base = {"_csrf": S["tok"], "title": "Rogs to Holloway (edited)", "party": "Defendant Holloway",
            "served_on": SERVED_ON.isoformat(), "due_on": "", "status": "review", "item_n": ["1", "2", "3"],
            "item_1_request": "Identify every person in the Holloway truck on May 1, 2026, with addresses.",
            "item_1_response": "", "item_1_flag": "",
            "item_2_request": "State whether you saw the plaintiff's brake lights before the collision.",
            "item_2_response": "Not applicable on a propounded set", "item_2_flag": "ask client",
            "item_2_objections": ["vague", "not_in_library"],
            "item_3_request": "Identify every provider who treated you for the cervical strain.",
            "item_3_response": "", "item_3_flag": ""}
    r = client.post(f"/discovery/{sid}/save", data=base)
    assert r.status_code == 302
    ds = _set(app, sid)
    assert ds.title == "Rogs to Holloway (edited)" and ds.status == "review" and ds.due_on is None
    assert ds.items[0]["request"].endswith("with addresses.")
    assert ds.items[1]["objections"] == ["vague"] and ds.items[1]["flag"] == "ask client"
    # move item 3 up, then delete item 1, then add one: edits on the same form are kept each time
    r = client.post(f"/discovery/{sid}/save", data=dict(base, action="up:3"))
    ds = _set(app, sid)
    assert [it["request"][:8] for it in ds.items] == ["Identify", "Identify", "State wh"]
    assert ds.items[1]["request"].startswith("Identify every provider")
    r = client.post(f"/discovery/{sid}/save", data={"_csrf": S["tok"], "action": "delete:1"})
    ds = _set(app, sid)
    assert len(ds.items) == 2 and ds.items[0]["n"] == 1 and ds.items[0]["request"].startswith("Identify every provider")
    r = client.post(f"/discovery/{sid}/save", data={"_csrf": S["tok"], "action": "add",
                                                     "new_request": "Admit nothing; identify your insurer."})
    ds = _set(app, sid)
    assert len(ds.items) == 3 and ds.items[2]["n"] == 3 and ds.items[2]["request"].startswith("Admit nothing")
    r = client.get(f"/discovery/{sid}")
    assert b"Rogs to Holloway (edited)" in r.data and b"needs client input" in r.data


def test_due_date_task_created_once(app, client):
    from app.models import Task
    sid = S["prop"]
    r = client.post(f"/discovery/{sid}/task", data={"_csrf": S["tok"]})
    assert r.status_code == 302
    r = client.post(f"/discovery/{sid}/task", data={"_csrf": S["tok"]})
    assert r.status_code == 302
    with app.app_context():
        ts = Task.query.filter(Task.title == "Discovery responses due: Rogs to Holloway (edited)").all()
        assert len(ts) == 1
        t = ts[0]
        assert t.due_on == SERVED_ON + timedelta(days=30) and t.matter_id == S["mid"] and t.kind == "deadline"
        assert not t.done
        ds = _set(app, sid)
        assert ds.due_on == SERVED_ON + timedelta(days=30)  # filled in because the editor had blanked it
    r = client.get(f"/discovery/{sid}")
    assert b"Deadline task" in r.data and f"/tasks/{t.id}".encode() in r.data
    assert b"Task already exists" in r.data


def test_export_pdf_filed_in_discovery_folder(app, client):
    from app.models import Document
    sid = S["prop"]
    r = client.post(f"/discovery/{sid}/export", data={"_csrf": S["tok"]})
    assert r.status_code == 302
    ds = _set(app, sid)
    assert ds.output_document_id
    text, d = _pdf_text(app, ds.output_document_id)
    assert d.folder == "Discovery" and d.mime == "application/pdf" and d.matter_id == S["mid"]
    assert d.name.startswith("Rogs to Holloway (edited)") and d.name.endswith(".pdf")
    assert "DISTRICT COURT OF TRAVIS COUNTY" in text.upper()
    assert "Cause No. TEST-2026-1" in text
    assert "TEST Plaintiff v. Holloway" in text
    assert "Propounded to: Defendant Holloway" in text
    assert "INTERROGATORY NO. 1:" in text and "INTERROGATORY NO. 3:" in text
    assert "Draft prepared for attorney review" in text
    with app.app_context():
        assert Document.query.filter_by(matter_id=S["mid"], folder="Discovery").count() == 1
    r = client.get(f"/discovery/{sid}")
    assert b"Last export" in r.data and f"/documents/{d.id}/download".encode() in r.data


# ---------------------------------------------------------------- respond
def test_parse_requests_labelled_and_numbered():
    from app.blueprints.discovery import parse_requests
    got = parse_requests(SERVED_ROGS)
    assert len(got) == 3
    assert got[0].startswith("State your full name") and got[0].endswith("last ten years.")
    assert got[1].startswith("Identify every person who witnessed")
    assert got[2].startswith("Describe every injury") and "Respectfully" not in got[2] and "ANSWER" not in got[2]
    plain = ("REQUESTS FOR PRODUCTION. Definitions apply. 1. All photographs of the scene. 2. All medical "
             "records since May 1, 2026. 3. All phone records for the day of the collision. Dated: Aug 1.")
    got = parse_requests(plain)
    assert got == ["All photographs of the scene.", "All medical records since May 1, 2026.",
                   "All phone records for the day of the collision."]
    rfp = "REQUEST FOR PRODUCTION NO. 1: All photos. RESPONSE: REQUEST FOR PRODUCTION NO. 2: All bills. RESPONSE:"
    assert parse_requests(rfp) == ["All photos.", "All bills."]
    assert parse_requests("") == [] and parse_requests("No numbers anywhere in this text.") == []


def test_respond_parses_served_document_into_items(app, client):
    from app.models import DiscoverySet
    r = client.post(f"/discovery/new?matter_id={S['mid']}", data={
        "_csrf": S["tok"], "direction": "respond", "kind": "interrogatories", "party": "Defendant Holloway",
        "served_on": SERVED_ON.isoformat(), "source_document_id": S["served_doc"]})
    assert r.status_code == 302, r.data[:300]
    sid = int(r.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    S["resp"] = sid
    with app.app_context():
        ds = DiscoverySet.query.get(sid)
        assert ds.direction == "respond" and ds.source_document_id == S["served_doc"]
        assert ds.title == "Responses to interrogatories from Defendant Holloway"
        assert len(ds.items) == 3 and [it["n"] for it in ds.items] == [1, 2, 3]
        assert ds.items[1]["request"].startswith("Identify every person who witnessed")
    r = client.get(f"/discovery/{sid}")
    assert b"Found 3 numbered requests" in r.data and b"Draft responses with AI" in r.data
    # a served set with no matching document is refused
    r = client.post(f"/discovery/new?matter_id={S['mid']}", data={
        "_csrf": S["tok"], "direction": "respond", "kind": "rfp", "party": "X", "source_document_id": "999999"})
    assert r.status_code == 200 and b"Pick the served set" in r.data


def test_ai_draft_fills_responses_objections_and_flags(app, client, monkeypatch):
    canned = {"items": [
        {"n": 1, "response": "Test Plaintiff, born [CLIENT TO CONFIRM]; addresses listed in Exhibit A.",
         "objections": [], "flag": "Client must confirm date of birth and prior addresses"},
        {"n": 2, "response": "Subject to the objections, the plaintiff identifies the responding officer.",
         "objections": ["overbroad", "unduly_burdensome", "made_up_objection"], "flag": ""},
        {"n": 3, "response": "Cervical strain, treated at Austin ER on May 1, 2026.",
         "objections": ["vague"], "flag": ""}]}
    calls = _fake_complete(monkeypatch, canned)
    r = client.post(f"/discovery/{S['resp']}/draft", data={"_csrf": S["tok"]})
    assert r.status_code == 302
    assert len(calls) == 1 and calls[0][1]["kind"] == "discovery_draft"
    assert "overbroad: Overbroad" in calls[0][0] and "Austin ER" in calls[0][0]
    ds = _set(app, S["resp"])
    assert ds.items[0]["response"].startswith("Test Plaintiff") and ds.items[0]["flag"].startswith("Client must")
    assert ds.items[1]["objections"] == ["overbroad", "unduly_burdensome"]  # library only
    assert ds.items[2]["objections"] == ["vague"]
    r = client.get(f"/discovery/{S['resp']}")
    assert b"Drafted 3 responses (1 need a fact from the client)" in r.data
    assert b"needs client input" in r.data
    assert re.search(rb'name="item_2_objections" value="overbroad" checked', r.data)
    assert re.search(rb'name="item_2_objections" value="vague"\s*>', r.data)


def test_ai_draft_fallback_fills_placeholders(app, client, no_keys):
    from app.blueprints.discovery import FALLBACK_RESPONSE
    r = client.post(f"/discovery/new?matter_id={S['mid']}", data={
        "_csrf": S["tok"], "direction": "respond", "kind": "interrogatories", "party": "Defendant Holloway",
        "source_document_id": S["served_doc"]})
    sid = int(r.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    r = client.post(f"/discovery/{sid}/draft", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert r.status_code == 200
    assert b"3 placeholder responses" in r.data and b"No AI key" in r.data
    ds = _set(app, sid)
    assert len(ds.items) == 3 and all(it["response"] == FALLBACK_RESPONSE for it in ds.items)
    assert FALLBACK_RESPONSE == "Objection: [none]. Response: [ATTORNEY TO COMPLETE]"
    # the due-date task needs a served date
    r = client.post(f"/discovery/{sid}/task", data={"_csrf": S["tok"]}, follow_redirects=True)
    assert b"Set the served date first" in r.data
    # export a respond set: responses and objections appear under each request
    r = client.post(f"/discovery/{sid}/export", data={"_csrf": S["tok"]})
    text, d = _pdf_text(app, _set(app, sid).output_document_id)
    assert d.folder == "Discovery" and "Served by: Defendant Holloway" in text
    assert "RESPONSE:" in text and "[ATTORNEY TO COMPLETE]" in text


# ---------------------------------------------------------------- depositions
def test_chunk_transcript_breaks_at_page_markers():
    from app.blueprints.discovery import chunk_transcript, has_markers
    pages = " ".join(f"Page {p} " + " ".join(f"{p}:{l} testimony line {l} of page {p}" for l in range(1, 26))
                     for p in range(1, 40))
    assert len(pages) > 30000 and has_markers(pages)
    chunks = chunk_transcript(pages)
    assert len(chunks) >= 3 and "".join(c + " " for c in chunks).split() == pages.split()
    assert all(len(c) <= 10000 for c in chunks)
    assert all(c.startswith("Page ") for c in chunks[1:])  # every piece after the first opens on a page marker
    assert chunk_transcript("short") == ["short"] and chunk_transcript("") == []
    assert not has_markers("no markers here")


def test_deposition_summary_with_canned_model(app, client, monkeypatch):
    from app.models import DepositionSummary, Document, Note
    canned = {"summary": "Holloway says she was stopped at Lamar and Fifth, not on her phone, and that the plaintiff "
                         "waited two weeks before seeing a doctor.",
              "key_testimony": [{"page": 12, "line": 6, "quote": "I was not on my phone at any point that morning.",
                                 "topic": "Phone use"},
                                {"page": 13, "line": 4, "quote": "He told me he did not see anyone for two weeks.",
                                 "topic": "Treatment delay"}],
              "contradictions": [{"testimony": "Plaintiff did not see anyone for two weeks",
                                  "conflicts_with": "Austin ER visit on May 1, 2026 for cervical strain",
                                  "source": "chronology 2026-05-01 Austin ER"}]}
    calls = _fake_complete(monkeypatch, canned)
    r = client.post(f"/discovery/depositions/new?matter_id={S['mid']}", data={
        "_csrf": S["tok"], "document_id": S["transcript_doc"], "deponent": "Jane Holloway",
        "taken_on": "2026-08-20"})
    assert r.status_code == 302, r.data[:300]
    did = int(r.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    S["dep"] = did
    assert len(calls) == 1 and calls[0][1]["kind"] == "deposition_summary"
    assert "page and line markers" in calls[0][0] and "12:5" in calls[0][0] and "Austin ER" in calls[0][0]
    with app.app_context():
        dep = DepositionSummary.query.get(did)
        assert dep.deponent == "Jane Holloway" and dep.taken_on == date(2026, 8, 20)
        assert dep.document_id == S["transcript_doc"] and dep.summary_text.startswith("Holloway says")
        assert json.loads(dep.key_testimony_json)[0]["page"] == 12
        assert json.loads(dep.contradictions_json)[0]["source"] == "chronology 2026-05-01 Austin ER"
    r = client.get(f"/discovery/depositions/{did}")
    assert r.status_code == 200
    assert b"Depo. Tr. 12:6" in r.data and b"Depo. Tr. 13:4" in r.data
    assert b"I was not on my phone" in r.data and b"Phone use" in r.data
    assert b"Austin ER visit on May 1, 2026" in r.data and b"chronology 2026-05-01 Austin ER" in r.data
    assert b"draft for attorney review" in r.data
    # editable summary
    r = client.post(f"/discovery/depositions/{did}/save", data={
        "_csrf": S["tok"], "summary_text": "Edited summary.", "deponent": "Jane Holloway", "taken_on": "2026-08-20",
        "status": "review"})
    assert r.status_code == 302
    with app.app_context():
        dep = DepositionSummary.query.get(did)
        assert dep.summary_text == "Edited summary." and dep.status == "review"
    # save as note
    with app.app_context():
        before = Note.query.filter_by(matter_id=S["mid"]).count()
    r = client.post(f"/discovery/depositions/{did}/note", data={"_csrf": S["tok"]})
    assert r.status_code == 302
    with app.app_context():
        notes = Note.query.filter_by(matter_id=S["mid"]).order_by(Note.id.desc()).all()
        assert len(notes) == before + 1
        assert "Edited summary." in notes[0].body and "Depo. Tr. 12:6" in notes[0].body
        assert "chronology 2026-05-01 Austin ER" in notes[0].body
    # export PDF
    r = client.post(f"/discovery/depositions/{did}/export", data={"_csrf": S["tok"]})
    assert r.status_code == 302
    with app.app_context():
        d = Document.query.filter_by(matter_id=S["mid"], folder="Depositions").order_by(Document.id.desc()).first()
        assert d and d.mime == "application/pdf" and d.name.startswith("Deposition summary Jane Holloway")
    text, _ = _pdf_text(app, d.id)
    assert "Deponent: Jane Holloway" in text and "Depo. Tr. 12:6" in text and "Edited summary." in text
    assert "POSSIBLE CONTRADICTIONS" in text and "Draft prepared for attorney review" in text
    r = client.get(f"/discovery/depositions?matter_id={S['mid']}")
    assert b"Jane Holloway" in r.data and b"holloway transcript.txt" in r.data


def test_deposition_multi_chunk_condenses(app, client, monkeypatch):
    """A long transcript makes one call per chunk plus a condensing call; key testimony from every chunk is kept."""
    from app.extensions import db
    from app.models import DepositionSummary, User
    from app.blueprints.documents import store_bytes
    from app import llm
    pages = " ".join(f"Page {p} " + " ".join(f"{p}:{l} the witness said thing {l} on page {p}" for l in range(1, 26))
                     for p in range(1, 30))
    with app.app_context():
        u = User.query.first()
        big, err = store_bytes(S["mid"], "long transcript.txt", pages.encode(), user_id=u.id)
        assert err is None
        db.session.commit()
        big_id = big.id
    n = {"i": 0}

    def fake(prompt, **kw):
        n["i"] += 1
        if kw.get("kind") == "deposition_condense":
            return json.dumps({"summary": "Condensed summary of every part."})
        return json.dumps({"summary": f"Part {n['i']} summary.",
                           "key_testimony": [{"page": n["i"], "line": 1, "quote": f"quote {n['i']}", "topic": "t"}],
                           "contradictions": []})
    monkeypatch.setattr(llm, "complete", fake)
    r = client.post(f"/discovery/depositions/new?matter_id={S['mid']}", data={
        "_csrf": S["tok"], "document_id": big_id, "deponent": "Long Witness"})
    did = int(r.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    with app.app_context():
        dep = DepositionSummary.query.get(did)
        key = json.loads(dep.key_testimony_json)
        assert dep.summary_text == "Condensed summary of every part."
        assert len(key) >= 3 and n["i"] == len(key) + 1


def test_deposition_without_model_still_creates_record(app, client, no_keys):
    from app.models import DepositionSummary
    r = client.post(f"/discovery/depositions/new?matter_id={S['mid']}", data={
        "_csrf": S["tok"], "document_id": S["transcript_doc"], "deponent": "No Model Witness"},
        follow_redirects=True)
    assert r.status_code == 200
    assert b"created without an AI summary" in r.data
    assert b"AI model is not configured" in r.data and b"No key testimony recorded" in r.data
    with app.app_context():
        dep = DepositionSummary.query.filter_by(deponent="No Model Witness").first()
        assert dep and dep.key_testimony_json == "[]" and "not configured" in dep.summary_text
    # transcript pick is validated
    r = client.post(f"/discovery/depositions/new?matter_id={S['mid']}", data={
        "_csrf": S["tok"], "document_id": "999999", "deponent": "X"})
    assert r.status_code == 200 and b"Pick the transcript" in r.data


# ---------------------------------------------------------------- wiring
def test_matter_page_has_buttons_and_feature_map_lists_it(app, client):
    r = client.get(f"/matters/{S['mid']}")
    assert r.status_code == 200
    assert f'href="/discovery?matter_id={S["mid"]}"'.encode() in r.data
    assert f'href="/discovery/depositions?matter_id={S["mid"]}"'.encode() in r.data
    from app.feature_map import FEATURE_MAP
    eve = next(c for c in FEATURE_MAP if c["company"] == "Eve Legal")
    routes = {f[1] for f in eve["features"]}
    assert "/discovery" in routes and "/discovery/depositions" in routes
    r = client.get("/discovery")
    assert r.status_code == 200 and b"Discovery" in r.data
    r = client.get("/discovery/depositions")
    assert r.status_code == 200
    r = client.get(f"/discovery/new?matter_id={S['mid']}")
    assert r.status_code == 200 and b"personal injury starter set" in r.data and b"served interrogatories.txt" in r.data
    r = client.get(f"/discovery/depositions/new?matter_id={S['mid']}")
    assert r.status_code == 200 and b"holloway transcript.txt" in r.data
    assert client.get("/discovery/new?matter_id=999999").status_code == 404
    assert client.get("/discovery/999999").status_code == 404
