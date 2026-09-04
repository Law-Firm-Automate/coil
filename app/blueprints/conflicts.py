"""Conflict checks: fuzzy name search across everything the firm has touched."""
import json
import re
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from rapidfuzz import fuzz
from ..extensions import db
from ..models import ConflictCheck, Contact, Matter, MatterParty, Note, IntakeLead, Message, Document, audit
from ..helpers import login_required, current_user

bp = Blueprint("conflicts", __name__, url_prefix="/conflicts")

OUTCOMES = ["clear", "conflict", "waived", "unresolved"]
FUZZY_MIN = 80


def normalise(s):
    s = re.sub(r"[^a-z0-9]+", " ", (s or "").lower())
    return " ".join(s.split())


def _score(query, text):
    """Exact substring wins; otherwise token_set_ratio at or above the threshold.
    Long texts (messages, file contents) only match on substring: fuzzy scoring a whole document
    against a name is slow and matches on common words."""
    nq, nt = normalise(query), normalise(text)
    if not nq or not nt:
        return None
    if len(nq) >= 3 and nq in nt:
        return 100
    if len(nt) > 200:
        return None
    s = fuzz.token_set_ratio(nq, nt)
    return int(s) if s >= FUZZY_MIN else None


def _index():
    """Everything we search, as (text, source, label, url, role)."""
    rows = []
    for c in Contact.query.all():
        role = "client" if c.is_client else "contact"
        url = f"/contacts/{c.id}"
        names = {c.display_name, f"{c.first_name} {c.last_name}".strip(), c.company_name or ""}
        names.update(a.strip() for a in (c.aliases or "").splitlines())
        for n in names:
            if n:
                rows.append((n, "contact", c.display_name, url, role))
        if c.email:
            rows.append((c.email, "contact", f"{c.display_name} <{c.email}>", url, role))
    for p in MatterParty.query.all():
        m = p.matter
        rows.append((p.name, "party", f"{p.name} on {m.label if m else '?'}", f"/matters/{p.matter_id}",
                     p.role.replace("_", " ")))
    for m in Matter.query.all():
        rows.append((m.name, "matter", m.label, f"/matters/{m.id}", "matter name"))
    for n in Note.query.all():
        url = f"/matters/{n.matter_id}" if n.matter_id else (f"/contacts/{n.contact_id}" if n.contact_id else "")
        snippet = " ".join(n.body.split())[:90]
        rows.append((n.body, "note", snippet, url, "note"))
    for msg in Message.query.all():
        if not msg.body:
            continue
        who = msg.contact.display_name if msg.contact else (msg.to_addr or msg.from_addr or "unknown")
        url = f"/messages/{msg.contact_id}" if msg.contact_id else "/messages"
        snippet = " ".join(msg.body.split())[:90]
        rows.append((msg.body, "message", f"{msg.channel} with {who}: {snippet}", url, "message"))
    for d in Document.query.all():
        url = f"/documents?matter_id={d.matter_id}"
        label = f"{d.name} on {d.matter.label if d.matter else '?'}"
        rows.append((d.name, "document", label, url, "file name"))
        if d.extracted_text:
            rows.append((d.extracted_text, "document", f"{label} (contents)", url, "file contents"))
    for l in IntakeLead.query.all():
        rows.append((l.name, "lead", l.name, f"/intake/{l.id}", "lead"))
        if l.email:
            rows.append((l.email, "lead", f"{l.name} <{l.email}>", f"/intake/{l.id}", "lead email"))
        if l.description:
            rows.append((l.description, "lead", f"{l.name}: " + " ".join(l.description.split())[:80], f"/intake/{l.id}",
                         "lead description"))
        if l.adverse_party:
            rows.append((l.adverse_party, "lead", f"{l.adverse_party} (adverse to lead {l.name})",
                         f"/intake/{l.id}", "adverse party"))
    return rows


def run_check(names, matter_id=None, contact_id=None, user_id=None):
    """Run a check and store it. Returns the ConflictCheck (already committed)."""
    queries = [n.strip() for n in names.splitlines() if n.strip()]
    index = _index()
    hits = {}
    for q in queries:
        for text, source, label, url, role in index:
            s = _score(q, text)
            if s is None:
                continue
            key = (q, source, url, label)
            if key not in hits or hits[key]["score"] < s:
                hits[key] = {"query": q, "source": source, "label": label, "score": s, "url": url, "role": role}
    results = sorted(hits.values(), key=lambda r: (-r["score"], r["query"], r["source"]))
    chk = ConflictCheck(run_by_id=user_id, query="\n".join(queries), results_json=json.dumps(results),
                        matter_id=matter_id, contact_id=contact_id,
                        outcome="unresolved" if results else "clear")
    db.session.add(chk)
    db.session.flush()
    audit("run", "conflict_check", chk.id, f"{len(queries)} names, {len(results)} hits", user_id)
    db.session.commit()
    return chk


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@bp.route("")
@login_required
def index():
    history = db.session.query(ConflictCheck).order_by(ConflictCheck.created_at.desc()).limit(50).all()
    matter_id = _int(request.args.get("matter_id"))
    contact_id = _int(request.args.get("contact_id"))
    prefill = request.args.get("q", "")
    matter = db.session.get(Matter, matter_id) if matter_id else None
    contact = db.session.get(Contact, contact_id) if contact_id else None
    if matter and not prefill:
        lines = [matter.client.display_name] + [p.name for p in matter.parties]
        prefill = "\n".join(lines)
    if contact and not prefill:
        prefill = contact.display_name
    matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    return render_template("conflicts/index.html", history=history, prefill=prefill, matter=matter, contact=contact,
                           matters=matters)


@bp.route("/run", methods=["POST"])
@login_required
def run():
    names = request.form.get("names", "")
    if not names.strip():
        flash("Enter at least one name to search.", "error")
        return redirect(url_for("conflicts.index"))
    chk = run_check(names, matter_id=_int(request.form.get("matter_id")),
                    contact_id=_int(request.form.get("contact_id")), user_id=current_user().id)
    return redirect(url_for("conflicts.detail", id=chk.id))


@bp.route("/<int:id>")
@login_required
def detail(id):
    chk = db.session.get(ConflictCheck, id) or abort(404)
    contact = db.session.get(Contact, chk.contact_id) if chk.contact_id else None
    return render_template("conflicts/detail.html", chk=chk, results=chk.results, contact=contact, outcomes=OUTCOMES)


@bp.route("/<int:id>/resolve", methods=["POST"])
@login_required
def resolve(id):
    chk = db.session.get(ConflictCheck, id) or abort(404)
    outcome = request.form.get("outcome", "")
    if outcome not in OUTCOMES:
        flash("Pick an outcome.", "error")
        return redirect(url_for("conflicts.detail", id=id))
    chk.outcome = outcome
    chk.notes = request.form.get("notes", "").strip()
    audit("resolve", "conflict_check", chk.id, outcome, current_user().id)
    if chk.matter_id:
        audit("conflict_check", "matter", chk.matter_id, f"check #{chk.id} marked {outcome}", current_user().id)
    db.session.commit()
    flash(f"Conflict check marked {outcome}.", "ok")
    return redirect(url_for("conflicts.detail", id=id))
