"""Phase 3, Agent E: court rules, holidays, deadline math, document templates.

Own SQLite file (data/test_phase3_e.db) seeded by seed.py, own UPLOAD_DIR and PDF_DIR. Never touches data/practice.db.
Run: .venv/bin/python -m pytest tests/test_phase3_e.py -q
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_phase3_e.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_phase3_e")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_phase3_e")

from tests.helpers import login  # noqa: E402


class R:
    """Duck-typed rule for compute_deadline unit tests."""
    def __init__(self, offset_days, day_type="calendar", direction="after", roll=True):
        self.offset_days, self.day_type, self.direction, self.roll = offset_days, day_type, direction, roll


@pytest.fixture(scope="module")
def app():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    env = dict(os.environ, DATABASE_URL=DB_URI)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], env=env, cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    from app import create_app
    a = create_app({"SQLALCHEMY_DATABASE_URI": DB_URI, "UPLOAD_DIR": UPLOAD_DIR, "PDF_DIR": PDF_DIR, "TESTING": True})
    yield a


@pytest.fixture
def owner(app):
    c = app.test_client()
    tok = login(c)
    return c, tok


def _matter(app, number="M-1001"):
    from app.models import Matter
    with app.app_context():
        m = Matter.query.filter_by(number=number).first()
        return m.id, m.client.display_name


# ---------------------------------------------------------------- compute_deadline
def test_calendar_days_landing_on_saturday_roll_to_monday():
    from app.blueprints.rules import compute_deadline
    # Sat Jan 3 2026 + 21 = Sat Jan 24 2026 -> Mon Jan 26
    assert compute_deadline(date(2026, 1, 3), R(21), []) == date(2026, 1, 26)
    # a weekday landing stays put
    assert compute_deadline(date(2026, 1, 5), R(21), []) == date(2026, 1, 26)


def test_court_days_skip_weekend_and_holiday():
    from app.blueprints.rules import compute_deadline
    mlk = date(2026, 1, 19)  # Monday
    # Wed Jan 14: Thu 15 (1), Fri 16 (2), [Sat, Sun, Mon MLK skipped] Tue 20 (3), Wed 21 (4), Thu 22 (5)
    assert compute_deadline(date(2026, 1, 14), R(5, "court"), [mlk]) == date(2026, 1, 22)
    # without the holiday, Monday counts and it lands a day earlier
    assert compute_deadline(date(2026, 1, 14), R(5, "court"), []) == date(2026, 1, 21)
    # holidays may be passed as Holiday-like objects with a .date
    class H:
        def __init__(self, d):
            self.date = d
    assert compute_deadline(date(2026, 1, 14), R(5, "court"), [H(mlk)]) == date(2026, 1, 22)


def test_days_before_roll_backwards():
    from app.blueprints.rules import compute_deadline
    presidents = date(2026, 2, 16)  # Monday holiday
    # conference Mon Mar 9 2026 minus 21 = Mon Feb 16 (holiday) -> back to Fri Feb 13
    assert compute_deadline(date(2026, 3, 9), R(21, "calendar", "before"), [presidents]) == date(2026, 2, 13)
    # without the holiday it stays on Monday Feb 16
    assert compute_deadline(date(2026, 3, 9), R(21, "calendar", "before"), []) == date(2026, 2, 16)
    # landing on a Saturday rolls back to Friday
    assert compute_deadline(date(2026, 3, 7), R(21, "calendar", "before"), []) == date(2026, 2, 13)
    # court days before: Mon Mar 9 minus 3 court days = Wed Mar 4
    assert compute_deadline(date(2026, 3, 9), R(3, "court", "before"), []) == date(2026, 3, 4)


def test_roll_off_keeps_weekend_date():
    from app.blueprints.rules import compute_deadline
    assert compute_deadline(date(2026, 1, 3), R(21, roll=False), []) == date(2026, 1, 24)
    assert date(2026, 1, 24).weekday() == 5
    assert compute_deadline(date(2026, 3, 7), R(21, "calendar", "before", roll=False), []) == date(2026, 2, 14)


def test_next_monday_after_twenty_days():
    from app.blueprints.rules import compute_deadline
    # Mon Jan 5 + 20 = Sun Jan 25 -> Mon Jan 26
    assert compute_deadline(date(2026, 1, 5), R(20, "nextmonday"), []) == date(2026, 1, 26)
    # Tue Jan 6 + 20 = Mon Jan 26 -> the Monday *next after* is Feb 2
    assert compute_deadline(date(2026, 1, 6), R(20, "nextmonday"), []) == date(2026, 2, 2)
    # if that Monday is a holiday, roll forward to Tuesday
    assert compute_deadline(date(2026, 1, 5), R(20, "nextmonday"), [date(2026, 1, 26)]) == date(2026, 1, 27)


def test_federal_holidays_computed():
    from app.blueprints.rules import federal_holidays
    hol = dict(federal_holidays(2026))
    assert len(hol) == 11
    assert hol[date(2026, 1, 1)].startswith("New Year")
    assert hol[date(2026, 1, 19)].startswith("Martin Luther King")
    assert hol[date(2026, 2, 16)] == "Presidents Day"
    assert hol[date(2026, 5, 25)] == "Memorial Day"
    assert hol[date(2026, 6, 19)] == "Juneteenth"
    assert hol[date(2026, 7, 3)] == "Independence Day (observed)"  # July 4 2026 is a Saturday
    assert hol[date(2026, 9, 7)] == "Labor Day"
    assert hol[date(2026, 10, 12)] == "Columbus Day"
    assert hol[date(2026, 11, 11)] == "Veterans Day"
    assert hol[date(2026, 11, 26)] == "Thanksgiving Day"
    assert hol[date(2026, 12, 25)] == "Christmas Day"


# ---------------------------------------------------------------- rules library pages
def test_starter_rulesets_created_lazily_with_caution_notes(app, owner):
    c, tok = owner
    from app.models import CourtRuleSet, CourtRule
    with app.app_context():
        assert CourtRuleSet.query.count() == 0
    r = c.get("/settings/rules")
    assert r.status_code == 200
    with app.app_context():
        sets = CourtRuleSet.query.order_by(CourtRuleSet.name).all()
        assert [s.name for s in sets] == ["Federal civil (FRCP), generic", "Texas civil (TRCP), generic"]
        assert len(sets[0].rules) == 6 and len(sets[1].rules) == 4
        for rule in CourtRule.query.all():
            assert "generic" in rule.notes.lower() and "local rules" in rule.notes.lower()
        frcp = sets[0]
        answer = next(x for x in frcp.rules if x.title == "Answer due")
        assert answer.offset_days == 21 and answer.direction == "after" and answer.trigger == "Service of complaint"
        conf = next(x for x in frcp.rules if "26(f)" in x.title and "conference" in x.title.lower())
        assert conf.direction == "before" and conf.offset_days == 21
        trcp = sets[1]
        tx_answer = next(x for x in trcp.rules if x.title.startswith("Answer due"))
        assert tx_answer.day_type == "nextmonday" and tx_answer.offset_days == 20
        fid = frcp.id
    c.get("/settings/rules")
    with app.app_context():
        assert CourtRuleSet.query.count() == 2  # not re-created
    r = c.get(f"/settings/rules/{fid}")
    assert r.status_code == 200 and b"Answer due" in r.data and b"generic" in r.data.lower()
    body = r.data.decode().lower()
    assert "authoritative" not in body and "complete set" not in body


def test_ruleset_crud_export_import(app, owner):
    c, tok = owner
    r = c.post("/settings/rules/new", data={"_csrf": tok, "name": "Travis County local", "jurisdiction": "Travis County",
                                            "description": "test", "is_active": "1"})
    assert r.status_code == 302
    sid = int(re.search(r"/settings/rules/(\d+)", r.headers["Location"]).group(1))
    r = c.post(f"/settings/rules/{sid}/rules", data={"_csrf": tok, "trigger": "Hearing set", "title": "Exhibits due",
                                                     "offset_days": "3", "day_type": "court", "direction": "before",
                                                     "roll": "1", "kind": "deadline", "notes": "local rule 3.2"})
    assert r.status_code == 302
    r = c.post(f"/settings/rules/{sid}/rules", data={"_csrf": tok, "trigger": "", "title": "no trigger"},
               follow_redirects=True)
    assert b"required" in r.data
    from app.models import CourtRuleSet, CourtRule
    with app.app_context():
        rs = CourtRuleSet.query.get(sid)
        assert len(rs.rules) == 1 and rs.rules[0].day_type == "court" and rs.rules[0].direction == "before"
        rid = rs.rules[0].id
    r = c.post(f"/settings/rules/{sid}/rules/{rid}/edit", data={"_csrf": tok, "trigger": "Hearing set",
                                                                "title": "Exhibit list due", "offset_days": "5",
                                                                "day_type": "calendar", "direction": "before",
                                                                "kind": "task", "notes": "", "sort": "0"})
    assert r.status_code == 302
    with app.app_context():
        rule = CourtRule.query.get(rid)
        assert rule.title == "Exhibit list due" and rule.offset_days == 5 and rule.roll is False and rule.kind == "task"
    r = c.get(f"/settings/rules/{sid}/export.json")
    assert r.status_code == 200 and r.mimetype == "application/json"
    data = json.loads(r.data)
    assert data["name"] == "Travis County local" and data["rules"][0]["title"] == "Exhibit list due"
    data["name"] = "Travis County local (imported)"
    r = c.post("/settings/rules/import", data={"_csrf": tok, "json": json.dumps(data)})
    assert r.status_code == 302
    with app.app_context():
        imp = CourtRuleSet.query.filter_by(name="Travis County local (imported)").first()
        assert imp and len(imp.rules) == 1 and imp.rules[0].offset_days == 5 and imp.rules[0].roll is False
        iid = imp.id
    r = c.post("/settings/rules/import", data={"_csrf": tok, "json": "{not json"}, follow_redirects=True)
    assert b"Could not import" in r.data
    r = c.post(f"/settings/rules/{iid}/delete", data={"_csrf": tok}, follow_redirects=True)
    assert b"Deleted rule set" in r.data
    with app.app_context():
        assert CourtRuleSet.query.get(iid) is None
    r = c.post(f"/settings/rules/{sid}/edit", data={"_csrf": tok, "name": "Travis County local", "is_active": ""})
    assert r.status_code == 302
    with app.app_context():
        assert CourtRuleSet.query.get(sid).is_active is False


def test_holidays_load_federal_set(app, owner):
    c, tok = owner
    from app.models import Holiday
    r = c.post("/settings/holidays/load", data={"_csrf": tok, "year": "2026"}, follow_redirects=True)
    assert r.status_code == 200 and b"11 added" in r.data
    with app.app_context():
        rows = Holiday.query.filter(Holiday.date >= date(2026, 1, 1), Holiday.date <= date(2026, 12, 31)).all()
        dates = {h.date for h in rows}
        assert len(rows) == 11
        assert {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25), date(2026, 6, 19),
                date(2026, 7, 3), date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11), date(2026, 11, 26),
                date(2026, 12, 25)} == dates
    # idempotent
    r = c.post("/settings/holidays/load", data={"_csrf": tok, "year": "2026"}, follow_redirects=True)
    assert b"0 added, 11 already present" in r.data
    with app.app_context():
        assert Holiday.query.filter(Holiday.date >= date(2026, 1, 1), Holiday.date <= date(2026, 12, 31)).count() == 11
    # manual add + duplicate refused + delete
    r = c.post("/settings/holidays", data={"_csrf": tok, "date": "2026-03-02", "name": "Texas Independence Day"})
    assert r.status_code == 302
    r = c.post("/settings/holidays", data={"_csrf": tok, "date": "2026-03-02", "name": "dupe"}, follow_redirects=True)
    assert b"already a holiday" in r.data
    r = c.get("/settings/holidays?year=2026")
    assert r.status_code == 200 and b"Texas Independence Day" in r.data and b"Thanksgiving" in r.data
    with app.app_context():
        hid = Holiday.query.filter_by(date=date(2026, 3, 2)).first().id
    r = c.post(f"/settings/holidays/{hid}/delete", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        assert Holiday.query.filter_by(date=date(2026, 3, 2)).first() is None


def test_apply_ruleset_to_matter_creates_tasks_and_skips_duplicates(app, owner):
    c, tok = owner
    mid, _ = _matter(app, "M-1002")
    from app.models import CourtRuleSet, Task, AuditLog
    with app.app_context():
        trcp = CourtRuleSet.query.filter_by(name="Texas civil (TRCP), generic").first()
        tid = trcp.id
        before = Task.query.filter_by(matter_id=mid).count()
    # GET form and preview
    r = c.get(f"/rules/matters/{mid}/apply")
    assert r.status_code == 200 and b"Texas civil (TRCP), generic" in r.data
    r = c.get(f"/rules/matters/{mid}/apply?ruleset_id={tid}&trigger=Signing+of+judgment&trigger_date=2026-01-05")
    assert r.status_code == 200 and b"Motion for new trial due" in r.data and b"Notice of appeal due" in r.data
    assert b"Feb 4, 2026" in r.data
    # POST creates both
    r = c.post(f"/rules/matters/{mid}/apply", data={"_csrf": tok, "ruleset_id": str(tid),
                                                    "trigger": "Signing of judgment", "trigger_date": "2026-01-05"},
               follow_redirects=True)
    assert r.status_code == 200 and b"Added 2 deadline(s)" in r.data
    with app.app_context():
        tasks = Task.query.filter_by(matter_id=mid, trigger_date=date(2026, 1, 5)).order_by(Task.title).all()
        assert [(t.title, t.due_on, t.kind, t.rule_trigger) for t in tasks] == [
            ("Motion for new trial due", date(2026, 2, 4), "deadline", "Signing of judgment"),
            ("Notice of appeal due", date(2026, 2, 4), "deadline", "Signing of judgment"),
        ]
        assert all(t.rule_id and t.notes and not t.done for t in tasks)
        assert Task.query.filter_by(matter_id=mid).count() == before + 2
        assert AuditLog.query.filter_by(action="apply_rules", entity="matter", entity_id=mid).count() == 1
        task_id = tasks[0].id
    # re-apply: none created
    r = c.post(f"/rules/matters/{mid}/apply", data={"_csrf": tok, "ruleset_id": str(tid),
                                                    "trigger": "Signing of judgment", "trigger_date": "2026-01-05"},
               follow_redirects=True)
    assert b"Added 0 deadline(s)" in r.data and b"Skipped 2" in r.data
    with app.app_context():
        assert Task.query.filter_by(matter_id=mid).count() == before + 2
    # a different trigger date is a new set of deadlines
    r = c.post(f"/rules/matters/{mid}/apply", data={"_csrf": tok, "ruleset_id": str(tid),
                                                    "trigger": "Signing of judgment", "trigger_date": "2026-01-06"},
               follow_redirects=True)
    assert b"Added 2 deadline(s)" in r.data
    # FRCP answer: served Sat Jan 3 -> Sat Jan 24 -> rolls to Mon Jan 26
    with app.app_context():
        fid = CourtRuleSet.query.filter_by(name="Federal civil (FRCP), generic").first().id
    r = c.post(f"/rules/matters/{mid}/apply", data={"_csrf": tok, "ruleset_id": str(fid),
                                                    "trigger": "Service of complaint", "trigger_date": "2026-01-03"},
               follow_redirects=True)
    assert b"Added 1 deadline(s)" in r.data
    with app.app_context():
        t = Task.query.filter_by(matter_id=mid, title="Answer due").first()
        assert t.due_on == date(2026, 1, 26)
    # bad input
    r = c.post(f"/rules/matters/{mid}/apply", data={"_csrf": tok, "ruleset_id": str(fid), "trigger": "Nope",
                                                    "trigger_date": "2026-01-03"})
    assert r.status_code == 200 and b"Pick a rule set" in r.data
    # tasks pages show the rule provenance
    r = c.get("/tasks")
    assert r.status_code == 200 and b"From court rules" in r.data and b"Signing of judgment" in r.data
    r = c.get(f"/tasks/{task_id}")
    assert r.status_code == 200 and b"From rule" in r.data and b"Texas civil (TRCP), generic" in r.data
    assert b"Jan 5, 2026" in r.data
    # matter tasks tab has the button
    r = c.get(f"/matters/{mid}?tab=tasks")
    assert f"/rules/matters/{mid}/apply".encode() in r.data and b"Court deadlines" in r.data


def test_rules_pages_permissions(app):
    from app.models import User
    from app.extensions import db
    with app.app_context():
        if not User.query.filter_by(email="para3e@example.test").first():
            u = User(email="para3e@example.test", name="Para", role="paralegal", initials="PA")
            u.set_password("password123")
            db.session.add(u)
            db.session.commit()
    c = app.test_client()
    tok = login(c, email="para3e@example.test")
    mid, _ = _matter(app, "M-1002")
    assert c.get("/settings/rules").status_code == 403  # settings prefix is owner territory
    assert c.get(f"/rules/matters/{mid}/apply").status_code == 200  # applying is for anyone who works matters
    assert c.get("/doctemplates").status_code == 200


# ---------------------------------------------------------------- document templates
def _make_docx(paragraphs):
    from docx import Document as DocxDocument
    d = DocxDocument()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return re.sub(r"<[^>]+>", "", xml)


def test_sample_html_template_created_lazily(app, owner):
    c, tok = owner
    from app.models import DocTemplate
    from app.extensions import db
    with app.app_context():
        DocTemplate.query.delete()
        db.session.commit()
        assert DocTemplate.query.count() == 0
    r = c.get("/doctemplates")
    assert r.status_code == 200
    with app.app_context():
        rows = DocTemplate.query.all()
        assert len(rows) == 1 and rows[0].kind == "html" and "closing" in rows[0].name.lower()
        assert "client_name" in rows[0].fields and "today" in rows[0].fields and "attorney_name" in rows[0].fields
    c.get("/doctemplates")
    with app.app_context():
        assert DocTemplate.query.count() == 1


def test_docx_template_upload_detects_fields_and_generates(app, owner):
    c, tok = owner
    mid, client_name = _matter(app, "M-1001")
    data = _make_docx(["Dear {{ client_name }},", "This letter concerns matter {{ matter_number }} ({{ matter_name }}).",
                       "Old style field: {CLAIM_NUMBER}", "Regards, {{ attorney_name }}"])
    r = c.post("/doctemplates/new", data={"_csrf": tok, "name": "Test letter", "kind": "docx",
                                          "practice_area": "Estate Planning", "description": "t", "is_active": "1",
                                          "file": (io.BytesIO(data), "letter.docx")},
               content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:300]
    tid = int(re.search(r"/doctemplates/(\d+)/edit", r.headers["Location"]).group(1))
    from app.models import DocTemplate, Document
    with app.app_context():
        t = DocTemplate.query.get(tid)
        assert t.kind == "docx" and t.path.startswith("templates/")
        assert os.path.isfile(os.path.join(UPLOAD_DIR, t.path))
        assert t.fields == ["client_name", "matter_number", "matter_name", "attorney_name", "CLAIM_NUMBER"]
    r = c.get("/doctemplates")
    assert b"Test letter" in r.data and b"client_name, matter_number" in r.data
    # not a docx
    r = c.post("/doctemplates/new", data={"_csrf": tok, "name": "Bad", "kind": "docx", "is_active": "1",
                                          "file": (io.BytesIO(b"hello"), "notes.txt")},
               content_type="multipart/form-data")
    assert r.status_code == 200 and b"Upload a .docx" in r.data
    # generate page prefills
    r = c.get(f"/doctemplates/{tid}/generate?matter_id={mid}")
    assert r.status_code == 200
    body = r.data.decode()
    assert f'name="f_client_name" value="{client_name}"' in body
    assert 'name="f_matter_number" value="M-1001"' in body
    assert 'name="f_CLAIM_NUMBER" value=""' in body and "not prefilled" in body
    # generate
    r = c.post(f"/doctemplates/{tid}/generate", data={"_csrf": tok, "matter_id": str(mid),
                                                      "f_client_name": client_name, "f_matter_number": "M-1001",
                                                      "f_matter_name": "Alvarez Estate Plan",
                                                      "f_attorney_name": "Test Attorney", "f_CLAIM_NUMBER": "x"})
    assert r.status_code == 302 and f"/matters/{mid}" in r.headers["Location"]
    with app.app_context():
        doc = Document.query.filter_by(matter_id=mid, template_id=tid).first()
        assert doc is not None
        assert doc.name == "Test letter - M-1001.docx"
        assert doc.mime.endswith("wordprocessingml.document") and doc.size > 0
        full = os.path.join(UPLOAD_DIR, doc.path)
        assert os.path.isfile(full) and doc.path.startswith(f"{mid}/")
        text = _docx_text(open(full, "rb").read())
        assert client_name in text and "M-1001" in text and "Test Attorney" in text
        assert "{{" not in text
        assert client_name in (doc.extracted_text or "")
        did = doc.id
    r = c.get(f"/documents/{did}/download")
    assert r.status_code == 200
    r = c.get(f"/matters/{mid}?tab=documents")
    assert b"Test letter - M-1001.docx" in r.data and b"Generate document" in r.data
    assert f"/doctemplates?matter_id={mid}".encode() in r.data
    # used template is deactivated, not deleted
    r = c.post(f"/doctemplates/{tid}/delete", data={"_csrf": tok}, follow_redirects=True)
    assert b"deactivated instead" in r.data
    with app.app_context():
        assert DocTemplate.query.get(tid).is_active is False


def test_html_template_generates_pdf_document(app, owner):
    c, tok = owner
    mid, client_name = _matter(app, "M-1001")
    from app.models import DocTemplate, Document, AuditLog
    with app.app_context():
        t = DocTemplate.query.filter_by(kind="html").first()
        tid = t.id
    r = c.get(f"/doctemplates/{tid}/generate?matter_id={mid}")
    assert r.status_code == 200 and client_name.encode() in r.data
    r = c.post(f"/doctemplates/{tid}/generate", data={"_csrf": tok, "matter_id": str(mid)}, follow_redirects=True)
    assert r.status_code == 200 and b"Generated" in r.data
    with app.app_context():
        doc = Document.query.filter_by(matter_id=mid, template_id=tid).first()
        assert doc is not None and doc.name.endswith(" - M-1001.pdf") and doc.mime == "application/pdf"
        full = os.path.join(UPLOAD_DIR, doc.path)
        assert os.path.isfile(full)
        assert open(full, "rb").read(5) == b"%PDF-"
        assert client_name in (doc.extracted_text or "")
        assert AuditLog.query.filter_by(action="generate_document", entity="matter", entity_id=mid).count() >= 1
    # edit the HTML body: syntax errors are refused, valid bodies re-scan fields
    r = c.post(f"/doctemplates/{tid}/edit", data={"_csrf": tok, "name": "Closing letter (sample)", "kind": "html",
                                                  "body_html": "<p>{{ client_name }</p>", "is_active": "1"})
    assert r.status_code == 200 and b"syntax" in r.data
    r = c.post(f"/doctemplates/{tid}/edit", data={"_csrf": tok, "name": "Closing letter (sample)", "kind": "html",
                                                  "body_html": "<p>{{ client_name }} {{ cf_claim_number }}</p>",
                                                  "is_active": "1"})
    assert r.status_code == 302
    with app.app_context():
        assert DocTemplate.query.get(tid).fields == ["client_name", "cf_claim_number"]
    # custom fields flow through as cf_<snake>
    from app.models import Matter
    from app.extensions import db
    with app.app_context():
        m = Matter.query.get(mid)
        m.custom_fields = {"Claim Number": "CLM-9"}
        db.session.commit()
    r = c.get(f"/doctemplates/{tid}/generate?matter_id={mid}")
    assert b'name="f_cf_claim_number" value="CLM-9"' in r.data
    # no matter picked: chooser page
    r = c.get(f"/doctemplates/{tid}/generate")
    assert r.status_code == 200 and b"Which matter" in r.data


def test_build_context_fields(app):
    from app.blueprints.doctemplates import build_context, snake
    from app.models import Matter
    assert snake("Claim Number") == "claim_number" and snake("  Date-of Birth ") == "date_of_birth"
    with app.app_context():
        m = Matter.query.filter_by(number="M-1002").first()
        ctx = build_context(m)
    for k in ("firm_name", "firm_address", "firm_phone", "firm_email", "office_address", "attorney_name",
              "attorney_email", "today", "client_name", "client_first_name", "client_last_name", "client_address",
              "client_email", "client_phone", "matter_name", "matter_number", "practice_area", "court",
              "case_number", "adverse_parties"):
        assert k in ctx
    assert ctx["matter_number"] == "M-1002" and ctx["adverse_parties"] == "Derek Holloway"
    assert ctx["client_name"] == "Bluebonnet Logistics LLC"
