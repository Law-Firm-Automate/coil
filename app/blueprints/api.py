"""REST API v1 with Bearer tokens (ApiToken rows; sha256 of the raw token is stored).

Every response is JSON. 401 for a missing, unknown or revoked token, 403 when the token lacks the scope
(read tokens cannot write; readonly-role users cannot write regardless), 429 past RATE_LIMIT calls per minute
per token. The browser session is ignored here on purpose; only the Authorization header counts.
/api/v1/ is in helpers.CSRF_EXEMPT_PREFIXES, which also puts it on permissions.ALWAYS_ALLOW.
"""
import hashlib
import secrets
import threading
import time as _time
from collections import deque
from datetime import date, datetime
from functools import wraps

from flask import Blueprint, request, jsonify, g, current_app
from sqlalchemy import or_
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


def create_token(user, name, scopes="read"):
    """-> (ApiToken, raw). The raw value is never stored; show it once."""
    raw = new_raw_token()
    scopes = "read,write" if "write" in (scopes or "") else "read"
    t = ApiToken(user_id=user.id, name=(name or "API token")[:120], token_hash=hash_token(raw), prefix=raw[:12],
                 scopes=scopes)
    db.session.add(t)
    return t, raw


def token_scopes(t):
    return {s.strip() for s in (t.scopes or "").split(",") if s.strip()}


# ---------------------------------------------------------------- plumbing
def _error(status, message):
    return jsonify({"error": message, "status": status}), status


def _rate_limited(token_id):
    limit = current_app.config.get("API_RATE_LIMIT", RATE_LIMIT)
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
        resp = _error(429, f"Rate limit of {current_app.config.get('API_RATE_LIMIT', RATE_LIMIT)} calls per minute reached.")
        resp[0].headers["Retry-After"] = "60"
        return resp
    g.api_token = tok
    g.api_user = tok.user
    g.user = tok.user  # so audit()/current_user() attribute writes to the token's owner
    if not tok.last_used_at or (now() - tok.last_used_at).total_seconds() > 60:
        tok.last_used_at = now()
        db.session.commit()


def scope_required(scope):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if scope not in token_scopes(g.api_token):
                return _error(403, f"This token does not have the '{scope}' scope.")
            if scope == "write" and (g.api_user.role or "") == "readonly":
                return _error(403, "Read-only users cannot write through the API.")
            return f(*a, **kw)
        return wrapper
    return deco


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


# ---------------------------------------------------------------- serializers
def matter_json(m):
    return {"id": m.id, "number": m.number, "name": m.name, "label": m.label, "status": m.status,
            "practice_area": m.practice_area, "billing_type": m.billing_type, "client_id": m.client_id,
            "client_name": m.client.display_name if m.client else "", "opened_on": _iso(m.opened_on),
            "closed_on": _iso(m.closed_on), "responsible_user_id": m.responsible_user_id,
            "court": m.court, "case_number": m.case_number}


def contact_json(c):
    return {"id": c.id, "name": c.display_name, "kind": c.kind, "email": c.email, "phone": c.phone,
            "is_client": bool(c.is_client)}


def time_json(t):
    return {"id": t.id, "matter_id": t.matter_id, "matter_number": t.matter.number if t.matter else "",
            "user_id": t.user_id, "date": _iso(t.date), "minutes": t.minutes, "hours": t.hours,
            "description": t.description, "rate_cents": t.rate_cents, "amount_cents": t.amount_cents,
            "billable": bool(t.billable), "invoice_id": t.invoice_id}


def timer_json(t):
    if not t:
        return None
    return {"id": t.id, "matter_id": t.matter_id, "matter_number": t.matter.number if t.matter else "",
            "description": t.description, "started_at": _iso(t.started_at), "paused": bool(t.paused),
            "elapsed_seconds": t.elapsed_seconds()}


def invoice_json(i):
    return {"id": i.id, "number": i.number, "status": i.status, "matter_id": i.matter_id, "client_id": i.client_id,
            "client_name": i.client.display_name if i.client else "", "issued_on": _iso(i.issued_on),
            "due_on": _iso(i.due_on), "total_cents": i.total_cents, "paid_cents": i.paid_cents,
            "balance_cents": i.balance_cents, "currency": i.currency}


def task_json(t):
    return {"id": t.id, "title": t.title, "kind": t.kind, "due_on": _iso(t.due_on), "priority": t.priority,
            "matter_id": t.matter_id, "matter_number": t.matter.number if t.matter else "",
            "assignee_id": t.assignee_id, "done": bool(t.done)}


# ---------------------------------------------------------------- endpoints
@bp.route("/me")
def me():
    u, t = g.api_user, g.api_token
    return jsonify({"user": {"id": u.id, "name": u.name, "email": u.email, "role": u.role},
                    "token": {"name": t.name, "prefix": t.prefix, "scopes": sorted(token_scopes(t))},
                    "firm": {"name": Firm.get().name}, "timer": timer_json(Timer.query.filter_by(user_id=u.id).first())})


@bp.route("/matters")
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
def matter(id):
    m = db.session.get(Matter, id)
    if not m:
        return _error(404, "No such matter.")
    d = matter_json(m)
    d["unbilled_time_cents"] = m.unbilled_time_cents()
    d["unbilled_expense_cents"] = m.unbilled_expense_cents()
    return jsonify(d)


@bp.route("/contacts")
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
    rows = query.order_by(TimeEntry.date.desc(), TimeEntry.id.desc()).limit(200).all()
    return jsonify({"time_entries": [time_json(t) for t in rows], "total_minutes": sum(t.minutes for t in rows)})


@bp.route("/time", methods=["POST"])
@scope_required("write")
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
def timer_status():
    return jsonify({"timer": timer_json(Timer.query.filter_by(user_id=g.api_user.id).first())})


@bp.route("/timer/start", methods=["POST"])
@scope_required("write")
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
@scope_required("write")
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
@scope_required("write")
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
@scope_required("write")
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
def capture_pending():
    from .capture import pending_query
    rows = pending_query(g.api_user).all()
    return jsonify({"pending": len(rows), "minutes": sum(int(s.minutes or 0) for s in rows),
                    "url": f"{current_app.config['BASE_URL']}/time/suggestions"})


@bp.route("/<path:_rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def _not_found(_rest):
    return _error(404, "No such endpoint.")
