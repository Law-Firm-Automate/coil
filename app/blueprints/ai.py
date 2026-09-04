"""AI features: invoice narrative polish, matter summary, date extraction from a document, natural-language search.

Every model call goes through app.llm (module reference, so tests can monkeypatch app.llm.complete). When the model
is unavailable (no key, AI off in Settings, daily cap, provider error) each page shows a calm explanation and, for
search, a plain substring search that always works.
"""
import json
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from ..extensions import db
from ..models import (Matter, Contact, Invoice, TimeEntry, Task, CalendarEvent, Document, Note, Message, User,
                      AiRun, audit)
from ..helpers import login_required, current_user, parse_date
from .. import llm
from ..llm import LLMUnavailable

bp = Blueprint("ai", __name__, url_prefix="/ai")

SYSTEM = ("You help the staff of a small law firm. Be accurate and plain. Never invent facts, names, dates or "
          "amounts that are not in the material you are given. No marketing language.")

DATE_KINDS = ("deadline", "court_date", "task", "event")
SEARCH_ENTITIES = ("matters", "contacts", "invoices", "time", "tasks")


def _uid():
    u = current_user()
    return u.id if u else None


def _fmt_money(c):
    c = int(c or 0)
    return f"${c // 100:,}.{c % 100:02d}"


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------
@bp.route("")
@login_required
def index():
    recent = AiRun.query.order_by(AiRun.created_at.desc()).limit(15).all()
    return render_template("ai/index.html", st=llm.status(), recent=recent)


# ---------------------------------------------------------------------------
# 1. invoice narrative polish
# ---------------------------------------------------------------------------
POLISH_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["id", "text"], "additionalProperties": False}}},
    "required": ["lines"], "additionalProperties": False,
}


def _can_apply(inv):
    return inv.status == "draft" and not inv.split_group


@bp.route("/invoice/<int:id>/polish", methods=["POST"])
@login_required
def invoice_polish(id):
    inv = db.session.get(Invoice, id) or abort(404)
    lines = [l for l in inv.lines if l.kind == "time" and (l.description or "").strip()]
    if not lines:
        flash("This invoice has no time lines with a description to rewrite.", "error")
        return redirect(url_for("invoices.detail", id=inv.id))
    payload = [{"id": l.id, "date": l.date.isoformat() if l.date else "", "text": l.description.strip()}
               for l in lines]
    prompt = ("Rewrite each time entry description below as a clear, client-facing narrative in the past tense, "
              "one or two sentences, so the client understands what was done and why it mattered. Keep every "
              "fact. Do not add work that is not described, do not mention hours, rates or amounts, and do not "
              "change the meaning. Expand obvious abbreviations (re: = regarding, tc = telephone call, w/ = with). "
              "Return JSON of the form {\"lines\": [{\"id\": <same id>, \"text\": \"<rewritten>\"}]} with one "
              "item per input line.\n\nLines:\n" + json.dumps(payload, ensure_ascii=False))
    try:
        data = llm.complete_json(prompt, POLISH_SCHEMA, system=SYSTEM, max_tokens=1800, kind="invoice_polish",
                                 entity="invoice", entity_id=inv.id, user_id=_uid())
    except LLMUnavailable as e:
        return render_template("ai/polish.html", inv=inv, rows=None, error=str(e), can_apply=_can_apply(inv))
    by_id = {}
    for r in (data.get("lines") if isinstance(data, dict) else []) or []:
        try:
            by_id[int(r.get("id"))] = str(r.get("text") or "").strip()
        except (TypeError, ValueError, AttributeError):
            continue
    rows = [(l, by_id.get(l.id) or l.description) for l in lines]
    changed = sum(1 for l, t in rows if t != l.description)
    return render_template("ai/polish.html", inv=inv, rows=rows, error=None, can_apply=_can_apply(inv),
                           changed=changed)


@bp.route("/invoice/<int:id>/polish/apply", methods=["POST"])
@login_required
def invoice_polish_apply(id):
    inv = db.session.get(Invoice, id) or abort(404)
    if inv.status != "draft":
        flash("Only draft invoices can be rewritten. This one has already been sent, so its lines stay as they are.",
              "error")
        return redirect(url_for("invoices.detail", id=inv.id))
    if inv.split_group:
        flash("This invoice is one share of a split group. Void the group and rebuild it to change the lines.",
              "error")
        return redirect(url_for("invoices.detail", id=inv.id))
    changed = 0
    for l in inv.lines:
        v = request.form.get(f"line_{l.id}")
        if v is None:
            continue
        v = v.strip()
        if v and v != (l.description or ""):
            l.description = v
            changed += 1
    if changed:
        inv.pdf_path = ""  # stale after edits; rebuilt on the next send or download
        audit("update", "invoice", inv.id, f"AI narrative applied to {changed} line(s)", _uid())
        db.session.commit()
    flash(f"Updated {changed} line description(s). Hours and amounts were not touched." if changed
          else "Nothing to change.", "ok")
    return redirect(url_for("invoices.detail", id=inv.id))


# ---------------------------------------------------------------------------
# 2. matter summary
# ---------------------------------------------------------------------------
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"},
                   "open_items": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "open_items"], "additionalProperties": False,
}


def _matter_context(m):
    parts = [f"Matter {m.number}: {m.name}",
             f"Client: {m.client.display_name if m.client else ''}",
             f"Practice area: {m.practice_area or ''}. Status: {m.status}. Opened {m.opened_on}.",
             f"Responsible attorney: {m.responsible.name if m.responsible else 'unassigned'}",
             f"Billing: {m.billing_type}" + (f", flat fee {_fmt_money(m.flat_fee_cents)}" if m.flat_fee_cents else "")
             + (f", hourly {_fmt_money(m.hourly_rate_cents)}/hr" if m.hourly_rate_cents else "")]
    if m.court or m.case_number:
        parts.append(f"Court: {m.court or ''} Case number: {m.case_number or ''}")
    if m.sol_date:
        parts.append(f"Limitations deadline: {m.sol_date} ({m.sol_basis or ''})")
    if m.description:
        parts.append("Description: " + m.description.strip())
    if m.parties:
        parts.append("Parties: " + "; ".join(f"{p.name} ({p.role})" for p in m.parties))
    notes = sorted(m.notes, key=lambda n: n.created_at or datetime.min, reverse=True)[:10]
    if notes:
        parts.append("Notes (newest first):\n" + "\n".join(
            f"- {n.created_at:%Y-%m-%d} {n.user.name if n.user else ''}: {n.body.strip()}" for n in notes))
    entries = sorted(m.time_entries, key=lambda t: (t.date or date.min, t.id), reverse=True)[:15]
    if entries:
        parts.append("Recent time entries:\n" + "\n".join(
            f"- {t.date} {t.user.name if t.user else ''} {t.hours}h: {t.description.strip()}" for t in entries))
    tasks = Task.query.filter_by(matter_id=m.id, done=False).order_by(Task.due_on.asc().nulls_last()).limit(15).all()
    if tasks:
        parts.append("Open tasks and deadlines:\n" + "\n".join(
            f"- {t.kind} {t.title} due {t.due_on or 'no date'}" + (" (overdue)" if t.is_overdue else "") for t in tasks))
    events = CalendarEvent.query.filter(CalendarEvent.matter_id == m.id, CalendarEvent.starts_at >= datetime.utcnow()
                                        ).order_by(CalendarEvent.starts_at).limit(8).all()
    if events:
        parts.append("Upcoming events:\n" + "\n".join(f"- {e.starts_at:%Y-%m-%d %H:%M} {e.title}" for e in events))
    msgs = Message.query.filter(db.or_(Message.matter_id == m.id, Message.contact_id == m.client_id)).order_by(
        Message.created_at.desc()).limit(10).all()
    if msgs:
        parts.append("Recent messages (newest first):\n" + "\n".join(
            f"- {x.created_at:%Y-%m-%d} {x.channel} {x.direction}: {(x.subject + ': ') if x.subject else ''}"
            f"{(x.body or '').strip()[:300]}" for x in msgs))
    invoices = [i for i in m.invoices if i.status not in ("void",)]
    if invoices:
        parts.append("Invoices: " + "; ".join(f"{i.number} {i.status} {_fmt_money(i.total_cents)} "
                                              f"balance {_fmt_money(i.balance_cents)}" for i in invoices))
    return "\n\n".join(parts)


@bp.route("/matter/<int:id>/summary", methods=["POST"])
@login_required
def matter_summary(id):
    m = db.session.get(Matter, id) or abort(404)
    ctx, cut = llm.clip(_matter_context(m), 11000)
    prompt = ("Write a summary of this matter for a lawyer who is picking it up cold: about 150 words, plain "
              "prose, past tense for what happened, present tense for where it stands. Then list the open items "
              "(deadlines, unanswered questions, unbilled work, unpaid invoices) as short strings. Use only the "
              "material below. Return JSON {\"summary\": \"...\", \"open_items\": [\"...\"]}.\n\n" + ctx)
    try:
        data = llm.complete_json(prompt, SUMMARY_SCHEMA, system=SYSTEM, max_tokens=1200, kind="matter_summary",
                                 entity="matter", entity_id=m.id, user_id=_uid())
    except LLMUnavailable as e:
        return render_template("ai/summary.html", m=m, summary=None, items=[], error=str(e), cut=cut)
    summary = str(data.get("summary") or "").strip() if isinstance(data, dict) else ""
    items = [str(x).strip() for x in (data.get("open_items") if isinstance(data, dict) else []) or [] if str(x).strip()]
    if not summary:
        return render_template("ai/summary.html", m=m, summary=None, items=[], cut=cut,
                               error="The AI answered in an unexpected format. Nothing was changed. Try again.")
    return render_template("ai/summary.html", m=m, summary=summary, items=items, error=None, cut=cut)


@bp.route("/matter/<int:id>/summary/save", methods=["POST"])
@login_required
def matter_summary_save(id):
    m = db.session.get(Matter, id) or abort(404)
    summary = request.form.get("summary", "").strip()
    items = [x.strip() for x in request.form.get("open_items", "").splitlines() if x.strip()]
    if not summary:
        flash("Nothing to save.", "error")
        return redirect(url_for("matters.detail", id=m.id))
    body = f"AI summary ({date.today():%b %-d, %Y}):\n{summary}"
    if items:
        body += "\n\nOpen items:\n" + "\n".join(f"- {x}" for x in items)
    n = Note(matter_id=m.id, user_id=_uid(), body=body)
    db.session.add(n)
    db.session.flush()
    audit("create", "note", n.id, f"AI summary saved on {m.number}", _uid())
    db.session.commit()
    flash("Summary saved as a note on the matter.", "ok")
    return redirect(url_for("matters.detail", id=m.id))


# ---------------------------------------------------------------------------
# 3. dates from a document
# ---------------------------------------------------------------------------
DATES_SCHEMA = {
    "type": "object",
    "properties": {"dates": {"type": "array", "items": {
        "type": "object",
        "properties": {"date": {"type": "string"}, "description": {"type": "string"},
                       "kind": {"type": "string", "enum": list(DATE_KINDS)}},
        "required": ["date", "description", "kind"], "additionalProperties": False}}},
    "required": ["dates"], "additionalProperties": False,
}


@bp.route("/document/<int:id>/dates", methods=["POST"])
@login_required
def document_dates(id):
    d = db.session.get(Document, id) or abort(404)
    text, cut = llm.clip((d.extracted_text or "").strip(), 10500)
    if not text:
        return render_template("ai/dates.html", doc=d, found=None, cut=False,
                               error="No text could be read from this file, so there is nothing to scan. "
                                     "Text, Markdown, CSV, Word and text-based PDF files work; scanned images do not.")
    prompt = (f"Today is {date.today().isoformat()}. Find every date in the document below that matters to the "
              "matter: filing deadlines, response due dates, hearings, trial settings, depositions, meetings, "
              "expiry dates, statute deadlines. For each give the calendar date as YYYY-MM-DD (resolve relative "
              "phrases like 'within 30 days of service' only when the document states the anchor date; otherwise "
              "skip it), a short description, and a kind: deadline (something must be filed or done by then), "
              "court_date (hearing, trial, deposition), event (meeting or appointment), task (other to-do). "
              "Ignore dates that are only history (signing dates, letter dates) unless a duty flows from them. "
              "Return JSON {\"dates\": [{\"date\": \"YYYY-MM-DD\", \"description\": \"...\", \"kind\": \"...\"}]}. "
              "Return an empty list when there are none.\n\nDocument: " + d.name + "\n\n" + text)
    try:
        data = llm.complete_json(prompt, DATES_SCHEMA, system=SYSTEM, max_tokens=1500, kind="document_dates",
                                 entity="document", entity_id=d.id, user_id=_uid())
    except LLMUnavailable as e:
        return render_template("ai/dates.html", doc=d, found=None, cut=cut, error=str(e))
    found = []
    for r in (data.get("dates") if isinstance(data, dict) else []) or []:
        if not isinstance(r, dict):
            continue
        dt = parse_date(str(r.get("date") or ""))
        desc = str(r.get("description") or "").strip()
        kind = str(r.get("kind") or "task").strip()
        if not dt or not desc:
            continue
        found.append(dict(date=dt, description=desc[:300], kind=kind if kind in DATE_KINDS else "task"))
    found.sort(key=lambda x: x["date"])
    return render_template("ai/dates.html", doc=d, found=found, cut=cut, error=None, kinds=DATE_KINDS)


@bp.route("/document/<int:id>/dates/create", methods=["POST"])
@login_required
def document_dates_create(id):
    d = db.session.get(Document, id) or abort(404)
    m = d.matter
    f = request.form
    try:
        n = int(f.get("n") or 0)
    except ValueError:
        n = 0
    made_tasks = made_events = skipped = 0
    for i in range(n):
        if f.get(f"sel_{i}") != "1":
            continue
        dt = parse_date(f.get(f"date_{i}"))
        desc = (f.get(f"desc_{i}") or "").strip()[:300]
        kind = (f.get(f"kind_{i}") or "task").strip()
        if not dt or not desc:
            skipped += 1
            continue
        if kind not in DATE_KINDS:
            kind = "task"
        if kind == "event":
            starts = datetime.combine(dt, datetime.min.time()).replace(hour=9)
            dup = CalendarEvent.query.filter_by(matter_id=m.id, title=desc).filter(
                CalendarEvent.starts_at >= datetime.combine(dt, datetime.min.time()),
                CalendarEvent.starts_at < datetime.combine(dt + timedelta(days=1), datetime.min.time())).first()
            if dup:
                skipped += 1
                continue
            ev = CalendarEvent(matter_id=m.id, title=desc, starts_at=starts, ends_at=starts + timedelta(hours=1),
                               all_day=True, notes=f"From document {d.name} (AI date extraction)")
            db.session.add(ev)
            db.session.flush()
            audit("create", "calendar_event", ev.id, f"{desc} from document #{d.id}", _uid())
            made_events += 1
        else:
            if Task.query.filter_by(matter_id=m.id, title=desc, due_on=dt).first():
                skipped += 1
                continue
            t = Task(matter_id=m.id, title=desc, kind=kind, due_on=dt, priority="high" if kind != "task" else "normal",
                     assignee_id=m.responsible_user_id, notes=f"From document {d.name} (AI date extraction)")
            db.session.add(t)
            db.session.flush()
            audit("create", "task", t.id, f"{kind} {desc} due {dt} from document #{d.id}", _uid())
            made_tasks += 1
    db.session.commit()
    bits = []
    if made_tasks:
        bits.append(f"{made_tasks} task(s) or deadline(s)")
    if made_events:
        bits.append(f"{made_events} calendar event(s)")
    msg = ("Created " + " and ".join(bits) + f" on {m.number}." if bits else "Nothing selected.")
    if skipped:
        msg += f" Skipped {skipped} that already existed or had no date."
    flash(msg, "ok" if bits else "error")
    return redirect(f"/matters/{m.id}?tab=tasks")


# ---------------------------------------------------------------------------
# 4. natural-language search
# ---------------------------------------------------------------------------
SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": {"type": "string", "enum": list(SEARCH_ENTITIES)}},
        "text": {"type": "string"},
        "person": {"type": "string"},
        "status": {"type": "string"},
        "practice_area": {"type": "string"},
        "date_from": {"type": "string"},
        "date_to": {"type": "string"},
        "overdue": {"type": "boolean"},
        "unpaid": {"type": "boolean"},
        "min_amount_cents": {"type": "integer"},
    },
    "required": ["entities", "text", "person", "status", "practice_area", "date_from", "date_to", "overdue",
                 "unpaid", "min_amount_cents"],
    "additionalProperties": False,
}


def _like(col, s):
    return col.ilike(f"%{s}%")


def _user_ids_named(name):
    if not name:
        return []
    return [u.id for u in User.query.filter(_like(User.name, name)).all()]


def _contact_ids_named(name):
    if not name:
        return []
    return [c.id for c in Contact.query.filter(db.or_(_like(Contact.first_name, name), _like(Contact.last_name, name),
                                                       _like(Contact.company_name, name),
                                                       _like(Contact.email, name))).all()]


def run_structured_search(flt):
    """flt is the parsed filter dict. Returns {entity: [rows]} (only entities that were searched)."""
    today = date.today()
    text = (flt.get("text") or "").strip()
    person = (flt.get("person") or "").strip()
    status = (flt.get("status") or "").strip().lower()
    area = (flt.get("practice_area") or "").strip()
    d_from = parse_date(flt.get("date_from"))
    d_to = parse_date(flt.get("date_to"))
    overdue = bool(flt.get("overdue"))
    unpaid = bool(flt.get("unpaid"))
    try:
        min_amt = int(flt.get("min_amount_cents") or 0)
    except (TypeError, ValueError):
        min_amt = 0
    entities = [e for e in (flt.get("entities") or []) if e in SEARCH_ENTITIES] or ["matters"]
    out = {}
    if "matters" in entities:
        q = Matter.query
        if text:
            q = q.filter(db.or_(_like(Matter.name, text), _like(Matter.number, text), _like(Matter.description, text),
                                _like(Matter.practice_area, text)))
        if status in ("open", "closed", "pending"):
            q = q.filter(Matter.status == status)
        if area:
            q = q.filter(_like(Matter.practice_area, area))
        if person:
            ids = _contact_ids_named(person)
            uids = _user_ids_named(person)
            conds = []
            if ids:
                conds.append(Matter.client_id.in_(ids))
            if uids:
                conds.append(Matter.responsible_user_id.in_(uids))
            q = q.filter(db.or_(*conds)) if conds else q.filter(db.false())
        if d_from:
            q = q.filter(Matter.opened_on >= d_from)
        if d_to:
            q = q.filter(Matter.opened_on <= d_to)
        out["matters"] = q.order_by(Matter.created_at.desc()).limit(25).all()
    if "contacts" in entities:
        q = Contact.query
        needle = text or person
        if needle:
            q = q.filter(db.or_(_like(Contact.first_name, needle), _like(Contact.last_name, needle),
                                _like(Contact.company_name, needle), _like(Contact.email, needle),
                                _like(Contact.tags, needle)))
        if status == "client":
            q = q.filter(Contact.is_client == True)  # noqa: E712
        out["contacts"] = q.order_by(Contact.last_name, Contact.first_name, Contact.company_name).limit(25).all()
    if "invoices" in entities:
        q = Invoice.query
        if text:
            ids = _contact_ids_named(text)
            q = q.filter(db.or_(_like(Invoice.number, text), Invoice.client_id.in_(ids) if ids else db.false()))
        if person:
            ids = _contact_ids_named(person)
            q = q.filter(Invoice.client_id.in_(ids) if ids else db.false())
        if status in ("draft", "sent", "viewed", "partial", "paid", "void"):
            q = q.filter(Invoice.status == status)
        if unpaid or overdue or status in ("unpaid", "outstanding", "overdue"):
            q = q.filter(Invoice.status.in_(["sent", "viewed", "partial"]))
        if overdue or status == "overdue":
            q = q.filter(Invoice.due_on != None, Invoice.due_on < today)  # noqa: E711
        if d_from:
            q = q.filter(Invoice.issued_on >= d_from)
        if d_to:
            q = q.filter(Invoice.issued_on <= d_to)
        if min_amt:
            q = q.filter(Invoice.total_cents >= min_amt)
        out["invoices"] = q.order_by(Invoice.issued_on.desc()).limit(25).all()
    if "time" in entities:
        q = TimeEntry.query
        if text:
            mids = [m.id for m in Matter.query.filter(db.or_(_like(Matter.name, text), _like(Matter.number, text))).all()]
            q = q.filter(db.or_(_like(TimeEntry.description, text),
                                TimeEntry.matter_id.in_(mids) if mids else db.false()))
        if person:
            uids = _user_ids_named(person)
            q = q.filter(TimeEntry.user_id.in_(uids) if uids else db.false())
        if d_from:
            q = q.filter(TimeEntry.date >= d_from)
        if d_to:
            q = q.filter(TimeEntry.date <= d_to)
        if status == "unbilled":
            q = q.filter(TimeEntry.invoice_id == None, TimeEntry.billable == True)  # noqa: E711,E712
        out["time"] = q.order_by(TimeEntry.date.desc()).limit(25).all()
    if "tasks" in entities:
        q = Task.query
        if text:
            q = q.filter(db.or_(_like(Task.title, text), _like(Task.notes, text)))
        if person:
            uids = _user_ids_named(person)
            q = q.filter(Task.assignee_id.in_(uids) if uids else db.false())
        if status == "done":
            q = q.filter(Task.done == True)  # noqa: E712
        elif status in ("open", "overdue") or overdue:
            q = q.filter(Task.done == False)  # noqa: E712
        if overdue or status == "overdue":
            q = q.filter(Task.due_on != None, Task.due_on < today)  # noqa: E711
        if d_from:
            q = q.filter(Task.due_on >= d_from)
        if d_to:
            q = q.filter(Task.due_on <= d_to)
        out["tasks"] = q.order_by(Task.due_on.asc().nulls_last()).limit(25).all()
    return out


def plain_search(q):
    """Substring search that needs no model. Returns {matters, contacts, documents}."""
    q = (q or "").strip()
    if not q:
        return {}
    terms = [t for t in q.split() if len(t) > 1][:6] or [q]

    def any_term(*cols):
        return db.or_(*[_like(c, t) for c in cols for t in terms])

    return {
        "matters": Matter.query.filter(any_term(Matter.name, Matter.number, Matter.description)).order_by(
            Matter.created_at.desc()).limit(25).all(),
        "contacts": Contact.query.filter(any_term(Contact.first_name, Contact.last_name, Contact.company_name,
                                                  Contact.email)).order_by(Contact.last_name).limit(25).all(),
        "documents": Document.query.filter(Document.is_current != False,  # noqa: E712
                                           any_term(Document.name, Document.tags, Document.folder,
                                                    Document.extracted_text)).order_by(
            Document.created_at.desc()).limit(25).all(),
    }


@bp.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()[:500]
    if not q:
        return render_template("ai/search.html", q="", structured=None, filters=None, plain=None, error=None)
    prompt = (f"Today is {date.today().isoformat()}. Turn this question from a law firm staff member into search "
              "filters over the practice management database. entities: which of matters, contacts, invoices, "
              "time, tasks to search (pick the ones that answer the question). text: a short keyword to match "
              "against names, numbers and descriptions, or empty. person: a client, attorney or staff name "
              "mentioned, or empty. status: one of open, closed, pending, client, draft, sent, paid, unpaid, "
              "overdue, done, unbilled, or empty. practice_area: e.g. 'Litigation', or empty. date_from and "
              "date_to as YYYY-MM-DD when a period is implied (\"last month\", \"this year\"), else empty. "
              "overdue and unpaid true only when asked. min_amount_cents when a dollar threshold is given, else 0. "
              "Return only JSON with every field present.\n\nQuestion: " + q)
    error = None
    structured = filters = None
    try:
        filters = llm.complete_json(prompt, SEARCH_SCHEMA, system=SYSTEM, max_tokens=600, kind="search",
                                    entity="search", user_id=_uid())
        if not isinstance(filters, dict):
            raise llm.LLMBadOutput("The AI answered in an unexpected format.")
        structured = run_structured_search(filters)
    except LLMUnavailable as e:
        error = str(e)
        structured = filters = None
    plain = plain_search(q)
    return render_template("ai/search.html", q=q, structured=structured, filters=filters, plain=plain, error=error)
