"""Time capture suggestions (Smokeball lane, Agent R).

The Chrome extension's background worker watches the active tab and posts segments ({started_at, minutes, title,
url, source}) to POST /api/v1/capture (the endpoint lives in api.py and calls ingest_segments here). Each
segment becomes a TimeSuggestion for the token's user with a guessed matter. Staff review them at
/time/suggestions and turn them into time entries (rounded up to six minutes, same as the timer) or dismiss them.

Matter guess order: a matter number in the title or url (M-1002), then a client display name or matter name
substring, case-insensitive, else none. Segments under two minutes are ignored. Segments with the same title
within 30 minutes of a pending suggestion are merged into it by adding the minutes.
"""
import re
from collections import OrderedDict
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app

from ..extensions import db
from ..models import Matter, TimeEntry, TimeSuggestion, audit, now
from ..helpers import login_required, current_user
from .time import round_up_minutes

bp = Blueprint("capture", __name__)

MIN_MINUTES = 2
MERGE_WINDOW = timedelta(minutes=30)
_NUMBER_RE = re.compile(r"\b([A-Za-z]{1,5}-\d{2,})\b")


# ---------------------------------------------------------------- guessing and ingest
def _parse_started(v):
    """ISO 8601 (with or without a trailing Z or offset) -> naive UTC datetime. None when unreadable."""
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v or "").strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def guess_matter(title, url="", matters=None):
    """Best matter for a tab title or url, or None. `matters` lets callers pass one loaded list for a batch."""
    text = f"{title or ''} {url or ''}"
    if not text.strip():
        return None
    if matters is None:
        matters = Matter.query.filter(Matter.status != "closed").all()
    by_number = {(m.number or "").lower(): m for m in matters if m.number}
    for hit in _NUMBER_RE.findall(text):
        m = by_number.get(hit.lower())
        if m:
            return m
    low = text.lower()
    best, best_len = None, 0
    for m in matters:
        names = []
        if m.client:
            names.append(m.client.display_name)
            names.append(_bare_company(m.client.display_name))
        names.append(m.name)
        for n in names:
            n = (n or "").strip().lower()
            if len(n) >= 4 and n in low and len(n) > best_len:
                best, best_len = m, len(n)
    return best


_SUFFIX_RE = re.compile(r"[\s,]+(llc|l\.l\.c\.|inc\.?|incorporated|corp\.?|corporation|co\.?|ltd\.?|"
                        r"llp|l\.l\.p\.|pllc|p\.c\.|pc|plc|lp)\s*$", re.I)


def _bare_company(name):
    """'Bluebonnet Logistics LLC' -> 'Bluebonnet Logistics' so an email subject without the suffix still matches."""
    return _SUFFIX_RE.sub("", name or "").strip()


def ingest_segments(user, segments):
    """Create or merge TimeSuggestion rows for `user`. Returns {"created", "merged", "ignored"}. Caller commits."""
    created = merged = ignored = 0
    matters = Matter.query.filter(Matter.status != "closed").all()
    for seg in segments or []:
        if not isinstance(seg, dict):
            ignored += 1
            continue
        try:
            minutes = int(round(float(seg.get("minutes") or 0)))
        except (TypeError, ValueError):
            minutes = 0
        started = _parse_started(seg.get("started_at"))
        title = (seg.get("title") or "").strip()[:300]
        if minutes < MIN_MINUTES or not started or not title:
            ignored += 1
            continue
        url = (seg.get("url") or "").strip()[:500]
        source = (seg.get("source") or "extension").strip()[:20] or "extension"
        lo, hi = started - MERGE_WINDOW, started + MERGE_WINDOW
        existing = (TimeSuggestion.query.filter_by(user_id=user.id, status="pending", title=title)
                    .filter(TimeSuggestion.started_at >= lo, TimeSuggestion.started_at <= hi)
                    .order_by(TimeSuggestion.started_at).first())
        if existing:
            existing.minutes = int(existing.minutes or 0) + minutes
            if started < existing.started_at:
                existing.started_at = started
            if not existing.url and url:
                existing.url = url
            merged += 1
            db.session.flush()
            continue
        m = guess_matter(title, url, matters)
        s = TimeSuggestion(user_id=user.id, source=source, started_at=started, minutes=minutes, title=title, url=url,
                           matter_id=m.id if m else None, status="pending")
        db.session.add(s)
        db.session.flush()
        created += 1
    return {"created": created, "merged": merged, "ignored": ignored}


def pending_query(user):
    return TimeSuggestion.query.filter_by(user_id=user.id, status="pending")


def pending_count(user):
    return pending_query(user).count()


def accept_suggestion(s, user, matter, description=None, minutes=None):
    """Turn a pending suggestion into a TimeEntry rounded up to six minutes. Caller commits. Returns the entry."""
    mins = int(minutes) if minutes else int(s.minutes or 0)
    rounded = round_up_minutes(mins * 60)
    entry = TimeEntry(matter_id=matter.id, user_id=user.id, date=(s.started_at or now()).date(), minutes=rounded,
                      description=(description if description is not None else s.title or "").strip(),
                      rate_cents=matter.effective_rate_cents(user), billable=True)
    db.session.add(entry)
    db.session.flush()
    s.matter_id = matter.id
    s.status = "accepted"
    s.time_entry_id = entry.id
    audit("create", "time_entry", entry.id, f"capture: {mins}m -> {rounded}m on {matter.number} ({s.source})", user.id)
    return entry


# ---------------------------------------------------------------- staff pages
def _grouped(rows):
    groups = OrderedDict()
    for s in rows:
        groups.setdefault(s.started_at.date(), []).append(s)
    return groups


@bp.route("/time/suggestions")
@login_required
def suggestions():
    u = current_user()
    rows = pending_query(u).order_by(TimeSuggestion.started_at.desc()).all()
    matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    recent = (TimeSuggestion.query.filter(TimeSuggestion.user_id == u.id, TimeSuggestion.status != "pending")
              .order_by(TimeSuggestion.created_at.desc()).limit(20).all())
    with_matter = sum(1 for s in rows if s.matter_id)
    total_minutes = sum(int(s.minutes or 0) for s in rows)
    return render_template("capture/suggestions.html", groups=_grouped(rows), matters=matters, recent=recent,
                           count=len(rows), with_matter=with_matter, total_minutes=total_minutes)


def _own(id):
    s = db.session.get(TimeSuggestion, id) or abort(404)
    if s.user_id != current_user().id and current_user().role != "owner":
        abort(403)
    return s


@bp.route("/time/suggestions/<int:id>/accept", methods=["POST"])
@login_required
def accept(id):
    s = _own(id)
    u = current_user()
    if s.status != "pending":
        flash("That suggestion was already handled.", "error")
        return redirect(url_for("capture.suggestions"))
    mid = request.form.get("matter_id", type=int)
    m = db.session.get(Matter, mid) if mid else None
    if request.form.get("action") == "save":
        s.matter_id = m.id if m else None
        db.session.commit()
        flash("Matter saved.", "ok")
        return redirect(url_for("capture.suggestions"))
    if not m:
        flash("Pick a matter before accepting so the time has somewhere to go.", "error")
        return redirect(url_for("capture.suggestions"))
    minutes = request.form.get("minutes", type=int)
    entry = accept_suggestion(s, s.user or u, m, request.form.get("description"), minutes)
    db.session.commit()
    flash(f"Logged {entry.minutes / 60:.2f} hours ({entry.minutes} minutes, rounded up to the next 6) on {m.number}.",
          "ok")
    return redirect(url_for("capture.suggestions"))


@bp.route("/time/suggestions/<int:id>/dismiss", methods=["POST"])
@login_required
def dismiss(id):
    s = _own(id)
    if s.status == "pending":
        s.status = "dismissed"
        audit("dismiss", "time_suggestion", s.id, s.title[:200], current_user().id)
        db.session.commit()
    flash("Dismissed.", "ok")
    return redirect(url_for("capture.suggestions"))


@bp.route("/time/suggestions/accept-all", methods=["POST"])
@login_required
def accept_all():
    u = current_user()
    rows = pending_query(u).filter(TimeSuggestion.matter_id != None).all()  # noqa: E711
    n = 0
    for s in rows:
        m = s.matter
        if not m or m.status == "closed":
            continue
        accept_suggestion(s, u, m)
        n += 1
    db.session.commit()
    flash(f"Accepted {n} suggestion{'' if n == 1 else 's'} with a matter." if n else
          "Nothing to accept: no pending suggestion has a matter yet.", "ok" if n else "error")
    return redirect(url_for("capture.suggestions"))


@bp.route("/time/suggestions/dismiss-all", methods=["POST"])
@login_required
def dismiss_all():
    u = current_user()
    n = 0
    for s in pending_query(u).all():
        s.status = "dismissed"
        n += 1
    db.session.commit()
    flash(f"Dismissed {n}.", "ok")
    return redirect(url_for("capture.suggestions"))
