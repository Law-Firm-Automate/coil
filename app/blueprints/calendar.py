"""Calendar: month grid of events, task due dates and limitations dates, plus an ICS subscription feed."""
import calendar as stdcal
import hashlib
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, Response
from sqlalchemy import or_, and_
from ..extensions import db
from ..models import CalendarEvent, Task, Matter, User, audit
from ..helpers import login_required, current_user, parse_date

bp = Blueprint("calendar", __name__, url_prefix="/calendar")


RECURRENCES = [("none", "Does not repeat"), ("daily", "Every day"), ("weekly", "Every week"), ("biweekly", "Every two weeks"),
               ("monthly", "Every month"), ("yearly", "Every year")]
RRULE_FREQ = {"daily": "FREQ=DAILY", "weekly": "FREQ=WEEKLY", "biweekly": "FREQ=WEEKLY;INTERVAL=2",
              "monthly": "FREQ=MONTHLY", "yearly": "FREQ=YEARLY"}


def feed_secret(user_id=None):
    """Firm-wide feed secret, or a per-user one when user_id is given. Both derive from SECRET_KEY."""
    salt = "ics" if user_id is None else f"ics-user-{user_id}"
    return hashlib.sha256((current_app.config["SECRET_KEY"] + salt).encode()).hexdigest()[:24]


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_dt(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _fill(e, form):
    e.title = form.get("title", "").strip()
    e.all_day = bool(form.get("all_day"))
    e.matter_id = _int(form.get("matter_id"))
    e.location = form.get("location", "").strip()
    e.notes = form.get("notes", "").strip()
    e.user_id = _int(form.get("user_id"))
    e.recurrence = form.get("recurrence") if form.get("recurrence") in dict(RECURRENCES) else "none"
    e.recurrence_until = parse_date(form.get("recurrence_until")) if e.recurrence != "none" else None
    if e.all_day:
        d = parse_date(form.get("date")) or (parse_date(form.get("starts_at")) if form.get("starts_at") else None)
        e.starts_at = datetime.combine(d, datetime.min.time()) if d else None
        e.ends_at = None
    else:
        e.starts_at = _parse_dt(form.get("starts_at"))
        e.ends_at = _parse_dt(form.get("ends_at"))
        if e.starts_at and e.ends_at and e.ends_at < e.starts_at:
            e.ends_at = e.starts_at + timedelta(hours=1)
        if e.starts_at and not e.ends_at:
            e.ends_at = e.starts_at + timedelta(hours=1)


def _form_context(e):
    return dict(e=e, matters=Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all(),
                users=User.query.filter_by(is_active=True).order_by(User.name).all(), recurrences=RECURRENCES)


def _month_arg():
    s = request.args.get("month", "")
    try:
        y, m = s.split("-")
        return date(int(y), int(m), 1)
    except (ValueError, AttributeError):
        t = date.today()
        return date(t.year, t.month, 1)


@bp.route("")
@login_required
def index():
    first = _month_arg()
    weeks = stdcal.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)
    grid_start, grid_end = weeks[0][0], weeks[-1][-1]
    items = {}

    def add(d, item):
        items.setdefault(d, []).append(item)

    win_start = datetime.combine(grid_start, datetime.min.time())
    win_end = datetime.combine(grid_end + timedelta(days=1), datetime.min.time())
    # "mine" = my events plus firm-wide; a user id = that person's plus firm-wide; "all" = everything.
    view = request.args.get("user", "all")
    q = CalendarEvent.query
    if view == "mine":
        q = q.filter(or_(CalendarEvent.user_id == current_user().id, CalendarEvent.user_id == None))
    elif _int(view):
        q = q.filter(or_(CalendarEvent.user_id == _int(view), CalendarEvent.user_id == None))
    # Recurring events may have started long before this month, so fetch them regardless of start date.
    evs = q.filter(or_(CalendarEvent.recurrence != "none",
                       and_(CalendarEvent.starts_at >= win_start, CalendarEvent.starts_at < win_end))).order_by(
        CalendarEvent.starts_at).all()
    for e in evs:
        for occ in e.occurrences(win_start, win_end):
            add(occ.date(), {"kind": "event", "title": e.title + (" \u21bb" if e.recurrence != "none" else ""),
                             "url": f"/calendar/{e.id}", "time": "" if e.all_day else occ.strftime("%-I:%M %p"),
                             "sort": 0 if e.all_day else 1, "at": occ,
                             "who": e.user.initials if e.user else ""})
    for t in Task.query.filter(Task.done == False, Task.due_on >= grid_start, Task.due_on <= grid_end).all():
        add(t.due_on, {"kind": t.kind, "title": t.title, "url": f"/tasks/{t.id}", "time": "", "sort": 2,
                       "at": datetime.combine(t.due_on, datetime.min.time())})
    for m in Matter.query.filter(Matter.status != "closed", Matter.sol_date >= grid_start,
                                 Matter.sol_date <= grid_end).all():
        add(m.sol_date, {"kind": "sol", "title": f"SOL: {m.label}", "url": f"/matters/{m.id}", "time": "", "sort": 0,
                         "at": datetime.combine(m.sol_date, datetime.min.time())})
    for d in items:
        items[d].sort(key=lambda i: (i["sort"], i["at"]))
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    upcoming = q.filter(CalendarEvent.starts_at >= datetime.utcnow() - timedelta(hours=1)).order_by(
        CalendarEvent.starts_at).limit(10).all()
    feed_url = f"{current_app.config['BASE_URL']}/calendar/feed/{feed_secret()}.ics"
    u = current_user()
    my_feed_url = f"{current_app.config['BASE_URL']}/calendar/feed/u/{u.id}/{feed_secret(u.id)}.ics"
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    return render_template("calendar/index.html", weeks=weeks, items=items, first=first, prev_month=prev_month,
                           next_month=next_month, today=date.today(), feed_url=feed_url, my_feed_url=my_feed_url,
                           upcoming=upcoming, month_label=first.strftime("%B %Y"), users=users, view=view)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    e = CalendarEvent()
    if request.method == "POST":
        _fill(e, request.form)
        if not e.title or not e.starts_at:
            flash("A title and a start date are required.", "error")
            return render_template("calendar/form.html", is_new=True, **_form_context(e))
        db.session.add(e)
        db.session.flush()
        audit("create", "calendar_event", e.id, e.title, current_user().id)
        if e.matter_id:
            audit("add_event", "matter", e.matter_id, f"{e.title} {e.starts_at:%Y-%m-%d}", current_user().id)
        db.session.commit()
        flash("Event added.", "ok")
        return redirect(url_for("calendar.detail", id=e.id))
    e.matter_id = _int(request.args.get("matter_id"))
    d = parse_date(request.args.get("date")) or date.today()
    e.starts_at = datetime.combine(d, datetime.min.time()).replace(hour=9)
    e.ends_at = e.starts_at + timedelta(hours=1)
    return render_template("calendar/form.html", is_new=True, **_form_context(e))


@bp.route("/<int:id>")
@login_required
def detail(id):
    e = db.session.get(CalendarEvent, id) or abort(404)
    return render_template("calendar/detail.html", e=e)


@bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit(id):
    e = db.session.get(CalendarEvent, id) or abort(404)
    if request.method == "POST":
        _fill(e, request.form)
        if not e.title or not e.starts_at:
            flash("A title and a start date are required.", "error")
            return render_template("calendar/form.html", is_new=False, **_form_context(e))
        db.session.commit()
        flash("Event saved.", "ok")
        return redirect(url_for("calendar.detail", id=e.id))
    return render_template("calendar/form.html", is_new=False, **_form_context(e))


@bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def delete(id):
    e = db.session.get(CalendarEvent, id) or abort(404)
    month = e.starts_at.strftime("%Y-%m") if e.starts_at else ""
    audit("delete", "calendar_event", e.id, e.title, current_user().id)
    db.session.delete(e)
    db.session.commit()
    flash("Event deleted.", "ok")
    return redirect(url_for("calendar.index", month=month) if month else url_for("calendar.index"))


# ICS feed
def _ics_escape(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace(
        "\n", "\\n")


def _fold(line):
    """RFC 5545 folding: lines longer than 75 octets continue on the next line after a single space."""
    b = line.encode("utf-8")
    if len(b) <= 75:
        return [line]
    out, cur = [], b""
    for ch in line:
        cb = ch.encode("utf-8")
        if len(cur) + len(cb) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += cb
    if cur:
        out.append(cur.decode("utf-8"))
    return [out[0]] + [" " + l for l in out[1:]]


def build_ics(events, name="Calendar"):
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Solo Practice//Calendar//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_escape(name)}"]
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e.uid or e.id}@solo-practice", f"DTSTAMP:{stamp}"]
        if e.all_day:
            start = e.starts_at.date()
            end = (e.ends_at.date() + timedelta(days=1)) if e.ends_at and e.ends_at.date() >= start else start + timedelta(days=1)
            lines += [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}"]
        else:
            end = e.ends_at or (e.starts_at + timedelta(hours=1))
            lines += [f"DTSTART:{e.starts_at:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}"]
        if e.recurrence in RRULE_FREQ:
            rule = RRULE_FREQ[e.recurrence]
            if e.recurrence_until:
                rule += f";UNTIL={e.recurrence_until:%Y%m%d}T235959Z"
            lines.append(f"RRULE:{rule}")
        lines.append(f"SUMMARY:{_ics_escape(e.title)}")
        if e.location:
            lines.append(f"LOCATION:{_ics_escape(e.location)}")
        desc = e.notes or ""
        if e.matter:
            desc = f"Matter: {e.matter.label}" + (f"\n{desc}" if desc else "")
        if desc:
            lines.append(f"DESCRIPTION:{_ics_escape(desc)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    folded = []
    for l in lines:
        folded += _fold(l)
    return "\r\n".join(folded) + "\r\n"


def _ics_response(events, name):
    body = build_ics(events, name=name)
    return Response(body, mimetype="text/calendar",
                    headers={"Content-Disposition": "inline; filename=calendar.ics", "Cache-Control": "no-cache"})


@bp.route("/feed/<secret>.ics")
def feed(secret):
    """Firm-wide feed: every event on every calendar."""
    if secret != feed_secret():
        abort(404)
    from ..models import Firm
    return _ics_response(CalendarEvent.query.order_by(CalendarEvent.starts_at).all(), Firm.get().name)


@bp.route("/feed/u/<int:user_id>/<secret>.ics")
def user_feed(user_id, secret):
    """One person's feed: their events plus firm-wide ones."""
    if secret != feed_secret(user_id):
        abort(404)
    u = db.session.get(User, user_id) or abort(404)
    events = CalendarEvent.query.filter(or_(CalendarEvent.user_id == user_id, CalendarEvent.user_id == None)).order_by(
        CalendarEvent.starts_at).all()
    from ..models import Firm
    return _ics_response(events, f"{Firm.get().name}: {u.name}")
