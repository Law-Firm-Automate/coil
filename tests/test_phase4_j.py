"""Phase 4, Agent J: legal research on CourtListener (/research/*).

Own SQLite file (data/test_phase4_j.db) seeded by seed.py. No network: the client transport (cl._get / cl._post)
is monkeypatched with canned JSON shaped like the real v4 API, and one test monkeypatches requests.get to check the
wire request. The fixture blanks the AI keys so no model call can leak.
"""
import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.helpers import login  # noqa: E402

TEST_DB = os.path.join(ROOT, "data", "test_phase4_j.db")
UPLOAD_DIR = os.path.join(ROOT, "data", "test_phase4_j_uploads")
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
                              "COURTLISTENER_TOKEN": ""})
    yield application


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    S["tok"] = login(c)
    return c


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    from app.blueprints import _courtlistener as cl
    cl.clear_cache()
    monkeypatch.delenv("COURTLISTENER_TOKEN", raising=False)
    # app.llm reads keys from the environment at call time, so a key in the developer's shell must not leak.
    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "LLM_ENABLED", "LLM_DAILY_CAP", "AI_DAILY_CAP_CENTS"):
        monkeypatch.delenv(k, raising=False)
    yield
    cl.clear_cache()


def _models():
    from app.extensions import db
    from app import models
    return db, models


# ---------------------------------------------------------------- canned API JSON (shape of the real v4 responses)
SEARCH_JSON = {
    "count": 2,
    "next": "https://www.courtlistener.com/api/rest/v4/search/?cursor=abc123&q=miranda&type=o",
    "previous": None,
    "results": [
        {"absolute_url": "/opinion/107252/miranda-v-arizona/", "caseName": "Miranda v. Arizona",
         "caseNameFull": "Ernesto A. MIRANDA v. State of ARIZONA", "citation": ["384 U.S. 436", "86 S. Ct. 1602"],
         "citeCount": 21540, "cluster_id": 107252, "court": "Supreme Court of the United States", "court_id": "scotus",
         "dateFiled": "1966-06-13", "docketNumber": "759", "status": "Published", "syllabus": "",
         "opinions": [{"id": 107252, "snippet": "prior to any questioning, the person must be warned that he has a "
                                                "right to remain silent during custodial interrogation", "type": "010combined"}]},
        {"absolute_url": "/opinion/4495611/state-v-edwards/", "caseName": "State v. Edwards", "caseNameFull": "",
         "citation": ["2018 Ohio 1739"], "citeCount": 2, "cluster_id": 4495611, "court": "Ohio Court of Appeals",
         "court_id": "ohioctapp", "dateFiled": "2018-05-04", "docketNumber": "WD-17-016", "status": "Unpublished",
         "syllabus": "", "opinions": [{"id": 4272864, "snippet": "<mark>Miranda</mark> warnings were given", "type": "combined-opinion"}]},
    ],
}
OPINION_JSON = {
    "id": 107252, "cluster": "https://www.courtlistener.com/api/rest/v4/clusters/107252/", "type": "010combined",
    "author_str": "Warren", "download_url": "", "plain_text": "",
    "html_with_citations": "<div><p>MR. CHIEF JUSTICE WARREN delivered the opinion of the Court.</p>"
                           "<p>The cases before us raise questions which go to the roots of our concepts of American "
                           "criminal jurisprudence: the restraints society must observe consistent with the Federal "
                           "Constitution in prosecuting individuals for crime.</p><p>Prior to any questioning, the person "
                           "must be warned that he has a right to remain silent &amp; that any statement he does make may "
                           "be used as evidence against him.</p></div>",
}
CLUSTER_JSON = {
    "id": 107252, "absolute_url": "/opinion/107252/miranda-v-arizona/", "case_name": "Miranda v. Arizona",
    "case_name_full": "Ernesto A. MIRANDA v. State of ARIZONA", "date_filed": "1966-06-13", "citation_count": 21540,
    "docket_number": "759", "precedential_status": "Published", "judges": "Warren", "syllabus": "", "summary": "",
    "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/107252/"],
    "citations": [{"volume": 384, "reporter": "U.S.", "page": "436", "type": 1},
                  {"volume": 86, "reporter": "S. Ct.", "page": "1602", "type": 3}],
}
CITE_JSON = [
    {"citation": "384 U.S. 436", "normalized_citations": ["384 U.S. 436"], "start_index": 22, "end_index": 34,
     "status": 200, "error_message": "",
     "clusters": [{"id": 107252, "case_name": "Miranda v. Arizona", "absolute_url": "/opinion/107252/miranda-v-arizona/",
                   "date_filed": "1966-06-13", "citations": [{"volume": 384, "reporter": "U.S.", "page": "436"}]}]},
    {"citation": "999 F.3d 9999", "normalized_citations": ["999 F.3d 9999"], "start_index": 60, "end_index": 73,
     "status": 404, "error_message": "Citation not found: '999 F.3d 9999'", "clusters": []},
]


def _fake_transport(monkeypatch, search=SEARCH_JSON, opinion=OPINION_JSON, cluster=CLUSTER_JSON, cite=CITE_JSON,
                    error=None):
    """Replace cl._get / cl._post. `error` (dict) makes every call fail the way the real client would report it."""
    from app.blueprints import _courtlistener as cl
    calls = []

    def fake_get(path, params=None):
        calls.append(("GET", path, dict(params or {})))
        if error:
            return error
        if path == "/search/":
            return {"ok": True, "data": search}
        if path.startswith("/opinions/"):
            return {"ok": True, "data": opinion}
        if path.startswith("/clusters/"):
            return {"ok": True, "data": cluster}
        return cl._error("not_found", "nope", 404)

    def fake_post(path, data=None):
        calls.append(("POST", path, dict(data or {})))
        if error:
            return error
        assert path == "/citation-lookup/"
        return {"ok": True, "data": cite}
    monkeypatch.setattr(cl, "_get", fake_get)
    monkeypatch.setattr(cl, "_post", fake_post)
    return calls


def _matter(app, number="M-1001"):
    db, M = _models()
    with app.app_context():
        return M.Matter.query.filter_by(number=number).first().id


# ---------------------------------------------------------------- client wire request
def test_client_builds_request_with_token_header(app, monkeypatch):
    import requests
    from app.blueprints import _courtlistener as cl
    seen = {}

    class R:
        status_code = 200

        def json(self):
            return SEARCH_JSON

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers, timeout=timeout)
        return R()
    monkeypatch.setattr(requests, "get", fake_get)
    db, M = _models()
    with app.app_context():
        f = M.Firm.get()
        f.courtlistener_token = "firm-token-123"
        db.session.commit()
        out = cl.search("custodial interrogation", court="scotus", filed_after="1960-01-01", filed_before="1970-12-31",
                        order_by="dateFiled desc")
        assert out["ok"] and out["count"] == 2 and out["next_cursor"] == "abc123"
        assert seen["url"] == "https://www.courtlistener.com/api/rest/v4/search/"
        assert seen["params"] == {"q": "custodial interrogation", "type": "o", "court": "scotus",
                                  "filed_after": "1960-01-01", "filed_before": "1970-12-31", "order_by": "dateFiled desc"}
        assert seen["headers"]["Authorization"] == "Token firm-token-123"
        assert seen["timeout"] == 20
        # normalisation: citations flattened, <mark> stripped from the API snippet, absolute URL built
        r = out["results"]
        assert r[0]["case_name"] == "Miranda v. Arizona" and r[0]["citations"] == ["384 U.S. 436", "86 S. Ct. 1602"]
        assert r[1]["snippet"] == "Miranda warnings were given"
        assert r[0]["url"] == "https://www.courtlistener.com/opinion/107252/miranda-v-arizona/"
        # cache: a second identical search does not hit the network
        seen.clear()
        assert cl.search("custodial interrogation", court="scotus", filed_after="1960-01-01", filed_before="1970-12-31",
                         order_by="dateFiled desc")["ok"]
        assert seen == {}
        # env token used when the firm field is blank; no header at all when neither is set
        f.courtlistener_token = ""
        db.session.commit()
        app.config["COURTLISTENER_TOKEN"] = "env-token-9"
        cl.search("second query")
        assert seen["headers"]["Authorization"] == "Token env-token-9"
        app.config["COURTLISTENER_TOKEN"] = ""
        cl.search("third query")
        assert "Authorization" not in seen["headers"]


def test_client_classifies_429_and_401(app, monkeypatch):
    import requests
    from app.blueprints import _courtlistener as cl

    class R:
        def __init__(self, code, detail):
            self.status_code, self._d = code, detail

        def json(self):
            return {"detail": self._d}
    responses = [R(429, "Request was throttled. Rate limit exceeded: 5/min. Expected available in 23 seconds."),
                 R(401, "Authentication credentials were not provided.")]
    monkeypatch.setattr(requests, "get", lambda *a, **k: responses.pop(0))
    with app.app_context():
        out = cl.search("throttled query")
        assert out["ok"] is False and out["error"] == "rate_limited" and "23 seconds" in out["message"]
        out = cl.opinion(5)
        assert out["ok"] is False and out["error"] == "auth" and "free" in out["message"]
        # timeouts surface as a structured error too
        def boom(*a, **k):
            raise requests.Timeout()
        monkeypatch.setattr(requests, "get", boom)
        out = cl.search("slow query")
        assert out["ok"] is False and out["error"] == "timeout"


def test_html_to_paragraphs_keeps_paragraphs():
    from app.blueprints._courtlistener import html_to_paragraphs
    paras = html_to_paragraphs("<p>One &amp; two.</p><p>Three<br>four.</p><script>x()</script>")
    assert paras == ["One & two.", "Three four."]
    assert html_to_paragraphs("line one\nstill one\n\nsecond para") == ["line one still one", "second para"]


# ---------------------------------------------------------------- search page
def test_search_renders_results_and_highlights(app, client, monkeypatch):
    calls = _fake_transport(monkeypatch)
    r = client.get("/research?q=miranda+interrogation&court=scotus&order_by=citeCount+desc")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Miranda v. Arizona" in html and "384 U.S. 436" in html and "State v. Edwards" in html
    assert "<mark>Miranda</mark>" in html and "<mark>interrogation</mark>" in html
    assert "Supreme Court of the United States" in html and "1966-06-13" in html
    assert 'href="https://www.courtlistener.com/opinion/107252/miranda-v-arizona/"' in html
    assert "/research/opinion/107252?cluster_id=107252" in html
    assert "free public database" in html and html.count("free public database") == 1
    assert "Next page" in html and "cursor=abc123" in html
    assert calls[0][2]["court"] == "scotus" and calls[0][2]["order_by"] == "citeCount desc"
    # an unknown court id is dropped rather than sent through
    client.get("/research?q=miranda&court=bogus")
    assert "court" not in calls[-1][2]


def test_search_api_error_renders_calm_notice(app, client, monkeypatch):
    from app.blueprints import _courtlistener as cl
    _fake_transport(monkeypatch, error=cl._error("rate_limited", "CourtListener is rate limiting requests right now. Try again in a minute.", 429))
    r = client.get("/research?q=anything+at+all")
    assert r.status_code == 200
    html = r.data.decode()
    assert "CourtListener is busy" in html and "rate limiting" in html
    assert "Miranda" not in html
    # the citation check and opinion pages show the same calm notice
    r = client.post("/research/cite-check", data={"text": "See 384 U.S. 436.", "_csrf": S["tok"]})
    assert r.status_code == 200 and "rate limiting" in r.data.decode()
    r = client.get("/research/opinion/107252?cluster_id=107252")
    assert r.status_code == 200 and "rate limiting" in r.data.decode()


def test_search_without_query_shows_form(client, monkeypatch):
    calls = _fake_transport(monkeypatch)
    r = client.get("/research")
    assert r.status_code == 200 and b"Search case law" in r.data and b"U.S. Supreme Court" in r.data
    assert calls == []


# ---------------------------------------------------------------- save to matter
def test_save_to_matter_creates_saved_authority(app, client, monkeypatch):
    db, M = _models()
    mid = _matter(app)
    r = client.post("/research/save", data={
        "matter_id": mid, "source_id": "107252", "case_name": "Miranda v. Arizona", "citation": "384 U.S. 436; 86 S. Ct. 1602",
        "court": "Supreme Court of the United States", "decided_on": "1966-06-13",
        "url": "https://www.courtlistener.com/opinion/107252/miranda-v-arizona/",
        "snippet": "prior to any questioning", "note": "Warnings requirement", "next": "/research?q=miranda",
        "_csrf": S["tok"]})
    assert r.status_code == 302 and r.headers["Location"].endswith("/research?q=miranda")
    with app.app_context():
        a = M.SavedAuthority.query.filter_by(source_id="107252").first()
        assert a and a.matter_id == mid and a.citation.startswith("384 U.S. 436") and a.notes == "Warnings requirement"
        assert a.decided_on.isoformat() == "1966-06-13" and a.source == "courtlistener" and a.saved_by_id
        assert M.AuditLog.query.filter_by(action="create", entity="saved_authority", entity_id=a.id).count() == 1
    # missing matter is refused politely
    r = client.post("/research/save", data={"case_name": "X v. Y", "_csrf": S["tok"]}, follow_redirects=True)
    assert r.status_code == 200 and b"Choose a matter" in r.data


# ---------------------------------------------------------------- opinion reader
def test_opinion_page_renders_text_and_metadata(app, client, monkeypatch):
    calls = _fake_transport(monkeypatch)
    r = client.get("/research/opinion/107252?cluster_id=107252&q=remain+silent")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Miranda v. Arizona" in html
    assert "delivered the opinion of the Court" in html
    assert "<mark>remain</mark> <mark>silent</mark>" in html
    assert "right to <mark>remain</mark> <mark>silent</mark> &amp; that any statement" in html
    assert "384 U.S. 436; 86 S. Ct. 1602" in html
    assert "21540 opinions" in html  # cited-by count from the cluster
    assert "Save to matter" in html
    assert "free public database" in html and html.count("free public database") == 1
    # the model is not configured, so the plain fallback shows and there is no Summarise button
    assert "not configured" in html and "Summarise this opinion" not in html
    assert [c[1] for c in calls] == ["/opinions/107252/", "/clusters/107252/"]


def test_opinion_page_falls_back_to_search_record_without_token(app, client, monkeypatch):
    from app.blueprints import _courtlistener as cl
    calls = []
    auth = cl._error("auth", "This CourtListener request needs an API token.", 401)

    def fake_get(path, params=None):
        calls.append(path)
        if path == "/search/":
            return {"ok": True, "data": {"count": 1, "next": None, "results": [SEARCH_JSON["results"][0]]}}
        return auth
    monkeypatch.setattr(cl, "_get", fake_get)
    r = client.get("/research/opinion/107252?cluster_id=107252")
    assert r.status_code == 200
    html = r.data.decode()
    assert "needs an API token" in html and "Miranda v. Arizona" in html and "must be warned" in html
    assert "shows the search record and excerpt" in html
    assert calls[-1] == "/search/"


def test_opinion_summarise_with_model_and_without(app, client, monkeypatch):
    _fake_transport(monkeypatch)
    db, M = _models()
    from app import llm
    with app.app_context():
        f = M.Firm.get()
        f.ai_enabled = True
        db.session.commit()
    # no key: calm fallback, status 200
    r = client.post("/research/opinion/107252/summarise", data={"cluster_id": "107252", "_csrf": S["tok"]})
    assert r.status_code == 200 and b"No AI key" in r.data
    # model answers
    seen = []

    def fake(prompt, **kw):
        seen.append((prompt, kw))
        return json.dumps({"summary": "The Court held that custodial statements need warnings.",
                           "holding": "Statements from custodial interrogation are inadmissible without warnings."})
    monkeypatch.setattr(llm, "complete", fake)
    monkeypatch.setattr(llm, "status", lambda: dict(available=True, reason="", provider="anthropic"))
    r = client.get("/research/opinion/107252?cluster_id=107252")
    assert b"Summarise this opinion" in r.data
    r = client.post("/research/opinion/107252/summarise", data={"cluster_id": "107252", "_csrf": S["tok"]})
    assert r.status_code == 200
    html = r.data.decode()
    assert "custodial statements need warnings" in html and "<strong>Holding.</strong>" in html
    assert "delivered the opinion of the Court" in seen[0][0] and seen[0][1]["kind"] == "opinion_summary"


# ---------------------------------------------------------------- saved page, notes, delete, memo export
def test_saved_page_groups_by_matter_and_exports_memo(app, client, monkeypatch):
    db, M = _models()
    m1 = _matter(app, "M-1001")
    m2 = _matter(app, "M-1002")
    with app.app_context():
        db.session.add(M.SavedAuthority(matter_id=m2, source_id="4495611", case_name="State v. Edwards",
                                        citation="2018 Ohio 1739", court="Ohio Court of Appeals", notes="Compare facts"))
        db.session.commit()
    r = client.get("/research/saved")
    html = r.data.decode()
    assert r.status_code == 200
    assert html.index("M-1001") < html.index("M-1002")
    assert "Miranda v. Arizona" in html and "State v. Edwards" in html and "Warnings requirement" in html
    assert "Export as memo" in html and html.count("free public database") == 1
    r = client.get(f"/research/saved?matter_id={m2}")
    html = r.data.decode()
    assert "State v. Edwards" in html and "Miranda v. Arizona" not in html
    # inline note edit
    with app.app_context():
        a = M.SavedAuthority.query.filter_by(source_id="107252").first()
        aid = a.id
    r = client.post(f"/research/saved/{aid}/note", data={"notes": "Edited note", "_csrf": S["tok"]})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(M.SavedAuthority, aid).notes == "Edited note"
    # memo export creates a PDF Document in the Research folder
    with app.app_context():
        before = M.Document.query.filter_by(matter_id=m1).count()
    r = client.post("/research/saved/export", data={"matter_id": m1, "_csrf": S["tok"]}, follow_redirects=True)
    assert r.status_code == 200 and b"Memo saved" in r.data
    with app.app_context():
        docs = M.Document.query.filter_by(matter_id=m1).order_by(M.Document.id.desc()).all()
        assert len(docs) == before + 1
        d = docs[0]
        assert d.folder == "Research" and d.mime == "application/pdf" and d.name.startswith("Research memo M-1001")
        path = os.path.join(app.config["UPLOAD_DIR"], d.path)
        with open(path, "rb") as fh:
            assert fh.read(5) == b"%PDF-"
        assert d.size > 500
        # text was extracted from the PDF at save time, so the memo is searchable
        assert "Miranda" in (d.extracted_text or "") and "Edited note" in (d.extracted_text or "")
    dl = client.get(f"/documents/{d.id}/download")
    assert dl.status_code == 200 and dl.data[:5] == b"%PDF-"
    # empty matter refuses
    with app.app_context():
        m3 = M.Matter.query.filter(M.Matter.id.notin_([m1, m2])).first()
    if m3:
        r = client.post("/research/saved/export", data={"matter_id": m3.id, "_csrf": S["tok"]}, follow_redirects=True)
        assert b"Nothing saved" in r.data
    # delete
    r = client.post(f"/research/saved/{aid}/delete", data={"_csrf": S["tok"]})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(M.SavedAuthority, aid) is None


# ---------------------------------------------------------------- citation check
def test_cite_check_flags_unresolved_and_saves_note(app, client, monkeypatch):
    calls = _fake_transport(monkeypatch)
    db, M = _models()
    mid = _matter(app)
    text = "As held in Miranda v. Arizona, 384 U.S. 436 (1966), and Smith v. Jones, 999 F.3d 9999 (9th Cir. 2099)."
    with app.app_context():
        before = M.Note.query.filter_by(matter_id=mid).count()
    r = client.post("/research/cite-check", data={"text": text, "matter_id": mid, "_csrf": S["tok"]})
    assert r.status_code == 200
    html = r.data.decode()
    assert "2 citations found" in html and "1 resolved" in html and "1 not found" in html
    assert "384 U.S. 436" in html and "Miranda v. Arizona" in html and ">resolved<" in html
    assert "999 F.3d 9999" in html and "not found, verify before filing" in html
    assert "Saved as a note" in html and html.count("free public database") == 1
    assert calls[0] == ("POST", "/citation-lookup/", {"text": text})
    with app.app_context():
        notes = M.Note.query.filter_by(matter_id=mid).order_by(M.Note.id.desc()).all()
        assert len(notes) == before + 1
        body = notes[0].body
        assert "Citation check (CourtListener)" in body
        assert "999 F.3d 9999: not found, verify before filing" in body
        assert "384 U.S. 436: Miranda v. Arizona (1966)" in body
        assert M.AuditLog.query.filter_by(action="create", entity="note", entity_id=notes[0].id).count() == 1
    # no matter chosen: nothing saved
    r = client.post("/research/cite-check", data={"text": text, "_csrf": S["tok"]})
    assert r.status_code == 200 and b"Saved as a note" not in r.data
    with app.app_context():
        assert M.Note.query.filter_by(matter_id=mid).count() == before + 1


def test_cite_check_from_document_uses_extracted_text(app, client, monkeypatch):
    calls = _fake_transport(monkeypatch)
    db, M = _models()
    mid = _matter(app, "M-1002")
    with app.app_context():
        from app.blueprints.documents import store_bytes
        doc, err = store_bytes(mid, "brief.txt", b"We rely on 384 U.S. 436 and 999 F.3d 9999 throughout.", user_id=1)
        assert not err
        db.session.commit()
        did = doc.id
    r = client.get(f"/research/cite-check?document_id={did}")
    assert r.status_code == 200 and f'value="{did}" selected' in r.data.decode()
    r = client.post("/research/cite-check", data={"document_id": did, "_csrf": S["tok"]})
    assert r.status_code == 200
    html = r.data.decode()
    assert "document brief.txt" in html and "not found, verify before filing" in html
    assert calls[-1][2]["text"].startswith("We rely on 384 U.S. 436")
    # the document's matter is used for the note when none was chosen
    with app.app_context():
        n = M.Note.query.filter_by(matter_id=mid).order_by(M.Note.id.desc()).first()
        assert n and "source: document brief.txt" in n.body
    # empty submission is refused politely
    r = client.post("/research/cite-check", data={"text": "", "_csrf": S["tok"]})
    assert r.status_code == 200 and b"Paste some text" in r.data


# ---------------------------------------------------------------- settings, matter link, feature map
def test_settings_token_field_and_integrations_card(app, client):
    db, M = _models()
    r = client.get("/settings")
    assert r.status_code == 200 and b'name="courtlistener_token"' in r.data and b"optional and free" in r.data
    r = client.get("/settings/integrations")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Research (CourtListener)" in html and "not configured" in html.split("Research (CourtListener)")[1][:200]
    r = client.post("/settings", data={"courtlistener_token": "abc-token", "_csrf": S["tok"]})
    assert r.status_code == 302
    with app.app_context():
        assert M.Firm.get().courtlistener_token == "abc-token"
    html = client.get("/settings/integrations").data.decode()
    assert "configured" in html.split("Research (CourtListener)")[1][:120] and "Token set in Settings" in html
    with app.app_context():
        M.Firm.get().courtlistener_token = ""
        db.session.commit()


def test_matter_detail_links_to_research_and_feature_map(app, client):
    mid = _matter(app)
    r = client.get(f"/matters/{mid}")
    assert r.status_code == 200 and f'href="/research/saved?matter_id={mid}"'.encode() in r.data
    from app.feature_map import FEATURE_MAP
    work = [c for c in FEATURE_MAP if c["company"] == "Clio Work"][0]
    routes = {f[1] for f in work["features"]}
    assert {"/research", "/research/saved", "/research/cite-check"} <= routes
    assert all(f[2] == "built" for f in work["features"]) and len(work["features"]) == 5
