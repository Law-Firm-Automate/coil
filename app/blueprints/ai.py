"""AI features: invoice narrative polish, matter summary, date extraction from a document, natural-language search.

Every model call goes through app.llm (module reference, so tests can monkeypatch app.llm.complete). When the model
is unavailable (no key, AI off in Settings, daily cap, provider error) each page shows a calm explanation and, for
search, a plain substring search that always works.
"""
import json
import re
from datetime import date, datetime, timedelta
from html import escape
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app
from ..extensions import db
from ..models import (Matter, Contact, Invoice, TimeEntry, Expense, Task, CalendarEvent, Document, Note, Message,
                      User, TrustTransaction, Firm, AiRun, audit, now)
from ..helpers import login_required, current_user, parse_date
from ..services.mail import send_email
from ..i18n import lang_for
from .. import llm
from ..llm import LLMUnavailable

bp = Blueprint("ai", __name__, url_prefix="/ai")

SYSTEM = ("You help the staff of a small law firm. Be accurate and plain. Never invent facts, names, dates or "
          "amounts that are not in the material you are given. No marketing language.")

DATE_KINDS = ("deadline", "court_date", "task", "event")
SEARCH_ENTITIES = ("matters", "contacts", "invoices", "time", "tasks")


# A note whose body starts with this is attorney work product and never reaches a client-facing draft.
# Anything Coil writes to a matter for the firm's own use carries it (see matter_summary_save).
INTERNAL_PREFIX = "[internal]"

# Notes that talk about money are excluded from the client update as well, whoever wrote them. The
# update email promises no fees, hours or invoices, and a note is free text nobody vetted for that.
_MONEY_NOTE = re.compile(
    r"[$\u20ac\u00a3]\s?\d"
    r"|\b\d+(?:[.,]\d+)?\s*(?:hours?|hrs?)\b"
    r"|\b(?:fee|fees|invoice|invoiced|invoices|billing|billed|billable|unbilled|bill|retainer|trust"
    r"|hourly|rate|rates|payment|payments|paid|unpaid|owes|owing|balance|write[- ]?off|write[- ]?down"
    r"|collect|collection|cost|costs|deposit|refund)\b", re.I)


def _is_internal(note):
    return (note.body or "").lstrip().lower().startswith(INTERNAL_PREFIX)


def _client_safe_note(note):
    """True when a matter note may be shown to the client in a status update."""
    body = (note.body or "").strip()
    return bool(body) and not _is_internal(note) and not _MONEY_NOTE.search(body)


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
    # This note is attorney work product: it routinely names fees, unbilled hours and candid opinions.
    # The [internal] prefix is what keeps it out of client-facing drafts (see update_facts below).
    body = f"{INTERNAL_PREFIX} AI summary ({date.today():%b %-d, %Y}):\n{summary}"
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
# 5. client update email (Agent I; Clio Manage AI parity: "drafts case update emails to clients")
# ---------------------------------------------------------------------------
UPDATE_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"], "additionalProperties": False,
}
UPDATE_DAYS = 30


def update_facts(m, today=None):
    """Client-safe facts for a status update: recent notes, recent work descriptions, tasks finished
    recently, upcoming deadlines and events. Fees and invoices are deliberately left out, so a note is
    dropped when it starts with [internal] (everything Coil writes to a matter for the firm's own use
    does) or when it mentions money, hours, invoices or billing. The same list feeds the AI prompt and
    the no-model template, so both honour the same promise."""
    today = today or date.today()
    since = today - timedelta(days=UPDATE_DAYS)
    since_dt = datetime.combine(since, datetime.min.time())
    notes = [n for n in sorted(m.notes, key=lambda n: n.created_at or datetime.min, reverse=True)
             if _client_safe_note(n) and n.created_at and n.created_at >= since_dt][:8]
    work = [t for t in sorted(m.time_entries, key=lambda t: (t.date or date.min, t.id), reverse=True)
            if t.date and t.date >= since and (t.description or "").strip()][:10]
    done = Task.query.filter(Task.matter_id == m.id, Task.done == True, Task.done_at != None,  # noqa: E712,E711
                             Task.done_at >= since_dt).order_by(Task.done_at.desc()).limit(8).all()
    upcoming = Task.query.filter(Task.matter_id == m.id, Task.done == False, Task.due_on != None,  # noqa: E712,E711
                                 Task.due_on >= today, Task.kind.in_(["deadline", "court_date"])).order_by(
        Task.due_on).limit(8).all()
    events = CalendarEvent.query.filter(CalendarEvent.matter_id == m.id,
                                        CalendarEvent.starts_at >= datetime.combine(today, datetime.min.time())
                                        ).order_by(CalendarEvent.starts_at).limit(6).all()
    return {"notes": notes, "work": work, "done": done, "upcoming": upcoming, "events": events, "since": since}


def _facts_text(m, facts):
    parts = [f"Matter: {m.name} (our reference {m.number})",
             f"Client: {m.client.display_name if m.client else ''}",
             f"Responsible attorney: {m.responsible.name if m.responsible else 'the firm'}",
             f"Status: {m.status}. Practice area: {m.practice_area or ''}"]
    if facts["notes"]:
        parts.append("Recent notes:\n" + "\n".join(f"- {n.created_at:%Y-%m-%d}: {n.body.strip()}" for n in facts["notes"]))
    if facts["work"]:
        parts.append("Work done recently:\n" + "\n".join(f"- {t.date}: {t.description.strip()}" for t in facts["work"]))
    if facts["done"]:
        parts.append("Tasks completed:\n" + "\n".join(f"- {t.done_at:%Y-%m-%d}: {t.title}" for t in facts["done"]))
    if facts["upcoming"]:
        parts.append("Upcoming deadlines and court dates:\n" + "\n".join(
            f"- {t.due_on}: {t.title} ({t.kind.replace('_', ' ')})" for t in facts["upcoming"]))
    if facts["events"]:
        parts.append("Upcoming events:\n" + "\n".join(f"- {e.starts_at:%Y-%m-%d %H:%M}: {e.title}" for e in facts["events"]))
    return "\n\n".join(parts)


_UPDATE_T = {
    "en": {
        "subject": "Update on {matter}",
        "greeting": "Dear {name},",
        "intro": "Here is a short update on your matter, {matter}.",
        "work": "Since {since}, we have:",
        "done": "Completed:",
        "upcoming": "Coming up:",
        "events": "Scheduled:",
        "nothing": "There has been no new activity on the file since {since}. We are monitoring it and will let you know as soon as anything changes.",
        "close": "Please reply to this email or call the office if you have any questions.",
        "sign": "Kind regards,\n{attorney}\n{firm}",
        "ymd": "%B %-d, %Y",
    },
    "es": {
        "subject": "Actualización sobre {matter}",
        "greeting": "Estimado/a {name}:",
        "intro": "Le escribimos para informarle brevemente sobre el estado de su asunto, {matter}.",
        "work": "Desde el {since}, hemos realizado lo siguiente:",
        "done": "Tareas completadas:",
        "upcoming": "Próximos plazos:",
        "events": "Citas programadas:",
        "nothing": "No ha habido novedades en su expediente desde el {since}. Seguimos pendientes y le avisaremos en cuanto haya algún cambio.",
        "close": "Si tiene alguna pregunta, responda a este correo o llame a nuestra oficina.",
        "sign": "Atentamente,\n{attorney}\n{firm}",
        "ymd": "%-d de %B de %Y",
    },
}


NOTE_LINE_CHARS = 200


def _note_line(note):
    """One line from a note for a client-facing draft. A note is the attorney's own scratch space, so the
    template takes its first line and truncates it rather than pasting the whole body into the email."""
    for raw in (note.body or "").splitlines():
        line = " ".join(raw.split())
        if line:
            return line if len(line) <= NOTE_LINE_CHARS else line[:NOTE_LINE_CHARS].rstrip() + "..."
    return ""


def _first_name(contact):
    if not contact:
        return ""
    return (contact.first_name or "").strip() or contact.display_name


def template_update(m, facts, lang="en"):
    """Plain update email from the facts, no model needed. Returns (subject, body)."""
    T = _UPDATE_T.get(lang) or _UPDATE_T["en"]
    firm = Firm.get()
    attorney = m.responsible.name if m.responsible else firm.name
    since = facts["since"].strftime(T["ymd"])
    lines = [T["greeting"].format(name=_first_name(m.client)), "", T["intro"].format(matter=m.name), ""]
    items = [t.description.strip() for t in facts["work"]] + [_note_line(n) for n in facts["notes"]]
    items = [x for x in items if x]
    if items:
        lines.append(T["work"].format(since=since))
        lines += [f"- {x}" for x in items]
        lines.append("")
    if facts["done"]:
        lines.append(T["done"])
        lines += [f"- {t.title}" for t in facts["done"]]
        lines.append("")
    if facts["upcoming"]:
        lines.append(T["upcoming"])
        lines += [f"- {t.due_on.strftime(T['ymd'])}: {t.title}" for t in facts["upcoming"]]
        lines.append("")
    if facts["events"]:
        lines.append(T["events"])
        lines += [f"- {e.starts_at.strftime(T['ymd'])}: {e.title}" for e in facts["events"]]
        lines.append("")
    if not (items or facts["done"] or facts["upcoming"] or facts["events"]):
        lines += [T["nothing"].format(since=since), ""]
    lines += [T["close"], "", T["sign"].format(attorney=attorney, firm=firm.name)]
    return T["subject"].format(matter=m.name), "\n".join(lines)


def _update_page(m, subject, body, source, error=None):
    return render_template("ai/update_email.html", m=m, subject=subject, body=body, source=source, error=error,
                           to=(m.client.email if m.client else "") or "", lang=lang_for(m.client))


@bp.route("/matter/<int:id>/update-email", methods=["POST"])
@login_required
def matter_update_email(id):
    m = db.session.get(Matter, id) or abort(404)
    facts = update_facts(m)
    lang = lang_for(m.client)
    language = {"es": "Spanish (formal usted)"}.get(lang, "English")
    firm = Firm.get()
    attorney = m.responsible.name if m.responsible else firm.name
    ctx, _cut = llm.clip(_facts_text(m, facts), 9000)
    prompt = (f"Today is {date.today().isoformat()}. Write a short status update email from the law firm to its "
              f"client about the matter below, in {language}. Address the client by name, say plainly what has "
              "been done since the last update, what was completed, and what is coming up with dates. Use only "
              "the facts given; if there is little activity, say so honestly. Never mention fees, hours, rates, "
              "invoices or internal opinions. Warm, professional, no marketing, no jargon, about 120 to 180 "
              f"words, plain text with blank lines between paragraphs. Sign off as {attorney}, {firm.name}. "
              "Return JSON {\"subject\": \"...\", \"body\": \"...\"}.\n\n" + ctx)
    try:
        data = llm.complete_json(prompt, UPDATE_SCHEMA, system=SYSTEM, max_tokens=900, kind="client_update",
                                 entity="matter", entity_id=m.id, user_id=_uid())
        subject = str(data.get("subject") or "").strip() if isinstance(data, dict) else ""
        body = str(data.get("body") or "").strip() if isinstance(data, dict) else ""
        if not body:
            raise llm.LLMBadOutput("The AI answered in an unexpected format.")
        subject = subject or template_update(m, facts, lang)[0]
        return _update_page(m, subject, body, source="model")
    except LLMUnavailable as e:
        subject, body = template_update(m, facts, lang)
        return _update_page(m, subject, body, source="template", error=str(e))


def _body_html(body):
    paras = [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]
    return ("<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1c2430;max-width:600px\">"
            + "".join(f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in paras) + "</div>")


def _update_message(m, to, subject, body, status):
    u = current_user()
    msg = Message(contact_id=m.client_id, matter_id=m.id, direction="out", channel="email", to_addr=to,
                  from_addr=current_app.config.get("MAIL_FROM", ""), subject=subject[:300], body=body, status=status,
                  user_id=u.id if u else None, provider_id=f"client-update:{m.id}:{now():%Y%m%d%H%M%S}")
    db.session.add(msg)
    db.session.flush()
    return msg


@bp.route("/matter/<int:id>/update-email/send", methods=["POST"])
@login_required
def matter_update_send(id):
    m = db.session.get(Matter, id) or abort(404)
    f = request.form
    subject = (f.get("subject") or "").strip()[:300]
    body = (f.get("body") or "").strip()
    to = (f.get("to") or (m.client.email if m.client else "") or "").strip()
    if not (subject and body):
        flash("Subject and body are both needed.", "error")
        return _update_page(m, subject, body, source="edited"), 400
    if not to:
        flash("The client has no email address. Add one on the contact, or save this as a draft.", "error")
        return _update_page(m, subject, body, source="edited"), 400
    firm = Firm.get()
    send_email(to, subject, _body_html(body), text=body, reply_to=firm.email or None)
    msg = _update_message(m, to, subject, body, "sent")
    audit("send", "message", msg.id, f"client update on {m.number} to {to}", _uid())
    db.session.commit()
    flash(f"Update sent to {to}.", "ok")
    return redirect(url_for("matters.detail", id=m.id))


@bp.route("/matter/<int:id>/update-email/draft", methods=["POST"])
@login_required
def matter_update_draft(id):
    m = db.session.get(Matter, id) or abort(404)
    f = request.form
    subject = (f.get("subject") or "").strip()[:300]
    body = (f.get("body") or "").strip()
    to = (f.get("to") or (m.client.email if m.client else "") or "").strip()
    if not body:
        flash("Nothing to save.", "error")
        return redirect(url_for("matters.detail", id=m.id))
    msg = _update_message(m, to, subject, body, "draft")
    audit("create", "message", msg.id, f"client update draft on {m.number}", _uid())
    db.session.commit()
    flash("Saved as a draft. Send it from Intake, Drafts when you are ready.", "ok")
    return redirect("/intake/drafts")


# ---------------------------------------------------------------------------
# 6. aggregate questions answered without the model (Agent I; "what is in AR over 90 days")
# ---------------------------------------------------------------------------
_OPEN = ("sent", "viewed", "partial")
_CMP_LESS = ("less than", "fewer than", "under", "below", "at most", "no more than", "<")
_CMP_MORE = ("more than", "over", "above", "at least", "exceeding", ">")
_CMP = "|".join(re.escape(c) for c in _CMP_LESS + _CMP_MORE)
_AR_WORDS = r"(?:a/?r\b|accounts?\s+receivable|receivables?|outstanding|owed|owing|unpaid|past\s+due|overdue|aged|ag(?:e)?ing)"


def _cmp_kind(word):
    return "less" if word in _CMP_LESS else "more"


def _month_bounds(q, today):
    if re.search(r"\blast\s+month\b", q):
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev, f"{last_prev:%B %Y}"
    return today.replace(day=1), today, f"{today:%B %Y}"


def answer_aggregate(q, today=None):
    """Recognise a handful of numeric questions and answer them straight from the database. Returns a dict
    (title, answer, amount or number, link, link_label, rows, columns) or None when the question is not one of
    them. No model call is made here, so it works with AI off."""
    today = today or date.today()
    ql = " ".join((q or "").lower().split())
    if not ql:
        return None

    # hours per timekeeper with a threshold: "which timekeeper has less than 150 billable hours this month"
    m = (re.search(rf"(?P<cmp>{_CMP})\s*(?P<n>\d+(?:\.\d+)?)\s*(?:billable\s+)?(?:hours?|hrs?)\b", ql)
         or re.search(rf"(?:hours?|hrs?)\s+(?P<cmp>{_CMP})\s*(?P<n>\d+(?:\.\d+)?)", ql))
    if m and re.search(r"\b(hours?|hrs?)\b", ql):
        kind = _cmp_kind(m.group("cmp"))
        threshold = float(m.group("n"))
        start, end, label = _month_bounds(ql, today)
        billable_only = "billable" in ql or "non-billable" not in ql
        rows_q = TimeEntry.query.filter(TimeEntry.date >= start, TimeEntry.date <= end)
        if billable_only:
            rows_q = rows_q.filter(TimeEntry.billable == True)  # noqa: E712
        minutes = {}
        for t in rows_q.all():
            minutes[t.user_id] = minutes.get(t.user_id, 0) + (t.minutes or 0)
        users = User.query.filter_by(is_active=True).order_by(User.name).all()
        rows, matched = [], []
        for u in users:
            hrs = minutes.get(u.id, 0) / 60.0
            hit = hrs < threshold if kind == "less" else hrs > threshold
            rows.append({"cells": [u.name, f"{hrs:.2f}"], "hit": hit})
            if hit:
                matched.append(f"{u.name} ({hrs:.2f} h)")
        word = "under" if kind == "less" else "over"
        answer = (f"{len(matched)} timekeeper{'s' if len(matched) != 1 else ''} {word} {threshold:g} "
                  f"{'billable ' if billable_only else ''}hours in {label}: " + (", ".join(matched) or "none"))
        return {"title": f"Hours per timekeeper, {label}", "answer": answer, "number": len(matched),
                "link": f"/reports/productivity?from={start.isoformat()}&to={end.isoformat()}",
                "link_label": "Productivity report", "columns": ["Timekeeper", "Hours"], "rows": rows}

    # AR over N days: "what amount do we have in AR aged over 90 days"
    m = re.search(rf"(?:over|older\s+than|more\s+than|past|beyond|aged|greater\s+than|>)\s*(?P<n>\d+)\s*(?:\+\s*)?days", ql)
    if m and re.search(_AR_WORDS, ql):
        n = int(m.group("n"))
        cutoff = today - timedelta(days=n)
        invs = [i for i in Invoice.query.filter(Invoice.status.in_(_OPEN)).all()
                if i.balance_cents > 0 and (i.due_on or i.issued_on or today) < cutoff]
        total = sum(i.balance_cents for i in invs)
        rows = [{"cells": [i.number, i.client.display_name if i.client else "", i.due_on.isoformat() if i.due_on else "",
                           _fmt_money(i.balance_cents)], "hit": True, "href": f"/invoices/{i.id}"} for i in invs]
        return {"title": f"A/R over {n} days", "amount": total,
                "answer": f"{_fmt_money(total)} across {len(invs)} invoice{'s' if len(invs) != 1 else ''} whose due date "
                          f"is more than {n} days ago.",
                "link": "/reports/ar-aging", "link_label": "A/R aging report",
                "columns": ["Invoice", "Client", "Due", "Balance"], "rows": rows}

    # overdue invoices (count + total)
    if re.search(r"\b(overdue|past\s+due|late)\b", ql) and re.search(r"\binvoices?\b", ql):
        invs = [i for i in Invoice.query.filter(Invoice.status.in_(_OPEN), Invoice.due_on != None,  # noqa: E711
                                                Invoice.due_on < today).order_by(Invoice.due_on).all() if i.balance_cents > 0]
        total = sum(i.balance_cents for i in invs)
        rows = [{"cells": [i.number, i.client.display_name if i.client else "", i.due_on.isoformat(),
                           str((today - i.due_on).days), _fmt_money(i.balance_cents)], "hit": True,
                 "href": f"/invoices/{i.id}"} for i in invs]
        return {"title": "Overdue invoices", "number": len(invs), "amount": total,
                "answer": f"{len(invs)} overdue invoice{'s' if len(invs) != 1 else ''} totalling {_fmt_money(total)}.",
                "link": "/invoices?status=overdue", "link_label": "Overdue invoices",
                "columns": ["Invoice", "Client", "Due", "Days late", "Balance"], "rows": rows}

    # unbilled WIP
    if re.search(r"\b(unbilled|wip|work\s+in\s+progress|uninvoiced|not\s+(?:yet\s+)?(?:billed|invoiced))\b", ql):
        t_cents = sum(t.amount_cents for t in TimeEntry.query.filter(TimeEntry.billable == True,  # noqa: E712
                                                                      TimeEntry.invoice_id == None).all())  # noqa: E711
        e_cents = sum(e.amount_cents or 0 for e in Expense.query.filter(Expense.billable == True,  # noqa: E712
                                                                        Expense.invoice_id == None).all())  # noqa: E711
        total = t_cents + e_cents
        return {"title": "Unbilled work in progress", "amount": total,
                "answer": f"{_fmt_money(total)} unbilled: {_fmt_money(t_cents)} in time and {_fmt_money(e_cents)} in expenses.",
                "link": "/reports/wip", "link_label": "WIP report", "columns": ["Kind", "Amount"],
                "rows": [{"cells": ["Time", _fmt_money(t_cents)], "hit": True},
                         {"cells": ["Expenses", _fmt_money(e_cents)], "hit": True}]}

    # trust total
    if re.search(r"\b(trust|iolta|retainer)\b", ql) and re.search(r"\b(balance|total|held|hold|have|much|funds?|account)\b", ql):
        total = int(db.session.query(db.func.coalesce(db.func.sum(TrustTransaction.amount_cents), 0)).scalar() or 0)
        by_client = {}
        for t in TrustTransaction.query.all():
            by_client[t.client_id] = by_client.get(t.client_id, 0) + (t.amount_cents or 0)
        rows = []
        for cid, cents in sorted(by_client.items(), key=lambda kv: -kv[1]):
            if cents:
                c = db.session.get(Contact, cid)
                rows.append({"cells": [c.display_name if c else str(cid), _fmt_money(cents)], "hit": True,
                             "href": f"/trust/ledger/{cid}"})
        return {"title": "Trust balance", "amount": total,
                "answer": f"{_fmt_money(total)} held in trust across {len(rows)} client{'s' if len(rows) != 1 else ''}.",
                "link": "/reports/trust-balances", "link_label": "Trust balances report",
                "columns": ["Client", "Balance"], "rows": rows}

    # AR total
    if re.search(_AR_WORDS, ql) and re.search(r"\b(total|how\s+much|what|amount|balance|sum|all)\b", ql):
        invs = [i for i in Invoice.query.filter(Invoice.status.in_(_OPEN)).all() if i.balance_cents > 0]
        total = sum(i.balance_cents for i in invs)
        overdue = sum(i.balance_cents for i in invs if i.due_on and i.due_on < today)
        return {"title": "Accounts receivable", "amount": total,
                "answer": f"{_fmt_money(total)} outstanding on {len(invs)} open invoice{'s' if len(invs) != 1 else ''}, "
                          f"of which {_fmt_money(overdue)} is past due.",
                "link": "/reports/ar-aging", "link_label": "A/R aging report", "columns": ["Bucket", "Amount"],
                "rows": [{"cells": ["Not yet due", _fmt_money(total - overdue)], "hit": True},
                         {"cells": ["Past due", _fmt_money(overdue)], "hit": True}]}
    return None


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
    answer = answer_aggregate(q)
    if answer:  # a number question: answered from the database, no model call
        return render_template("ai/search.html", q=q, structured=None, filters=None, plain=None, error=None,
                               answer=answer)
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
