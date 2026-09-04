"""Tasks, deadlines and court dates."""
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from ..extensions import db
from ..models import Task, Matter, User, now, audit
from ..helpers import login_required, current_user, parse_date

bp = Blueprint("tasks", __name__, url_prefix="/tasks")

KINDS = ["task", "deadline", "court_date"]
PRIORITIES = ["low", "normal", "high"]
SOL_WINDOW_DAYS = 120


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fill(t, form):
    t.title = form.get("title", "").strip()
    k = form.get("kind", "task")
    t.kind = k if k in KINDS else "task"
    p = form.get("priority", "normal")
    t.priority = p if p in PRIORITIES else "normal"
    t.due_on = parse_date(form.get("due_on"))
    t.matter_id = _int(form.get("matter_id"))
    t.assignee_id = _int(form.get("assignee_id"))
    t.notes = form.get("notes", "").strip()


def _form_context(t):
    return dict(t=t, kinds=KINDS, priorities=PRIORITIES,
                matters=Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all(),
                users=User.query.filter_by(is_active=True).order_by(User.name).all())


def _safe_next(default):
    n = request.form.get("next") or request.args.get("next") or ""
    return n if n.startswith("/") and not n.startswith("//") else default


@bp.route("")
@login_required
def index():
    assignee_id = _int(request.args.get("assignee_id"))
    matter_id = _int(request.args.get("matter_id"))
    kind = request.args.get("kind", "")
    priority = request.args.get("priority", "")
    q = Task.query
    if assignee_id:
        q = q.filter_by(assignee_id=assignee_id)
    if matter_id:
        q = q.filter_by(matter_id=matter_id)
    if kind in KINDS:
        q = q.filter_by(kind=kind)
    if priority in PRIORITIES:
        q = q.filter_by(priority=priority)
    open_tasks = q.filter(Task.done == False).order_by(Task.due_on.asc().nulls_last(), Task.priority.desc()).all()
    done_tasks = q.filter(Task.done == True).order_by(Task.done_at.desc()).limit(20).all()
    today = date.today()
    week_end = today + timedelta(days=6 - today.weekday())
    groups = {"Overdue": [], "Today": [], "This week": [], "Later": []}
    for t in open_tasks:
        if t.due_on and t.due_on < today:
            groups["Overdue"].append(t)
        elif t.due_on == today:
            groups["Today"].append(t)
        elif t.due_on and t.due_on <= week_end:
            groups["This week"].append(t)
        else:
            groups["Later"].append(t)
    sol_matters = Matter.query.filter(Matter.status != "closed", Matter.sol_date != None,
                                      Matter.sol_date <= today + timedelta(days=SOL_WINDOW_DAYS)).order_by(
        Matter.sol_date).all()
    # Agent E: open tasks that came from a court rule set (see app/blueprints/rules.py)
    rule_tasks = q.filter(Task.done == False, Task.rule_id != None).order_by(  # noqa: E711,E712
        Task.due_on.asc().nulls_last()).limit(30).all()
    open_matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    return render_template("tasks/index.html", groups=groups, done_tasks=done_tasks, sol_matters=sol_matters,
                           rule_tasks=rule_tasks, open_matters=open_matters,
                           today=today, kinds=KINDS, priorities=PRIORITIES,
                           users=User.query.filter_by(is_active=True).order_by(User.name).all(),
                           matters=Matter.query.order_by(Matter.number).all(),
                           assignee_id=assignee_id, matter_id=matter_id, kind=kind, priority=priority,
                           sol_window=SOL_WINDOW_DAYS)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    t = Task(kind="task", priority="normal")
    if request.method == "POST":
        _fill(t, request.form)
        if not t.title:
            flash("A title is required.", "error")
            return render_template("tasks/form.html", is_new=True, **_form_context(t))
        db.session.add(t)
        db.session.flush()
        audit("create", "task", t.id, t.title, current_user().id)
        if t.matter_id:
            audit("add_task", "matter", t.matter_id, t.title, current_user().id)
        db.session.commit()
        flash("Task added.", "ok")
        return redirect(_safe_next(url_for("tasks.detail", id=t.id)))
    t.matter_id = _int(request.args.get("matter_id"))
    t.assignee_id = current_user().id
    k = request.args.get("kind", "task")
    t.kind = k if k in KINDS else "task"
    t.due_on = parse_date(request.args.get("due_on"))
    return render_template("tasks/form.html", is_new=True, **_form_context(t))


@bp.route("/<int:id>")
@login_required
def detail(id):
    t = db.session.get(Task, id) or abort(404)
    return render_template("tasks/detail.html", t=t)


@bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit(id):
    t = db.session.get(Task, id) or abort(404)
    if request.method == "POST":
        _fill(t, request.form)
        if not t.title:
            flash("A title is required.", "error")
            return render_template("tasks/form.html", is_new=False, **_form_context(t))
        db.session.commit()
        flash("Task saved.", "ok")
        return redirect(url_for("tasks.detail", id=t.id))
    return render_template("tasks/form.html", is_new=False, **_form_context(t))


@bp.route("/<int:id>/done", methods=["POST"])
@login_required
def toggle_done(id):
    t = db.session.get(Task, id) or abort(404)
    t.done = not t.done
    t.done_at = now() if t.done else None
    audit("done" if t.done else "reopen", "task", t.id, t.title, current_user().id)
    db.session.commit()
    return redirect(_safe_next(url_for("tasks.index")))


@bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def delete(id):
    t = db.session.get(Task, id) or abort(404)
    audit("delete", "task", t.id, t.title, current_user().id)
    db.session.delete(t)
    db.session.commit()
    flash("Task deleted.", "ok")
    return redirect(_safe_next(url_for("tasks.index")))
