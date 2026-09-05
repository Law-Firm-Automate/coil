"""Phase 4, Agent K: personal injury module (app/blueprints/pi.py, templates pi/).

Own SQLite file (data/test_phase4_k.db) seeded by seed.py, own UPLOAD_DIR and PDF_DIR. Never touches data/practice.db.
Run: .venv/bin/python -m pytest tests/test_phase4_k.py -q

The fixture adds its own client and matter (so the trust ledger starts at zero) with one $1,245.00 expense (124500 cents).
The seed's own Certified mail expense on M-1002 is 1245 cents, $12.45, so the brief's $1,245 figure is recreated here.
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_phase4_k.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_phase4_k")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_phase4_k")

from tests.helpers import login  # noqa: E402

DOL = date.today() - timedelta(days=100)
MATTER_NUMBER = "M-PI01"


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
    a = create_app({"SQLALCHEMY_DATABASE_URI": DB_URI, "UPLOAD_DIR": UPLOAD_DIR, "PDF_DIR": PDF_DIR, "TESTING": True})
    with a.app_context():
        from app.extensions import db
        from app.models import Contact, Matter, Expense, User
        u = User.query.first()
        c = Contact(first_name="Test", last_name="Claimant", email="claimant@example.test", is_client=True,
                    address="9 Test Way\nAustin, TX 78701")
        c.custom_fields = {"dob": "1985-04-12"}
        db.session.add(c)
        db.session.flush()
        m = Matter(number=MATTER_NUMBER, client_id=c.id, name="TEST PI Claimant v. Driver", practice_area="Personal Injury",
                   billing_type="contingency", contingency_pct=0.0, responsible_user_id=u.id, status="open")
        db.session.add(m)
        db.session.flush()
        db.session.add(Expense(matter_id=m.id, user_id=u.id, description="Certified mail (test)", amount_cents=124500,
                               category="Postage", billable=True))
        db.session.commit()
    yield a


@pytest.fixture
def owner(app):
    c = app.test_client()
    tok = login(c)
    return c, tok


def _mid(app):
    from app.models import Matter
    with app.app_context():
        return Matter.query.filter_by(number=MATTER_NUMBER).first().id


def _case(app, mid):
    from app.models import PiCase
    with app.app_context():
        return PiCase.query.filter_by(matter_id=mid).first()


# ---------------------------------------------------------------- start
def test_board_renders_and_start_creates_case(app, owner):
    c, tok = owner
    mid = _mid(app)
    r = c.get("/pi")
    assert r.status_code == 200 and b"Start PI case on a matter" in r.data
    assert MATTER_NUMBER.encode() in r.data  # offered in the start select
    r = c.post("/pi/start", data={"_csrf": tok, "matter_id": mid})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/pi/{mid}")
    case = _case(app, mid)
    assert case is not None and case.stage == "intake"
    # starting again is a no-op
    c.post("/pi/start", data={"_csrf": tok, "matter_id": mid})
    from app.models import PiCase
    with app.app_context():
        assert PiCase.query.filter_by(matter_id=mid).count() == 1


def test_matter_page_link_starts_case_on_first_visit(app, owner):
    c, tok = owner
    from app.models import Matter, PiCase
    with app.app_context():
        other = Matter.query.filter_by(number="M-1001").first().id
        assert PiCase.query.filter_by(matter_id=other).count() == 0
    r = c.get(f"/matters/{other}")
    assert r.status_code == 200 and f'href="/pi/{other}"'.encode() in r.data
    assert c.get(f"/pi/{other}").status_code == 200
    with app.app_context():
        assert PiCase.query.filter_by(matter_id=other).count() == 1


def test_save_facts(app, owner):
    c, tok = owner
    mid = _mid(app)
    r = c.post(f"/pi/{mid}/facts", data={
        "_csrf": tok, "date_of_loss": DOL.isoformat(), "incident_type": "auto", "treatment_status": "mmi",
        "incident_description": "Rear-ended at a light.", "injuries": "Cervical strain.", "insurer": "Acme Mutual",
        "claim_number": "CL-778", "adjuster_name": "Pat Adjuster", "adjuster_phone": "555-0100",
        "adjuster_email": "pat@acme.test", "policy_limits": "30,000.00", "um_uim_limits": "50000",
        "liability_notes": "Other driver cited.", "stage": "treating"})
    assert r.status_code == 302
    case = _case(app, mid)
    assert case.date_of_loss == DOL and case.insurer == "Acme Mutual" and case.stage == "treating"
    assert case.policy_limits_cents == 3000000 and case.um_uim_limits_cents == 5000000
    r = c.get(f"/pi/{mid}")
    assert r.status_code == 200 and b"Acme Mutual" in r.data and b"DOB 1985-04-12" in r.data


# ---------------------------------------------------------------- providers
def test_providers_and_records_request(app, owner):
    c, tok = owner
    mid = _mid(app)
    for name, billed, first, last in (("Austin ER", "4,250.00", DOL, DOL),
                                      ("Lamar Chiropractic", "6,000.00", DOL + timedelta(days=5), DOL + timedelta(days=60))):
        r = c.post(f"/pi/{mid}/providers/new", data={
            "_csrf": tok, "name": name, "specialty": "test", "address": "1 Provider St\nAustin, TX",
            "first_visit_on": first.isoformat(), "last_visit_on": last.isoformat(), "total_billed": billed})
        assert r.status_code == 302, r.data[:300]
    from app.models import MedicalProvider, Document
    with app.app_context():
        provs = MedicalProvider.query.filter_by(matter_id=mid).order_by(MedicalProvider.id).all()
        assert [p.total_billed_cents for p in provs] == [425000, 600000]
        pid = provs[0].id
    r = c.post(f"/pi/{mid}/providers/{pid}/request-records", data={"_csrf": tok})
    assert r.status_code == 302 and "/documents/" in r.headers["Location"]
    with app.app_context():
        p = MedicalProvider.query.get(pid)
        assert p.records_requested_on == date.today()
        doc = Document.query.filter_by(matter_id=mid, folder="Medical records").first()
        assert doc is not None and doc.name.startswith("Records request - Austin ER")
        full = os.path.join(UPLOAD_DIR, doc.path)
        assert os.path.isfile(full) and doc.size > 0
        with open(full, "rb") as fh:
            assert fh.read(5) == b"%PDF-"
        assert _case(app, mid).stage == "records"
    # bills request works the same way
    r = c.post(f"/pi/{mid}/providers/{pid}/request-bills", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        assert MedicalProvider.query.get(pid).bills_requested_on == date.today()
        assert Document.query.filter_by(matter_id=mid, folder="Medical records").count() == 2
    r = c.get(f"/pi/{mid}")
    assert b"Lamar Chiropractic" in r.data and b"$10,250.00" in r.data  # total billed


# ---------------------------------------------------------------- liens
def test_liens_and_reduction_letter(app, owner):
    c, tok = owner
    mid = _mid(app)
    r = c.post(f"/pi/{mid}/liens/new", data={"_csrf": tok, "holder": "Blue Health Plan", "type": "health_plan",
                                             "original": "8,000.00", "reduced": "5,000.00", "status": "resolved"})
    assert r.status_code == 302
    r = c.post(f"/pi/{mid}/liens/new", data={"_csrf": tok, "holder": "Austin ER", "type": "medical",
                                             "original": "2500", "reduced": "", "status": "open"})
    assert r.status_code == 302
    from app.models import Lien, Document
    with app.app_context():
        liens = Lien.query.filter_by(matter_id=mid).order_by(Lien.id).all()
        assert [l.payable_cents for l in liens] == [500000, 250000]
        assert liens[1].reduced_cents is None
        lid = liens[1].id
    r = c.post(f"/pi/{mid}/liens/{lid}/reduction-letter", data={"_csrf": tok, "pct": "40"})
    assert r.status_code == 302 and "/documents/" in r.headers["Location"]
    with app.app_context():
        doc = Document.query.filter_by(matter_id=mid, folder="Liens").first()
        assert doc is not None and "Lien reduction request - Austin ER" in doc.name
        assert os.path.isfile(os.path.join(UPLOAD_DIR, doc.path))
        l = Lien.query.get(lid)
        assert l.status == "negotiating" and l.reduced_cents is None  # the letter asks; it does not change the figure
    r = c.post(f"/pi/{mid}/liens/{lid}/reduction-letter", data={"_csrf": tok, "pct": "150"}, follow_redirects=True)
    assert b"between 0 and 100" in r.data


# ---------------------------------------------------------------- demand
def test_demand_package(app, owner):
    c, tok = owner
    mid = _mid(app)
    r = c.post(f"/pi/{mid}/demand/package", data={"_csrf": tok, "demand_amount": "75,000.00", "mark_sent": "1"})
    assert r.status_code == 302 and "/documents/" in r.headers["Location"]
    from app.models import Document
    with app.app_context():
        doc = Document.query.filter_by(matter_id=mid, folder="Demand").first()
        assert doc is not None and doc.name == f"Demand package - {MATTER_NUMBER}.pdf"
        full = os.path.join(UPLOAD_DIR, doc.path)
        assert os.path.isfile(full) and os.path.getsize(full) > 1000
    case = _case(app, mid)
    assert case.demand_sent_on == date.today() and case.demand_amount_cents == 7500000 and case.stage == "demand"
    # the demand form saves the offer
    r = c.post(f"/pi/{mid}/demand", data={"_csrf": tok, "demand_sent_on": date.today().isoformat(),
                                          "demand_amount": "75000", "offer": "40,000.00"})
    assert r.status_code == 302
    assert _case(app, mid).offer_cents == 4000000


# ---------------------------------------------------------------- worksheet
def test_worksheet_math(app, owner):
    c, tok = owner
    mid = _mid(app)
    # a first draft that the real one replaces
    r = c.post(f"/pi/{mid}/worksheet", data={"_csrf": tok, "gross": "90,000.00", "fee_pct": "33.33"})
    assert r.status_code == 302
    r = c.post(f"/pi/{mid}/worksheet", data={"_csrf": tok, "gross": "100,000.00", "fee_pct": "33.33",
                                             "other_deductions": "", "extra_desc": "", "extra_amount": ""})
    assert r.status_code == 302
    from app.models import SettlementWorksheet
    import json
    with app.app_context():
        rows = SettlementWorksheet.query.filter_by(matter_id=mid).order_by(SettlementWorksheet.id).all()
        assert len(rows) == 2 and rows[0].is_current is False and rows[1].is_current is True
        ws = rows[1]
        assert ws.gross_cents == 10000000
        assert ws.fee_pct == 33.33 and ws.fee_cents == 3333000
        assert ws.costs_cents == 124500
        assert ws.liens_cents == 750000  # 5,000 reduced + 2,500 original
        assert ws.other_deductions_cents == 0
        assert ws.net_to_client_cents == 10000000 - 3333000 - 124500 - 750000 == 5792500
        assert ws.fee_cents + ws.costs_cents + ws.liens_cents + ws.other_deductions_cents + ws.net_to_client_cents == ws.gross_cents
        d = json.loads(ws.detail_json)
        assert d["balanced"] is True and len(d["expenses"]) == 1 and d["expenses"][0]["cents"] == 124500
        assert [l["cents"] for l in d["liens"]] == [500000, 250000]
        assert ws.status == "draft"
    r = c.get(f"/pi/{mid}")
    for s in (b"$100,000.00", b"$33,330.00", b"$1,245.00", b"$7,500.00", b"$57,925.00", b"balanced"):
        assert s in r.data, s


def test_approve_then_disburse_refused_without_funds(app, owner):
    c, tok = owner
    mid = _mid(app)
    from app.models import SettlementWorksheet, TrustTransaction, Matter
    with app.app_context():
        ws = SettlementWorksheet.query.filter_by(matter_id=mid, is_current=True).first()
        wid = ws.id
        cid = Matter.query.get(mid).client_id
    # cannot disburse a draft
    r = c.post(f"/pi/{mid}/worksheet/{wid}/disburse", data={"_csrf": tok, "record_deposit": "1"}, follow_redirects=True)
    assert b"Approve the current worksheet" in r.data
    r = c.post(f"/pi/{mid}/worksheet/{wid}/approve", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        ws = SettlementWorksheet.query.get(wid)
        assert ws.status == "approved" and ws.approved_on == date.today()
    # no deposit ticked and nothing in trust: refused, nothing written
    r = c.post(f"/pi/{mid}/worksheet/{wid}/disburse", data={"_csrf": tok}, follow_redirects=True)
    assert b"Refused" in r.data and b"overdraw" in r.data
    with app.app_context():
        assert TrustTransaction.query.filter_by(client_id=cid).count() == 0
        assert SettlementWorksheet.query.get(wid).status == "approved"


def test_disburse_with_deposit_posts_trust_rows(app, owner):
    c, tok = owner
    mid = _mid(app)
    from app.models import SettlementWorksheet, TrustTransaction, Matter, Contact, Lien, Firm
    with app.app_context():
        wid = SettlementWorksheet.query.filter_by(matter_id=mid, is_current=True).first().id
        cid = Matter.query.get(mid).client_id
    r = c.post(f"/pi/{mid}/worksheet/{wid}/disburse", data={"_csrf": tok, "record_deposit": "1"}, follow_redirects=True)
    assert r.status_code == 200 and b"Disbursement recorded" in r.data
    with app.app_context():
        ws = SettlementWorksheet.query.get(wid)
        assert ws.status == "disbursed" and ws.disbursed_on == date.today()
        txns = TrustTransaction.query.filter_by(client_id=cid).order_by(TrustTransaction.id).all()
        got = [(t.type, t.amount_cents, t.payee) for t in txns]
        firm = Firm.get().name
        client_name = Contact.query.get(cid).display_name
        assert got == [
            ("deposit", 10000000, "Acme Mutual"),
            ("to_operating", -3333000, firm),
            ("to_operating", -124500, firm),
            ("disbursement", -500000, "Blue Health Plan"),
            ("disbursement", -250000, "Austin ER"),
            ("disbursement", -5792500, client_name),
        ]
        assert all(t.matter_id == mid and t.reference == f"WS-{wid}" for t in txns)
        assert Contact.query.get(cid).trust_balance_cents() == 0
        assert Matter.query.get(mid).trust_balance_cents() == 0
        assert {l.status for l in Lien.query.filter_by(matter_id=mid)} == {"paid"}
        assert _case(app, mid).stage == "settled"
    # a second disbursement is refused
    r = c.post(f"/pi/{mid}/worksheet/{wid}/disburse", data={"_csrf": tok, "record_deposit": "1"}, follow_redirects=True)
    assert b"Approve the current worksheet" in r.data
    with app.app_context():
        assert TrustTransaction.query.filter_by(client_id=cid).count() == 6
    # the trust ledger page shows the postings
    r = c.get(f"/trust/ledger/{cid}")
    assert r.status_code == 200 and b"Net settlement to client" in r.data


def test_worksheet_pdf_saved_to_documents(app, owner):
    c, tok = owner
    mid = _mid(app)
    from app.models import SettlementWorksheet, Document
    with app.app_context():
        wid = SettlementWorksheet.query.filter_by(matter_id=mid, is_current=True).first().id
    r = c.post(f"/pi/{mid}/worksheet/{wid}/pdf", data={"_csrf": tok})
    assert r.status_code == 302 and "/documents/" in r.headers["Location"]
    with app.app_context():
        doc = Document.query.filter_by(matter_id=mid, folder="Settlement").first()
        assert doc is not None and doc.name.startswith("Settlement worksheet")
        assert os.path.isfile(os.path.join(UPLOAD_DIR, doc.path))
        assert SettlementWorksheet.query.get(wid).pdf_path == doc.path
    r = c.get(r.headers["Location"])
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"


# ---------------------------------------------------------------- tasks
def test_standard_tasks_created_once(app, owner):
    c, tok = owner
    mid = _mid(app)
    from app.models import Task
    with app.app_context():
        before = Task.query.filter_by(matter_id=mid).count()
    r = c.post(f"/pi/{mid}/tasks/standard", data={"_csrf": tok}, follow_redirects=True)
    assert r.status_code == 200 and b"standard PI task" in r.data
    with app.app_context():
        tasks = {t.title: t for t in Task.query.filter_by(matter_id=mid).all()}
        assert len(tasks) == before + 5
        assert tasks["Request records: Austin ER"].due_on == DOL + timedelta(days=30)
        assert tasks["Request records: Lamar Chiropractic"].due_on == DOL + timedelta(days=30)
        fu = tasks["Follow up on records request: Austin ER"]
        assert fu.due_on == date.today() + timedelta(days=30)
        assert "Follow up on records request: Lamar Chiropractic" not in tasks  # never requested
        sol = tasks["Statute of limitations check"]
        assert sol.kind == "deadline" and sol.due_on == DOL.replace(year=DOL.year + 2)
        assert "limitations period" in sol.notes
        assert tasks["Demand follow-up"].due_on == date.today() + timedelta(days=30)
    # idempotent by title
    r = c.post(f"/pi/{mid}/tasks/standard", data={"_csrf": tok}, follow_redirects=True)
    assert b"already exists" in r.data
    with app.app_context():
        assert Task.query.filter_by(matter_id=mid).count() == before + 5


def test_standard_tasks_need_date_of_loss(app, owner):
    c, tok = owner
    from app.models import Matter, Task
    with app.app_context():
        other = Matter.query.filter_by(number="M-1001").first().id
        before = Task.query.filter_by(matter_id=other).count()
    r = c.post(f"/pi/{other}/tasks/standard", data={"_csrf": tok}, follow_redirects=True)
    assert b"date of loss first" in r.data
    with app.app_context():
        assert Task.query.filter_by(matter_id=other).count() == before


# ---------------------------------------------------------------- board
def test_board_shows_case_in_its_stage(app, owner):
    c, tok = owner
    r = c.get("/pi")
    assert r.status_code == 200
    html = r.data.decode()
    assert MATTER_NUMBER in html and "Acme Mutual" in html and "$30,000.00" in html  # limits
    assert "$10,250.00" in html  # total billed
    assert "$75,000.00" in html and "$40,000.00" in html  # demand and offer
    # the settled card sits in the Settled column and the seeded M-1001 case in Intake
    settled = re.search(r'<h3><span>Settled</span>.*?</div>\s*<div class="picol">', html, re.S)
    assert settled and MATTER_NUMBER in settled.group(0)
    intake = re.search(r'<h3><span>Intake</span>.*?</div>\s*<div class="picol">', html, re.S)
    assert intake and "M-1001" in intake.group(0)


def test_no_em_dashes_in_module(app):
    for path in ("app/blueprints/pi.py", "app/templates/pi/case.html", "app/templates/pi/index.html",
                 "app/templates/pi/provider_form.html", "app/templates/pi/lien_form.html"):
        with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
            body = fh.read()
        # the PDF text cleaner maps an incoming em-dash to a hyphen; that one literal is allowed
        assert body.count("—") <= (1 if path.endswith("pi.py") else 0), path
        prose = re.sub(r"-{3,}", "", body.replace("<!--", "").replace("-->", ""))  # comment rulers are not prose
        assert "--" not in prose, path
