"""CourtListener REST v4 client for the research module. Every HTTP call the research pages make lives here.

CourtListener (courtlistener.com, run by Free Law Project) is a free public database of case law. Anonymous use is
rate limited and, as of 2026, only the search endpoint answers without a token: /opinions/, /clusters/ and
/citation-lookup/ return 401 anonymously. A free token from courtlistener.com raises the limits and unlocks those.
The token is read from Firm.courtlistener_token first, then the COURTLISTENER_TOKEN environment variable.

Every function returns a dict. Success: {"ok": True, ...}. Failure: {"ok": False, "error": <kind>, "message": <text
safe to show staff>, "status": <http status or None>}. Nothing here raises for network or API problems, so the pages
can always render a calm notice with status 200.

GET responses are cached in memory for CACHE_TTL seconds (search results and opinion fetches), keyed by path and
params. Tests monkeypatch _get / _post to return canned JSON, or requests.get to check the wire request.
"""
import os
import re
import time
from html import unescape

import requests
from flask import current_app, has_app_context

BASE = "https://www.courtlistener.com/api/rest/v4"
SITE = "https://www.courtlistener.com"
TIMEOUT = 20
CACHE_TTL = 600  # 10 minutes
CITE_TEXT_CAP = 64000  # the citation-lookup endpoint refuses more than this
USER_AGENT = "Coil practice manager (research module)"

# The ids CourtListener uses for the courts a solo firm searches most. Verified against /api/rest/v4/courts/<id>/.
COURTS = [
    ("scotus", "U.S. Supreme Court"),
    ("ca1", "1st Circuit"), ("ca2", "2nd Circuit"), ("ca3", "3rd Circuit"), ("ca4", "4th Circuit"),
    ("ca5", "5th Circuit"), ("ca6", "6th Circuit"), ("ca7", "7th Circuit"), ("ca8", "8th Circuit"),
    ("ca9", "9th Circuit"), ("ca10", "10th Circuit"), ("ca11", "11th Circuit"),
    ("cadc", "D.C. Circuit"), ("cafc", "Federal Circuit"),
    ("cal", "California Supreme Court"), ("ny", "New York Court of Appeals"), ("tex", "Texas Supreme Court"),
    ("fla", "Florida Supreme Court"), ("ill", "Illinois Supreme Court"), ("pa", "Pennsylvania Supreme Court"),
]
COURT_IDS = {c for c, _ in COURTS}
COURT_NAMES = dict(COURTS)

ORDERINGS = [("score desc", "Relevance"), ("dateFiled desc", "Newest first"), ("citeCount desc", "Most cited")]

_cache = {}


# ---------------------------------------------------------------------------
# token, headers, cache
# ---------------------------------------------------------------------------
def token():
    """Firm setting first, then the environment. Empty string when neither is set."""
    t = ""
    if has_app_context():
        try:
            from ..models import Firm
            t = (Firm.get().courtlistener_token or "").strip()
        except Exception:  # noqa: BLE001  (no DB in some contexts)
            t = ""
        if not t:
            t = (current_app.config.get("COURTLISTENER_TOKEN") or "").strip()
    if not t:
        t = (os.environ.get("COURTLISTENER_TOKEN") or "").strip()
    return t


def has_token():
    return bool(token())


def _headers():
    h = {"Accept": "application/json", "User-Agent": USER_AGENT}
    t = token()
    if t:
        h["Authorization"] = f"Token {t}"
    return h


def clear_cache():
    _cache.clear()


def _cache_key(path, params):
    return (path, tuple(sorted((k, str(v)) for k, v in (params or {}).items() if v not in (None, ""))))


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
def _error(kind, message, status=None):
    return {"ok": False, "error": kind, "message": message, "status": status}


def _classify(resp):
    """Turn a non-2xx response into a structured error. None when the response is fine."""
    code = resp.status_code
    if 200 <= code < 300:
        return None
    detail = ""
    try:
        detail = str((resp.json() or {}).get("detail") or "")
    except Exception:  # noqa: BLE001
        detail = ""
    if code == 429:
        wait = re.search(r"available in (\d+) seconds", detail)
        when = f" Try again in about {wait.group(1)} seconds." if wait else " Try again in a minute."
        extra = "" if has_token() else " Adding a free CourtListener token in Settings raises the limit."
        return _error("rate_limited", "CourtListener is rate limiting requests right now." + when + extra, code)
    if code in (401, 403):
        if has_token():
            return _error("auth", "CourtListener rejected the API token. Check the token in Settings, Research.", code)
        return _error("auth", "This CourtListener request needs an API token. Tokens are free: create an account at "
                              "courtlistener.com, copy the token from your profile, and paste it under Settings.", code)
    if code == 404:
        return _error("not_found", "CourtListener has no record with that id.", code)
    return _error("http", f"CourtListener returned an error (HTTP {code}). Try again shortly.", code)


def _send(method, path, params=None, data=None):
    url = BASE + path
    try:
        if method == "GET":
            resp = requests.get(url, params=params, headers=_headers(), timeout=TIMEOUT)
        else:
            resp = requests.post(url, data=data, headers=_headers(), timeout=TIMEOUT)
    except requests.Timeout:
        return _error("timeout", "CourtListener did not answer within 20 seconds. Try again shortly.")
    except requests.RequestException as e:  # DNS, connection refused, TLS
        return _error("network", f"Could not reach CourtListener ({e.__class__.__name__}). Check the connection and try again.")
    err = _classify(resp)
    if err:
        return err
    try:
        body = resp.json()
    except ValueError:
        return _error("http", "CourtListener sent a reply that was not JSON. Try again shortly.", resp.status_code)
    return {"ok": True, "data": body}


def _get(path, params=None):
    """GET with the 10-minute cache. Errors are not cached."""
    key = _cache_key(path, params)
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    out = _send("GET", path, params=params)
    if out.get("ok"):
        _cache[key] = (time.time() + CACHE_TTL, out)
    return out


def _post(path, data=None):
    return _send("POST", path, data=data)


# ---------------------------------------------------------------------------
# normalisation helpers
# ---------------------------------------------------------------------------
def _strip_marks(s):
    return re.sub(r"</?mark>", "", s or "")


def _abs(url):
    url = url or ""
    if url.startswith("http"):
        return url
    return SITE + url if url else ""


def _cluster_id_from_url(url):
    m = re.search(r"/clusters/(\d+)/", url or "")
    return int(m.group(1)) if m else None


def _opinion_id_from_url(url):
    m = re.search(r"/opinions/(\d+)/", url or "")
    return int(m.group(1)) if m else None


def format_citation(c):
    """Cluster citations arrive as {volume, reporter, page}; search results send plain strings."""
    if isinstance(c, dict):
        parts = [str(c.get("volume") or "").strip(), str(c.get("reporter") or "").strip(), str(c.get("page") or "").strip()]
        return " ".join(p for p in parts if p)
    return str(c or "").strip()


def html_to_paragraphs(html):
    """Opinion HTML (or plain text) to a list of paragraph strings, tags stripped, paragraph breaks kept."""
    s = html or ""
    if "<" in s and ">" in s:
        s = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", " ", s, flags=re.I | re.S)
        s = re.sub(r"<\s*br\s*/?>", "\n", s, flags=re.I)
        s = re.sub(r"</\s*(p|div|blockquote|h[1-6]|li|tr|center|footnote|opinion|section|headnotes?|syllabus|summary)\s*>",
                   "\n\n", s, flags=re.I)
        s = re.sub(r"<\s*(p|div|blockquote|h[1-6]|li|tr)\b[^>]*>", "\n\n", s, flags=re.I)
        s = re.sub(r"<[^>]+>", "", s)
        s = unescape(s)
    paras = []
    for block in re.split(r"\n\s*\n", s):
        text = " ".join(block.split())
        if text:
            paras.append(text)
    return paras


def _normalise_result(r):
    ops = r.get("opinions") or []
    first = ops[0] if ops else {}
    snippet = _strip_marks(first.get("snippet") or r.get("syllabus") or "")
    snippet = " ".join(snippet.split())
    return {
        "cluster_id": r.get("cluster_id"),
        "opinion_id": first.get("id"),
        "opinion_ids": [o.get("id") for o in ops if o.get("id")],
        "case_name": _strip_marks(r.get("caseName") or r.get("caseNameFull") or "Untitled"),
        "citations": [format_citation(c) for c in (r.get("citation") or []) if c],
        "court": _strip_marks(r.get("court") or ""),
        "court_id": r.get("court_id") or "",
        "date_filed": r.get("dateFiled") or "",
        "docket_number": _strip_marks(r.get("docketNumber") or ""),
        "cite_count": r.get("citeCount"),
        "status": r.get("status") or "",
        "snippet": snippet[:600],
        "url": _abs(r.get("absolute_url")),
    }


def _normalise_cluster(c):
    return {
        "cluster_id": c.get("id"),
        "case_name": c.get("case_name") or c.get("case_name_full") or c.get("case_name_short") or "Untitled",
        "case_name_full": c.get("case_name_full") or "",
        "citations": [format_citation(x) for x in (c.get("citations") or []) if format_citation(x)],
        "date_filed": c.get("date_filed") or "",
        "citation_count": c.get("citation_count"),
        "docket_number": c.get("docket_number") or "",
        "precedential_status": c.get("precedential_status") or "",
        "judges": c.get("judges") or "",
        "syllabus": " ".join(html_to_paragraphs(c.get("syllabus") or ""))[:1500],
        "summary": " ".join(html_to_paragraphs(c.get("summary") or ""))[:1500],
        "opinion_ids": [_opinion_id_from_url(u) for u in (c.get("sub_opinions") or []) if _opinion_id_from_url(u)],
        "url": _abs(c.get("absolute_url")),
    }


def _cursor_from(url):
    m = re.search(r"[?&]cursor=([^&]+)", url or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def search(q, court="", filed_after="", filed_before="", order_by="score desc", cursor=""):
    """Opinion search. court: one id or several separated by spaces. Dates YYYY-MM-DD. Returns
    {ok, results: [normalised], count, next_cursor} or an error dict."""
    q = (q or "").strip()
    if not q:
        return {"ok": True, "results": [], "count": 0, "next_cursor": ""}
    params = {"q": q, "type": "o"}
    court = " ".join(c for c in (court or "").replace(",", " ").split() if c)
    if court:
        params["court"] = court
    if filed_after:
        params["filed_after"] = filed_after
    if filed_before:
        params["filed_before"] = filed_before
    if order_by and order_by != "score desc":
        params["order_by"] = order_by
    if cursor:
        params["cursor"] = cursor
    out = _get("/search/", params)
    if not out.get("ok"):
        return out
    data = out["data"] or {}
    return {"ok": True,
            "results": [_normalise_result(r) for r in (data.get("results") or [])],
            "count": data.get("count") or 0,
            "next_cursor": _cursor_from(data.get("next"))}


def search_cluster(cluster_id):
    """Metadata and snippet for one cluster through the search endpoint, which works without a token."""
    out = _get("/search/", {"q": f"cluster_id:{int(cluster_id)}", "type": "o"})
    if not out.get("ok"):
        return out
    results = (out["data"] or {}).get("results") or []
    if not results:
        return _error("not_found", "CourtListener has no record with that id.", 404)
    return {"ok": True, "result": _normalise_result(results[0])}


def opinion(opinion_id):
    """Full text of one opinion plus its cluster id. Needs a token on CourtListener's side."""
    out = _get(f"/opinions/{int(opinion_id)}/")
    if not out.get("ok"):
        return out
    d = out["data"] or {}
    html = ""
    for field in ("html_with_citations", "html", "html_lawbox", "html_columbia", "html_anon_2020", "xml_harvard"):
        if (d.get(field) or "").strip():
            html = d[field]
            break
    paragraphs = html_to_paragraphs(html) if html else html_to_paragraphs(d.get("plain_text") or "")
    return {"ok": True,
            "opinion_id": d.get("id") or int(opinion_id),
            "cluster_id": _cluster_id_from_url(d.get("cluster")),
            "type": d.get("type") or "",
            "author": d.get("author_str") or "",
            "download_url": d.get("download_url") or "",
            "paragraphs": paragraphs,
            "text": "\n\n".join(paragraphs),
            "url": _abs(d.get("absolute_url")) if d.get("absolute_url") else ""}


def cluster(cluster_id):
    out = _get(f"/clusters/{int(cluster_id)}/")
    if not out.get("ok"):
        return out
    c = _normalise_cluster(out["data"] or {})
    c["ok"] = True
    return c


def citation_lookup(text):
    """POST the text to /citation-lookup/. Returns {ok, citations: [...]} where each item has
    citation, normalized, status, found, ambiguous, case_name, cluster_id, url, date_filed, citations, error_message."""
    text = (text or "")[:CITE_TEXT_CAP]
    if not text.strip():
        return {"ok": True, "citations": []}
    out = _post("/citation-lookup/", {"text": text})
    if not out.get("ok"):
        return out
    items = out["data"] if isinstance(out["data"], list) else []
    found = []
    for it in items:
        status = it.get("status")
        clusters = it.get("clusters") or []
        first = clusters[0] if clusters else {}
        norm = it.get("normalized_citations") or []
        found.append({
            "citation": it.get("citation") or (norm[0] if norm else ""),
            "normalized": norm[0] if norm else (it.get("citation") or ""),
            "status": status,
            "found": status == 200 and bool(clusters),
            "ambiguous": status == 300 or (status == 200 and len(clusters) > 1),
            "match_count": len(clusters),
            "case_name": first.get("case_name") or first.get("case_name_full") or "",
            "cluster_id": first.get("id"),
            "url": _abs(first.get("absolute_url")) if first.get("absolute_url") else "",
            "date_filed": first.get("date_filed") or "",
            "citations": [format_citation(x) for x in (first.get("citations") or []) if format_citation(x)],
            "error_message": it.get("error_message") or "",
            "start_index": it.get("start_index"),
            "end_index": it.get("end_index"),
        })
    return {"ok": True, "citations": found}
