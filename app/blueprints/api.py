"""REST API v1 with Bearer tokens (ApiToken rows; sha256 of the raw token is stored).

Every response is JSON. 401 for a missing, unknown or revoked token, 403 when the token lacks the scope
(read tokens cannot write; readonly-role users cannot write regardless), 429 past RATE_LIMIT calls per minute
per token. The browser session is ignored here on purpose; only the Authorization header counts.
/api/v1/ is in helpers.CSRF_EXEMPT_PREFIXES, which also puts it on permissions.ALWAYS_ALLOW.
"""
import hashlib
import os
import secrets
import threading
import time as _time
from collections import deque
from datetime import date, datetime
from functools import wraps

from flask import Blueprint, request, jsonify, g, current_app
from sqlalchemy import or_, func
from werkzeug.exceptions import HTTPException

from ..extensions import db
from ..models import ApiToken, Matter, Contact, TimeEntry, Timer, Invoice, Task, Firm, IntakeLead, audit, now
from ..helpers import parse_date, parse_minutes

bp = Blueprint("api", __name__, url_prefix="/api/v1")

RATE_LIMIT = 120  # calls per RATE_WINDOW per token
RATE_WINDOW = 60.0
_rate = {}
_rate_lock = threading.Lock()


# ---------------------------------------------------------------- tokens
def new_raw_token():
    return "coil_" + secrets.token_urlsafe(32)


def hash_token(raw):
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- scopes
# A firm decides what each token may touch, resource by resource. This exists because
# the obvious consumer is now an AI agent, and "read everything and write anything" is
# not a choice a firm should have to make to let an assistant log time.
#
# Deliberately NOT addressable by any scope, at all:
#   trust accounting, payments, users and permissions, firm settings, and deletes.
# Those are absent from the API surface rather than gated, so no token, however
# configured, can reach client funds or remove a record.
RESOURCES = ("matters", "contacts", "time", "invoices", "tasks",
             "documents", "calendar", "notes", "leads", "voice")
ACCESS = ("read", "write")
ALL_SCOPES = tuple(f"{r}:{a}" for r in RESOURCES for a in ACCESS)

RESOURCE_LABELS = {
    "matters": "Matters and cases",
    "contacts": "Contacts and clients",
    "time": "Time entries and the timer",
    "invoices": "Invoices (draft only; never sending or payments)",
    "tasks": "Tasks and deadlines",
    "documents": "Documents (metadata and text; never the file bytes)",
    "calendar": "Calendar events",
    "notes": "Matter notes",
    "leads": "Intake leads",
    "voice": "The phone line (client status, notes and time by phone)",
}


def normalise_scopes(raw):
    """Accept a list or comma string; keep only real scopes. Legacy values still work.

    Tokens issued before scopes were granular carry "read" or "read,write", and those
    tokens are in the wild on installs we cannot reach. They keep meaning what they
    meant: everything readable, and everything writable.
    """
    # Accept a list of checkbox values, a comma string, or a list holding a comma string
    # (which is what the form posts when the legacy single-value control is still in use).
    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    parts = [p.strip() for item in items for p in str(item or "").split(",") if p.strip()]
    if not parts:
        return ["matters:read"]
    if set(parts) <= {"read", "write"}:
        wanted = [s for s in ALL_SCOPES if s.endswith(":read")]
        if "write" in parts:
            wanted += [s for s in ALL_SCOPES if s.endswith(":write")]
        return wanted
    return [p for p in ALL_SCOPES if p in parts] or ["matters:read"]


CONFIDENTIALITY = ("redacted", "full")


def create_token(user, name, scopes="read", confidentiality="redacted"):
    """-> (ApiToken, raw). The raw value is never stored; show it once.

    Defaults to redacted. Anything that wants the whole file has to ask for it.
    """
    raw = new_raw_token()
    mode = confidentiality if confidentiality in CONFIDENTIALITY else "redacted"
    t = ApiToken(user_id=user.id, name=(name or "API token")[:120], token_hash=hash_token(raw), prefix=raw[:12],
                 scopes=",".join(normalise_scopes(scopes)), confidentiality=mode)
    db.session.add(t)
    return t, raw


def token_scopes(t):
    return set(normalise_scopes(t.scopes if t is not None else ""))


# ---------------------------------------------------------------- plumbing
def _error(status, message):
    return jsonify({"error": message, "status": status}), status


def _effective_limit():
    """The per-worker share of the advertised limit.

    The counter is a dict in this process, and gunicorn runs several workers, so each one
    was enforcing the full limit on its own share of the traffic. With two workers the
    advertised 120 a minute was really 240, and it grew with every worker added. Dividing
    by the worker count makes the number Coil promises the number it enforces.

    WEB_CONCURRENCY has to match the -w in the Dockerfile. Set too low the limit is merely
    stricter than advertised, which is the safe direction to be wrong in.
    """
    # A typo in either value must not take the API down, and must not silently switch the
    # limiter off. Anything unreadable falls back to the stricter interpretation.
    try:
        limit = int(current_app.config.get("API_RATE_LIMIT") or RATE_LIMIT)
    except (TypeError, ValueError):
        limit = RATE_LIMIT
    try:
        workers = max(1, int(os.environ.get("WEB_CONCURRENCY") or 1))
    except (TypeError, ValueError):
        workers = 1
    return max(1, limit // workers)


def _rate_limited(token_id):
    limit = _effective_limit()
    t = _time.monotonic()
    with _rate_lock:
        dq = _rate.setdefault(token_id, deque())
        while dq and dq[0] <= t - RATE_WINDOW:
            dq.popleft()
        if len(dq) >= limit:
            return True
        dq.append(t)
    return False


def reset_rate_limits():
    with _rate_lock:
        _rate.clear()


@bp.before_request
def _authenticate():
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return _error(401, "Send an Authorization: Bearer <token> header.")
    raw = header[7:].strip()
    tok = ApiToken.query.filter_by(token_hash=hash_token(raw)).first() if raw else None
    if not tok or tok.revoked_at or not tok.user or not tok.user.is_active:
        return _error(401, "Unknown or revoked token.")
    if _rate_limited(tok.id):
        resp = _error(429, f"Rate limit of {current_app.config.get('API_RATE_LIMIT') or RATE_LIMIT} "
                            f"calls per minute reached.")
        resp[0].headers["Retry-After"] = "60"
        return resp
    g.api_token = tok
    g.api_user = tok.user
    g.user = tok.user  # so audit()/current_user() attribute writes to the token's owner
    if not tok.last_used_at or (now() - tok.last_used_at).total_seconds() > 60:
        tok.last_used_at = now()
        db.session.commit()


def scope_required(scope):
    """Gate an endpoint on one scope, e.g. "time:write".

    The user's own role still wins: a readonly user cannot write no matter how the token
    was configured, because a token must never be a way around the permissions a firm
    already set on a person.
    """
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if scope not in token_scopes(g.api_token):
                return _error(403, f"This token does not have the '{scope}' scope.")
            if scope.endswith(":write") and (g.api_user.role or "") == "readonly":
                return _error(403, "Read-only users cannot write through the API.")
            return f(*a, **kw)
        return wrapper
    return deco


def read_required(resource):
    return scope_required(f"{resource}:read")


@bp.errorhandler(HTTPException)
def _http_error(e):
    return _error(e.code or 500, e.description if isinstance(e.description, str) else e.name)


def _body():
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form.to_dict() if request.form else {}


def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _truthy(v, default=True):
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------ confidentiality
# Two modes, chosen per token, because the obvious consumer is a language model and
# "send the whole practice to a third party" is not one decision, it is two.
#
#   full      Everything. For a local model, or a provider under a zero-retention
#             agreement the firm has actually checked. The firm asserts that.
#   redacted  Structure without identity or substance. The default.
#
# Redacted withholds exactly two classes:
#
#   Identity   any name, email, phone or address of a client or contact. A matter
#              keeps its number, so an agent can still act on "M-1001".
#   Substance  the free text a lawyer wrote about the representation: matter
#              descriptions, time narratives, note bodies, document text.
#
# What survives is ids, numbers, dates, amounts, minutes, statuses and counts. That is
# enough for "how much did I bill last week", "what is overdue", "start a timer on
# M-1001" without the model ever learning who the client is or what the case is about.
#
# This is not a substitute for the firm's own judgement under Rule 1.6. It is a way to
# make the safe option the easy one.
REDACTED = "[redacted]"


def is_redacted():
    return getattr(g, "api_token", None) is not None and \
        (g.api_token.confidentiality or "full") == "redacted"


def _r(value):
    """Substance: drop it, but say something was there rather than pretend it was empty."""
    return REDACTED if value else value


def _r_name(kind, ident):
    """Identity: a stable label so an agent can still refer to the same thing twice."""
    return f"{kind} #{ident}"


# ---------------------------------------------------------------- serializers
def matter_json(m):
    d = {"id": m.id, "number": m.number, "name": m.name, "label": m.label, "status": m.status,
         "practice_area": m.practice_area, "billing_type": m.billing_type, "client_id": m.client_id,
         "client_name": m.client.display_name if m.client else "", "opened_on": _iso(m.opened_on),
         "closed_on": _iso(m.closed_on), "responsible_user_id": m.responsible_user_id,
         "court": m.court, "case_number": m.case_number}
    if is_redacted():
        # The matter NAME is usually the case caption, so it carries both parties.
        d["name"] = _r_name("Matter", m.id)
        d["label"] = m.number or _r_name("Matter", m.id)
        d["client_name"] = _r_name("Client", m.client_id) if m.client_id else ""
        d["case_number"] = _r(m.case_number)
        d["court"] = _r(m.court)
    return d


def contact_json(c):
    d = {"id": c.id, "name": c.display_name, "kind": c.kind, "email": c.email, "phone": c.phone,
         "is_client": bool(c.is_client)}
    if is_redacted():
        d.update(name=_r_name("Contact", c.id), email=_r(c.email), phone=_r(c.phone))
    return d


def time_json(t):
    d = {"id": t.id, "matter_id": t.matter_id, "matter_number": t.matter.number if t.matter else "",
         "user_id": t.user_id, "date": _iso(t.date), "minutes": t.minutes, "hours": t.hours,
         "description": t.description, "rate_cents": t.rate_cents, "amount_cents": t.amount_cents,
         "billable": bool(t.billable), "invoice_id": t.invoice_id}
    if is_redacted():
        # The narrative is what the lawyer did for the client. That is the representation.
        d["description"] = _r(t.description)
    return d


def timer_json(t):
    if not t:
        return None
    d = {"id": t.id, "matter_id": t.matter_id, "matter_number": t.matter.number if t.matter else "",
         "description": t.description, "started_at": _iso(t.started_at), "paused": bool(t.paused),
         "elapsed_seconds": t.elapsed_seconds()}
    if is_redacted():
        d["description"] = _r(t.description)
    return d


def invoice_json(i):
    d = {"id": i.id, "number": i.number, "status": i.status, "matter_id": i.matter_id, "client_id": i.client_id,
         "client_name": i.client.display_name if i.client else "", "issued_on": _iso(i.issued_on),
         "due_on": _iso(i.due_on), "total_cents": i.total_cents, "paid_cents": i.paid_cents,
         "balance_cents": i.balance_cents, "currency": i.currency}
    if is_redacted():
        # Amounts and dates stay: billing questions are the point, and a number is not
        # a confidence. Who it belongs to is.
        d["client_name"] = _r_name("Client", i.client_id) if i.client_id else ""
    return d


def task_json(t):
    d = {"id": t.id, "title": t.title, "kind": t.kind, "due_on": _iso(t.due_on), "priority": t.priority,
         "matter_id": t.matter_id, "matter_number": t.matter.number if t.matter else "",
         "assignee_id": t.assignee_id, "done": bool(t.done)}
    if is_redacted():
        # Titles read "Depose Dr Alvarez re: the fall" as often as "File answer".
        d["title"] = _r(t.title)
    return d


def document_json(d):
    """Metadata and nothing else. The API never returns file bytes: a document is the
    most concentrated form of a client confidence in the system, and an agent that can
    list what exists does not need to read it to be useful."""
    out = {"id": d.id, "matter_id": d.matter_id, "name": d.name, "folder": d.folder,
           "tags": d.tags, "size": d.size, "mime": d.mime, "version": d.version,
           "is_current": bool(d.is_current), "created_at": _iso(d.created_at)}
    if is_redacted():
        # File names are captions too: "Smith - settlement demand.pdf".
        out["name"] = _r_name("Document", d.id)
        out["folder"] = _r(d.folder)
        out["tags"] = _r(d.tags)
    return out


def event_json(e):
    out = {"id": e.id, "matter_id": e.matter_id, "title": e.title, "starts_at": _iso(e.starts_at),
           "ends_at": _iso(e.ends_at), "all_day": bool(e.all_day), "location": e.location,
           "user_id": e.user_id}
    if is_redacted():
        out["title"] = _r(e.title)
        out["location"] = _r(e.location)
    return out


def note_json(n):
    out = {"id": n.id, "matter_id": n.matter_id, "contact_id": n.contact_id,
           "user_id": n.user_id, "body": n.body, "created_at": _iso(n.created_at)}
    if is_redacted():
        out["body"] = _r(n.body)
    return out


# ---------------------------------------------------------------- endpoints
@bp.route("/me")
def me():
    u, t = g.api_user, g.api_token
    mode = (t.confidentiality or "full")
    return jsonify({
        "user": {"id": u.id, "name": u.name, "email": u.email, "role": u.role},
        # An agent reads this to decide which tools to offer and what it is allowed to
        # infer. `confidentiality` tells it whether what it is seeing is the real thing.
        "token": {"name": t.name, "prefix": t.prefix, "scopes": sorted(token_scopes(t)),
                  "confidentiality": mode,
                  "note": ("Client identities and the substance of matters are withheld from "
                           "this token. Do not guess at either." if mode == "redacted"
                           else "This token returns unredacted client data.")},
        "firm": {"name": Firm.get().name},
        "timer": timer_json(Timer.query.filter_by(user_id=u.id).first())})


@bp.route("/matters")
@read_required("matters")
def matters():
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "open").strip().lower()
    query = Matter.query
    if status != "all":
        query = query.filter(Matter.status == status)
    if q:
        like = f"%{q}%"
        query = query.outerjoin(Contact, Contact.id == Matter.client_id).filter(or_(
            Matter.number.ilike(like), Matter.name.ilike(like), Matter.case_number.ilike(like),
            Contact.first_name.ilike(like), Contact.last_name.ilike(like), Contact.company_name.ilike(like)))
    rows = query.order_by(Matter.number.desc()).limit(50).all()
    return jsonify({"matters": [matter_json(m) for m in rows]})


@bp.route("/matters/<int:id>")
@read_required("matters")
def matter(id):
    m = db.session.get(Matter, id)
    if not m:
        return _error(404, "No such matter.")
    d = matter_json(m)
    d["unbilled_time_cents"] = m.unbilled_time_cents()
    d["unbilled_expense_cents"] = m.unbilled_expense_cents()
    return jsonify(d)


@bp.route("/contacts")
@read_required("contacts")
def contacts():
    q = (request.args.get("q") or "").strip()
    query = Contact.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Contact.first_name.ilike(like), Contact.last_name.ilike(like),
                                 Contact.company_name.ilike(like), Contact.email.ilike(like)))
    rows = query.order_by(Contact.last_name, Contact.company_name, Contact.first_name).limit(50).all()
    return jsonify({"contacts": [contact_json(c) for c in rows]})


@bp.route("/time")
@read_required("time")
def time_list():
    query = TimeEntry.query
    mid = request.args.get("matter_id", type=int)
    if mid:
        query = query.filter(TimeEntry.matter_id == mid)
    uid = request.args.get("user_id", type=int)
    if uid:
        query = query.filter(TimeEntry.user_id == uid)
    d_from = parse_date(request.args.get("from"))
    d_to = parse_date(request.args.get("to"))
    if d_from:
        query = query.filter(TimeEntry.date >= d_from)
    if d_to:
        query = query.filter(TimeEntry.date <= d_to)
    total_minutes = query.with_entities(func.sum(TimeEntry.minutes)).scalar() or 0
    limit = min(int(request.args.get("limit", 200)), 200)
    rows = query.order_by(TimeEntry.date.desc(), TimeEntry.id.desc()).limit(limit).all()
    return jsonify({"time_entries": [time_json(t) for t in rows], "total_minutes": total_minutes})


@bp.route("/time", methods=["POST"])
@scope_required("time:write")
def time_create():
    b = _body()
    u = g.api_user
    try:
        mid = int(b.get("matter_id") or 0)
    except (TypeError, ValueError):
        mid = 0
    m = db.session.get(Matter, mid) if mid else None
    if not m:
        return _error(400, "matter_id is required and must be an existing matter.")
    try:
        minutes = int(b.get("minutes")) if b.get("minutes") not in (None, "") else parse_minutes(b.get("hours"))
    except (TypeError, ValueError):
        return _error(400, "minutes must be a whole number, or hours like 1.5 or 1:30.")
    if minutes <= 0:
        return _error(400, "Give minutes or hours greater than zero.")
    entry = TimeEntry(matter_id=m.id, user_id=u.id, date=parse_date(b.get("date"), date.today()), minutes=minutes,
                      description=(b.get("description") or "").strip(), rate_cents=m.effective_rate_cents(u),
                      billable=_truthy(b.get("billable"), True))
    db.session.add(entry)
    db.session.flush()
    audit("create", "time_entry", entry.id, f"api: {minutes}m on {m.number}", u.id)
    db.session.commit()
    return jsonify(time_json(entry)), 201


@bp.route("/timer")
@read_required("time")
def timer_status():
    return jsonify({"timer": timer_json(Timer.query.filter_by(user_id=g.api_user.id).first())})


@bp.route("/timer/start", methods=["POST"])
@scope_required("time:write")
def timer_start():
    u = g.api_user
    if Timer.query.filter_by(user_id=u.id).first():
        return _error(409, "A timer is already running. Stop it first.")
    b = _body()
    try:
        mid = int(b.get("matter_id") or 0)
    except (TypeError, ValueError):
        mid = 0
    m = db.session.get(Matter, mid) if mid else None
    if not m:
        return _error(400, "matter_id is required and must be an existing matter.")
    t = Timer(user_id=u.id, matter_id=m.id, description=(b.get("description") or "").strip(), started_at=now(),
              accumulated_seconds=0, paused=False)
    db.session.add(t)
    db.session.commit()
    return jsonify({"timer": timer_json(t)}), 201


@bp.route("/timer/stop", methods=["POST"])
@scope_required("time:write")
def timer_stop():
    from .time import round_up_minutes
    u = g.api_user
    t = Timer.query.filter_by(user_id=u.id).first()
    if not t:
        return _error(404, "No timer is running.")
    b = _body()
    try:
        mid = int(b.get("matter_id") or 0) or t.matter_id
    except (TypeError, ValueError):
        mid = t.matter_id
    m = db.session.get(Matter, mid) if mid else None
    if not m:
        return _error(400, "Pass matter_id so the time has somewhere to go.")
    seconds = t.elapsed_seconds()
    minutes = round_up_minutes(seconds)
    entry = TimeEntry(matter_id=m.id, user_id=u.id, date=date.today(), minutes=minutes,
                      description=(b.get("description") or t.description or "").strip(),
                      rate_cents=m.effective_rate_cents(u), billable=_truthy(b.get("billable"), True))
    db.session.add(entry)
    db.session.delete(t)
    db.session.flush()
    audit("create", "time_entry", entry.id, f"api timer stop: {seconds}s -> {minutes}m on {m.number}", u.id)
    db.session.commit()
    d = time_json(entry)
    d["elapsed_seconds"] = seconds
    return jsonify(d), 201


@bp.route("/invoices")
@read_required("invoices")
def invoices():
    status = (request.args.get("status") or "").strip().lower()
    query = Invoice.query
    if status == "open":
        query = query.filter(Invoice.status.in_(["sent", "viewed", "partial"]))
    elif status and status != "all":
        query = query.filter(Invoice.status == status)
    rows = query.order_by(Invoice.issued_on.desc(), Invoice.id.desc()).limit(100).all()
    return jsonify({"invoices": [invoice_json(i) for i in rows]})


@bp.route("/tasks")
@read_required("tasks")
def tasks():
    due = (request.args.get("due") or "").strip().lower()
    today = date.today()
    query = Task.query.filter(Task.done == False)  # noqa: E712
    if due == "today":
        query = query.filter(Task.due_on == today)
    elif due == "overdue":
        query = query.filter(Task.due_on != None, Task.due_on < today)  # noqa: E711
    elif due == "week":
        from datetime import timedelta
        query = query.filter(Task.due_on != None, Task.due_on <= today + timedelta(days=7))  # noqa: E711
    if _truthy(request.args.get("mine"), False):
        query = query.filter(Task.assignee_id == g.api_user.id)
    rows = query.order_by(Task.due_on.is_(None), Task.due_on, Task.priority.desc()).limit(200).all()
    return jsonify({"tasks": [task_json(t) for t in rows]})


# ---------------------------------------------------------------- leads (Ruby / Smith.ai lane, Agent R)
def _find_lead_by_ref(external_id):
    if not external_id:
        return None
    tag = f"[ref: {external_id}]"
    return IntakeLead.query.filter(IntakeLead.description.like(f"%{tag}%")).order_by(IntakeLead.id).first()


def lead_json(l, created=True):
    return {"id": l.id, "url": f"{current_app.config['BASE_URL']}/intake/{l.id}", "name": l.name,
            "status": l.status, "stage": l.stage, "score": l.score, "source": l.source, "created": bool(created)}


@bp.route("/leads", methods=["POST"])
@scope_required("leads:write")
def lead_create():
    """Phone intake from an answering service or the voice agent. Idempotent on external_id: the id is kept at
    the tail of the description as "[ref: <id>]" and a retry with the same id returns the existing lead."""
    from .intake import _score
    b = _body()
    external_id = str(b.get("external_id") or "").strip()[:120]
    if external_id and ("]" in external_id or "\n" in external_id):
        return _error(400, "external_id may not contain ] or a newline.")
    existing = _find_lead_by_ref(external_id)
    if existing:
        return jsonify(lead_json(existing, created=False)), 200
    name = str(b.get("name") or "").strip()[:200]
    if not name:
        return _error(400, "name is required.")
    parts = [str(b.get("description") or "").strip()]
    summary = str(b.get("call_summary") or "").strip()
    if summary:
        parts.append("Call summary:\n" + summary)
    transcript = b.get("transcript")
    if isinstance(transcript, list):
        lines = []
        for t in transcript:
            if isinstance(t, dict):
                who = t.get("role") or t.get("speaker") or ""
                text = t.get("text") or t.get("content") or ""
                lines.append(f"{who}: {text}".strip(": ") if who else str(text))
            else:
                lines.append(str(t))
        transcript = "\n".join(l for l in lines if l)
    transcript = str(transcript or "").strip()
    if transcript:
        parts.append("Transcript:\n" + transcript[:20000])
    if external_id:
        parts.append(f"[ref: {external_id}]")
    lead = IntakeLead(name=name, email=str(b.get("email") or "").strip()[:200],
                      phone=str(b.get("phone") or "").strip()[:50],
                      matter_type=str(b.get("matter_type") or "").strip()[:100],
                      description="\n\n".join(p for p in parts if p),
                      adverse_party=str(b.get("adverse_party") or "").strip()[:300],
                      source=(str(b.get("source") or "").strip() or "phone")[:100])
    db.session.add(lead)
    db.session.flush()
    _score(lead)
    audit("create", "intake_lead", lead.id, f"{lead.name} via api ({lead.source})"
          + (f" ref {external_id}" if external_id else ""), g.api_user.id)
    db.session.commit()
    return jsonify(lead_json(lead, created=True)), 201


# ---------------------------------------------------------------- time capture (Smokeball lane, Agent R)
@bp.route("/capture", methods=["POST"])
@scope_required("time:write")
def capture_create():
    """Segments from the extension: [{started_at ISO, minutes, title, url, source}]. Under two minutes is ignored;
    the same title within 30 minutes of a pending suggestion is merged into it. Logic lives in capture.py."""
    from .capture import ingest_segments, pending_count
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        segments = data.get("segments")
    else:
        segments = data
    if not isinstance(segments, list):
        return _error(400, "Send a JSON list of segments, or {\"segments\": [...]}.")
    if len(segments) > 500:
        return _error(400, "At most 500 segments per call.")
    r = ingest_segments(g.api_user, segments)
    db.session.commit()
    r["pending"] = pending_count(g.api_user)
    return jsonify(r), 201 if r["created"] else 200


@bp.route("/capture/pending")
@read_required("time")
def capture_pending():
    from .capture import pending_query
    rows = pending_query(g.api_user).all()
    return jsonify({"pending": len(rows), "minutes": sum(int(s.minutes or 0) for s in rows),
                    "url": f"{current_app.config['BASE_URL']}/time/suggestions"})


@bp.route("/documents")
@read_required("documents")
def documents():
    from ..models import Document
    q = Document.query.filter_by(is_current=True)
    mid = request.args.get("matter_id")
    if mid:
        q = q.filter_by(matter_id=int(mid))
    rows = q.order_by(Document.created_at.desc()).limit(min(int(request.args.get("limit", 50)), 200)).all()
    return jsonify({"documents": [document_json(d) for d in rows]})


@bp.route("/calendar")
@read_required("calendar")
def calendar_events():
    from ..models import CalendarEvent
    q = CalendarEvent.query
    if request.args.get("matter_id"):
        q = q.filter_by(matter_id=int(request.args["matter_id"]))
    frm = parse_date(request.args.get("from")) if request.args.get("from") else None
    if frm:
        q = q.filter(CalendarEvent.starts_at >= datetime.combine(frm, datetime.min.time()))
    rows = q.order_by(CalendarEvent.starts_at).limit(min(int(request.args.get("limit", 50)), 200)).all()
    return jsonify({"events": [event_json(e) for e in rows]})


@bp.route("/calendar", methods=["POST"])
@scope_required("calendar:write")
def create_event():
    from ..models import CalendarEvent
    b = _body()
    title = (b.get("title") or "").strip()
    starts = b.get("starts_at")
    if not title or not starts:
        return _error(400, "title and starts_at are required.")
    try:
        starts_at = datetime.fromisoformat(str(starts))
    except ValueError:
        return _error(400, "starts_at must be ISO 8601, e.g. 2026-09-08T14:30:00.")
    e = CalendarEvent(title=title[:300], starts_at=starts_at, user_id=g.api_user.id,
                      matter_id=int(b["matter_id"]) if b.get("matter_id") else None,
                      location=(b.get("location") or "")[:300])
    db.session.add(e)
    audit("calendar_create", "calendar_event", None, title[:120], g.api_user.id)
    db.session.commit()
    return jsonify({"event": event_json(e)}), 201


@bp.route("/notes")
@read_required("notes")
def notes():
    from ..models import Note
    q = Note.query
    if request.args.get("matter_id"):
        q = q.filter_by(matter_id=int(request.args["matter_id"]))
    rows = q.order_by(Note.created_at.desc()).limit(min(int(request.args.get("limit", 50)), 200)).all()
    return jsonify({"notes": [note_json(n) for n in rows]})


@bp.route("/notes", methods=["POST"])
@scope_required("notes:write")
def create_note():
    from ..models import Note
    b = _body()
    body_text = (b.get("body") or "").strip()
    if not body_text:
        return _error(400, "body is required.")
    if not b.get("matter_id"):
        return _error(400, "matter_id is required.")
    m = Matter.query.get(int(b["matter_id"]))
    if not m:
        return _error(404, "No such matter.")
    n = Note(matter_id=m.id, user_id=g.api_user.id, body=body_text[:20000])
    db.session.add(n)
    audit("note_create", "note", None, f"on {m.number}", g.api_user.id)
    db.session.commit()
    return jsonify({"note": note_json(n)}), 201


@bp.route("/<path:_rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def _not_found(_rest):
    return _error(404, "No such endpoint.")
