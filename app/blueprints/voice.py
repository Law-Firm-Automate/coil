"""Voice line: the Coil side of the phone agent (Ruby / Smith.ai lane).

Three things live here.

1. The voice API under /api/v1/voice. Bearer tokens and scopes exactly as api.py (its auth helper is imported
   and run from a decorator, because api.py's before_request only covers its own blueprint). Every endpoint is
   JSON, and every endpoint that receives a call_id keeps one VoiceCall row per call_id, updating it as the
   call progresses. Business refusals come back as {"ok": false, "reason": "..."} with a 403 (or 409 for an
   ambiguous matter hint) so the agent can branch on the JSON without parsing prose.

   Data rules the endpoints enforce, independent of what the phone agent asks for:
   - lookup never returns anything about the contact, only whether the caller id is known and what to ask;
   - verify returns matters only when the spoken name fuzzy-matches the contact (or an alias) AND the firm has
     turned on client status by phone; no note bodies, documents, trust balances or other clients ever go out;
   - note and time need a PIN verified for the same call within 30 minutes, and five wrong PINs from one
     phone lock that phone for an hour. Both are in-memory, which is fine for one Coil process.

2. Outbound reminder calls (run_voice_reminders, wired to `python -m app.cli voice_reminders`): Twilio REST
   Calls with inline TwiML, one call per event per client, idempotent through AuditLog action=voice_reminded.

3. Staff pages: /voice (recent calls), /voice/<id>, /settings/voice (owner), and the voice phone + PIN fields
   handled by settings.py on the user form.
"""
import json
import re
import threading
from collections import deque
from datetime import date, datetime, timedelta
from functools import wraps
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

import requests
from flask import Blueprint, render_template, request, jsonify, g, current_app, redirect, url_for, flash, abort
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from ..models import (Firm, User, Contact, Matter, Task, CalendarEvent, Note, TimeEntry, Message, VoiceCall,
                      IntakeLead, AuditLog, audit, now)
from ..helpers import login_required, owner_required, current_user
from ..services import sms as smssvc
from .api import _authenticate as api_authenticate, _error, _body, token_scopes
from .time import round_up_minutes

bp = Blueprint("voice", __name__)

PRACTICE_AREAS = [
    ("dui", "DUI / DWI"), ("criminal", "Criminal defense"), ("personal_injury", "Personal injury"),
    ("family", "Family law"), ("estate", "Estate planning"), ("business", "Business"),
    ("immigration", "Immigration"), ("employment", "Employment"), ("general", "General (anything else)"),
]
PRACTICE_AREA_KEYS = [k for k, _ in PRACTICE_AREAS]
APPROVED_LINE_KEYS = [
    ("no_advice", "No legal advice", "Said when a caller asks what they should do or what will happen."),
    ("fees", "Fees", "Said when a caller asks what the firm charges."),
    ("do_not_discuss", "Do not discuss", "The closing line: what the caller should not say to anyone before the attorney calls."),
    ("hours", "Hours and callback", "Office hours and how callbacks work."),
]
DEFAULT_APPROVED_LINES = {
    "no_advice": "I am not able to give legal advice. The attorney will go over that with you when they call back.",
    "fees": "The attorney will go over fees on the call back. There is no charge for that call.",
    "do_not_discuss": "Until the attorney calls, please do not discuss the case with anyone but a lawyer, including the police.",
    "hours": "The office is open Monday through Friday, nine to five. After hours, this line files your intake and the on-call attorney calls you back.",
}
DEFAULT_CALLBACK = {"urgent_minutes": 15, "high_minutes": 30, "standard": "by 9:00 tomorrow morning"}
CALL_KINDS = ["intake", "status", "memo", "reminder", "other"]
OUTCOMES = ["filed", "verified", "unverified", "note_saved", "time_saved", "reminded", "no_answer", "failed"]

NAME_MATCH_THRESHOLD = 85
HINT_MATCH_THRESHOLD = 70
HINT_AMBIGUOUS_GAP = 10
PIN_LOCK_FAILURES = 5
PIN_LOCK_SECONDS = 3600
PIN_SESSION_SECONDS = 1800
MAX_MATTERS_FOR_ATTORNEY = 40

# In-memory state. One Coil process serves a firm, so a dict is enough; a restart simply asks for the PIN again.
_pin_failures = {}   # phone digits -> deque of datetimes
_pin_sessions = {}   # call_id or session token -> (user_id, verified_at)
_state_lock = threading.Lock()


def reset_voice_state():
    with _state_lock:
        _pin_failures.clear()
        _pin_sessions.clear()


# ---------------------------------------------------------------- small helpers
def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def _last10(s):
    d = _digits(s)
    return d[-10:] if len(d) >= 10 else d


def _json(d):
    try:
        v = json.loads(d or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def approved_lines(firm):
    lines = dict(DEFAULT_APPROVED_LINES)
    lines.update({k: v for k, v in _json(firm.voice_approved_lines_json).items() if isinstance(v, str)})
    return lines


def callback_tiers(firm):
    tiers = dict(DEFAULT_CALLBACK)
    tiers.update(_json(firm.voice_callback_json))
    return tiers


def practice_areas(firm):
    return [p.strip() for p in (firm.voice_practice_areas or "").split(",") if p.strip()]


def greeting_name(firm):
    return (firm.voice_greeting_name or "").strip() or firm.name


def firm_tz(firm):
    try:
        return ZoneInfo(firm.timezone or "America/Chicago")
    except Exception:
        return ZoneInfo("UTC")


def _local(dt, firm):
    """Naive UTC -> aware local datetime in the firm's zone."""
    if not dt:
        return None
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(firm_tz(firm))


def _spoken_when(dt, firm, all_day=False):
    loc = _local(dt, firm)
    if not loc:
        return ""
    if all_day:
        return loc.strftime("%A, %B %-d")
    return loc.strftime("%A, %B %-d at %-I:%M %p ") + (loc.tzname() or "")


def _fuzz():
    from rapidfuzz import fuzz
    return fuzz


def _name_score(spoken, contact):
    fuzz = _fuzz()
    spoken = " ".join(str(spoken or "").lower().split())
    if not spoken:
        return 0
    names = [contact.display_name] + [a.strip() for a in (contact.aliases or "").splitlines() if a.strip()]
    if contact.kind != "company" and contact.first_name and contact.last_name:
        names.append(f"{contact.last_name} {contact.first_name}")
    best = 0
    for n in names:
        n = " ".join(n.lower().split())
        if not n:
            continue
        best = max(best, fuzz.ratio(spoken, n), fuzz.token_sort_ratio(spoken, n))
    return best


def _contacts_by_phone(phone):
    key = _last10(phone)
    if len(key) < 7:
        return []
    return [c for c in Contact.query.filter(Contact.phone != "").all() if _last10(c.phone) == key]


def _users_by_phone(phone):
    key = _last10(phone)
    if len(key) < 7:
        return []
    return [u for u in User.query.filter(User.is_active == True, User.voice_phone != "").all()  # noqa: E712
            if _last10(u.voice_phone) == key and (u.voice_pin_hash or "")]


def _matter_brief(m):
    return {"id": m.id, "number": m.number, "name": m.name, "label": m.label,
            "client": m.client.display_name if m.client else ""}


# ---------------------------------------------------------------- VoiceCall bookkeeping
def _touch_call(call_id, **fields):
    """Get or create the VoiceCall for this provider call id and set any non-empty fields. Caller commits."""
    call_id = str(call_id or "").strip()[:120]
    if not call_id:
        return None
    vc = VoiceCall.query.filter_by(call_id=call_id).first()
    if not vc:
        vc = VoiceCall(call_id=call_id, direction="in")
        db.session.add(vc)
    for k, v in fields.items():
        if v in (None, ""):
            continue
        setattr(vc, k, v)
    return vc


# ---------------------------------------------------------------- API plumbing
def voice_api(scope="read"):
    """Bearer auth and scope exactly as api.py, then the firm-level switch. Off = 403 {ok: false}."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            resp = api_authenticate()
            if resp is not None:
                return resp
            if scope not in token_scopes(g.api_token):
                return _error(403, f"This token does not have the '{scope}' scope.")
            if scope == "write" and (g.api_user.role or "") == "readonly":
                return _error(403, "Read-only users cannot write through the API.")
            firm = Firm.get()
            if not firm.voice_enabled:
                return jsonify({"ok": False, "reason": "voice_disabled",
                                "message": "The voice line is switched off in Settings > Voice line."}), 403
            g.firm = firm
            return f(*a, **kw)
        return wrapper
    return deco


def _refuse(reason, message, status=403, **extra):
    d = {"ok": False, "reason": reason, "message": message}
    d.update(extra)
    return jsonify(d), status


@bp.errorhandler(HTTPException)
def _http_error(e):
    if request.path.startswith("/api/"):
        return _error(e.code or 500, e.description if isinstance(e.description, str) else e.name)
    return render_template("error.html", code=e.code or 500, message=e.description or e.name), e.code or 500


def voice_endpoints(base):
    root = f"{base}/api/v1/voice"
    return [
        ("GET", f"{root}/config", "firm identity, practice areas, approved lines, callback tiers, flags"),
        ("POST", f"{root}/lookup", "{phone, name?, call_id?} -> is this caller id known (no data)"),
        ("POST", f"{root}/verify", "{phone, name, matter_hint?, call_id?} -> matters with next event and balance"),
        ("POST", f"{root}/pin", "{phone, pin, call_id?} -> attorney identity and open matters"),
        ("POST", f"{root}/note", "{user_id, matter_id|matter_hint, body, call_id} -> note on the matter"),
        ("POST", f"{root}/time", "{user_id, matter_id|matter_hint, minutes, description, call_id} -> time entry"),
        ("POST", f"{root}/calls", "{call_id, kind, from, to, summary, transcript, duration_seconds, outcome, ...} at hangup"),
        ("POST", f"{base}/api/v1/leads", "existing phone intake endpoint (COIL_LEADS_URL)"),
    ]


# ---------------------------------------------------------------- GET /api/v1/voice/config
@bp.route("/api/v1/voice/config")
@voice_api("read")
def api_config():
    f = g.firm
    return jsonify({
        "ok": True,
        "name": f.name, "greeting_name": greeting_name(f), "phone": f.phone, "timezone": f.timezone,
        "practice_areas": practice_areas(f),
        "approved_lines": approved_lines(f),
        "callback_tiers": callback_tiers(f),
        "voice_client_status": bool(f.voice_client_status),
        "voice_reminders": bool(f.voice_reminders),
        "voice_reminder_days": int(f.voice_reminder_days or 1),
        "endpoints": {m + " " + u.replace(current_app.config["BASE_URL"], ""): d
                      for m, u, d in voice_endpoints(current_app.config["BASE_URL"])},
    })


# ---------------------------------------------------------------- POST /api/v1/voice/lookup
@bp.route("/api/v1/voice/lookup", methods=["POST"])
@voice_api("read")
def api_lookup():
    b = _body()
    phone = str(b.get("phone") or "").strip()
    if not phone:
        return _refuse("phone_required", "Send the caller id as phone.", 400)
    found = bool(_contacts_by_phone(phone))
    vc = _touch_call(b.get("call_id"), kind="status", from_number=phone[:50],
                     summary=("Caller id matched a contact; verification pending." if found
                              else "Caller id did not match a contact."))
    if vc and not vc.outcome:
        vc.outcome = "unverified"
    db.session.commit()
    return jsonify({"ok": True, "found": found, "verification_required": True,
                    "hint": "ask for full name and the matter"})


# ---------------------------------------------------------------- POST /api/v1/voice/verify
def _next_event(m, firm):
    ev = CalendarEvent.query.filter(CalendarEvent.matter_id == m.id, CalendarEvent.starts_at >= now()) \
        .order_by(CalendarEvent.starts_at).first()
    if not ev:
        return None
    return {"title": ev.title, "when": _spoken_when(ev.starts_at, firm, ev.all_day),
            "starts_at": _local(ev.starts_at, firm).isoformat(timespec="minutes")}


def _next_task(m):
    t = Task.query.filter(Task.matter_id == m.id, Task.done == False, Task.due_on != None,  # noqa: E711,E712
                          Task.due_on >= date.today()).order_by(Task.due_on).first()
    if not t:
        return None
    return {"title": t.title, "due_on": t.due_on.isoformat(), "kind": t.kind}


def _last_update(m):
    """"<date>: <what changed>". Deliberately never the note body: internal notes are not client-facing."""
    candidates = []
    if m.stage_changed_at and m.stage:
        candidates.append((m.stage_changed_at, f"stage changed to {m.stage}"))
    note = Note.query.filter_by(matter_id=m.id).order_by(Note.created_at.desc()).first()
    if note:
        who = note.user.name if note.user else "the office"
        candidates.append((note.created_at, f"a note was added by {who}"))
    if m.closed_on:
        candidates.append((datetime.combine(m.closed_on, datetime.min.time()), "matter closed"))
    if not candidates:
        opened = datetime.combine(m.opened_on or date.today(), datetime.min.time())
        candidates.append((opened, "matter opened"))
    when, what = max(candidates, key=lambda c: c[0])
    return f"{when:%B %-d, %Y}: {what}"


def _matter_status(m, firm):
    return {"id": m.id, "number": m.number, "name": m.name, "stage": m.stage or m.status, "status": m.status,
            "next_event": _next_event(m, firm), "next_task_due": _next_task(m),
            "balance_cents": m.outstanding_cents(), "last_update": _last_update(m)}


@bp.route("/api/v1/voice/verify", methods=["POST"])
@voice_api("read")
def api_verify():
    f = g.firm
    b = _body()
    phone = str(b.get("phone") or "").strip()
    name = str(b.get("name") or "").strip()
    hint = str(b.get("matter_hint") or "").strip()
    call_id = b.get("call_id")
    if not f.voice_client_status:
        _touch_call(call_id, kind="status", from_number=phone[:50], outcome="unverified",
                    summary="Client status by phone is off; caller asked for status.")
        db.session.commit()
        return _refuse("client_status_off", "This firm does not give case status by phone. Offer a callback.")
    if not phone or not name:
        return _refuse("phone_and_name_required", "Send phone (caller id) and the caller's full name.", 400)
    matches = _contacts_by_phone(phone)
    if not matches:
        _touch_call(call_id, kind="status", from_number=phone[:50], outcome="unverified",
                    summary=f"Status request from an unknown number; name given: {name[:80]}")
        db.session.commit()
        return _refuse("no_match", "That number is not on file. Offer a callback and do not confirm any client.")
    scored = sorted(((_name_score(name, c), c) for c in matches), key=lambda x: -x[0])
    score, contact = scored[0]
    if score < NAME_MATCH_THRESHOLD:
        _touch_call(call_id, kind="status", from_number=phone[:50], outcome="unverified",
                    summary=f"Name did not match the contact on this number (score {int(score)}).")
        db.session.commit()
        return _refuse("name_mismatch", "The name did not match our records. Offer a callback.")
    matters = [m for m in contact.matters if m.status != "closed"]
    matters.sort(key=lambda m: (m.opened_on or date.min), reverse=True)
    if hint and matters:
        picked, ambiguous = _resolve_hint(hint, matters)
        if picked:
            matters = [picked]
        elif ambiguous:
            matters = ambiguous
    payload = [_matter_status(m, f) for m in matters]
    vc = _touch_call(call_id, kind="status", from_number=phone[:50], contact_id=contact.id,
                     matter_id=matters[0].id if len(matters) == 1 else None, outcome="verified",
                     summary=f"Verified {contact.display_name} by name (score {int(score)}); "
                             f"{len(matters)} matter(s) read.")
    audit("voice_verify", "contact", contact.id, f"call {call_id or 'no id'}: {len(matters)} matter(s)", g.api_user.id)
    db.session.commit()
    return jsonify({"ok": True, "contact_id": contact.id, "name": contact.display_name, "matters": payload,
                    "call_record_id": vc.id if vc else None})


# ---------------------------------------------------------------- matter hint resolution
_NUMBER_RE = re.compile(r"^\s*([A-Za-z]{0,6})\s*[-\s]?\s*(\d{2,})\s*$")
_HINT_STOP = {"the", "a", "an", "case", "matter", "file", "on", "for", "of", "my", "that", "this", "one", "v", "vs"}


def _hint_words(s):
    """Lowercase, punctuation out, filler words out, so "the Bluebonnet contract case" meets "Bluebonnet v. Holloway (contract)"."""
    words = re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).split()
    kept = [w for w in words if w not in _HINT_STOP]
    return " ".join(kept or words)


def _resolve_hint(hint, candidates, prefer_ids=()):
    """-> (matter or None, ambiguous list). A matter number wins outright; otherwise fuzzy on matter name and
    client name, with a small bonus for the matters in prefer_ids (the attorney's own). Two close scores come
    back as ambiguous so the agent can ask which one."""
    hint = " ".join(str(hint or "").split())
    if not hint or not candidates:
        return None, []
    m_num = _NUMBER_RE.match(hint)
    if m_num:
        digits = m_num.group(2)
        exact = [m for m in candidates if (m.number or "").lower() == hint.lower().replace(" ", "")]
        if exact:
            return exact[0], []
        by_digits = [m for m in candidates if _digits(m.number) == digits]
        if len(by_digits) == 1:
            return by_digits[0], []
        if len(by_digits) > 1:
            return None, by_digits[:2]
    fuzz = _fuzz()
    h = _hint_words(hint)
    scored = []
    for m in candidates:
        texts = [_hint_words(t) for t in (m.name or "", m.client.display_name if m.client else "", m.label)]
        s = max(max(fuzz.partial_ratio(h, t), fuzz.token_set_ratio(h, t)) for t in texts if t)
        if m.id in prefer_ids:
            s = min(100, s + 5)
        scored.append((s, m))
    scored.sort(key=lambda x: (-x[0], x[1].id))
    best, m1 = scored[0]
    if best < HINT_MATCH_THRESHOLD:
        return None, []
    if len(scored) > 1 and scored[1][0] >= HINT_MATCH_THRESHOLD and best - scored[1][0] < HINT_AMBIGUOUS_GAP:
        return None, [m1, scored[1][1]]
    return m1, []


def _attorney_matters(user):
    q = Matter.query.filter(Matter.status != "closed",
                            db.or_(Matter.responsible_user_id == user.id, Matter.originating_user_id == user.id))
    return q.order_by(Matter.opened_on.desc(), Matter.id.desc()).limit(MAX_MATTERS_FOR_ATTORNEY).all()


def _pick_matter(b, user):
    """-> (matter, error_response). Uses matter_id when given, else matter_hint against every open matter with
    a small preference for the attorney's own."""
    mid = b.get("matter_id")
    if mid not in (None, ""):
        try:
            m = db.session.get(Matter, int(mid))
        except (TypeError, ValueError):
            m = None
        if not m:
            return None, _refuse("no_matter", "No matter with that id.", 404)
        return m, None
    hint = str(b.get("matter_hint") or "").strip()
    if not hint:
        return None, _refuse("matter_required", "Send matter_id or matter_hint.", 400)
    mine = {m.id for m in _attorney_matters(user)}
    every = Matter.query.filter(Matter.status != "closed").order_by(Matter.id).all()
    picked, ambiguous = _resolve_hint(hint, every, prefer_ids=mine)
    if ambiguous:
        return None, _refuse("ambiguous", "More than one matter fits. Ask which one.", 409,
                             ambiguous=[_matter_brief(m) for m in ambiguous])
    if not picked:
        return None, _refuse("no_matter", "No open matter matched that description. Ask for the matter number.", 404)
    return picked, None


# ---------------------------------------------------------------- POST /api/v1/voice/pin
def _locked(key):
    with _state_lock:
        dq = _pin_failures.get(key)
        if not dq:
            return False
        cutoff = now() - timedelta(seconds=PIN_LOCK_SECONDS)
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq) >= PIN_LOCK_FAILURES


def _record_failure(key):
    with _state_lock:
        _pin_failures.setdefault(key, deque()).append(now())


def _open_session(key, user_id):
    with _state_lock:
        _pin_sessions[key] = (user_id, now())


def _session_user_id(*keys):
    """user_id verified under any of these keys within the window, else None."""
    cutoff = now() - timedelta(seconds=PIN_SESSION_SECONDS)
    with _state_lock:
        for k in keys:
            k = str(k or "").strip()
            if not k:
                continue
            v = _pin_sessions.get(k)
            if v and v[1] >= cutoff:
                return v[0]
    return None


@bp.route("/api/v1/voice/pin", methods=["POST"])
@voice_api("write")
def api_pin():
    import secrets
    b = _body()
    phone = str(b.get("phone") or "").strip()
    pin = _digits(b.get("pin"))
    call_id = str(b.get("call_id") or "").strip()
    key = _last10(phone) or "unknown"
    if not phone:
        return _refuse("phone_required", "The attorney line needs the caller id to know whose PIN to check.", 400)
    if _locked(key):
        _touch_call(call_id, kind="memo", from_number=phone[:50], outcome="unverified",
                    summary="PIN locked after repeated failures.")
        db.session.commit()
        return _refuse("locked", "Too many wrong PINs from this number. Try again in an hour.", 423)
    users = _users_by_phone(phone)
    user = next((u for u in users if pin and check_password_hash(u.voice_pin_hash, pin)), None)
    if not user:
        _record_failure(key)
        _touch_call(call_id, kind="memo", from_number=phone[:50], outcome="unverified",
                    summary="Wrong PIN or unknown attorney number.")
        db.session.commit()
        left = max(0, PIN_LOCK_FAILURES - len(_pin_failures.get(key, ())))
        return _refuse("bad_pin", "That PIN did not match." + (f" {left} attempt(s) left." if left else " Locked for an hour."),
                       403, attempts_left=left)
    with _state_lock:
        _pin_failures.pop(key, None)
    session_key = call_id or secrets.token_urlsafe(12)
    _open_session(session_key, user.id)
    _touch_call(call_id, kind="memo", from_number=phone[:50], user_id=user.id, outcome="verified",
                summary=f"PIN verified for {user.name}.")
    audit("voice_pin", "user", user.id, f"call {call_id or session_key}", user.id)
    db.session.commit()
    return jsonify({"ok": True, "user_id": user.id, "name": user.name, "pin_session": session_key,
                    "expires_in_seconds": PIN_SESSION_SECONDS,
                    "matters": [{"id": m.id, "number": m.number, "name": m.name} for m in _attorney_matters(user)]})


def _verified_user(b):
    """-> (user, error). The PIN must have been verified for this call_id / pin_session within 30 minutes and
    the user_id in the body must be the one that verified."""
    uid = _session_user_id(b.get("call_id"), b.get("pin_session"))
    if not uid:
        return None, _refuse("pin_required", "Verify the PIN for this call first (POST /api/v1/voice/pin).", 403)
    try:
        claimed = int(b.get("user_id") or 0)
    except (TypeError, ValueError):
        claimed = 0
    if claimed and claimed != uid:
        return None, _refuse("user_mismatch", "user_id does not match the PIN that was verified on this call.", 403)
    user = db.session.get(User, uid)
    if not user or not user.is_active:
        return None, _refuse("no_user", "That user is no longer active.", 403)
    return user, None


# ---------------------------------------------------------------- POST /api/v1/voice/note
@bp.route("/api/v1/voice/note", methods=["POST"])
@voice_api("write")
def api_note():
    b = _body()
    user, err = _verified_user(b)
    if err:
        return err
    body = " ".join(str(b.get("body") or "").split())
    if not body:
        return _refuse("body_required", "Send the dictated note as body.", 400)
    matter, err = _pick_matter(b, user)
    if err:
        return err
    note = Note(matter_id=matter.id, user_id=user.id, body=f"[by phone] {body}"[:20000])
    db.session.add(note)
    db.session.flush()
    audit("create", "note", note.id, f"voice note on {matter.number}", user.id)
    _touch_call(b.get("call_id"), kind="memo", user_id=user.id, matter_id=matter.id, outcome="note_saved",
                summary=f"Note saved on {matter.label} by {user.name}.")
    db.session.commit()
    return jsonify({"ok": True, "note_id": note.id, "matter": _matter_brief(matter),
                    "read_back": f"Saved a note on {matter.number}, {matter.name}."})


# ---------------------------------------------------------------- POST /api/v1/voice/time
@bp.route("/api/v1/voice/time", methods=["POST"])
@voice_api("write")
def api_time():
    b = _body()
    user, err = _verified_user(b)
    if err:
        return err
    try:
        raw_minutes = float(b.get("minutes") or 0)
    except (TypeError, ValueError):
        raw_minutes = 0
    if raw_minutes <= 0 or raw_minutes > 24 * 60:
        return _refuse("minutes_required", "Send minutes as a positive number.", 400)
    description = " ".join(str(b.get("description") or "").split())
    if not description:
        return _refuse("description_required", "Send a short description of the work.", 400)
    matter, err = _pick_matter(b, user)
    if err:
        return err
    minutes = round_up_minutes(int(round(raw_minutes * 60)))
    entry = TimeEntry(matter_id=matter.id, user_id=user.id, date=date.today(), minutes=minutes,
                      description=description[:2000], rate_cents=matter.effective_rate_cents(user), billable=True)
    db.session.add(entry)
    db.session.flush()
    audit("create", "time_entry", entry.id, f"voice: {raw_minutes:g}m -> {minutes}m on {matter.number}", user.id)
    _touch_call(b.get("call_id"), kind="memo", user_id=user.id, matter_id=matter.id, outcome="time_saved",
                summary=f"{minutes} minutes logged on {matter.label} by {user.name}.")
    db.session.commit()
    hours = minutes / 60
    return jsonify({"ok": True, "time_entry_id": entry.id, "minutes": minutes, "hours": round(hours, 2),
                    "rate_cents": entry.rate_cents, "amount_cents": entry.amount_cents, "matter": _matter_brief(matter),
                    "read_back": f"Logged {hours:.1f} hours on {matter.number}, {matter.name}: {description}."})


# ---------------------------------------------------------------- POST /api/v1/voice/calls
def _flatten_transcript(t):
    if isinstance(t, list):
        lines = []
        for turn in t:
            if isinstance(turn, dict):
                who = turn.get("role") or turn.get("speaker") or ""
                text = turn.get("text") or turn.get("content") or ""
                lines.append(f"{who}: {text}".strip(": ") if who else str(text))
            else:
                lines.append(str(turn))
        return "\n".join(l for l in lines if l)
    return str(t or "")


@bp.route("/api/v1/voice/calls", methods=["POST"])
@voice_api("write")
def api_calls():
    b = _body()
    call_id = str(b.get("call_id") or "").strip()
    if not call_id:
        return _refuse("call_id_required", "Send the provider call id as call_id.", 400)
    kind = str(b.get("kind") or "").strip().lower()
    outcome = str(b.get("outcome") or "").strip().lower()
    try:
        duration = int(b.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        duration = 0

    def _id(key, model):
        v = b.get(key)
        if v in (None, ""):
            return None
        try:
            return int(v) if db.session.get(model, int(v)) else None
        except (TypeError, ValueError):
            return None

    vc = _touch_call(call_id, kind=kind if kind in CALL_KINDS else "",
                     from_number=str(b.get("from") or b.get("from_number") or "")[:50],
                     to_number=str(b.get("to") or b.get("to_number") or "")[:50],
                     summary=str(b.get("summary") or "")[:20000],
                     transcript=_flatten_transcript(b.get("transcript"))[:200000],
                     outcome=outcome if outcome in OUTCOMES else "",
                     duration_seconds=duration or None,
                     lead_id=_id("lead_id", IntakeLead), contact_id=_id("contact_id", Contact),
                     matter_id=_id("matter_id", Matter))
    if not vc.kind:
        vc.kind = "other"
    if vc.lead_id and not vc.contact_id and vc.lead and vc.lead.contact_id:
        vc.contact_id = vc.lead.contact_id
    db.session.flush()
    audit("voice_call", "voice_call", vc.id, f"{vc.kind} {vc.outcome or 'no outcome'} {duration}s", g.api_user.id)
    db.session.commit()
    return jsonify({"ok": True, "id": vc.id, "call_id": vc.call_id, "kind": vc.kind, "outcome": vc.outcome,
                    "url": f"{current_app.config['BASE_URL']}/voice/{vc.id}"})


# ---------------------------------------------------------------- outbound reminder calls
def _twiml(text):
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="alice">{xml_escape(text)}</Say></Response>'


def place_call(to, text):
    """Twilio REST Calls with inline TwiML (no callback URL). -> (sid, status). Unconfigured -> ('', 'unconfigured')."""
    if not smssvc.configured():
        current_app.logger.info("[VOICE-DEV] would call %s and say: %s", to, text)
        return "", "unconfigured"
    c = current_app.config
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{c['TWILIO_ACCOUNT_SID']}/Calls.json",
        auth=(c["TWILIO_ACCOUNT_SID"], c["TWILIO_AUTH_TOKEN"]),
        data={"To": to, "From": c["TWILIO_FROM_NUMBER"], "Twiml": _twiml(text)}, timeout=20)
    if r.status_code >= 300:
        return "", f"error:{r.status_code}"
    j = r.json()
    return j.get("sid", ""), j.get("status", "queued")


def reminder_text(firm, title, when_text, is_task=False):
    verb = "is due on" if is_task else "is scheduled for"
    return (f"Hello. This is a reminder from {greeting_name(firm)}. {title} {verb} {when_text}. "
            f"Please call the office with any questions. Thank you, goodbye.")


def _already_voice_reminded(entity, entity_id, detail):
    return AuditLog.query.filter_by(action="voice_reminded", entity=entity, entity_id=entity_id,
                                    detail=detail).first() is not None


def _record_reminder(firm, entity, entity_id, detail, matter, client, text, sid, status):
    ok = status not in ("unconfigured",) and not status.startswith("error")
    vc = VoiceCall(direction="out", kind="reminder", call_id=sid or "", from_number=current_app.config.get("TWILIO_FROM_NUMBER", ""),
                   to_number=client.phone, contact_id=client.id, matter_id=matter.id if matter else None,
                   summary=text, outcome="reminded" if ok else "failed")
    db.session.add(vc)
    db.session.add(Message(contact_id=client.id, matter_id=matter.id if matter else None, direction="out", channel="voice",
                           to_addr=client.phone, from_addr=current_app.config.get("TWILIO_FROM_NUMBER", ""),
                           body=text, provider_id=sid or "", status=status))
    audit("voice_reminded", entity, entity_id, detail, None)
    return vc


def upcoming_reminders(firm, today=None):
    """[(entity, entity_id, detail, matter, client, text)] for every event and court date inside the window
    whose matter has a client with a phone. Recurring events are expanded; detail carries the occurrence date."""
    today = today or date.today()
    days = max(0, int(firm.voice_reminder_days or 1))
    tz = firm_tz(firm)
    start_local = datetime.combine(today, datetime.min.time()).replace(tzinfo=tz)
    end_local = datetime.combine(today + timedelta(days=days + 1), datetime.min.time()).replace(tzinfo=tz)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    out = []
    events = CalendarEvent.query.filter(CalendarEvent.matter_id != None).order_by(CalendarEvent.starts_at).all()  # noqa: E711
    for ev in events:
        m = ev.matter
        client = m.client if m else None
        if not m or m.status == "closed" or not client or not _last10(client.phone):
            continue
        for occ in ev.occurrences(start_utc, end_utc):
            detail = f"contact:{client.id} on:{_local(occ, firm):%Y-%m-%d}"
            text = reminder_text(firm, ev.title, _spoken_when(occ, firm, ev.all_day))
            out.append(("calendar_event", ev.id, detail, m, client, text))
    tasks = Task.query.filter(Task.kind == "court_date", Task.done == False, Task.matter_id != None,  # noqa: E711,E712
                              Task.due_on != None, Task.due_on >= today,  # noqa: E711
                              Task.due_on <= today + timedelta(days=days)).order_by(Task.due_on).all()
    for t in tasks:
        m = t.matter
        client = m.client if m else None
        if not m or m.status == "closed" or not client or not _last10(client.phone):
            continue
        detail = f"contact:{client.id} on:{t.due_on:%Y-%m-%d}"
        text = reminder_text(firm, t.title, t.due_on.strftime("%A, %B %-d"), is_task=True)
        out.append(("task", t.id, detail, m, client, text))
    return out


def run_voice_reminders(today=None):
    """-> dict(placed=[VoiceCall], would=[(to, text)], skipped=int, reason=str). Without Twilio it only logs."""
    firm = Firm.get()
    result = {"placed": [], "would": [], "skipped": 0, "failed": 0, "reason": ""}
    if not firm.voice_reminders:
        result["reason"] = "voice reminders are off"
        return result
    configured = smssvc.configured()
    if not configured:
        result["reason"] = "Twilio is not configured; nothing was called or recorded"
    for entity, entity_id, detail, matter, client, text in upcoming_reminders(firm, today):
        if _already_voice_reminded(entity, entity_id, detail):
            result["skipped"] += 1
            continue
        if not configured:
            current_app.logger.info("[VOICE-DEV] would call %s (%s) and say: %s", client.phone, client.display_name, text)
            result["would"].append((client.phone, text))
            continue
        sid, status = place_call(client.phone, text)
        vc = _record_reminder(firm, entity, entity_id, detail, matter, client, text, sid, status)
        db.session.commit()
        result["placed"].append(vc)
        if vc.outcome == "failed":
            result["failed"] += 1
    return result


# ---------------------------------------------------------------- staff pages
@bp.route("/voice")
@login_required
def index():
    kind = (request.args.get("kind") or "").strip()
    matter_id = request.args.get("matter_id", type=int)
    q = VoiceCall.query
    if kind in CALL_KINDS:
        q = q.filter(VoiceCall.kind == kind)
    if matter_id:
        q = q.filter(VoiceCall.matter_id == matter_id)
    rows = q.order_by(VoiceCall.started_at.desc(), VoiceCall.id.desc()).limit(200).all()
    counts = {k: VoiceCall.query.filter_by(kind=k).count() for k in CALL_KINDS}
    matter = db.session.get(Matter, matter_id) if matter_id else None
    f = Firm.get()
    return render_template("voice/index.html", rows=rows, kind=kind, kinds=CALL_KINDS, counts=counts,
                           matter=matter, f=f, twilio=smssvc.configured())


@bp.route("/voice/<int:id>")
@login_required
def detail(id):
    vc = db.session.get(VoiceCall, id) or abort(404)
    return render_template("voice/detail.html", vc=vc)


@bp.route("/voice/reminders/test", methods=["POST"])
@owner_required
def reminders_test():
    f = Firm.get()
    to = (request.form.get("to") or "").strip()
    if len(_digits(to)) < 10:
        flash("Enter a ten digit number to call.", "error")
        return redirect(url_for("voice.settings_page"))
    text = (f"Hello. This is a test call from {greeting_name(f)}. Reminder calls from the office are working. "
            f"Thank you, goodbye.")
    sid, status = place_call(to, text)
    if status == "unconfigured":
        flash(f"Twilio is not configured, so no call was placed. It would have said: {text}", "error")
        return redirect(url_for("voice.settings_page"))
    vc = VoiceCall(direction="out", kind="reminder", call_id=sid or "", to_number=to[:50],
                   from_number=current_app.config.get("TWILIO_FROM_NUMBER", ""), summary=f"TEST: {text}",
                   outcome="reminded" if sid else "failed", user_id=current_user().id)
    db.session.add(vc)
    audit("voice_test_call", "voice_call", None, f"to {to}: {status}", current_user().id)
    db.session.commit()
    flash(f"Test call placed to {to} ({status}).", "ok" if sid else "error")
    return redirect(url_for("voice.settings_page"))


# ---------------------------------------------------------------- settings
def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@bp.route("/settings/voice", methods=["GET", "POST"])
@owner_required
def settings_page():
    f = Firm.get()
    if request.method == "POST":
        form = request.form
        f.voice_enabled = form.get("voice_enabled") == "1"
        f.voice_greeting_name = (form.get("voice_greeting_name") or "").strip()[:120]
        areas = [a for a in form.getlist("practice_areas") if a in PRACTICE_AREA_KEYS]
        f.voice_practice_areas = ",".join(areas)
        lines = {}
        for key, _label, _help in APPROVED_LINE_KEYS:
            v = " ".join((form.get(f"line_{key}") or "").split())
            lines[key] = v or DEFAULT_APPROVED_LINES[key]
        f.voice_approved_lines_json = json.dumps(lines)
        tiers = {"urgent_minutes": max(1, _int(form.get("urgent_minutes"), DEFAULT_CALLBACK["urgent_minutes"])),
                 "high_minutes": max(1, _int(form.get("high_minutes"), DEFAULT_CALLBACK["high_minutes"])),
                 "standard": (form.get("standard") or "").strip()[:200] or DEFAULT_CALLBACK["standard"]}
        f.voice_callback_json = json.dumps(tiers)
        f.voice_client_status = form.get("voice_client_status") == "1"
        f.voice_reminders = form.get("voice_reminders") == "1"
        f.voice_reminder_days = min(30, max(0, _int(form.get("voice_reminder_days"), 1)))
        audit("update", "firm", f.id, "voice line settings", current_user().id)
        db.session.commit()
        flash("Voice line settings saved.", "ok")
        return redirect(url_for("voice.settings_page"))
    base = current_app.config["BASE_URL"]
    pin_users = User.query.filter(User.is_active == True).order_by(User.name).all()  # noqa: E712
    return render_template("settings/voice.html", f=f, areas=PRACTICE_AREAS, chosen=set(practice_areas(f)),
                           line_keys=APPROVED_LINE_KEYS, lines=approved_lines(f), tiers=callback_tiers(f),
                           base=base, endpoints=voice_endpoints(base), twilio=smssvc.configured(),
                           pin_users=pin_users)


# ---------------------------------------------------------------- user form helpers (called from settings.py)
PIN_RE = re.compile(r"^\d{4,6}$")


def apply_user_voice_fields(u, form):
    """Voice phone and PIN from the user form. Returns an error string or None. Blank PIN keeps the current one."""
    if "voice_phone" in form:
        u.voice_phone = (form.get("voice_phone") or "").strip()[:50]
    pin = (form.get("voice_pin") or "").strip()
    if form.get("voice_pin_clear") == "1":
        u.voice_pin_hash = ""
    elif pin:
        if not PIN_RE.match(pin):
            return "The voice PIN must be 4 to 6 digits."
        u.voice_pin_hash = generate_password_hash(pin)
    return None
