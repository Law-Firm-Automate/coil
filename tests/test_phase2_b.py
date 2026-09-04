"""Phase 2, Agent B: permissions, offices, audit log, matter templates, contact custom fields, fmt_money.

Own SQLite file (data/test_phase2_b.db) seeded by seed.py, own UPLOAD_DIR. Never touches data/practice.db.
Run: .venv/bin/python -m pytest tests/test_phase2_b.py -q
"""
import os
import re
import subprocess
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_phase2_b.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_phase2_b")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_phase2_b")

from tests.helpers import login  # noqa: E402


def _id_from(location):
    return int(re.search(r"/(\d+)(?:\?|$)", location).group(1))


def _tok(client):
    r = client.get("/")
    m = re.search(rb'name="_csrf" value="([^"]+)"', r.data)
    return m.group(1).decode() if m else None


@pytest.fixture(scope="module")
def app():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    env = dict(os.environ, DATABASE_URL=DB_URI)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], env=env, cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    from app import create_app
    a = create_app({"SQLALCHEMY_DATABASE_URI": DB_URI, "UPLOAD_DIR": UPLOAD_DIR, "PDF_DIR": PDF_DIR, "TESTING": True})
    with a.app_context():
        from app.extensions import db
        from app.models import User
        for email, role in (("ro@example.test", "readonly"), ("para@example.test", "paralegal"),
                            ("bill@example.test", "billing"), ("atty@example.test", "attorney"),
                            ("legacy@example.test", "staff")):
            u = User(email=email, name=email.split("@")[0].title(), role=role, initials=role[:2].upper())
            u.set_password("password123")
            db.session.add(u)
        db.session.commit()
    yield a


@pytest.fixture
def owner(app):
    c = app.test_client()
    tok = login(c)
    return c, tok


def _as(app, email):
    c = app.test_client()
    tok = login(c, email=email)
    return c, tok


def _matter_id(app):
    from app.models import Matter
    with app.app_context():
        return Matter.query.order_by(Matter.id).first().id


# ---------------------------------------------------------------- permissions
def test_readonly_can_read_but_not_post(app):
    c, tok = _as(app, "ro@example.test")
    mid = _matter_id(app)
    assert c.get(f"/matters/{mid}").status_code == 200
    assert c.get("/contacts").status_code == 200
    assert c.get("/invoices").status_code == 200
    r = c.post(f"/matters/{mid}/notes", data={"_csrf": tok, "body": "should be refused"})
    assert r.status_code == 403 and b"readonly" in r.data
    r = c.post("/contacts/new", data={"_csrf": tok, "kind": "person", "first_name": "No", "last_name": "Way"})
    assert r.status_code == 403
    r = c.post("/tasks/new", data={"_csrf": tok, "title": "nope"})
    assert r.status_code == 403
    # logout is a POST and must still work for readonly
    assert c.post("/logout", data={"_csrf": tok}).status_code == 302


def test_paralegal_blocked_from_trust_and_settings(app):
    c, tok = _as(app, "para@example.test")
    mid = _matter_id(app)
    assert c.get(f"/matters/{mid}").status_code == 200
    assert c.get("/trust/").status_code == 403 and c.get("/trust").status_code == 403
    assert c.get("/settings").status_code == 403
    assert c.get("/settings/audit").status_code == 403
    assert c.get("/invoices").status_code == 403
    assert c.get("/reports").status_code == 403
    assert c.get("/tasks").status_code == 200
    assert c.get("/calendar").status_code == 200
    # a paralegal can still write matters and time
    r = c.post(f"/matters/{mid}/notes", data={"_csrf": tok, "body": "paralegal note"})
    assert r.status_code == 302
    # and may open their own account page but nobody else's
    from app.models import User
    with app.app_context():
        me = User.query.filter_by(email="para@example.test").first().id
        other = User.query.filter_by(email="owner@example.com").first().id
    assert c.get(f"/settings/users/{me}/edit").status_code == 200
    assert c.get(f"/settings/users/{other}/edit").status_code == 403
    r = c.post(f"/settings/users/{me}/edit", data={"_csrf": tok, "name": "Para Legal", "email": "para@example.test",
                                                   "initials": "PL", "role": "owner", "is_active": "1"})
    assert r.status_code == 302
    with app.app_context():
        u = User.query.filter_by(email="para@example.test").first()
        assert u.name == "Para Legal" and u.role == "paralegal" and u.is_active  # role not escalated


def test_billing_reaches_trust_and_invoices_not_settings(app):
    c, tok = _as(app, "bill@example.test")
    assert c.get("/trust/").status_code == 200
    assert c.get("/invoices").status_code == 200
    assert c.get("/payments").status_code == 200
    assert c.get("/reports").status_code == 200
    assert c.get("/exports").status_code == 200
    assert c.get("/settings").status_code == 403
    assert c.get("/settings/offices").status_code == 403
    assert c.get("/dev/outbox").status_code == 403
    mid = _matter_id(app)
    assert c.get(f"/matters/{mid}").status_code == 200  # matters_view
    r = c.post(f"/matters/{mid}/notes", data={"_csrf": tok, "body": "billing cannot write matters"})
    assert r.status_code == 403


def test_attorney_and_legacy_staff_alias(app):
    from app.permissions import canonical_role, has_permission, permissions_for
    assert canonical_role("staff") == "attorney"
    assert permissions_for("staff") == permissions_for("attorney")
    for email in ("atty@example.test", "legacy@example.test"):
        c, tok = _as(app, email)
        assert c.get("/matters").status_code == 200
        assert c.get("/invoices").status_code == 200
        assert c.get("/reports").status_code == 200
        assert c.get("/trust/").status_code == 403 and c.get("/trust").status_code == 403
        assert c.get("/payments").status_code == 403
        assert c.get("/exports").status_code == 403
        assert c.get("/settings").status_code == 403
        assert c.get("/settings/templates").status_code == 200  # read the template list, cannot edit it
        assert c.get("/settings/templates/new").status_code == 403

    class U:
        role = "staff"
    assert has_permission(U(), "matters") and has_permission(U(), "billing_view") and not has_permission(U(), "trust")


def test_owner_reaches_everything(owner):
    c, tok = owner
    for path in ("/", "/contacts", "/matters", "/tasks", "/calendar", "/documents", "/time", "/invoices", "/reports",
                 "/trust/", "/payments", "/exports", "/settings", "/settings/users", "/settings/offices",
                 "/settings/audit", "/settings/templates", "/settings/integrations", "/dev/outbox", "/messages",
                 "/intake", "/engagements", "/conflicts"):
        r = c.get(path)
        assert r.status_code in (200, 302), (path, r.status_code)
        assert r.status_code != 403


def test_permission_required_decorator(app):
    from app.helpers import permission_required
    from flask import Flask
    calls = []

    @permission_required("trust")
    def view():
        calls.append(1)
        return "ok"

    with app.test_request_context("/x"):
        from app.helpers import current_user
        from flask import g, session
        from app.models import User
        session["user_id"] = User.query.filter_by(email="para@example.test").first().id
        g.pop("user", None)
        import werkzeug.exceptions
        with pytest.raises(werkzeug.exceptions.Forbidden):
            view()
        session["user_id"] = User.query.filter_by(email="bill@example.test").first().id
        g.pop("user", None)
        assert view() == "ok"


# ---------------------------------------------------------------- users, offices
def test_user_form_five_roles_cost_rate_office(app, owner):
    c, tok = owner
    r = c.get("/settings/users/new")
    assert r.status_code == 200
    for role in ("owner", "attorney", "paralegal", "billing", "readonly"):
        assert f'value="{role}"'.encode() in r.data
    assert b"cost_rate" in r.data and b"office_id" in r.data
    r = c.post("/settings/offices/new", data={"_csrf": tok, "name": "Downtown", "address": "1 Congress Ave\nAustin, TX",
                                              "phone": "512-555-0100", "is_default": "1"})
    assert r.status_code == 302
    from app.models import Office, User
    with app.app_context():
        off = Office.query.filter_by(name="Downtown").first()
        assert off and off.is_default
        oid = off.id
    r = c.post("/settings/users/new", data={"_csrf": tok, "name": "Pat Paralegal", "email": "pat@example.test",
                                            "role": "paralegal", "hourly_rate": "150", "cost_rate": "62.50",
                                            "office_id": str(oid), "password": "password123"})
    assert r.status_code == 302, r.data[:300]
    with app.app_context():
        u = User.query.filter_by(email="pat@example.test").first()
        assert u.role == "paralegal" and u.cost_rate_cents == 6250 and u.office_id == oid and u.hourly_rate_cents == 15000
    r = c.get("/settings/users")
    assert r.status_code == 200 and b"Pat Paralegal" in r.data and b"Downtown" in r.data and b"$62.50" in r.data
    # legacy "staff" still accepted as posted (older tests rely on it) and displays as attorney
    r = c.post("/settings/users/new", data={"_csrf": tok, "name": "Old Staff", "email": "old@example.test",
                                            "role": "staff", "hourly_rate": "100", "password": "password123"})
    assert r.status_code == 302
    with app.app_context():
        assert User.query.filter_by(email="old@example.test").first().role == "staff"
    r = c.get("/settings/users")
    assert b"legacy staff" in r.data


def test_office_delete_refused_while_referenced(app, owner):
    c, tok = owner
    r = c.post("/settings/offices/new", data={"_csrf": tok, "name": "North Branch", "address": "9 Elm St"})
    assert r.status_code == 302
    from app.models import Office, Matter, Contact
    with app.app_context():
        off = Office.query.filter_by(name="North Branch").first()
        oid = off.id
        client_id = Contact.query.filter_by(is_client=True).first().id
    # new matter picks the office
    r = c.post("/matters/new", data={"_csrf": tok, "client_id": client_id, "name": "Branch matter", "status": "open",
                                     "billing_type": "hourly", "hourly_rate": "300", "office_id": str(oid),
                                     "opened_on": date.today().isoformat()})
    assert r.status_code == 302, r.data[:300]
    mid = _id_from(r.headers["Location"])
    with app.app_context():
        assert Matter.query.get(mid).office_id == oid
    r = c.get(f"/matters/{mid}")
    assert b"North Branch" in r.data
    r = c.post(f"/settings/offices/{oid}/delete", data={"_csrf": tok}, follow_redirects=True)
    assert r.status_code == 200 and b"still used by" in r.data
    with app.app_context():
        assert Office.query.get(oid) is not None
    # unreference, then delete works
    r = c.post(f"/matters/{mid}/edit", data={"_csrf": tok, "client_id": client_id, "name": "Branch matter",
                                             "status": "open", "billing_type": "hourly", "hourly_rate": "300",
                                             "office_id": "", "opened_on": date.today().isoformat()})
    assert r.status_code == 302
    r = c.post(f"/settings/offices/{oid}/delete", data={"_csrf": tok}, follow_redirects=True)
    assert r.status_code == 200 and b"Deleted office" in r.data
    with app.app_context():
        assert Office.query.get(oid) is None
    # make-default toggle
    r = c.post("/settings/offices/new", data={"_csrf": tok, "name": "South Branch"})
    with app.app_context():
        south = Office.query.filter_by(name="South Branch").first()
        assert not south.is_default
        sid = south.id
    r = c.post(f"/settings/offices/{sid}/default", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        assert Office.query.get(sid).is_default
        assert Office.query.filter_by(is_default=True).count() == 1


# ---------------------------------------------------------------- audit log
def test_audit_page_filters_by_action(app, owner):
    c, tok = owner
    r = c.get("/settings/audit")
    assert r.status_code == 200 and b"Audit log" in r.data
    r = c.get("/settings/audit?action=create&entity=office")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Downtown" in body and "North Branch" in body
    rows = re.findall(r"<tr>\s*<td class=\"nowrap\">", body)
    # every row shown is a create: no "delete" or "update" action cells
    assert "<td>delete</td>" not in body and "<td>update</td>" not in body and "<td>create</td>" in body
    r = c.get("/settings/audit?action=delete&entity=office")
    assert b"North Branch" in r.data and b"<td>create</td>" not in r.data
    r = c.get("/settings/audit?q=North+Branch")
    assert b"North Branch" in r.data
    r = c.get("/settings/audit?action=nonexistent_action")
    assert b"Nothing matches" in r.data
    r = c.get("/settings/audit?entity=matter&entity_id=1")
    assert r.status_code == 200
    # paging params are accepted
    assert c.get("/settings/audit?page=2").status_code == 200
    assert c.get("/settings/audit?from=2020-01-01&to=2099-01-01&user_id=1").status_code == 200
    # matter activity tab links to the full log
    mid = _matter_id(app)
    r = c.get(f"/matters/{mid}?tab=activity")
    assert f"/settings/audit?entity=matter&entity_id={mid}".encode() in r.data


# ---------------------------------------------------------------- templates
def test_sample_templates_created_lazily(app, owner):
    c, tok = owner
    from app.models import MatterTemplate
    with app.app_context():
        MatterTemplate.query.delete()
        from app.extensions import db
        db.session.commit()
        assert MatterTemplate.query.count() == 0
    r = c.get("/settings/templates")
    assert r.status_code == 200
    with app.app_context():
        assert MatterTemplate.query.count() == 2
        names = {t.name for t in MatterTemplate.query.all()}
    assert any("injury" in n.lower() for n in names) and any("estate" in n.lower() for n in names)
    r = c.get("/settings/templates")
    with app.app_context():
        assert MatterTemplate.query.count() == 2  # not re-created


def test_template_create_and_matter_from_it(app, owner):
    c, tok = owner
    r = c.post("/settings/templates/new", data={
        "_csrf": tok, "name": "Test PI template", "practice_area": "Personal Injury", "description": "Test",
        "billing_type": "hybrid", "flat_fee": "1,000.00", "hourly_rate": "250", "contingency_pct": "25",
        "sol_years": "2", "sol_basis": "2-year PI", "trust_minimum": "500", "trust_replenish_to": "2,000",
        "is_active": "1",
        "ms_description": ["Retainer", ""], "ms_amount": ["1,000.00", ""], "ms_offset": ["0", ""],
        "t_title": ["Send rep letter", "Request records", ""], "t_kind": ["task", "deadline", "task"],
        "t_offset": ["3", "10", ""], "t_priority": ["high", "normal", "normal"],
        "t_assignee": ["responsible", "none", "responsible"],
        "cf_key": ["Claim number", "Carrier", ""], "cf_value": ["", "Acme Mutual", ""],
    })
    assert r.status_code == 302, r.data[:300]
    from app.models import MatterTemplate, Matter, Task, Contact, User
    with app.app_context():
        t = MatterTemplate.query.filter_by(name="Test PI template").first()
        assert t and t.billing_type == "hybrid" and t.flat_fee_cents == 100000 and t.hourly_rate_cents == 25000
        assert t.contingency_pct == 25.0 and t.sol_years == 2.0 and t.trust_minimum_cents == 50000
        assert t.trust_replenish_to_cents == 200000
        assert len(t.milestones) == 1 and t.milestones[0]["amount_cents"] == 100000 and t.milestones[0]["due_offset_days"] == 0
        assert [x["title"] for x in t.tasks] == ["Send rep letter", "Request records"]
        assert t.tasks[1]["assignee"] == "none" and t.tasks[1]["kind"] == "deadline" and t.tasks[0]["priority"] == "high"
        assert t.custom_fields == {"Claim number": "", "Carrier": "Acme Mutual"}
        tid = t.id
        client_id = Contact.query.filter_by(is_client=True).first().id
        owner_id = User.query.filter_by(email="owner@example.com").first().id
    # prefill
    r = c.get(f"/matters/new?template_id={tid}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Test PI template" in body and 'value="Acme Mutual"' in body and 'value="Claim number"' in body
    assert f'name="template_id" value="{tid}"' in body and "2-year PI" in body
    # create a matter from it; custom field Claim number set by hand in the form must survive
    opened = date(2026, 3, 15)
    r = c.post("/matters/new", data={
        "_csrf": tok, "client_id": client_id, "name": "PI from template", "practice_area": "Personal Injury",
        "status": "open", "billing_type": "hybrid", "flat_fee": "1,000.00", "hourly_rate": "250",
        "contingency_pct": "25", "opened_on": opened.isoformat(), "responsible_user_id": str(owner_id),
        "sol_basis": "2-year PI", "template_id": str(tid),
        "cf_key": ["Claim number", "Carrier"], "cf_value": ["CLM-77", "Acme Mutual"],
    })
    assert r.status_code == 302, r.data[:300]
    mid = _id_from(r.headers["Location"])
    with app.app_context():
        m = Matter.query.get(mid)
        assert m.template_id == tid
        assert m.sol_date == date(2028, 3, 15) and m.sol_basis == "2-year PI"
        assert m.custom_fields == {"Claim number": "CLM-77", "Carrier": "Acme Mutual"}
        assert m.trust_minimum_cents == 50000 and m.trust_replenish_to_cents == 200000
        assert [ms.description for ms in m.milestones] == ["Retainer"]
        assert m.milestones[0].due_on == opened and m.milestones[0].amount_cents == 100000
        tasks = Task.query.filter_by(matter_id=mid).order_by(Task.due_on).all()
        assert [(t.title, t.due_on, t.kind, t.priority, t.assignee_id) for t in tasks] == [
            ("Send rep letter", opened + timedelta(days=3), "task", "high", owner_id),
            ("Request records", opened + timedelta(days=10), "deadline", "normal", None),
        ]
    r = c.get(f"/matters/{mid}")
    assert r.status_code == 200 and b"Test PI template" in r.data and b"CLM-77" in r.data
    r = c.get(f"/matters/{mid}?tab=tasks")
    assert b"Send rep letter" in r.data
    # matter without a template still works and shows none
    r = c.post("/matters/new", data={"_csrf": tok, "client_id": client_id, "name": "Plain matter", "status": "open",
                                     "billing_type": "flat", "opened_on": date.today().isoformat()})
    assert r.status_code == 302
    with app.app_context():
        assert Matter.query.get(_id_from(r.headers["Location"])).template_id is None
    c.tid = tid


def test_apply_template_to_existing_matter_keeps_values(app, owner):
    c, tok = owner
    from app.models import MatterTemplate, Matter, Task, FlatFeeMilestone
    with app.app_context():
        tid = MatterTemplate.query.filter_by(name="Test PI template").first().id
        # the seeded hourly matter M-1002 has an SOL already and no custom fields
        m = Matter.query.filter_by(number="M-1002").first()
        mid = m.id
        old_sol = m.sol_date
        m.custom_fields = {"Carrier": "Keep Me Insurance"}
        from app.extensions import db
        db.session.commit()
        before_tasks = Task.query.filter_by(matter_id=mid).count()
    r = c.post(f"/matters/{mid}/apply-template", data={"_csrf": tok, "template_id": str(tid)}, follow_redirects=True)
    assert r.status_code == 200 and b"Applied Test PI template" in r.data
    with app.app_context():
        m = Matter.query.get(mid)
        assert m.template_id == tid
        assert m.custom_fields["Carrier"] == "Keep Me Insurance"  # existing value not overwritten
        assert m.custom_fields["Claim number"] == ""  # missing key added
        assert m.sol_date == old_sol  # existing SOL kept
        assert Task.query.filter_by(matter_id=mid).count() == before_tasks + 2
        assert FlatFeeMilestone.query.filter_by(matter_id=mid, description="Retainer").count() == 1
    # applying again is idempotent for rows
    r = c.post(f"/matters/{mid}/apply-template", data={"_csrf": tok, "template_id": str(tid)}, follow_redirects=True)
    with app.app_context():
        assert Task.query.filter_by(matter_id=mid).count() == before_tasks + 2
        assert FlatFeeMilestone.query.filter_by(matter_id=mid, description="Retainer").count() == 1
    # missing template id
    r = c.post(f"/matters/{mid}/apply-template", data={"_csrf": tok, "template_id": ""}, follow_redirects=True)
    assert b"Pick a template" in r.data


def test_template_duplicate_edit_delete(app, owner):
    c, tok = owner
    from app.models import MatterTemplate
    with app.app_context():
        src = MatterTemplate.query.filter_by(name="Test PI template").first()
        sid = src.id
    r = c.post(f"/settings/templates/{sid}/duplicate", data={"_csrf": tok})
    assert r.status_code == 302
    with app.app_context():
        dup = MatterTemplate.query.filter_by(name="Test PI template (copy)").first()
        assert dup and dup.tasks == src.tasks if False else dup is not None
        assert len(dup.tasks) == 2 and dup.custom_fields == {"Claim number": "", "Carrier": "Acme Mutual"}
        did = dup.id
    r = c.get(f"/settings/templates/{did}/edit")
    assert r.status_code == 200 and b"Send rep letter" in r.data
    r = c.post(f"/settings/templates/{did}/edit", data={"_csrf": tok, "name": "Renamed copy", "billing_type": "flat",
                                                        "is_active": "1"})
    assert r.status_code == 302
    with app.app_context():
        assert MatterTemplate.query.get(did).name == "Renamed copy"
    # unused: deleted. used (src): deactivated instead
    r = c.post(f"/settings/templates/{did}/delete", data={"_csrf": tok}, follow_redirects=True)
    assert b"Deleted template" in r.data
    r = c.post(f"/settings/templates/{sid}/delete", data={"_csrf": tok}, follow_redirects=True)
    assert b"deactivated instead" in r.data
    with app.app_context():
        assert MatterTemplate.query.get(did) is None
        assert MatterTemplate.query.get(sid).is_active is False
    r = c.get("/settings/templates")
    assert r.status_code == 200 and b"inactive" in r.data


# ---------------------------------------------------------------- contacts
def test_contact_custom_fields_and_language_roundtrip(app, owner):
    c, tok = owner
    r = c.post("/contacts/new", data={
        "_csrf": tok, "kind": "person", "first_name": "Lucia", "last_name": "Fernandez", "is_client": "1",
        "language": "es", "ledes_client_id": "CL-4471",
        "cf_key": ["Date of birth", "Referral source", ""], "cf_value": ["1980-04-02", "Google", ""],
    })
    assert r.status_code == 302, r.data[:300]
    cid = _id_from(r.headers["Location"])
    from app.models import Contact
    with app.app_context():
        ct = Contact.query.get(cid)
        assert ct.language == "es" and ct.ledes_client_id == "CL-4471"
        assert ct.custom_fields == {"Date of birth": "1980-04-02", "Referral source": "Google"}
    r = c.get(f"/contacts/{cid}")
    assert r.status_code == 200
    assert b"Spanish" in r.data and b"CL-4471" in r.data and b"Referral source" in r.data and b"1980-04-02" in r.data
    r = c.get(f"/contacts/{cid}/edit")
    assert b'value="Date of birth"' in r.data and b'value="1980-04-02"' in r.data
    assert b'<option value="es" selected>' in r.data
    # edit: drop a field, change language back to firm default
    r = c.post(f"/contacts/{cid}/edit", data={
        "_csrf": tok, "kind": "person", "first_name": "Lucia", "last_name": "Fernandez", "is_client": "1",
        "language": "", "ledes_client_id": "CL-4471", "cf_key": ["Referral source"], "cf_value": ["Yelp"],
    })
    assert r.status_code == 302
    with app.app_context():
        ct = Contact.query.get(cid)
        assert ct.language == "" and ct.custom_fields == {"Referral source": "Yelp"}
    r = c.get(f"/contacts/{cid}")
    assert b"firm default" in r.data and b"Yelp" in r.data


# ---------------------------------------------------------------- money
def test_fmt_money_and_cur_filter(app):
    from app.helpers import fmt_money
    assert fmt_money(123456, "GBP") == "£1,234.56"
    assert fmt_money(123456, "USD") == "$1,234.56"
    assert fmt_money(123456) == "$1,234.56"
    assert fmt_money(-500, "EUR") == "(€5.00)"
    assert fmt_money(100, "CAD") == "CA$1.00"
    assert fmt_money(100, "AUD") == "A$1.00"
    assert fmt_money(100, "MXN") == "MX$1.00"
    assert fmt_money(100, "xyz") == "XYZ 1.00"
    assert fmt_money(100, None) == "$1.00"
    from app.helpers import cents_to_str
    assert cents_to_str(123456) == "$1,234.56"  # money() unchanged
    out = app.jinja_env.from_string("{{ 250|cur('GBP') }} {{ 250|cur(code) }}").render(code="EUR")
    assert out == "£2.50 €2.50"
