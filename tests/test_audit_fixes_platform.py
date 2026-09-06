"""Regressions for three audit findings on the platform slice.

1. (high) app/permissions.py inverted the role map: readonly held every "<area>_view" including settings_view and
   exports_view, so the least privileged role opened owner-only pages (and the webhook signing secret) that
   attorney, paralegal and billing were refused. Several data-bearing prefixes were missing from PREFIX_PERMS
   entirely and fell through to "any signed-in user".
2. (high) importer.prep_matters treated ANY matter with the same number as an update target, including matters
   Coil created itself and never imported, and rewrote its name, client, status and dates.
3. (medium) /settings/api was owner-only, so a non-owner could not mint the per-user token the Chrome extension
   needs and would have to log time under the owner's name.

Own SQLite file, own UPLOAD_DIR. Run: .venv/bin/python -m pytest tests/test_audit_fixes_platform.py -q
"""
import io
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_audit_fixes_platform.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_audit_fixes_platform")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_audit_fixes_platform")

from tests.helpers import login  # noqa: E402

ROLE_EMAILS = {"owner": "owner@example.com", "attorney": "af_atty@example.test", "paralegal": "af_para@example.test",
               "billing": "af_bill@example.test", "readonly": "af_ro@example.test"}


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
        for role, email in ROLE_EMAILS.items():
            if role == "owner":
                continue
            u = User(email=email, name=role.title() + " User", role=role, initials=role[:2].upper())
            u.set_password("password123")
            db.session.add(u)
        db.session.commit()
    yield a


def _as(app, role):
    c = app.test_client()
    c.tok = login(c, email=ROLE_EMAILS[role])
    return c


@pytest.fixture(scope="module")
def clients(app):
    return {role: _as(app, role) for role in ROLE_EMAILS}


def _matter_id(app):
    from app.models import Matter
    with app.app_context():
        return Matter.query.order_by(Matter.id).first().id


# ---------------------------------------------------------------- 1. permission matrix
# path -> the roles that may GET it. Everyone else must get 403.
def _matrix(mid):
    everyone = {"owner", "attorney", "paralegal", "billing", "readonly"}
    case_work = {"owner", "attorney", "paralegal", "readonly"}   # matters area, readonly read-only
    money = {"owner", "billing"}                                  # trust / payments / accounting
    owner_only = {"owner"}
    return {
        "/": everyone,
        "/matters": everyone,
        "/contacts": everyone,
        "/tasks": everyone,
        "/conflicts": everyone,
        "/intake": everyone,
        "/engagements": everyone,
        "/time": {"owner", "attorney", "paralegal", "billing", "readonly"},
        "/documents": case_work,
        "/signatures": case_work,
        "/calendar": case_work,
        "/messages": case_work,
        "/invoices": {"owner", "attorney", "billing", "readonly"},
        "/reports": {"owner", "attorney", "billing", "readonly"},
        "/trust/": money,
        "/payments": money,
        "/exports": money,
        # prefixes the audit found missing from PREFIX_PERMS
        "/statements": money,
        "/money/plans": money,
        "/accounting/": money,
        # matter sub-modules: everyone who can read a matter can read these; billing holds matters_view so it
        # reads them but (asserted below) cannot write.
        "/voice": everyone,
        "/audit": everyone,
        "/pi": everyone,
        "/criminal": everyone,
        "/discovery": everyone,
        f"/records/{mid}": everyone,
        "/research": everyone,
        "/doctemplates": case_work,
        # settings and firm administration: owner only, and readonly must NOT be the exception
        "/settings": owner_only,
        "/settings/integrations": owner_only,
        "/settings/webhooks": owner_only,
        "/settings/rules": owner_only,
        "/settings/audit": owner_only,
        "/settings/users": owner_only,
        "/settings/offices": owner_only,
        "/dev/outbox": owner_only,
        "/import": owner_only,
    }


def test_permission_matrix_get(app, clients):
    mid = _matter_id(app)
    problems = []
    for path, allowed in _matrix(mid).items():
        for role, c in clients.items():
            code = c.get(path).status_code
            if role in allowed and code == 403:
                problems.append(f"{role} should reach {path} but got 403")
            if role not in allowed and code != 403:
                problems.append(f"{role} must NOT reach {path} but got {code}")
    assert not problems, "\n".join(problems)


def test_new_prefixes_are_write_gated(app, clients):
    """The added prefixes must gate writes too, not just reads."""
    mid = _matter_id(app)
    posts = [("/pi/start", {"matter_id": str(mid)}),
             ("/criminal/start", {"matter_id": str(mid)}),
             (f"/records/{mid}/overview/note", {"note": "x"}),
             ("/audit/run", {"matter_id": str(mid)})]
    for role in ("billing", "readonly"):
        c = clients[role]
        for path, data in posts:
            r = c.post(path, data={"_csrf": c.tok, **data})
            assert r.status_code == 403, f"{role} POST {path} -> {r.status_code}"
    # money prefixes refuse everyone but owner and billing, for reads as well as writes
    for role in ("attorney", "paralegal", "readonly"):
        c = clients[role]
        assert c.post("/money/plans/new", data={"_csrf": c.tok}).status_code == 403
        assert c.get(f"/statements/1").status_code == 403


def test_readonly_is_not_the_most_privileged_role(app, clients):
    """The exact audit repro: readonly opened owner-only settings pages and the trust/contact exports."""
    ro = clients["readonly"]
    for path in ("/settings", "/settings/api", "/settings/integrations", "/settings/webhooks", "/settings/rules",
                 "/exports/trust.csv", "/exports/contacts.csv"):
        assert ro.get(path).status_code == 403, path


def test_readonly_still_reads_its_own_areas(app, clients):
    ro = clients["readonly"]
    for path in ("/", "/matters", "/contacts", "/invoices", "/reports", "/time"):
        assert ro.get(path).status_code == 200, path
    mid = _matter_id(app)
    r = ro.post(f"/matters/{mid}/notes", data={"_csrf": ro.tok, "body": "must be refused"})
    assert r.status_code == 403


def test_role_perms_shape(app):
    from app.permissions import ROLE_PERMS, has_permission

    class U:
        def __init__(self, role):
            self.role = role

    assert "settings_view" not in ROLE_PERMS["readonly"] and "settings" not in ROLE_PERMS["readonly"]
    for perm in ("settings", "settings_view", "trust_view", "payments_view", "exports_view", "accounting_view"):
        assert not has_permission(U("readonly"), perm), perm
    # readonly is a view-only mirror of the attorney, never wider
    for perm in ROLE_PERMS["readonly"]:
        base = perm[:-5] if perm.endswith("_view") else perm
        assert has_permission(U("attorney"), base), perm


def test_webhook_secret_is_not_printed_in_full(app, clients):
    from app.extensions import db
    from app.models import Webhook
    secret = "sup3rsecretwebhooksigningkey987654"
    with app.app_context():
        db.session.add(Webhook(url="https://hooks.example.test/coil", events="invoice.sent", secret=secret,
                               is_active=True))
        db.session.commit()
    html = clients["owner"].get("/settings/webhooks").data.decode()
    assert "Signing secret" in html or "signing secret" in html
    assert secret not in html, "the full webhook signing secret is rendered on the page"
    assert "forge" in html.lower() or "sign" in html.lower()


# ---------------------------------------------------------------- 2. importer number collision
def _upload(c, entity, body, filename, source="clio"):
    data = {"_csrf": c.tok, "source": source,
            "file": (io.BytesIO(body.encode() if isinstance(body, str) else body), filename)}
    r = c.post(f"/import/{entity}/upload", data=data, content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:400]
    return r.headers["Location"].rsplit("/", 1)[-1]


def _commit(c, token, **extra):
    r = c.post(f"/import/preview/{token}", data={"_csrf": c.tok, "do": "commit", **extra})
    assert r.status_code == 302, r.data[:600]
    loc = r.headers["Location"]
    assert "/import/jobs/" in loc, loc
    return int(loc.rsplit("/", 1)[-1])


def _matter_by_number(app, number):
    from app.models import Matter
    with app.app_context():
        m = Matter.query.filter_by(number=number).first()
        return None if m is None else {"id": m.id, "name": m.name, "client": m.client.display_name,
                                       "status": m.status}


def test_import_must_not_overwrite_an_unlinked_matter(app, clients):
    """Audit repro: a Clio row whose Display Number collides with a Coil-assigned number for a DIFFERENT client
    silently renamed and reassigned the Coil matter."""
    c = clients["owner"]
    before = _matter_by_number(app, "M-1001")
    assert before and before["name"] == "Alvarez Estate Plan", before

    tok = _upload(c, "contacts", "Id,Name,Type,Email\nZC1,Zed Newclient,Person,zed@example.test\n", "contacts.csv")
    _commit(c, tok)

    tok = _upload(c, "matters",
                  "Unique ID,Display Number,Description,Client Name,Status,Open Date\n"
                  "ZX1,M-1001,Zed Injury Claim,Zed Newclient,Open,03/01/2026\n", "matters.csv")
    job_id = _commit(c, tok)

    after = _matter_by_number(app, "M-1001")
    assert after["id"] == before["id"], "M-1001 was replaced"
    assert after["name"] == "Alvarez Estate Plan", f"M-1001 was overwritten: {after}"
    assert after["client"] == before["client"], f"M-1001 was reassigned to another client: {after}"

    from app.models import Matter
    with app.app_context():
        zed = Matter.query.filter_by(name="Zed Injury Claim").first()
        assert zed is not None, "the incoming matter was not created at all"
        assert zed.number and zed.number != "M-1001", f"incoming matter kept the taken number: {zed.number}"
        assert zed.client.display_name == "Zed Newclient"

    html = c.get(f"/import/jobs/{job_id}").data.decode()
    assert "M-1001" in html and "Zed Injury Claim" in html, "the collision was not reported on the job page"
    assert "Alvarez Estate Plan" in html, "the warning does not name the matter that was protected"


def test_import_still_updates_a_matter_it_imported_before(app, clients):
    """The ExternalRef link, not the number, is what makes a row an update. Re-importing must not duplicate."""
    c = clients["owner"]
    tok = _upload(c, "matters",
                  "Unique ID,Display Number,Description,Client Name,Status,Open Date\n"
                  "ZX9,IMP-9001,Zed Contract Review,Zed Newclient,Open,03/02/2026\n", "matters2.csv")
    _commit(c, tok)
    from app.models import Matter, ExternalRef
    with app.app_context():
        m = Matter.query.filter_by(number="IMP-9001").first()
        assert m is not None and m.name == "Zed Contract Review"
        assert ExternalRef.query.filter_by(source="clio", entity="matter", external_id="ZX9").first().coil_id == m.id
        first_id = m.id

    tok = _upload(c, "matters",
                  "Unique ID,Display Number,Description,Client Name,Status,Open Date\n"
                  "ZX9,IMP-9001,Zed Contract Review (renamed),Zed Newclient,Open,03/02/2026\n", "matters3.csv")
    job_id = _commit(c, tok)
    with app.app_context():
        assert Matter.query.filter_by(number="IMP-9001").count() == 1
        m = Matter.query.filter_by(number="IMP-9001").first()
        assert m.id == first_id and m.name == "Zed Contract Review (renamed)"
        from app.models import ImportJob
        j = ImportJob.query.get(job_id)
        assert j.updated == 1 and j.created == 0, (j.created, j.updated)


def test_import_collision_warning_names_both_matters(app, clients):
    """A second collision, checked at the preview stage so the firm sees it before committing."""
    c = clients["owner"]
    tok = _upload(c, "matters",
                  "Unique ID,Display Number,Description,Client Name,Status,Open Date\n"
                  "ZX2,M-1002,Zed Second Claim,Zed Newclient,Open,03/03/2026\n", "matters4.csv")
    html = c.get(f"/import/preview/{tok}").data.decode()
    assert "M-1002" in html
    assert "Bluebonnet" in html, "the preview does not name the existing matter the number belongs to"


# ---------------------------------------------------------------- 3. per-user API tokens
def test_non_owner_can_create_and_revoke_their_own_token(app, clients):
    from app.models import ApiToken
    c = clients["attorney"]
    assert c.get("/settings/api").status_code == 200
    r = c.post("/settings/api", data={"_csrf": c.tok, "name": "Chrome extension (attorney)", "scopes": "read,write"})
    assert r.status_code == 302, r.data[:400]
    with app.app_context():
        from app.models import User
        uid = User.query.filter_by(email=ROLE_EMAILS["attorney"]).first().id
        t = ApiToken.query.filter_by(name="Chrome extension (attorney)").first()
        assert t is not None and t.user_id == uid, "the token was not filed under the signed-in user"
        tid = t.id
    r = c.post(f"/settings/api/{tid}/revoke", data={"_csrf": c.tok})
    assert r.status_code == 302
    with app.app_context():
        assert ApiToken.query.get(tid).revoked_at is not None


def test_paralegal_can_mint_a_token_but_readonly_cannot(app, clients):
    assert clients["paralegal"].get("/settings/api").status_code == 200
    assert clients["readonly"].get("/settings/api").status_code == 403


def test_token_list_does_not_leak_other_users_tokens(app, clients):
    from app.extensions import db
    from app.models import ApiToken, User
    with app.app_context():
        owner = User.query.filter_by(email=ROLE_EMAILS["owner"]).first()
        t = ApiToken(user_id=owner.id, name="Owner private token", token_hash="af-hash-1", prefix="ownerpre",
                     scopes="read,write")
        db.session.add(t)
        db.session.commit()
    html = clients["attorney"].get("/settings/api").data.decode()
    assert "Owner private token" not in html, "a non-owner sees another user's token"
    assert "ownerpre" not in html, "a non-owner sees another user's token prefix"
    owner_html = clients["owner"].get("/settings/api").data.decode()
    assert "Owner private token" in owner_html and "Chrome extension (attorney)" in owner_html


def test_non_owner_cannot_revoke_someone_elses_token(app, clients):
    from app.models import ApiToken
    with app.app_context():
        tid = ApiToken.query.filter_by(name="Owner private token").first().id
    c = clients["paralegal"]
    assert c.post(f"/settings/api/{tid}/revoke", data={"_csrf": c.tok}).status_code == 403
    with app.app_context():
        assert ApiToken.query.get(tid).revoked_at is None
