"""Phase 5, Agent L: records into a chronology, case overview, narrative demand (app/blueprints/records.py).

Own SQLite file (data/test_phase5_l.db) seeded by seed.py, own UPLOAD_DIR and PDF_DIR. No network: the model is
monkeypatched at app.llm.complete_json (and app.llm.complete for the raw path) and the fixture blanks both API keys
in app.config so a key in the developer's shell cannot leak into a real call.
Run: .venv/bin/python -m pytest tests/test_phase5_l.py -q
"""
import io
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
DB_PATH = os.path.join(ROOT, "data", "test_phase5_l.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_phase5_l")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_phase5_l")

from tests.helpers import login  # noqa: E402

DOL = date.today() - timedelta(days=120)
MATTER_NUMBER = "M-REC01"
CLIENT_NAME = "Testa Recordsclient"
S = {}

RECORDS_TEXT = (
    "Page 1 of 3 CENTRAL TEXAS ER Patient: Testa Recordsclient. Date of service 03/12/2025. "
    "Chief complaint neck pain after MVC. Diagnosis: cervical strain. CT cervical spine negative. Charges $1,850.00. "
    "Page 2 of 3 Central Texas ER discharge instructions, follow up with primary care. "
    "Page 3 of 3 LONE STAR PHYSICAL THERAPY visit 03/20/2025 initial evaluation, cervical ROM limited. Charges $250.00. "
    "Lone Star Physical Therapy visit 03/27/2025 therapeutic exercise. Charges $175.00."
)

MODEL_ENTRIES = {"entries": [
    {"date": "2025-03-12", "provider": "Central Texas ER", "visit_type": "ER", "diagnosis": "Cervical strain",
     "procedure": "CT cervical spine", "charges": "1,850.00", "page_ref": "1", "notes": ""},
    {"date": "03/20/2025", "provider": "Lone Star Physical Therapy", "visit_type": "PT",
     "diagnosis": "Limited cervical ROM", "procedure": "Initial evaluation", "charges": "250", "page_ref": "3",
     "notes": ""},
    {"date": "March 27, 2025", "provider": "Lone Star Physical Therapy", "visit_type": "PT", "diagnosis": "",
     "procedure": "Therapeutic exercise", "charges": "$175.00", "page_ref": "3", "notes": "last visit in the record"},
]}
MODEL_OVERVIEW = {"facts": "Rear-ended at a red light on the date of loss.", "parties": "Client Testa Recordsclient; insurer Acme Mutual.",
                  "injuries_and_treatment": "Cervical strain treated at Central Texas ER then physical therapy.",
                  "liability": "The other driver was cited.", "damages_summary": "Specials of $2,275.00 so far.",
                  "open_questions": ["Are the PT records complete?", "Confirm policy limits."]}
MODEL_DEMAND = {"intro": "This office represents Testa Recordsclient in her claim against your insured.",
                "facts": "On the date of loss our client was stopped at a red light when your insured struck her from behind.",
                "liability": "Your insured was cited at the scene and liability is clear.",
                "injuries_and_treatment": "Our client was seen at Central Texas ER and completed physical therapy at Lone Star.",
                "damages": "Medical specials total $2,275.00.", "demand_and_deadline": "Our client demands $45,000.00 within 30 days.",
                "closing": "We look forward to your response."}


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
        from app.models import Contact, Matter, User, Firm, PiCase, MedicalProvider, Lien
        u = User.query.first()
        c = Contact(first_name="Testa", last_name="Recordsclient", email="records@example.test", is_client=True,
                    address="9 Test Way\nAustin, TX 78701")
        db.session.add(c)
        db.session.flush()
        m = Matter(number=MATTER_NUMBER, client_id=c.id, name="TEST Records v. Driver", practice_area="Personal Injury",
                   billing_type="contingency", responsible_user_id=u.id, status="open")
        db.session.add(m)
        db.session.flush()
        db.session.add(PiCase(matter_id=m.id, stage="records", date_of_loss=DOL, incident_type="auto",
                              incident_description="Rear-ended at a red light.", injuries="Cervical strain.",
                              liability_notes="Other driver cited.", insurer="Acme Mutual", claim_number="CL-900",
                              adjuster_name="Pat Adjuster", policy_limits_cents=3000000, demand_amount_cents=4500000))
        # one provider already on the matter with a typed total; extraction should link to it, not duplicate it
        db.session.add(MedicalProvider(matter_id=m.id, name="Central Texas ER", specialty="Emergency",
                                       total_billed_cents=99900))
        db.session.add(Lien(matter_id=m.id, holder="Acme Health Plan", type="health_plan", original_cents=120000))
        Firm.get().ai_enabled = True
        db.session.commit()
        S["mid"] = m.id
    yield a


@pytest.fixture
def owner(app):
    c = app.test_client()
    tok = login(c)
    return c, tok


@pytest.fixture
def no_keys(monkeypatch):
    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "LLM_ENABLED", "LLM_DAILY_CAP", "AI_DAILY_CAP_CENTS"):
        monkeypatch.delenv(k, raising=False)


def _fake_json(monkeypatch, by_kind):
    """app.llm.complete_json answers from by_kind[kind]; a value that is an exception is raised."""
    from app import llm
    calls = []

    def fake(prompt, schema, **kw):
        calls.append((prompt, kw))
        v = by_kind[kw.get("kind")]
        if isinstance(v, Exception):
            raise v
        return json.loads(json.dumps(v))
    monkeypatch.setattr(llm, "complete_json", fake)
    return calls


def _unavailable(monkeypatch):
    from app import llm

    def fake(prompt, schema=None, **kw):
        raise llm.LLMUnavailable("No AI key is configured. Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY to use this.")
    monkeypatch.setattr(llm, "complete_json", fake)
    monkeypatch.setattr(llm, "complete", fake)


def _entries(app):
    from app.models import ChronologyEntry
    with app.app_context():
        return ChronologyEntry.query.filter_by(matter_id=S["mid"]).order_by(ChronologyEntry.date, ChronologyEntry.id).all()


def _pdf_text(path):
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)


# ---------------------------------------------------------------- chronology page + extraction
def test_upload_records_and_extract(app, owner, monkeypatch, no_keys):
    c, tok = owner
    mid = S["mid"]
    r = c.post("/documents/upload", data={"_csrf": tok, "matter_id": mid, "folder": "Medical records",
                                          "file": (io.BytesIO(RECORDS_TEXT.encode()), "er-records.txt")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    from app.models import Document, MedicalProvider
    with app.app_context():
        doc = Document.query.filter_by(matter_id=mid, name="er-records.txt").first()
        assert doc is not None and "Central Texas ER" in doc.extracted_text
        S["doc_id"] = doc.id
    r = c.get(f"/records/{mid}")
    assert r.status_code == 200 and b"er-records.txt" in r.data and b"No entries yet" in r.data

    calls = _fake_json(monkeypatch, {"records_extract": MODEL_ENTRIES})
    r = c.post(f"/records/{mid}/extract", data={"_csrf": tok, "document_id": S["doc_id"]}, follow_redirects=True)
    assert r.status_code == 200
    html = r.data.decode()
    assert "Added 3 unconfirmed entries" in html and "Lone Star Physical Therapy" in html
    assert len(calls) == 1 and "[Page 1]" in calls[0][0] and "[Page 3]" in calls[0][0]
    assert calls[0][1]["kind"] == "records_extract" and calls[0][1]["entity_id"] == S["doc_id"]
    entries = _entries(app)
    assert len(entries) == 3 and all(e.origin == "ai" and not e.confirmed and e.source_document_id == S["doc_id"]
                                     for e in entries)
    assert [e.date for e in entries] == [date(2025, 3, 12), date(2025, 3, 20), date(2025, 3, 27)]
    assert [e.charges_cents for e in entries] == [185000, 25000, 17500]
    assert entries[0].page_ref == "1" and entries[0].visit_type == "ER"
    with app.app_context():
        provs = MedicalProvider.query.filter_by(matter_id=mid).order_by(MedicalProvider.id).all()
        assert [p.name for p in provs] == ["Central Texas ER", "Lone Star Physical Therapy"]
        assert provs[1].notes == "added from records extraction"
        assert entries[0].provider_id == provs[0].id and entries[1].provider_id == provs[1].id
        assert provs[0].total_billed_cents == 99900  # not touched until recalc
    assert html.count("unconfirmed</span>") == 3
    assert "draft for attorney review and may contain errors" in html


def test_rerun_dedupes(app, owner, monkeypatch, no_keys):
    c, tok = owner
    mid = S["mid"]
    _fake_json(monkeypatch, {"records_extract": MODEL_ENTRIES})
    r = c.post(f"/records/{mid}/extract", data={"_csrf": tok, "document_id": S["doc_id"]}, follow_redirects=True)
    assert b"Added 0 unconfirmed entries" in r.data and b"3 already on the chronology" in r.data
    assert len(_entries(app)) == 3


def test_hand_edits_confirm_and_recalc(app, owner):
    c, tok = owner
    mid = S["mid"]
    from app.models import MedicalProvider
    with app.app_context():
        pt = MedicalProvider.query.filter_by(matter_id=mid, name="Lone Star Physical Therapy").first().id
    # add by hand (confirmed, linked to PT)
    r = c.post(f"/records/{mid}/entries", data={"_csrf": tok, "date": "2025-04-03", "provider_id": pt,
                                               "visit_type": "PT", "procedure": "Therapeutic exercise",
                                               "charges": "175.00"}, follow_redirects=True)
    assert r.status_code == 200 and b"Entry added." in r.data
    entries = _entries(app)
    assert len(entries) == 4 and entries[-1].confirmed and entries[-1].origin == "user" and entries[-1].provider_id == pt
    # edit one
    eid = entries[0].id
    r = c.get(f"/records/{mid}/entries/{eid}/edit")
    assert r.status_code == 200 and b"unconfirmed" in r.data
    r = c.post(f"/records/{mid}/entries/{eid}/edit", data={"_csrf": tok, "date": "2025-03-12", "provider_id": entries[0].provider_id,
                                                          "provider_name": "Central Texas ER", "visit_type": "ER",
                                                          "diagnosis": "Cervical strain", "procedure": "CT cervical spine",
                                                          "charges": "1,900.00", "page_ref": "1", "confirm": "1"})
    assert r.status_code == 302
    e0 = _entries(app)[0]
    assert e0.charges_cents == 190000 and e0.confirmed
    # recalc before the rest are confirmed: only the ER (one confirmed linked row) and PT (the hand row) change
    r = c.post(f"/records/{mid}/recalc-specials", data={"_csrf": tok}, follow_redirects=True)
    assert b"Specials recalculated for 2 providers" in r.data
    with app.app_context():
        er = MedicalProvider.query.filter_by(matter_id=mid, name="Central Texas ER").first()
        assert er.total_billed_cents == 190000 and er.first_visit_on == date(2025, 3, 12)
        assert MedicalProvider.query.get(pt).total_billed_cents == 17500
    # confirm all, recalc again
    r = c.post(f"/records/{mid}/confirm-all", data={"_csrf": tok}, follow_redirects=True)
    assert b"Confirmed 2 entries" in r.data
    assert all(e.confirmed for e in _entries(app))
    c.post(f"/records/{mid}/recalc-specials", data={"_csrf": tok})
    with app.app_context():
        assert MedicalProvider.query.get(pt).total_billed_cents == 25000 + 17500 + 17500
        assert MedicalProvider.query.get(pt).first_visit_on == date(2025, 3, 20)
        assert MedicalProvider.query.get(pt).last_visit_on == date(2025, 4, 3)
        S["specials"] = 190000 + 25000 + 17500 + 17500
    # unlink then link through the select
    eid = _entries(app)[1].id
    r = c.post(f"/records/{mid}/entries/{eid}/link", data={"_csrf": tok, "provider_id": ""}, follow_redirects=True)
    assert b"Provider link removed." in r.data
    r = c.get(f"/records/{mid}")
    assert b"link to provider" in r.data
    r = c.post(f"/records/{mid}/entries/{eid}/link", data={"_csrf": tok, "provider_id": pt}, follow_redirects=True)
    assert b"Linked to Lone Star Physical Therapy." in r.data
    # delete the hand entry
    last = _entries(app)[-1].id
    r = c.post(f"/records/{mid}/entries/{last}/delete", data={"_csrf": tok}, follow_redirects=True)
    assert b"Entry deleted." in r.data and len(_entries(app)) == 3
    # the PI case page shows the counts and the extract select
    r = c.get(f"/pi/{mid}")
    assert r.status_code == 200
    html = r.data.decode()
    assert "3 entries" in html and "0 unconfirmed" in html and 'action="/records/%d/extract"' % mid in html
    assert "er-records.txt" in html and "Draft narrative demand" in html


def test_regex_fallback_when_model_unavailable(app, owner, monkeypatch, no_keys):
    c, tok = owner
    mid = S["mid"]
    text = ("Page 1 HILL COUNTRY IMAGING MRI cervical spine performed on 04/15/2025 for Testa Recordsclient. "
            "Total charges: $2,400.00. Impression: C5-6 disc bulge. Page 2 Central Texas ER follow up 04/20/2025 no charge.")
    r = c.post("/documents/upload", data={"_csrf": tok, "matter_id": mid, "folder": "Medical records",
                                          "file": (io.BytesIO(text.encode()), "mri.txt")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    from app.models import Document, MedicalProvider
    with app.app_context():
        doc_id = Document.query.filter_by(matter_id=mid, name="mri.txt").first().id
        # the imaging centre is on the matter already (typed by hand), so the scan can find it
        db_prov = MedicalProvider(matter_id=mid, name="Hill Country Imaging", specialty="Radiology")
        from app.extensions import db
        db.session.add(db_prov)
        db.session.commit()
    _unavailable(monkeypatch)
    r = c.post(f"/records/{mid}/extract", data={"_csrf": tok, "document_id": doc_id}, follow_redirects=True)
    html = r.data.decode()
    assert "The AI was not available" in html and "plain text scan" in html
    entries = _entries(app)
    new = [e for e in entries if e.source_document_id == doc_id]
    assert len(new) == 2
    mri = next(e for e in new if e.provider_name == "Hill Country Imaging")
    assert mri.date == date(2025, 4, 15) and mri.charges_cents == 240000 and mri.page_ref == "1" and not mri.confirmed
    assert "fallback text scan" in mri.notes and mri.provider_id is not None
    er = next(e for e in new if e.provider_name == "Central Texas ER")
    assert er.date == date(2025, 4, 20) and er.charges_cents == 0 and er.page_ref == "2"
    # clean up so the totals used later stay predictable
    for e in new:
        c.post(f"/records/{mid}/entries/{e.id}/delete", data={"_csrf": tok})
    assert len(_entries(app)) == 3


def test_extract_refuses_document_without_text(app, owner, monkeypatch, no_keys):
    c, tok = owner
    mid = S["mid"]
    r = c.post("/documents/upload", data={"_csrf": tok, "matter_id": mid,
                                          "file": (io.BytesIO(b"\x00\x01\x02binary"), "scan.bin")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    from app.models import Document
    with app.app_context():
        doc_id = Document.query.filter_by(matter_id=mid, name="scan.bin").first().id
    _fake_json(monkeypatch, {"records_extract": MODEL_ENTRIES})
    r = c.post(f"/records/{mid}/extract", data={"_csrf": tok, "document_id": doc_id}, follow_redirects=True)
    assert b"has no readable text" in r.data and len(_entries(app)) == 3
    # a document from another matter is refused
    from app.models import Matter
    with app.app_context():
        other = Matter.query.filter(Matter.id != mid).first().id
    r = c.post("/documents/upload", data={"_csrf": tok, "matter_id": other,
                                          "file": (io.BytesIO(b"Other matter records 01/02/2025 $10.00"), "other.txt")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        other_doc = Document.query.filter_by(matter_id=other, name="other.txt").first().id
    r = c.post(f"/records/{mid}/extract", data={"_csrf": tok, "document_id": other_doc}, follow_redirects=True)
    assert b"Pick one of this matter" in r.data and len(_entries(app)) == 3


def test_chunking_keeps_page_markers():
    from app.blueprints.records import chunk_text, parse_any_date
    text = " ".join(f"Page {n} of 4 " + ("word " * 1200) for n in range(1, 5))
    chunks = chunk_text(text, size=9000)
    assert len(chunks) >= 3 and all(len(ch) <= 9200 for ch in chunks)
    assert chunks[0].startswith("[Page 1]") and any("[Page 4]" in ch for ch in chunks)
    plain = chunk_text("x" * 20000 + " tail", size=9000)
    assert len(plain) == 3 and "[Page" not in plain[0]
    ff = chunk_text("first\fsecond\fthird", size=9000)
    assert ff == ["[Page 1]\nfirst\n[Page 2]\nsecond\n[Page 3]\nthird\n"]
    assert parse_any_date("3/5/24") == date(2024, 3, 5) and parse_any_date("Mar 5, 2024") == date(2024, 3, 5)
    assert parse_any_date("2024-03-05") == date(2024, 3, 5) and parse_any_date("sometime") is None


# ---------------------------------------------------------------- overview
def test_overview_generated_and_saved_as_note(app, owner, monkeypatch, no_keys):
    c, tok = owner
    mid = S["mid"]
    from app.extensions import db
    from app.models import Note, PiCase
    with app.app_context():
        db.session.add(Note(matter_id=mid, user_id=1, body="Client called: still having headaches."))
        db.session.commit()
    calls = _fake_json(monkeypatch, {"case_overview": MODEL_OVERVIEW})
    r = c.post(f"/records/{mid}/overview", data={"_csrf": tok})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/pi/{mid}#overview")
    assert len(calls) == 1
    prompt, kw = calls[0]
    assert kw["kind"] == "case_overview" and kw["entity_id"] == mid
    assert "Acme Mutual" in prompt and "Central Texas ER" in prompt and "still having headaches" in prompt
    assert "er-records.txt" in prompt and "Confirmed treatment chronology" in prompt and "Acme Health Plan" in prompt
    assert len(prompt) <= 12000
    with app.app_context():
        case = PiCase.query.filter_by(matter_id=mid).first()
        assert case.overview_at is not None
        txt = case.overview_text
        assert txt.startswith("Facts\nRear-ended") and "Open questions\n- Are the PT records complete?" in txt
        assert "Written by the AI" in txt and "er-records.txt" in txt
    r = c.get(f"/pi/{mid}")
    html = r.data.decode()
    assert "Rear-ended at a red light on the date of loss." in html and "Regenerate" in html and "Save as note" in html
    assert html.count("draft for attorney review and may contain errors") >= 1
    r = c.post(f"/records/{mid}/overview/note", data={"_csrf": tok}, follow_redirects=True)
    assert b"Overview saved as a note" in r.data
    with app.app_context():
        n = Note.query.filter_by(matter_id=mid).order_by(Note.id.desc()).first()
        assert n.body.startswith("Case overview (") and "Open questions" in n.body and "attorney review" in n.body


def test_overview_plain_fallback(app, owner, monkeypatch, no_keys):
    c, tok = owner
    mid = S["mid"]
    _unavailable(monkeypatch)
    r = c.post(f"/records/{mid}/overview", data={"_csrf": tok}, follow_redirects=True)
    assert b"assembled from the case data without the model" in r.data
    from app.models import PiCase
    with app.app_context():
        txt = PiCase.query.filter_by(matter_id=mid).first().overview_text
    assert "Facts\nDate of loss:" in txt and "Rear-ended at a red light." in txt
    assert "Parties\nClient: Testa Recordsclient." in txt and "Insurer: Acme Mutual, claim CL-900, adjuster Pat Adjuster." in txt
    assert "Liability\nOther driver cited." in txt
    assert "Medical specials: $2,500.00 across 3 providers." in txt and "Liens payable: $1,200.00" in txt
    assert "Policy limits: $30,000.00" in txt and "Demand: $45,000.00" in txt
    assert "Open questions\n" in txt and "Records not received from Central Texas ER." in txt
    assert "Assembled from the structured data without the model" in txt


# ---------------------------------------------------------------- narrative demand
def test_demand_draft_generated_and_saved_as_pdf(app, owner, monkeypatch, no_keys):
    c, tok = owner
    mid = S["mid"]
    # a style example on another matter, tagged style-example, is offered to the model
    from app.models import Matter
    with app.app_context():
        other = Matter.query.filter(Matter.id != mid).first().id
    r = c.post("/documents/upload", data={"_csrf": tok, "matter_id": other, "tags": "Style-Example, demand",
                                          "file": (io.BytesIO(b"Dear Ms. Adjuster: We write on behalf of our client, and we do so plainly. " * 20),
                                                   "old-demand.txt")}, content_type="multipart/form-data")
    assert r.status_code == 302
    r = c.get(f"/records/{mid}/demand-draft")
    assert r.status_code == 200 and b"No draft yet" in r.data and b"old-demand.txt" in r.data
    calls = _fake_json(monkeypatch, {"demand_draft": MODEL_DEMAND})
    r = c.post(f"/records/{mid}/demand-draft", data={"_csrf": tok, "demand_amount": "45,000.00",
                                                     "demand_style_notes": "Firm but courteous."})
    assert r.status_code == 302
    assert len(calls) == 1
    prompt, kw = calls[0]
    assert kw["kind"] == "demand_draft"
    assert "Firm but courteous." in prompt and "old-demand.txt" in prompt and "we do so plainly" in prompt
    assert "$45,000.00" in prompt and "policy limits of $30,000.00" in prompt and "Total specials: $2,500.00" in prompt
    assert len(prompt) <= 12000
    r = c.get(f"/records/{mid}/demand-draft")
    html = r.data.decode()
    assert "Current draft" in html and 'badge overdue">template text' not in html
    for k, v in MODEL_DEMAND.items():
        assert f'name="{k}"' in html and v in html
    assert "draft for attorney review and may contain errors" in html and "following old-demand.txt" in html
    from app.models import PiCase
    with app.app_context():
        case = PiCase.query.filter_by(matter_id=mid).first()
        assert case.demand_style_notes == "Firm but courteous." and case.demand_amount_cents == 4500000
    # save with an edit
    form = {"_csrf": tok, "demand_amount": "45,000.00"}
    form.update(MODEL_DEMAND)
    form["closing"] = "We look forward to your response within thirty days."
    r = c.post(f"/records/{mid}/demand-draft/save", data=form)
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/pi/{mid}#demand")
    from app.models import Document
    with app.app_context():
        doc = Document.query.filter_by(matter_id=mid, folder="Demand").order_by(Document.id.desc()).first()
        assert doc is not None and doc.name == f"Narrative demand - {MATTER_NUMBER}.pdf" and doc.mime == "application/pdf"
        full = os.path.join(UPLOAD_DIR, doc.path)
        assert os.path.isfile(full) and open(full, "rb").read(5) == b"%PDF-"
        text = _pdf_text(full)
        S["demand_doc_id"] = doc.id
    assert CLIENT_NAME in text and "$2,500.00" in text and "Total medical specials" in text
    assert "within thirty days" in text and "Draft prepared for attorney review" in text
    assert "Acme Mutual" in text and "Pat Adjuster" in text and "Lone Star Physical Therapy" in text
    # the PI page links the saved letter and the draft
    r = c.get(f"/pi/{mid}")
    html = r.data.decode()
    assert f"/documents/{S['demand_doc_id']}/download" in html and "Open narrative demand draft" in html


def test_demand_template_fallback(app, owner, monkeypatch, no_keys):
    c, tok = owner
    mid = S["mid"]
    _unavailable(monkeypatch)
    r = c.post(f"/records/{mid}/demand-draft", data={"_csrf": tok, "demand_amount": "45000"}, follow_redirects=True)
    html = r.data.decode()
    assert "A template letter was filled in" in html and "template text" in html
    assert "[Template text. The AI was not available" in html
    assert "our client demands $45,000.00" in html and "Central Texas ER: $1,900.00." in html
    assert "Rear-ended at a red light." in html and "Other driver cited." in html
    assert "March 12, 2025, Central Texas ER, ER, Cervical strain, CT cervical spine." in html
    # missing amount is refused
    from app.extensions import db
    from app.models import PiCase
    with app.app_context():
        case = PiCase.query.filter_by(matter_id=mid).first()
        case.demand_amount_cents = 0
        db.session.commit()
    r = c.post(f"/records/{mid}/demand-draft", data={"_csrf": tok, "demand_amount": ""}, follow_redirects=True)
    assert b"Enter the demand amount first." in r.data


def test_records_page_starts_case_on_plain_matter(app, owner):
    c, tok = owner
    from app.models import Matter, PiCase
    with app.app_context():
        other = Matter.query.filter(Matter.id != S["mid"], Matter.number == "M-1001").first().id
        PiCase.query.filter_by(matter_id=other).delete()
        from app.extensions import db
        db.session.commit()
    r = c.get(f"/records/{other}")
    assert r.status_code == 200
    with app.app_context():
        assert PiCase.query.filter_by(matter_id=other).count() == 1


def test_feature_map_and_no_em_dashes(app):
    from app.feature_map import FEATURE_MAP
    eve = next(g for g in FEATURE_MAP if g["company"] == "Eve Legal")
    names = " | ".join(f[0].lower() for f in eve["features"])
    assert "chronology" in names and "overview" in names and "narrative demand" in names
    for path in ("app/blueprints/records.py", "app/templates/records/chronology.html",
                 "app/templates/records/entry_form.html", "app/templates/records/demand_draft.html",
                 "app/templates/pi/case.html", "tests/test_phase5_l.py"):
        with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
            body = fh.read()
        assert chr(8212) not in body, path  # em-dash
        prose = re.sub(r"-{3,}", "", body)
        prose = prose.replace("var(" + "-" * 2, "var(")  # CSS custom properties are not prose
        assert "-" * 2 not in prose, path
