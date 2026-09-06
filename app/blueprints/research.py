"""Legal research on CourtListener (Clio Work lane): case law search, opinion reader with optional AI summary,
authorities saved to matters, research memo export, and a citation check for hallucinated or mistyped cites.

All HTTP goes through app/blueprints/_courtlistener.py (module reference `cl`, so tests monkeypatch cl._get,
cl._post or cl.search). When CourtListener is down, throttled, or wants a token, every page renders a calm notice
with status 200 rather than an error page.
"""
import re
from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from markupsafe import Markup, escape

from ..extensions import db
from ..models import Matter, Document, Note, SavedAuthority, Firm, audit
from ..helpers import login_required, current_user, parse_date
from ..services.pdf import DocPDF, html_to_pdf_body
from .. import llm
from ..llm import LLMUnavailable
from . import _courtlistener as cl
from .documents import store_bytes

bp = Blueprint("research", __name__, url_prefix="/research")

SYSTEM = ("You help the staff of a small law firm read case law. Be accurate and plain. Use only the opinion text "
          "you are given. Never invent a holding, a party, a date or a citation. No marketing language.")

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "holding": {"type": "string"}},
    "required": ["summary", "holding"], "additionalProperties": False,
}


def _uid():
    u = current_user()
    return u.id if u else None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _matters():
    return Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()


def _all_matters():
    return Matter.query.order_by(Matter.status, Matter.number).all()


def highlight(text, q):
    """Escape text and wrap each query word (2+ chars, quotes and operators dropped) in <mark>."""
    text = escape(text or "")
    words = []
    for w in re.findall(r"[\w'\-]+", (q or "").replace('"', " ")):
        w = w.strip("'-")
        if len(w) >= 2 and w.lower() not in ("and", "or", "not") and w.lower() not in words:
            words.append(w.lower())
    if not words:
        return Markup(text)
    pat = re.compile("(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + ")", re.I)
    return Markup(pat.sub(lambda m: f"<mark>{m.group(1)}</mark>", str(text)))


def _authority_from_form(form):
    """Fields every Save-to-matter form posts. Returns (SavedAuthority, error)."""
    matter_id = _int(form.get("matter_id"))
    m = db.session.get(Matter, matter_id) if matter_id else None
    if not m:
        return None, "Choose a matter."
    case_name = (form.get("case_name") or "").strip()
    if not case_name:
        return None, "That result has no case name."
    a = SavedAuthority(
        matter_id=m.id, source="courtlistener",
        source_id=(form.get("source_id") or "")[:60],
        citation=(form.get("citation") or "").strip()[:200],
        case_name=case_name[:400],
        court=(form.get("court") or "").strip()[:200],
        decided_on=parse_date(form.get("decided_on")),
        url=(form.get("url") or "").strip()[:500],
        snippet=(form.get("snippet") or "").strip()[:2000],
        notes=(form.get("note") or "").strip(),
        saved_by_id=_uid(),
    )
    return a, None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
@bp.route("")
@login_required
def index():
    q = (request.args.get("q") or "").strip()
    court = request.args.get("court") or ""
    if court and court not in cl.COURT_IDS:
        court = ""
    filed_after = request.args.get("filed_after") or ""
    filed_before = request.args.get("filed_before") or ""
    order_by = request.args.get("order_by") or "score desc"
    if order_by not in dict(cl.ORDERINGS):
        order_by = "score desc"
    cursor = request.args.get("cursor") or ""
    results, count, next_cursor, api_error = [], 0, "", None
    if q:
        out = cl.search(q, court=court, filed_after=filed_after, filed_before=filed_before, order_by=order_by,
                        cursor=cursor)
        if out.get("ok"):
            results, count, next_cursor = out["results"], out["count"], out["next_cursor"]
        else:
            api_error = out
    return render_template("research/index.html", q=q, court=court, filed_after=filed_after,
                           filed_before=filed_before, order_by=order_by, results=results, count=count,
                           next_cursor=next_cursor, api_error=api_error, courts=cl.COURTS, orderings=cl.ORDERINGS,
                           matters=_matters(), highlight=highlight, has_token=cl.has_token())


@bp.route("/save", methods=["POST"])
@login_required
def save():
    a, err = _authority_from_form(request.form)
    nxt = request.form.get("next") or ""
    if err:
        flash(err, "error")
        return redirect(nxt or url_for("research.index"))
    db.session.add(a)
    db.session.flush()
    audit("create", "saved_authority", a.id, f"{a.case_name} {a.citation} saved to {a.matter.label}", _uid())
    db.session.commit()
    flash(Markup(f"Saved {escape(a.case_name)} to {escape(a.matter.label)}. "
                 f"<a href=\"/research/saved?matter_id={a.matter_id}\">Open saved authorities</a>"), "ok")
    return redirect(nxt or url_for("research.saved", matter_id=a.matter_id))


# ---------------------------------------------------------------------------
# opinion reader
# ---------------------------------------------------------------------------
def _load_opinion(opinion_id, cluster_id):
    """Full text plus cluster metadata. Falls back to the search endpoint (no token needed) for metadata and a
    snippet when the opinion endpoint refuses. Returns (op, meta, api_error, fallback)."""
    op = cl.opinion(opinion_id)
    meta, api_error, fallback = None, None, False
    if op.get("ok"):
        cid = op.get("cluster_id") or cluster_id
        if cid:
            c = cl.cluster(cid)
            if c.get("ok"):
                meta = c
            else:
                s = cl.search_cluster(cid)
                if s.get("ok"):
                    meta = s["result"]
        return op, meta, None, False
    api_error = op
    op = None
    if cluster_id:
        s = cl.search_cluster(cluster_id)
        if s.get("ok"):
            meta = s["result"]
            fallback = True
    return op, meta, api_error, fallback


def _render_opinion(opinion_id, cluster_id, q, summary=None, holding=None, ai_error=None, cut=False):
    op, meta, api_error, fallback = _load_opinion(opinion_id, cluster_id)
    if not op and not meta:
        api_error = api_error or cl._error("not_found", "CourtListener has no record with that id.", 404)
    st = llm.status()
    return render_template("research/opinion.html", opinion_id=opinion_id, cluster_id=cluster_id or (meta or {}).get("cluster_id"),
                           op=op, meta=meta, api_error=api_error, fallback=fallback, q=q, highlight=highlight,
                           matters=_matters(), ai=st, summary=summary, holding=holding, ai_error=ai_error, cut=cut,
                           has_token=cl.has_token())


@bp.route("/opinion/<int:id>")
@login_required
def opinion(id):
    return _render_opinion(id, _int(request.args.get("cluster_id")), (request.args.get("q") or "").strip())


@bp.route("/opinion/<int:id>/summarise", methods=["POST"])
@login_required
def summarise(id):
    cluster_id = _int(request.form.get("cluster_id"))
    q = (request.form.get("q") or "").strip()
    op = cl.opinion(id)
    if not op.get("ok"):
        return _render_opinion(id, cluster_id, q, ai_error="The opinion text could not be loaded, so there is nothing to summarise.")
    text, cut = llm.clip(op["text"], 11000)
    prompt = ("Summarise this court opinion for a lawyer in about 200 words of plain prose: the parties, the "
              "question before the court, how the court reasoned, and the outcome. Then state the holding in one or "
              "two sentences. Use only the text below. Return JSON {\"summary\": \"...\", \"holding\": \"...\"}.\n\n"
              + text)
    try:
        data = llm.complete_json(prompt, SUMMARY_SCHEMA, system=SYSTEM, max_tokens=900, kind="opinion_summary",
                                 entity="opinion", entity_id=id, user_id=_uid())
    except LLMUnavailable as e:
        return _render_opinion(id, cluster_id, q, ai_error=str(e), cut=cut)
    summary = str(data.get("summary") or "").strip() if isinstance(data, dict) else ""
    holding = str(data.get("holding") or "").strip() if isinstance(data, dict) else ""
    if not summary:
        return _render_opinion(id, cluster_id, q, ai_error="The AI answered in an unexpected format. Try again.", cut=cut)
    return _render_opinion(id, cluster_id, q, summary=summary, holding=holding, cut=cut)


# ---------------------------------------------------------------------------
# saved authorities
# ---------------------------------------------------------------------------
@bp.route("/saved")
@login_required
def saved():
    matter_id = _int(request.args.get("matter_id"))
    q = SavedAuthority.query
    if matter_id:
        q = q.filter(SavedAuthority.matter_id == matter_id)
    rows = q.order_by(SavedAuthority.matter_id, SavedAuthority.created_at.desc()).all()
    groups = []  # [(matter, [rows])]
    for a in rows:
        if not groups or groups[-1][0].id != a.matter_id:
            groups.append((a.matter, []))
        groups[-1][1].append(a)
    groups.sort(key=lambda g: (g[0].status == "closed", g[0].number or ""))
    matter = db.session.get(Matter, matter_id) if matter_id else None
    return render_template("research/saved.html", groups=groups, matter=matter, matter_id=matter_id,
                           matters=_all_matters())


@bp.route("/saved/<int:id>/note", methods=["POST"])
@login_required
def saved_note(id):
    a = db.session.get(SavedAuthority, id) or abort(404)
    a.notes = (request.form.get("notes") or "").strip()
    db.session.commit()
    flash("Note saved.", "ok")
    return redirect(url_for("research.saved", matter_id=a.matter_id))


@bp.route("/saved/<int:id>/delete", methods=["POST"])
@login_required
def saved_delete(id):
    a = db.session.get(SavedAuthority, id) or abort(404)
    mid = a.matter_id
    audit("delete", "saved_authority", a.id, f"{a.case_name} {a.citation} removed from {a.matter.label}", _uid())
    db.session.delete(a)
    db.session.commit()
    flash("Removed.", "ok")
    return redirect(url_for("research.saved", matter_id=mid))


def build_memo_pdf(matter, rows):
    """Research memo: one block per authority (case name, citation, court, date, notes). Returns PDF bytes."""
    firm = Firm.get()
    pdf = DocPDF(firm, title=f"Research memo {matter.number}")
    pdf.alias_nb_pages()
    pdf.add_page()
    parts = [f"<h1>Research memo: {matter.label}</h1>",
             f"<p>Client: {matter.client.display_name if matter.client else ''}. Prepared {date.today().strftime('%b %-d, %Y')}. "
             f"{len(rows)} authorit{'y' if len(rows) == 1 else 'ies'}.</p>"]
    for i, a in enumerate(rows, 1):
        parts.append(f"<h3>{i}. {a.case_name}</h3>")
        line = ", ".join(x for x in [a.citation, a.court, a.decided_on.strftime("%b %-d, %Y") if a.decided_on else ""] if x)
        if line:
            parts.append(f"<p>{line}</p>")
        if a.url:
            parts.append(f"<p>{a.url}</p>")
        if a.notes:
            for para in re.split(r"\n\s*\n", a.notes):
                parts.append(f"<p>{para.strip()}</p>")
        elif a.snippet:
            parts.append(f"<p>Excerpt: {a.snippet[:600]}</p>")
    parts.append("<p>Source: CourtListener, a free public database run by Free Law Project. Verify every authority "
                 "against the official reporter before citing it.</p>")
    html_to_pdf_body(pdf, "".join(p.replace("&", "&amp;") for p in parts))
    return bytes(pdf.output())


@bp.route("/saved/export", methods=["POST"])
@login_required
def saved_export():
    matter_id = _int(request.form.get("matter_id"))
    m = db.session.get(Matter, matter_id) if matter_id else None
    if not m:
        flash("Choose a matter to export.", "error")
        return redirect(url_for("research.saved"))
    rows = SavedAuthority.query.filter(SavedAuthority.matter_id == m.id).order_by(SavedAuthority.created_at).all()
    if not rows:
        flash("Nothing saved on that matter yet.", "error")
        return redirect(url_for("research.saved", matter_id=m.id))
    data = build_memo_pdf(m, rows)
    name = f"Research memo {m.number} {date.today().isoformat()}.pdf"
    doc, err = store_bytes(m.id, name, data, mime="application/pdf", user_id=_uid(), folder="Research",
                           tags="research")
    if err:
        flash(err, "error")
        return redirect(url_for("research.saved", matter_id=m.id))
    db.session.flush()
    audit("create", "document", doc.id, f"research memo exported for {m.label} ({len(rows)} authorities)", _uid())
    db.session.commit()
    flash(Markup(f"Memo saved to the matter's documents in the Research folder. "
                 f"<a href=\"/documents/{doc.id}/download\">Download</a> or "
                 f"<a href=\"/documents?matter_id={m.id}\">open Documents</a>."), "ok")
    return redirect(url_for("research.saved", matter_id=m.id))


# ---------------------------------------------------------------------------
# citation check
# ---------------------------------------------------------------------------
def _cite_documents():
    return (Document.query.filter(Document.is_current == True, Document.extracted_text != "")  # noqa: E712
            .order_by(Document.matter_id, Document.created_at.desc()).all())


def _cite_counts(items):
    """(resolved, ambiguous, not found). Ambiguous is its own bucket: the citation matched more than one
    case, which is a different problem from a citation that matched nothing."""
    res = [c["resolution"] for c in items]
    return res.count("resolved"), res.count("ambiguous"), res.count("not_found")


def _candidate_label(c):
    return c["case_name"] + (f" ({c['date_filed'][:4]})" if c.get("date_filed") else "")


def _cite_note_body(items, source_label):
    resolved, ambiguous, missing = _cite_counts(items)
    # [internal] marks this as attorney work product so it is never quoted into a client-facing draft
    # (app/blueprints/ai.py:update_facts reads that prefix).
    lines = [f"[internal] Citation check (CourtListener) on {date.today().strftime('%b %-d, %Y')}, "
             f"source: {source_label}.",
             f"{len(items)} citation{'s' if len(items) != 1 else ''} found, {resolved} resolved, "
             f"{ambiguous} ambiguous, {missing} not found."]
    for c in items:
        if c["resolution"] == "resolved":
            tail = _candidate_label(c) or c["case_name"]
        elif c["resolution"] == "ambiguous":
            names = "; ".join(_candidate_label(x) for x in (c.get("candidates") or []) if x.get("case_name"))
            tail = f"{c['match_count']} possible matches"
            if names:
                tail += f" ({names})"
            tail += ", verify which one before filing"
        else:
            tail = "not found, verify before filing"
        lines.append(f"- {c['citation']}: {tail}")
    lines.append("CourtListener is a free public database; verify against the official reporter.")
    return "\n".join(lines)


@bp.route("/cite-check", methods=["GET", "POST"])
@login_required
def cite_check():
    ctx = dict(documents=_cite_documents(), matters=_all_matters(), text="", document_id=None, matter_id=None,
               items=None, api_error=None, note=None, has_token=cl.has_token(), source_label="")
    if request.method == "GET":
        ctx["document_id"] = _int(request.args.get("document_id"))
        ctx["matter_id"] = _int(request.args.get("matter_id"))
        if ctx["document_id"] and not ctx["matter_id"]:
            d = db.session.get(Document, ctx["document_id"])
            ctx["matter_id"] = d.matter_id if d else None
        return render_template("research/cite_check.html", **ctx)
    text = (request.form.get("text") or "").strip()
    document_id = _int(request.form.get("document_id"))
    matter_id = _int(request.form.get("matter_id"))
    doc = db.session.get(Document, document_id) if document_id else None
    source_label = "pasted text"
    if doc and not text:
        text = doc.extracted_text or ""
        source_label = f"document {doc.name}"
        if not matter_id:
            matter_id = doc.matter_id
    ctx.update(text=text if source_label == "pasted text" else "", document_id=document_id, matter_id=matter_id,
               source_label=source_label)
    if not text.strip():
        flash("Paste some text or pick a document that has readable text.", "error")
        return render_template("research/cite_check.html", **ctx)
    out = cl.citation_lookup(text)
    if not out.get("ok"):
        ctx["api_error"] = out
        return render_template("research/cite_check.html", **ctx)
    items = out["citations"]
    ctx["items"] = items
    m = db.session.get(Matter, matter_id) if matter_id else None
    if m:
        n = Note(matter_id=m.id, user_id=_uid(), body=_cite_note_body(items, source_label))
        db.session.add(n)
        db.session.flush()
        resolved, ambiguous, missing = _cite_counts(items)
        audit("create", "note", n.id, f"citation check saved on {m.label}: {len(items)} citations, "
              f"{ambiguous} ambiguous, {missing} not found", _uid())
        db.session.commit()
        ctx["note"] = n
    return render_template("research/cite_check.html", **ctx)
