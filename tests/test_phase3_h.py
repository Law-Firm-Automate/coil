"""Phase 3, Agent H: AI features (app/llm.py + /ai/*) and the CRM pipeline with follow-up sequences.

Runs seed.py against its own SQLite file (data/test_phase3_h.db). No network: every model call is either
monkeypatched at app.llm.complete (feature tests) or at app.llm._call_provider (AiRun / cap tests), and the
fixture blanks both API keys in app.config so a key in the developer's shell cannot leak into a real call.
"""
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402

TEST_DB = os.path.join(ROOT, "data", "test_phase3_h.db")
UPLOAD_DIR = os.path.join(ROOT, "data", "test_phase3_h_uploads")
S = {}


@pytest.fixture(scope="module")
def app():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{TEST_DB}")
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT, env=env)
    from app import create_app
    application = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{TEST_DB}", "TESTING": True, "SMTP_HOST": "",
                              "UPLOAD_DIR": UPLOAD_DIR, "OPENROUTER_API_KEY": "", "ANTHROPIC_API_KEY": "",
                              "BOOKING_URL": "https://book.example.test/consult"})
    yield application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    S["tok"] = login(c)
    return c


@pytest.fixture
def no_keys(monkeypatch):
    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "LLM_ENABLED", "LLM_DAILY_CAP", "AI_DAILY_CAP_CENTS"):
        monkeypatch.delenv(k, raising=False)


def _models():
    from app.extensions import db
    from app import models
    return db, models


def _set_ai(app, on):
    db, M = _models()
    with app.app_context():
        f = M.Firm.get()
        f.ai_enabled = on
        db.session.commit()


def _fake_complete(monkeypatch, payload):
    """Replace app.llm.complete with a canned answer (dict -> JSON text)."""
    from app import llm
    calls = []

    def fake(prompt, **kw):
        calls.append((prompt, kw))
        return json.dumps(payload) if not isinstance(payload, str) else payload
    monkeypatch.setattr(llm, "complete", fake)
    return calls


# ---------------------------------------------------------------- llm.py
def test_llm_gates_raise_unavailable(app, no_keys):
    from app import llm
    _set_ai(app, False)
    with app.app_context():
        with pytest.raises(llm.LLMUnavailable) as e:
            llm.complete("hi")
        assert "turned off" in str(e.value)
    _set_ai(app, True)
    with app.app_context():
        with pytest.raises(llm.LLMUnavailable) as e:
            llm.complete("hi")
        assert "No AI key" in str(e.value)
        st = llm.status()
        assert st["available"] is False and st["provider"] is None and st["firm_on"] is True


@pytest.mark.parametrize("model,expected", [
    ("claude-haiku-4-5", True), ("anthropic/claude-haiku-4.5", True), ("claude-sonnet-4-6", True),
    ("anthropic/claude-sonnet-4.6", True), ("claude-sonnet-5", False), ("anthropic/claude-sonnet-5", False),
    ("claude-opus-4-7", False), ("anthropic/claude-opus-4.8", False), ("claude-opus-5", False),
    ("claude-fable-5-1", False), ("anthropic/claude-mythos-1", False),
])
def test_accepts_sampling_both_id_styles(model, expected):
    from app.llm import accepts_sampling
    assert accepts_sampling(model) is expected


def test_airun_recorded_with_cost_then_daily_cap(app, no_keys, monkeypatch):
    from app import llm
    db, M = _models()
    _set_ai(app, True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    seen = {}

    def fake_provider(prov, model, prompt, system, max_tokens, schema, temperature, effort):
        seen.update(prov=prov, model=model, prompt=prompt, schema=schema)
        return "Polished text.", 1000, 200
    monkeypatch.setattr(llm, "_call_provider", fake_provider)
    with app.app_context():
        before = M.AiRun.query.count()
        assert llm.provider() == "anthropic"
        out = llm.complete("x" * 20000, kind="unit", entity="matter", entity_id=1, user_id=1)
        assert out == "Polished text."
        assert seen["prov"] == "anthropic" and seen["model"] == "claude-haiku-4-5"
        assert len(seen["prompt"]) < 20000 and seen["prompt"].endswith(llm.TRUNCATION_NOTE)
        run = M.AiRun.query.order_by(M.AiRun.id.desc()).first()
        assert M.AiRun.query.count() == before + 1
        assert run.ok and run.kind == "unit" and run.entity == "matter" and run.entity_id == 1 and run.user_id == 1
        assert run.model == "claude-haiku-4-5" and run.output_chars == len("Polished text.")
        assert run.cost_cents == 1  # 1000 in + 200 out on Haiku is well under a cent, rounded up
        assert run.prompt_chars == len(seen["prompt"])
        # JSON helper parses
        monkeypatch.setattr(llm, "_call_provider", lambda *a: ('```json\n{"a": 1}\n```', 10, 5))
        assert llm.complete_json("q", {"type": "object"}) == {"a": 1}
        # provider failure: recorded ok=False, raised as LLMUnavailable

        def boom(*a):
            raise RuntimeError("socket closed")
        monkeypatch.setattr(llm, "_call_provider", boom)
        with pytest.raises(llm.LLMUnavailable):
            llm.complete("q")
        assert M.AiRun.query.order_by(M.AiRun.id.desc()).first().ok is False
        # spend cap: spent >= cap -> unavailable before any call
        monkeypatch.setattr(llm, "_call_provider", fake_provider)
        monkeypatch.setenv("AI_DAILY_CAP_CENTS", "1")
        with pytest.raises(llm.LLMUnavailable) as e:
            llm.complete("q")
        assert "budget" in str(e.value)
        monkeypatch.delenv("AI_DAILY_CAP_CENTS")
        # call cap
        monkeypatch.setenv("LLM_DAILY_CAP", "1")
        with pytest.raises(llm.LLMUnavailable) as e:
            llm.complete("q")
        assert "limit" in str(e.value)
        monkeypatch.delenv("LLM_DAILY_CAP")
        # kill switch
        monkeypatch.setenv("LLM_ENABLED", "off")
        n = M.AiRun.query.count()
        with pytest.raises(llm.LLMUnavailable):
            llm.complete("q")
        assert M.AiRun.query.count() == n  # nothing recorded when switched off


# ---------------------------------------------------------------- 1. invoice polish
def test_invoice_polish_before_after_and_apply_on_draft_only(app, client, monkeypatch):
    db, M = _models()
    _set_ai(app, True)
    with app.app_context():
        m = M.Matter.query.filter_by(number="M-1002").first()
        inv = M.Invoice(number="INV-H-1", matter_id=m.id, client_id=m.client_id, kind="hourly", status="draft",
                        issued_on=date.today(), due_on=date.today() + timedelta(days=30))
        db.session.add(inv)
        db.session.flush()
        l1 = M.InvoiceLine(invoice_id=inv.id, kind="time", date=date.today(), description="tc w/ client re: settlement",
                           quantity=0.5, unit_cents=35000, amount_cents=17500, sort=0)
        l2 = M.InvoiceLine(invoice_id=inv.id, kind="time", date=date.today(), description="draft demand ltr",
                           quantity=1.5, unit_cents=35000, amount_cents=52500, sort=1)
        db.session.add_all([l1, l2])
        db.session.flush()
        inv.recalc()
        sent = M.Invoice(number="INV-H-2", matter_id=m.id, client_id=m.client_id, kind="hourly", status="sent",
                         issued_on=date.today(), due_on=date.today() + timedelta(days=30), sent_at=datetime.utcnow())
        db.session.add(sent)
        db.session.flush()
        l3 = M.InvoiceLine(invoice_id=sent.id, kind="time", date=date.today(), description="review file",
                           quantity=1.0, unit_cents=35000, amount_cents=35000, sort=0)
        db.session.add(l3)
        db.session.commit()
        S.update(inv=inv.id, l1=l1.id, l2=l2.id, sent=sent.id, l3=l3.id, total=inv.total_cents)
    calls = _fake_complete(monkeypatch, {"lines": [
        {"id": S["l1"], "text": "Telephone call with the client regarding settlement posture."},
        {"id": S["l2"], "text": "Drafted the demand letter to opposing counsel."}]})
    r = client.post(f"/ai/invoice/{S['inv']}/polish", data={"_csrf": S["tok"]})
    assert r.status_code == 200, r.data[:400]
    assert b"tc w/ client re: settlement" in r.data and b"Telephone call with the client" in r.data
    assert b"draft demand ltr" in r.data and b"Drafted the demand letter" in r.data
    assert b"Apply to the draft invoice" in r.data
    assert len(calls) == 1 and calls[0][1]["kind"] == "invoice_polish" and calls[0][1]["schema"]
    r = client.post(f"/ai/invoice/{S['inv']}/polish/apply", data={
        "_csrf": S["tok"], f"line_{S['l1']}": "Telephone call with the client regarding settlement posture.",
        f"line_{S['l2']}": "Drafted the demand letter to opposing counsel."})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(M.InvoiceLine, S["l1"]).description == "Telephone call with the client regarding settlement posture."
        assert db.session.get(M.InvoiceLine, S["l2"]).description == "Drafted the demand letter to opposing counsel."
        inv = db.session.get(M.Invoice, S["inv"])
        assert inv.total_cents == S["total"] and inv.lines[0].quantity == 0.5  # money and hours untouched
        assert M.AuditLog.query.filter_by(entity="invoice", entity_id=S["inv"], action="update").count() >= 1
    # sent invoice: before/after still shown, apply is refused
    _fake_complete(monkeypatch, {"lines": [{"id": S["l3"], "text": "Reviewed the file."}]})
    r = client.post(f"/ai/invoice/{S['sent']}/polish", data={"_csrf": S["tok"]})
    assert r.status_code == 200 and b"Reviewed the file." in r.data and b"cannot be changed" in r.data
    r = client.post(f"/ai/invoice/{S['sent']}/polish/apply", data={"_csrf": S["tok"], f"line_{S['l3']}": "Reviewed the file."})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(M.InvoiceLine, S["l3"]).description == "review file"
    # unavailable: calm notice, no crash
    from app import llm

    def raising(*a, **k):
        raise llm.LLMUnavailable("No AI key is configured.")
    monkeypatch.setattr(llm, "complete", raising)
    r = client.post(f"/ai/invoice/{S['inv']}/polish", data={"_csrf": S["tok"]})
    assert r.status_code == 200 and b"AI is not available right now" in r.data and b"No AI key" in r.data


# ---------------------------------------------------------------- 2. matter summary
def test_matter_summary_saves_note(app, client, monkeypatch):
    db, M = _models()
    with app.app_context():
        mid = M.Matter.query.filter_by(number="M-1002").first().id
        notes_before = M.Note.query.filter_by(matter_id=mid).count()
    calls = _fake_complete(monkeypatch, {"summary": "Bluebonnet Logistics sued Derek Holloway over a contract. "
                                                    "A demand letter is drafted and awaiting service.",
                                         "open_items": ["Serve demand letter", "Unbilled time on the matter"]})
    r = client.post(f"/ai/matter/{mid}/summary", data={"_csrf": S["tok"]})
    assert r.status_code == 200 and b"Bluebonnet Logistics sued Derek Holloway" in r.data
    assert b"Serve demand letter" in r.data and b"Save as a note" in r.data
    assert "Draft demand letter" in calls[0][0] and "M-1002" in calls[0][0]  # context carries time entries
    r = client.post(f"/ai/matter/{mid}/summary/save", data={"_csrf": S["tok"], "summary": "Bluebonnet summary text.",
                                                            "open_items": "Serve demand letter\nUnbilled time"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/matters/{mid}")
    with app.app_context():
        assert M.Note.query.filter_by(matter_id=mid).count() == notes_before + 1
        n = M.Note.query.filter_by(matter_id=mid).order_by(M.Note.id.desc()).first()
        # the saved summary is attorney work product, so it is marked [internal] and stays out of client drafts
        assert n.body.startswith("[internal] AI summary (") and "Bluebonnet summary text." in n.body
        assert "- Unbilled time" in n.body
        assert n.user_id is not None
    r = client.get(f"/matters/{mid}")
    assert r.status_code == 200 and b"AI summary" in r.data


# ---------------------------------------------------------------- 3. dates from a document
def test_document_dates_create_selected(app, client, monkeypatch):
    db, M = _models()
    with app.app_context():
        m = M.Matter.query.filter_by(number="M-1002").first()
        d = M.Document(matter_id=m.id, name="scheduling-order.txt", path="/nonexistent/scheduling-order.txt",
                       size=120, mime="text/plain", uploaded_by_id=1,
                       extracted_text="Answer due 2026-10-15. Deposition of plaintiff set for 2026-11-02 at 9am. "
                                      "Mediation on 2026-11-20. Trial setting 2027-01-12.")
        db.session.add(d)
        db.session.commit()
        did, mid = d.id, m.id
        tasks_before = M.Task.query.filter_by(matter_id=mid).count()
        events_before = M.CalendarEvent.query.filter_by(matter_id=mid).count()
    _fake_complete(monkeypatch, {"dates": [
        {"date": "2026-10-15", "description": "Answer due", "kind": "deadline"},
        {"date": "2026-11-02", "description": "Deposition of plaintiff", "kind": "court_date"},
        {"date": "2026-11-20", "description": "Mediation", "kind": "event"},
        {"date": "not a date", "description": "junk", "kind": "task"}]})
    r = client.post(f"/ai/document/{did}/dates", data={"_csrf": S["tok"]})
    assert r.status_code == 200 and b"Answer due" in r.data and b"Deposition of plaintiff" in r.data
    assert b'name="sel_2"' in r.data and b"junk" not in r.data
    r = client.post(f"/ai/document/{did}/dates/create", data={
        "_csrf": S["tok"], "n": "3",
        "sel_0": "1", "date_0": "2026-10-15", "desc_0": "Answer due", "kind_0": "deadline",
        "date_1": "2026-11-02", "desc_1": "Deposition of plaintiff", "kind_1": "court_date",  # not selected
        "sel_2": "1", "date_2": "2026-11-20", "desc_2": "Mediation", "kind_2": "event"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/matters/{mid}?tab=tasks")
    with app.app_context():
        assert M.Task.query.filter_by(matter_id=mid).count() == tasks_before + 1
        t = M.Task.query.filter_by(matter_id=mid, title="Answer due").first()
        assert t and t.kind == "deadline" and t.due_on == date(2026, 10, 15) and t.assignee_id == 1
        assert M.Task.query.filter_by(matter_id=mid, title="Deposition of plaintiff").count() == 0
        assert M.CalendarEvent.query.filter_by(matter_id=mid).count() == events_before + 1
        ev = M.CalendarEvent.query.filter_by(matter_id=mid, title="Mediation").first()
        assert ev and ev.starts_at.date() == date(2026, 11, 20)
    # re-submitting the same selection is a no-op (dedupe)
    r = client.post(f"/ai/document/{did}/dates/create", data={"_csrf": S["tok"], "n": "1", "sel_0": "1",
                                                              "date_0": "2026-10-15", "desc_0": "Answer due",
                                                              "kind_0": "deadline"})
    assert r.status_code == 302
    with app.app_context():
        assert M.Task.query.filter_by(matter_id=mid, title="Answer due").count() == 1
    # documents list carries the button
    r = client.get("/documents")
    assert r.status_code == 200 and f'formaction="/ai/document/{did}/dates"'.encode() in r.data


# ---------------------------------------------------------------- 4. natural-language search
def test_search_structured_then_plain_fallback(app, client, monkeypatch, no_keys):
    from app import llm
    _fake_complete(monkeypatch, {"entities": ["matters", "invoices"], "text": "Bluebonnet", "person": "",
                                 "status": "", "practice_area": "", "date_from": "", "date_to": "",
                                 "overdue": False, "unpaid": False, "min_amount_cents": 0})
    r = client.get("/ai/search?q=anything+about+bluebonnet")
    assert r.status_code == 200 and b"M-1002" in r.data and b"INV-H-1" in r.data and b"Understood as" in r.data
    assert b"plain search" not in r.data

    def raising(*a, **k):
        raise llm.LLMUnavailable("No AI key is configured. Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY to use this.")
    monkeypatch.setattr(llm, "complete", raising)
    r = client.get("/ai/search?q=Bluebonnet")
    assert r.status_code == 200 and b"plain search" in r.data and b"No AI key" in r.data
    assert b"M-1002" in r.data and b"Bluebonnet Logistics LLC" in r.data and b"scheduling-order.txt" not in r.data
    r = client.get("/ai/search?q=scheduling")
    assert r.status_code == 200 and b"scheduling-order.txt" in r.data  # documents searched by name and text
    # with AI off in Settings and no monkeypatch the page still answers
    monkeypatch.undo()
    _set_ai(app, False)
    r = client.get("/ai/search?q=Alvarez")
    assert r.status_code == 200 and b"M-1001" in r.data and b"Maria Alvarez" in r.data and b"turned off" in r.data
    r = client.get("/ai/search")
    assert r.status_code == 200
    r = client.get("/ai")
    assert r.status_code == 200 and b"Status" in r.data


# ---------------------------------------------------------------- CRM pipeline
def test_pipeline_stage_fields_convert_decline(app, client):
    db, M = _models()
    with app.app_context():
        a = M.IntakeLead(name="Hank Pipeline", email="hank@example.test", matter_type="Litigation", source="test")
        b = M.IntakeLead(name="Lucy Lost", email="lucy@example.test", matter_type="Family law", source="test")
        db.session.add_all([a, b])
        db.session.commit()
        aid, bid = a.id, b.id
        priya = M.IntakeLead.query.filter_by(name="Priya Natarajan").first().id
    r = client.get("/intake/pipeline")
    assert r.status_code == 200 and b"Hank Pipeline" in r.data and b'data-stage="consult_scheduled"' in r.data
    # plain form POST
    r = client.post(f"/intake/{aid}/stage", data={"_csrf": S["tok"], "stage": "proposal"})
    assert r.status_code == 302
    with app.app_context():
        l = db.session.get(M.IntakeLead, aid)
        assert l.stage == "proposal" and l.status == "contacted"
        assert M.AuditLog.query.filter_by(entity="intake_lead", entity_id=aid, action="stage").count() == 1
    # fetch-style POST answers JSON
    r = client.post(f"/intake/{aid}/stage", data={"stage": "consult_scheduled"},
                    headers={"X-CSRF-Token": S["tok"], "X-Requested-With": "fetch", "Accept": "application/json"})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "stage": "consult_scheduled", "status": "contacted"}
    r = client.post(f"/intake/{aid}/stage", data={"stage": "bogus"},
                    headers={"X-CSRF-Token": S["tok"], "X-Requested-With": "fetch"})
    assert r.status_code == 400 and r.get_json()["ok"] is False
    # lead fields
    r = client.post(f"/intake/{aid}/fields", data={"_csrf": S["tok"], "value": "4,500.00", "assigned_user_id": "1",
                                                  "next_follow_up_on": "2026-09-10", "lost_reason": ""})
    assert r.status_code == 302
    with app.app_context():
        l = db.session.get(M.IntakeLead, aid)
        assert l.value_cents == 450000 and l.assigned_user_id == 1 and l.next_follow_up_on == date(2026, 9, 10)
    r = client.get(f"/intake/{aid}")
    assert r.status_code == 200 and b"4500.0" in r.data and b"Follow-up sequences" in r.data
    r = client.get("/intake/pipeline")
    assert b"$4,500.00" in r.data
    # decline -> lost with reason
    r = client.post(f"/intake/{bid}/decline", data={"_csrf": S["tok"], "reason": "Went with another firm"})
    assert r.status_code == 302
    with app.app_context():
        l = db.session.get(M.IntakeLead, bid)
        assert l.stage == "lost" and l.status == "declined" and l.lost_reason == "Went with another firm"
    # convert -> won
    r = client.post(f"/intake/{priya}/convert", data={
        "_csrf": S["tok"], "contact_mode": "new", "first_name": "Priya", "last_name": "Natarajan",
        "email": "priya@example.com", "matter_name": "Natarajan LLC", "practice_area": "Business formation",
        "billing_type": "flat", "flat_fee_cents": "0", "flat_fee": "1,000.00"})
    assert r.status_code == 302 and "/matters/" in r.headers["Location"]
    with app.app_context():
        l = db.session.get(M.IntakeLead, priya)
        assert l.stage == "won" and l.status == "converted"
    # a converted lead cannot be dragged out of Won
    r = client.post(f"/intake/{priya}/stage", data={"stage": "new"},
                    headers={"X-CSRF-Token": S["tok"], "X-Requested-With": "fetch"})
    assert r.status_code == 400
    r = client.get("/intake?status=all")
    assert r.status_code == 200 and b"Consult scheduled" in r.data


def test_intake_thanks_shows_booking_url(app, client):
    from app.blueprints import intake
    intake._submissions.clear()
    r = app.test_client().post("/intake/submit", data={"name": "Booker Test", "email": "booker@example.test",
                                                       "matter_type": "Other", "website": ""})
    assert r.status_code == 200 and b"Thank you" in r.data
    assert b'href="https://book.example.test/consult"' in r.data and b"Book a consultation" in r.data


# ---------------------------------------------------------------- follow-up sequences
class _FakeDate(date):
    fixed = date(2026, 9, 4)

    @classmethod
    def today(cls):
        return cls.fixed


def test_sequences_drafts_then_send_idempotent_and_day3(app, client, monkeypatch):
    from app import cli
    from app.services.mail import dev_outbox, _dev_outbox
    db, M = _models()
    fixed = date(2026, 9, 4)
    _FakeDate.fixed = fixed
    monkeypatch.setattr(cli, "date", _FakeDate)
    with app.app_context():
        f = M.Firm.get()
        f.sequences_auto_send = False
        db.session.commit()
    # settings copy says plainly that nothing is sent unless the firm turns it on
    r = client.get("/settings")
    assert r.status_code == 200 and b"nothing is sent" in r.data and b'name="sequences_auto_send"' in r.data
    # CRUD
    r = client.get("/intake/sequences")
    assert r.status_code == 200 and b"New lead follow-up" in r.data  # sample created lazily
    r = client.post("/intake/sequences/new", data={
        "_csrf": S["tok"], "name": "Two touch", "is_active": "1",
        "step_day_0": "0", "step_subject_0": "Thanks {{ first_name }} from {{ firm_name }}",
        "step_body_0": "Hi {{ first_name }},\n\nAbout your {{ matter_type }} question. Book here: {{ booking_url }}",
        "step_day_1": "3", "step_subject_1": "Checking in, {{ name }}", "step_body_1": "Still need help? {{ attorney_name }}"})
    assert r.status_code == 302, r.data[:300]
    r = client.post("/intake/sequences/new", data={"_csrf": S["tok"], "name": "Broken", "step_subject_0": "{% if %}",
                                                   "step_body_0": "x"})
    assert r.status_code == 400 and b"syntax error" in r.data
    with app.app_context():
        seq = M.FollowUpSequence.query.filter_by(name="Two touch").first()
        assert seq and [s["day"] for s in seq.steps] == [0, 3]
        sid = seq.id
        lead = M.IntakeLead(name="Sam Sequence", email="sam.seq@example.test", matter_type="Real estate", source="test")
        db.session.add(lead)
        db.session.commit()
        lid = lead.id
    r = client.post(f"/intake/{lid}/sequence/start", data={"_csrf": S["tok"], "sequence_id": sid,
                                                          "started_on": fixed.isoformat()})
    assert r.status_code == 302
    with app.app_context():
        ls = M.LeadSequence.query.filter_by(lead_id=lid).first()
        assert ls and ls.status == "active" and ls.next_step == 0 and ls.started_on == fixed
        lsid = ls.id
        outbox_before = len(_dev_outbox)
        # day 0: draft, not sent
        assert cli.run_sequences() == (0, 1)
        assert cli.run_sequences() == (0, 0)  # idempotent
        msgs = M.Message.query.filter(M.Message.provider_id.like(f"lead-seq:{lsid}:%")).all()
        assert len(msgs) == 1 and msgs[0].status == "draft" and msgs[0].direction == "out" and msgs[0].channel == "email"
        assert msgs[0].subject == "Thanks Sam from Demo Law PLLC"
        assert "About your Real estate question" in msgs[0].body and "https://book.example.test/consult" in msgs[0].body
        assert len(_dev_outbox) == outbox_before
        assert db.session.get(M.LeadSequence, lsid).next_step == 1
        draft_id = msgs[0].id
    r = client.get("/intake/drafts")
    assert r.status_code == 200 and b"Sam Sequence" in r.data and b"Thanks Sam from Demo Law PLLC" in r.data
    r = client.get(f"/intake/{lid}")
    assert r.status_code == 200 and b"Two touch" in r.data and b"draft" in r.data
    # human presses Send
    r = client.post(f"/intake/drafts/{draft_id}/send", data={"_csrf": S["tok"], "to": "sam.seq@example.test",
                                                            "subject": "Thanks Sam from Demo Law PLLC",
                                                            "body": "Edited body before sending."})
    assert r.status_code == 302
    with app.app_context():
        m = db.session.get(M.Message, draft_id)
        assert m.status == "sent" and m.body == "Edited body before sending."
        assert dev_outbox()[0]["to"] == "sam.seq@example.test" and dev_outbox()[0]["subject"] == "Thanks Sam from Demo Law PLLC"
        # day 2: nothing; day 3: second step drafted
        _FakeDate.fixed = fixed + timedelta(days=2)
        assert cli.run_sequences() == (0, 0)
        _FakeDate.fixed = fixed + timedelta(days=3)
        assert cli.run_sequences() == (0, 1)
        assert cli.run_sequences() == (0, 0)
        ls = db.session.get(M.LeadSequence, lsid)
        assert ls.next_step == 2 and ls.status == "done"
        m2 = M.Message.query.filter_by(provider_id=f"lead-seq:{lsid}:1").first()
        assert m2.status == "draft" and m2.subject == "Checking in, Sam Sequence" and "Demo Owner" not in m2.body
        assert "Demo Law PLLC" in m2.body  # no assigned attorney, falls back to the firm name
    # auto-send on: the step goes straight out through the dev outbox
    _FakeDate.fixed = fixed
    r = client.post("/settings", data={"_csrf": S["tok"], "_form": "1", "name": "Demo Law PLLC",
                                       "sequences_auto_send": "1", "ai_enabled": "1"})
    assert r.status_code == 302
    with app.app_context():
        f = M.Firm.get()
        assert f.sequences_auto_send is True and f.ai_enabled is True
        lead2 = M.IntakeLead(name="Ava Auto", email="ava.auto@example.test", matter_type="Employment", source="test")
        db.session.add(lead2)
        db.session.commit()
        lid2 = lead2.id
    r = client.post(f"/intake/{lid2}/sequence/start", data={"_csrf": S["tok"], "sequence_id": sid,
                                                           "started_on": fixed.isoformat()})
    assert r.status_code == 302
    with app.app_context():
        n = len(_dev_outbox)
        assert cli.run_sequences() == (1, 0)
        assert cli.run_sequences() == (0, 0)
        assert len(_dev_outbox) == n + 1 and dev_outbox()[0]["to"] == "ava.auto@example.test"
        ls2 = M.LeadSequence.query.filter_by(lead_id=lid2).first()
        m3 = M.Message.query.filter_by(provider_id=f"lead-seq:{ls2.id}:0").first()
        assert m3.status == "sent" and ls2.next_step == 1
        # a lost lead stops its sequence instead of emailing
        lead2 = db.session.get(M.IntakeLead, lid2)
        lead2.stage, lead2.status = "lost", "declined"
        db.session.commit()
        _FakeDate.fixed = fixed + timedelta(days=3)
        assert cli.run_sequences() == (0, 0)
        assert M.LeadSequence.query.filter_by(lead_id=lid2).first().status == "stopped"
        # CLI entry point accepts the command
        f = M.Firm.get()
        f.sequences_auto_send = False
        db.session.commit()
    # no-email lead cannot start
    with app.app_context():
        lead3 = M.IntakeLead(name="No Email", email="", matter_type="Other", source="test")
        db.session.add(lead3)
        db.session.commit()
        lid3 = lead3.id
    r = client.post(f"/intake/{lid3}/sequence/start", data={"_csrf": S["tok"], "sequence_id": sid})
    assert r.status_code == 302
    with app.app_context():
        assert M.LeadSequence.query.filter_by(lead_id=lid3).count() == 0
    # message threads page ignores lead drafts (no contact) and does not crash
    r = client.get("/messages")
    assert r.status_code == 200
