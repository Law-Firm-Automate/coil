"""Phase 5, Agent N: nightly case audit (rules plus labelled AI flags) and case scoring at intake and on PI cases.

Own SQLite file (data/test_phase5_n.db) seeded by seed.py. No network: the model is monkeypatched at
app.llm.complete and the fixture blanks both API keys in app.config so a shell key cannot leak into a real call.
"""
import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402

TEST_DB = os.path.join(ROOT, "data", "test_phase5_n.db")
UPLOAD_DIR = os.path.join(ROOT, "data", "test_phase5_n_uploads")
S = {}
TODAY = date.today()


@pytest.fixture(scope="module")
def app():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{TEST_DB}")
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT, env=env)
    from app import create_app
    application = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{TEST_DB}", "TESTING": True, "SMTP_HOST": "",
                              "UPLOAD_DIR": UPLOAD_DIR, "OPENROUTER_API_KEY": "", "ANTHROPIC_API_KEY": ""})
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


def _findings(M, matter_id, **flt):
    return M.CaseAuditFinding.query.filter_by(matter_id=matter_id, **flt).order_by(M.CaseAuditFinding.id).all()


def _fake_complete(monkeypatch, payload):
    from app import llm
    calls = []

    def fake(prompt, **kw):
        calls.append((prompt, kw))
        return json.dumps(payload)
    monkeypatch.setattr(llm, "complete", fake)
    return calls


# ---------------------------------------------------------------- setup: one PI matter with every problem
def test_build_pi_matter(app):
    db, M = _models()
    with app.app_context():
        u = M.User.query.filter_by(email="owner@example.com").first()
        c = M.Contact(first_name="Rosa", last_name="Quintero", email="rosa@example.test", phone="5125550199",
                      is_client=True)
        db.session.add(c)
        db.session.flush()
        m = M.Matter(number="M-5001", client_id=c.id, name="Quintero v. Ochoa (auto)", practice_area="Personal injury",
                     billing_type="contingency", contingency_pct=33.33, responsible_user_id=u.id, status="open",
                     opened_on=TODAY - timedelta(days=120), sol_date=TODAY + timedelta(days=30))
        db.session.add(m)
        db.session.flush()
        pi = M.PiCase(matter_id=m.id, incident_type="auto", date_of_loss=TODAY - timedelta(days=130),
                      incident_description="Rear-ended on Lamar. Client hit her head on the headrest.",
                      injuries="Neck pain, headaches. MRI recommended by the ER doctor.",
                      treatment_status="treating", stage="treating")
        db.session.add(pi)
        p = M.MedicalProvider(matter_id=m.id, name="Austin ER Associates", specialty="Emergency",
                              records_requested_on=TODAY - timedelta(days=45), total_billed_cents=480000)
        db.session.add(p)
        db.session.add(M.ChronologyEntry(matter_id=m.id, date=TODAY - timedelta(days=128), provider_name="Austin ER",
                                         visit_type="ER", diagnosis="Cervical strain", confirmed=True))
        db.session.add(M.ChronologyEntry(matter_id=m.id, date=TODAY - timedelta(days=68), provider_name="Chiro",
                                         visit_type="office", diagnosis="Follow-up", confirmed=True))
        db.session.add(M.Lien(matter_id=m.id, holder="BlueCross", type="health_plan", original_cents=120000,
                              status="open", contact=""))
        db.session.commit()
        S["matter_id"], S["pi_id"], S["provider_id"], S["contact_id"] = m.id, pi.id, p.id, c.id


def test_rules_fire_once_and_last_seen_updates(app, no_keys):
    from app.blueprints.caseaudit import run_case_audit
    from app.services.mail import dev_outbox
    db, M = _models()
    mid = S["matter_id"]
    with app.app_context():
        M.Firm.get().ai_enabled = False
        db.session.commit()
        before = len([e for e in dev_outbox() if "Case audit" in e["subject"]])
        r1 = run_case_audit(today=TODAY)
        assert r1["matters"] >= 1 and r1["pi_matters"] >= 1
        rows = _findings(M, mid, status="open")
        kinds = [f.kind for f in rows]
        for k in ("missing_records", "treatment_gap", "imaging_not_obtained", "sol_near", "lien_no_contact",
                  "no_activity"):
            assert kinds.count(k) == 1, (k, kinds)
        assert "bills_missing" not in kinds and "demand_unanswered" not in kinds and "limits_unknown" not in kinds
        assert all(f.origin == "rule" for f in rows)
        assert all(f.first_seen_on == TODAY and f.last_seen_on == TODAY for f in rows)
        sol = next(f for f in rows if f.kind == "sol_near")
        assert sol.severity == "high" and "no deadline task" in sol.message
        gap = next(f for f in rows if f.kind == "treatment_gap")
        assert "60-day gap" in gap.message
        n_first = M.CaseAuditFinding.query.filter_by(matter_id=mid).count()
        # summary email once, listing the matter and its high finding
        out = [e for e in dev_outbox() if "Case audit" in e["subject"]]
        assert len(out) == before + 1
        assert "M-5001" in out[0]["html"] and "Limitations date" in out[0]["html"]
        assert M.AuditLog.query.filter_by(action="case_audit_sent", detail=TODAY.isoformat()).count() == 1

        # second run the next day: same rows, last_seen moves, nothing duplicated, no second email
        r2 = run_case_audit(today=TODAY + timedelta(days=1))
        assert len(r2["new"]) == 0 or all(f.matter_id != mid for f in r2["new"])
        assert M.CaseAuditFinding.query.filter_by(matter_id=mid).count() == n_first
        rows2 = _findings(M, mid, status="open")
        assert sorted(f.kind for f in rows2) == sorted(kinds)
        assert all(f.last_seen_on == TODAY + timedelta(days=1) and f.first_seen_on == TODAY for f in rows2)
        assert len([e for e in dev_outbox() if "Case audit" in e["subject"]]) == before + 1
        # same-day re-run with the day already emailed: still one email
        run_case_audit(today=TODAY)
        assert len([e for e in dev_outbox() if "Case audit" in e["subject"]]) == before + 1
        assert M.AuditLog.query.filter_by(action="case_audit_run").count() >= 3


def test_fixing_condition_resolves_finding(app, no_keys):
    from app.blueprints.caseaudit import run_case_audit
    db, M = _models()
    mid = S["matter_id"]
    with app.app_context():
        p = db.session.get(M.MedicalProvider, S["provider_id"])
        p.records_received_on = TODAY
        db.session.commit()
        run_case_audit(today=TODAY + timedelta(days=2))
        mr = [f for f in _findings(M, mid, kind="missing_records")]
        assert len(mr) == 1 and mr[0].status == "resolved"
        # records in, bills never requested: the next rule takes over, once
        bm = _findings(M, mid, kind="bills_missing")
        assert len(bm) == 1 and bm[0].status == "open"
        # add the limitations deadline task: sol_near resolves too
        db.session.add(M.Task(matter_id=mid, title="Statute of limitations check", kind="deadline",
                              due_on=TODAY + timedelta(days=30)))
        db.session.commit()
        run_case_audit(today=TODAY + timedelta(days=2))
        assert _findings(M, mid, kind="sol_near")[0].status == "resolved"
        assert M.CaseAuditFinding.query.filter_by(matter_id=mid, kind="sol_near").count() == 1


def test_ai_findings_recorded_when_model_answers_and_none_when_unavailable(app, no_keys, monkeypatch):
    from app.blueprints.caseaudit import run_case_audit
    from app import llm
    db, M = _models()
    mid = S["matter_id"]
    with app.app_context():
        M.Firm.get().ai_enabled = True
        db.session.commit()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
        calls = _fake_complete(monkeypatch, {"findings": [
            {"kind": "injury_mention", "title": "Head strike without a concussion screen",
             "reason": "The facts say she hit her head but the injury list has no concussion or TBI work-up.",
             "severity": "high"},
            {"kind": "mass_tort_signal", "title": "Headrest failure", "reason": "Possible product angle on the headrest.",
             "severity": "low"},
        ]})
        r = run_case_audit(today=TODAY + timedelta(days=3))
        assert r["ai"] >= 2
        pi_calls = [c for c in calls if "M-5001" in c[0]]
        assert len(pi_calls) == 1 and pi_calls[0][1].get("kind") == "case_audit"
        assert "hit her head" in pi_calls[0][0]
        ai_rows = _findings(M, mid, origin="ai")
        assert len(ai_rows) == 2
        assert {f.kind for f in ai_rows} == {"injury_mention", "mass_tort_signal"}
        assert {f.severity for f in ai_rows} == {"high", "low"}
        assert all(f.status == "open" for f in ai_rows)
        # same answer again: no duplicates, last_seen moves
        run_case_audit(today=TODAY + timedelta(days=4))
        ai_rows = _findings(M, mid, origin="ai")
        assert len(ai_rows) == 2 and all(f.last_seen_on == TODAY + timedelta(days=4) for f in ai_rows)
        # cap at 5 per matter
        _fake_complete(monkeypatch, {"findings": [
            {"kind": "injury_mention", "title": f"Flag {i}", "reason": "r", "severity": "medium"} for i in range(9)]})
        run_case_audit(today=TODAY + timedelta(days=5))
        assert M.CaseAuditFinding.query.filter_by(matter_id=mid, origin="ai").count() == 2 + 5

        # model unavailable: nothing new, nothing lost
        def raising(prompt, **kw):
            raise llm.LLMUnavailable("No AI key is configured.")
        monkeypatch.setattr(llm, "complete", raising)
        n = M.CaseAuditFinding.query.filter_by(matter_id=mid, origin="ai").count()
        r = run_case_audit(today=TODAY + timedelta(days=6))
        assert r["ai"] == 0
        assert M.CaseAuditFinding.query.filter_by(matter_id=mid, origin="ai").count() == n
        # AI off in Settings: the model is not even asked
        M.Firm.get().ai_enabled = False
        db.session.commit()
        calls2 = _fake_complete(monkeypatch, {"findings": [{"kind": "injury_mention", "title": "x", "reason": "y",
                                                             "severity": "low"}]})
        r = run_case_audit(today=TODAY + timedelta(days=7))
        assert r["ai"] == 0 and calls2 == []


def test_pages_and_dismiss_resolve_endpoints(app, client):
    db, M = _models()
    mid = S["matter_id"]
    r = client.get("/audit")
    assert r.status_code == 200
    body = r.data.decode()
    assert "M-5001" in body and "Case audit" in body
    assert ">AI<" in body  # AI-origin findings are labelled
    assert "Head strike without a concussion screen" in body
    r = client.get("/audit?severity=high")
    assert r.status_code == 200 and "Head strike" in r.data.decode()
    r = client.get("/audit?kind=lien_no_contact")
    assert r.status_code == 200 and "BlueCross" in r.data.decode() and "Head strike" not in r.data.decode()
    r = client.get(f"/audit/{mid}")
    assert r.status_code == 200 and "resolved" in r.data.decode()
    with app.app_context():
        lien_f = _findings(M, mid, kind="lien_no_contact")[0]
        gap_f = _findings(M, mid, kind="treatment_gap")[0]
        lid, gid = lien_f.id, gap_f.id
    r = client.post(f"/audit/finding/{lid}/dismiss", data={"_csrf": S["tok"], "next": f"/audit/{mid}"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/audit/{mid}")
    r = client.post(f"/audit/finding/{gid}/resolve", data={"_csrf": S["tok"]})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(M.CaseAuditFinding, lid).status == "dismissed"
        assert db.session.get(M.CaseAuditFinding, gid).status == "resolved"
        assert M.AuditLog.query.filter_by(action="case_audit_dismissed", entity_id=lid).count() == 1
        assert M.AuditLog.query.filter_by(action="case_audit_resolved", entity_id=gid).count() == 1
    # the nightly run keeps a dismissed finding dismissed and reopens the resolved one (its condition persists)
    from app.blueprints.caseaudit import run_case_audit
    with app.app_context():
        M.Firm.get().ai_enabled = False
        db.session.commit()
        run_case_audit(today=TODAY + timedelta(days=8))
        assert db.session.get(M.CaseAuditFinding, lid).status == "dismissed"
        g = db.session.get(M.CaseAuditFinding, gid)
        assert g.status == "open" and g.first_seen_on == TODAY + timedelta(days=8)
        assert M.CaseAuditFinding.query.filter_by(matter_id=mid, kind="treatment_gap").count() == 1
    # owner can run from the page
    r = client.post("/audit/run", data={"_csrf": S["tok"]})
    assert r.status_code == 302
    # PI case page card and board badge, dashboard card
    r = client.get(f"/pi/{mid}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Case audit" in body and "open</span>" in body and f'href="/audit/{mid}"' in body
    r = client.get("/pi")
    assert r.status_code == 200 and "finding" in r.data.decode() and f'href="/audit/{mid}"' in r.data.decode()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode()
    assert 'data-card="case_audit"' in body and "M-5001" in body
    r = client.get("/dashboard/customize")
    assert r.status_code == 200 and "card_case_audit" in r.data.decode()


def test_cli_case_audit(app, monkeypatch):
    from app import cli
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{TEST_DB}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert cli.main(["case_audit"]) == 0
    assert cli.main([]) == 2


# ---------------------------------------------------------------- scoring
def test_lead_scoring_rich_beats_empty_and_badge_renders(app, client):
    from app.blueprints.caseaudit import score_lead
    db, M = _models()
    with app.app_context():
        empty = M.IntakeLead(name="Nobody Much")
        rich = M.IntakeLead(name="Elena Park", email="elena@example.test", phone="5125550123",
                            matter_type="Personal injury", adverse_party="Ochoa Trucking",
                            description="I was hit by a truck and taken to the hospital. There is a police report and "
                                        "the insurance company already called. I may need surgery. The doctor said "
                                        "there is a deadline for the claim so I signed nothing yet. " * 2)
        db.session.add_all([empty, rich])
        db.session.flush()
        s_empty, f_empty = score_lead(empty)
        s_rich, f_rich = score_lead(rich)
        db.session.commit()
        assert 0 <= s_empty < s_rich <= 100
        assert s_rich >= 80 and s_empty <= 15
        assert empty.score == s_empty and rich.score == s_rich
        labels = " ".join(f["label"] for f in json.loads(rich.score_json)["factors"])
        assert "Personal injury" in labels and "hospital" in labels and "Other party" in labels
        S["rich_lead"] = rich.id
    # created through the public form: scored on create
    r = client.post("/intake/submit", data={"name": "Marco Diaz", "email": "marco@example.test", "phone": "5125550100",
                                            "matter_type": "Personal injury", "adverse_party": "A driver",
                                            "description": "Car accident, went to the hospital, police report filed."})
    assert r.status_code == 200
    with app.app_context():
        lead = M.IntakeLead.query.filter_by(name="Marco Diaz").first()
        assert lead.score is not None and lead.score >= 60
        S["form_lead"] = lead.id
    # update recomputes; badge with factors on the pipeline and the detail page
    r = client.post(f"/intake/{S['form_lead']}/fields", data={"_csrf": S["tok"], "value": "5000"})
    assert r.status_code == 302
    r = client.get("/intake/pipeline")
    assert r.status_code == 200
    body = r.data.decode()
    assert "score-badge" in body and re.search(r'title="Case score \d+/100: Matter type: Personal injury \+30', body)
    r = client.get(f"/intake/{S['rich_lead']}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Case score" in body and "Key words:" in body and "Rescore" in body
    r = client.post(f"/audit/lead/{S['rich_lead']}/rescore", data={"_csrf": S["tok"]})
    assert r.status_code == 302


def test_pi_case_score_computed_and_shown(app, client, no_keys, monkeypatch):
    from app.blueprints.caseaudit import score_pi_case
    db, M = _models()
    mid = S["matter_id"]
    with app.app_context():
        pi = db.session.get(M.PiCase, S["pi_id"])
        assert pi.case_score is None
        score, data = score_pi_case(pi)
        db.session.commit()
        assert 0 < score <= 100 and pi.case_score == score
        labels = " ".join(f["label"] for f in data["factors"])
        assert "provider" in labels and "Imaging on file" in labels and "Treatment ongoing" in labels
        assert "ai_adjustment" not in data
        low = score
        pi.liability_notes = "Police report cites the other driver. Two witnesses."
        pi.policy_limits_cents = 3000000
        pi.demand_sent_on = TODAY
        db.session.commit()
        score2, _ = score_pi_case(pi)
        db.session.commit()
        assert score2 > low
        # optional AI refinement: bounded, recorded with its reason, never required
        M.Firm.get().ai_enabled = True
        db.session.commit()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
        _fake_complete(monkeypatch, {"adjustment": 40, "reason": "Strong liability facts and known limits."})
        score3, data3 = score_pi_case(pi, refine=True)
        db.session.commit()
        assert data3["ai_adjustment"]["delta"] == 15 and score3 == min(100, score2 + 15)
        assert json.loads(pi.case_score_json)["ai_adjustment"]["reason"].startswith("Strong")
    r = client.post(f"/audit/pi/{mid}/rescore", data={"_csrf": S["tok"]})
    assert r.status_code == 302 and r.headers["Location"].endswith("#audit")
    r = client.get(f"/pi/{mid}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Case score" in body and f"{score2}/100" in body and "Liability notes on file" in body
    r = client.get("/pi")
    assert r.status_code == 200 and f"score {score2}" in r.data.decode()
    with app.app_context():
        M.Firm.get().ai_enabled = False
        db.session.commit()
