"""Time entries, the running timer, and expenses."""
import os
import math
import secrets
from datetime import date
from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort,
                   current_app, send_file)
from werkzeug.utils import secure_filename
from ..extensions import db
from ..models import Matter, TimeEntry, Timer, Expense, User, audit, now
from ..helpers import login_required, current_user, parse_money, parse_date, parse_minutes
from .ledes import choices as utbms_choices, valid_code

bp = Blueprint("time", __name__, url_prefix="/time")

EXPENSE_CATEGORIES = ["Filing fee", "Postage", "Copies", "Travel", "Expert", "Other"]
# UTBMS selects: [(code, label)] built from app/blueprints/ledes.py. Kept as module names so templates and
# tests can reach them the same way they always have.
ACTIVITY_CODES = utbms_choices("activity")
TASK_CODES = utbms_choices("task")
EXPENSE_CODES = utbms_choices("expense")


def _code(form, field, kind):
    """Accept 'A103' or the older 'A103 Draft/revise' style and keep only a valid UTBMS code."""
    raw = (form.get(field) or "").strip().split(" ")[0]
    return valid_code(kind, raw)


def _dollars(cents):
    """Cents -> '350.00' for prefilling a text input (no symbol, no commas)."""
    return f"{int(cents or 0) / 100:.2f}"


def _open_matters(include_id=None):
    q = Matter.query.filter(Matter.status != "closed")
    matters = q.order_by(Matter.number).all()
    if include_id and not any(m.id == include_id for m in matters):
        m = db.session.get(Matter, include_id)
        if m:
            matters.append(m)
    return matters


def _matter_rates(matters, user):
    return {m.id: m.effective_rate_cents(user) for m in matters}


def round_up_minutes(seconds, increment=6):
    """Round elapsed seconds UP to the next `increment` minutes, minimum one increment."""
    minutes = math.ceil(max(0, int(seconds)) / 60.0)
    return max(increment, int(math.ceil(minutes / increment) * increment))


# ---------------------------------------------------------------- time entries
@bp.route("")
@login_required
def index():
    q = TimeEntry.query
    matter_id = request.args.get("matter_id", type=int)
    user_id = request.args.get("user_id", type=int)
    d_from = parse_date(request.args.get("from"))
    d_to = parse_date(request.args.get("to"))
    if matter_id:
        q = q.filter(TimeEntry.matter_id == matter_id)
    if user_id:
        q = q.filter(TimeEntry.user_id == user_id)
    if d_from:
        q = q.filter(TimeEntry.date >= d_from)
    if d_to:
        q = q.filter(TimeEntry.date <= d_to)
    entries = q.order_by(TimeEntry.date.desc(), TimeEntry.id.desc()).all()
    total_minutes = sum(e.minutes for e in entries)
    total_amount = sum(e.amount_cents for e in entries if e.billable)
    unbilled_amount = sum(e.amount_cents for e in entries if e.billable and e.invoice_id is None)
    timer = Timer.query.filter_by(user_id=current_user().id).first()
    return render_template("time/index.html", entries=entries, matters=Matter.query.order_by(Matter.number).all(),
                           users=User.query.order_by(User.name).all(), total_minutes=total_minutes,
                           total_amount=total_amount, unbilled_amount=unbilled_amount, timer=timer,
                           f={"matter_id": matter_id, "user_id": user_id,
                              "from": request.args.get("from", ""), "to": request.args.get("to", "")})


def _entry_from_form(entry, form):
    """Apply form fields to a TimeEntry. Returns an error string or None."""
    matter = db.session.get(Matter, form.get("matter_id", type=int))
    if not matter:
        return "Pick a matter."
    try:
        minutes = parse_minutes(form.get("duration"))
    except (ValueError, TypeError):
        return "Duration should look like 1.5, 1:30, or 90m."
    if minutes <= 0:
        return "Duration must be greater than zero."
    entry.matter_id = matter.id
    entry.date = parse_date(form.get("date"), date.today())
    entry.minutes = minutes
    entry.description = (form.get("description") or "").strip()
    entry.rate_cents = parse_money(form.get("rate"))
    entry.billable = bool(form.get("billable"))
    entry.activity_code = _code(form, "activity_code", "activity")
    entry.task_code = _code(form, "task_code", "task")
    return None


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    u = current_user()
    matter_id = request.args.get("matter_id", type=int) or request.form.get("matter_id", type=int)
    matters = _open_matters(matter_id)
    rates = _matter_rates(matters, u)
    if request.method == "POST":
        entry = TimeEntry(user_id=u.id)
        err = _entry_from_form(entry, request.form)
        if err:
            flash(err, "error")
            return render_template("time/form.html", entry=entry, matters=matters, rates=rates,
                                   activity_codes=ACTIVITY_CODES, task_codes=TASK_CODES, form=request.form), 400
        db.session.add(entry)
        db.session.flush()
        audit("create", "time_entry", entry.id, f"{entry.minutes}m on {entry.matter.number}", u.id)
        db.session.commit()
        flash(f"Logged {entry.minutes / 60:.2f} hours on {entry.matter.number}.", "ok")
        nxt = request.form.get("next") or ""
        if nxt.startswith("/"):
            return redirect(nxt)
        return redirect(url_for("time.index", matter_id=entry.matter_id))
    entry = TimeEntry(date=date.today(), billable=True, user_id=u.id)
    if matter_id:
        entry.matter_id = matter_id
        entry.rate_cents = rates.get(matter_id, 0)
    elif matters:
        entry.rate_cents = rates.get(matters[0].id, 0)
    return render_template("time/form.html", entry=entry, matters=matters, rates=rates,
                           activity_codes=ACTIVITY_CODES, task_codes=TASK_CODES, form=None, next=request.args.get("next", ""))


@bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit(id):
    u = current_user()
    entry = db.session.get(TimeEntry, id) or abort(404)
    matters = _open_matters(entry.matter_id)
    rates = _matter_rates(matters, u)
    if entry.invoice_id:
        if request.method == "POST":
            flash("This entry is on an invoice and cannot be changed.", "error")
            return redirect(url_for("time.edit", id=id))
        return render_template("time/form.html", entry=entry, matters=matters, rates=rates,
                               activity_codes=ACTIVITY_CODES, task_codes=TASK_CODES, form=None, locked=True)
    if request.method == "POST":
        err = _entry_from_form(entry, request.form)
        if err:
            flash(err, "error")
            return render_template("time/form.html", entry=entry, matters=matters, rates=rates,
                                   activity_codes=ACTIVITY_CODES, task_codes=TASK_CODES, form=request.form), 400
        audit("update", "time_entry", entry.id, f"{entry.minutes}m", u.id)
        db.session.commit()
        flash("Time entry saved.", "ok")
        return redirect(url_for("time.index", matter_id=entry.matter_id))
    return render_template("time/form.html", entry=entry, matters=matters, rates=rates,
                           activity_codes=ACTIVITY_CODES, task_codes=TASK_CODES, form=None)


@bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def delete(id):
    entry = db.session.get(TimeEntry, id) or abort(404)
    if entry.invoice_id:
        flash("This entry is on an invoice and cannot be deleted. Void the invoice first.", "error")
        return redirect(url_for("time.edit", id=id))
    matter_id = entry.matter_id
    audit("delete", "time_entry", entry.id, f"{entry.minutes}m {entry.description[:80]}", current_user().id)
    db.session.delete(entry)
    db.session.commit()
    flash("Time entry deleted.", "ok")
    return redirect(url_for("time.index", matter_id=matter_id))


# ---------------------------------------------------------------- timer
@bp.route("/timer")
@login_required
def timer():
    u = current_user()
    t = Timer.query.filter_by(user_id=u.id).first()
    matters = _open_matters(t.matter_id if t else None)
    return render_template("time/timer.html", timer=t, matters=matters,
                           matter_id=request.args.get("matter_id", type=int))


@bp.route("/timer/start", methods=["POST"])
@login_required
def timer_start():
    u = current_user()
    if Timer.query.filter_by(user_id=u.id).first():
        flash("You already have a timer running. Stop it before starting another.", "error")
        return redirect(url_for("time.timer"))
    matter = db.session.get(Matter, request.form.get("matter_id", type=int))
    if not matter:
        flash("Pick a matter to time against.", "error")
        return redirect(url_for("time.timer"))
    t = Timer(user_id=u.id, matter_id=matter.id, description=(request.form.get("description") or "").strip(),
              started_at=now(), accumulated_seconds=0, paused=False)
    db.session.add(t)
    db.session.commit()
    flash(f"Timer started on {matter.number}.", "ok")
    return redirect(url_for("time.timer"))


@bp.route("/timer/pause", methods=["POST"])
@login_required
def timer_pause():
    t = Timer.query.filter_by(user_id=current_user().id).first() or abort(404)
    if not t.paused:
        t.accumulated_seconds = t.elapsed_seconds()
        t.paused = True
        t.started_at = None
        db.session.commit()
    return redirect(url_for("time.timer"))


@bp.route("/timer/resume", methods=["POST"])
@login_required
def timer_resume():
    t = Timer.query.filter_by(user_id=current_user().id).first() or abort(404)
    if t.paused:
        t.paused = False
        t.started_at = now()
        db.session.commit()
    return redirect(url_for("time.timer"))


@bp.route("/timer/stop", methods=["POST"])
@login_required
def timer_stop():
    u = current_user()
    t = Timer.query.filter_by(user_id=u.id).first() or abort(404)
    matter_id = request.form.get("matter_id", type=int) or t.matter_id
    matter = db.session.get(Matter, matter_id) if matter_id else None
    if not matter:
        flash("Pick a matter before stopping the timer so the time has somewhere to go.", "error")
        return redirect(url_for("time.timer"))
    seconds = t.elapsed_seconds()
    minutes = round_up_minutes(seconds)
    description = (request.form.get("description") or t.description or "").strip()
    entry = TimeEntry(matter_id=matter.id, user_id=u.id, date=date.today(), minutes=minutes,
                      description=description, rate_cents=matter.effective_rate_cents(u), billable=True)
    db.session.add(entry)
    db.session.delete(t)
    db.session.flush()
    audit("create", "time_entry", entry.id, f"timer stop: {seconds}s -> {minutes}m on {matter.number}", u.id)
    db.session.commit()
    flash(f"Timer stopped. Logged {minutes / 60:.2f} hours ({minutes} minutes, rounded up to the next 6) on "
          f"{matter.number}. Adjust below if needed.", "ok")
    return redirect(url_for("time.edit", id=entry.id))


# ---------------------------------------------------------------- expenses
@bp.route("/expenses")
@login_required
def expenses():
    q = Expense.query
    matter_id = request.args.get("matter_id", type=int)
    if matter_id:
        q = q.filter(Expense.matter_id == matter_id)
    items = q.order_by(Expense.date.desc(), Expense.id.desc()).all()
    total = sum(e.amount_cents for e in items)
    unbilled = sum(e.amount_cents for e in items if e.billable and e.invoice_id is None)
    return render_template("time/expenses.html", expenses=items, total=total, unbilled=unbilled,
                           matters=Matter.query.order_by(Matter.number).all(), matter_id=matter_id)


def _save_receipt(file):
    if not file or not file.filename:
        return ""
    folder = os.path.join(current_app.config["UPLOAD_DIR"], "expenses")
    os.makedirs(folder, exist_ok=True)
    name = secure_filename(file.filename) or "receipt"
    fname = f"{secrets.token_hex(6)}-{name}"
    file.save(os.path.join(folder, fname))
    return os.path.join("expenses", fname)


def _expense_from_form(exp, form, files):
    matter = db.session.get(Matter, form.get("matter_id", type=int))
    if not matter:
        return "Pick a matter."
    amount = parse_money(form.get("amount"))
    has_receipt = bool(files.get("receipt") and files.get("receipt").filename) or bool(exp.receipt_path)
    if amount < 0:
        return "Amount cannot be negative."
    if amount == 0 and not has_receipt:
        return "Enter an amount, or attach a receipt to fill the amount in later."
    exp.matter_id = matter.id
    exp.date = parse_date(form.get("date"), date.today())
    exp.description = (form.get("description") or "").strip()
    cat = form.get("category") or "Other"
    exp.category = cat if cat in EXPENSE_CATEGORIES else "Other"
    exp.amount_cents = amount
    exp.billable = bool(form.get("billable"))
    exp.expense_code = _code(form, "expense_code", "expense")
    receipt = _save_receipt(files.get("receipt"))
    if receipt:
        exp.receipt_path = receipt
    return None


@bp.route("/expenses/new", methods=["GET", "POST"])
@login_required
def expense_new():
    u = current_user()
    matter_id = request.args.get("matter_id", type=int) or request.form.get("matter_id", type=int)
    matters = _open_matters(matter_id)
    if request.method == "POST":
        exp = Expense(user_id=u.id)
        err = _expense_from_form(exp, request.form, request.files)
        if err:
            flash(err, "error")
            return render_template("time/expense_form.html", expense=exp, matters=matters,
                                   categories=EXPENSE_CATEGORIES, expense_codes=EXPENSE_CODES, form=request.form), 400
        db.session.add(exp)
        db.session.flush()
        audit("create", "expense", exp.id, f"{exp.category} {exp.amount_cents}c on {exp.matter.number}", u.id)
        db.session.commit()
        flash("Expense saved.", "ok")
        nxt = request.form.get("next") or ""
        if nxt.startswith("/"):
            return redirect(nxt)
        return redirect(url_for("time.expenses", matter_id=exp.matter_id))
    exp = Expense(date=date.today(), billable=True, matter_id=matter_id, category="Other")
    return render_template("time/expense_form.html", expense=exp, matters=matters, categories=EXPENSE_CATEGORIES, expense_codes=EXPENSE_CODES,
                           form=None, next=request.args.get("next", ""))


@bp.route("/expenses/<int:id>/edit", methods=["GET", "POST"])
@login_required
def expense_edit(id):
    u = current_user()
    exp = db.session.get(Expense, id) or abort(404)
    matters = _open_matters(exp.matter_id)
    if exp.invoice_id:
        if request.method == "POST":
            flash("This expense is on an invoice and cannot be changed.", "error")
            return redirect(url_for("time.expense_edit", id=id))
        return render_template("time/expense_form.html", expense=exp, matters=matters,
                               categories=EXPENSE_CATEGORIES, expense_codes=EXPENSE_CODES, form=None, locked=True)
    if request.method == "POST":
        err = _expense_from_form(exp, request.form, request.files)
        if err:
            flash(err, "error")
            return render_template("time/expense_form.html", expense=exp, matters=matters,
                                   categories=EXPENSE_CATEGORIES, expense_codes=EXPENSE_CODES, form=request.form), 400
        audit("update", "expense", exp.id, f"{exp.amount_cents}c", u.id)
        db.session.commit()
        flash("Expense saved.", "ok")
        return redirect(url_for("time.expenses", matter_id=exp.matter_id))
    return render_template("time/expense_form.html", expense=exp, matters=matters, categories=EXPENSE_CATEGORIES, expense_codes=EXPENSE_CODES,
                           form=None)


@bp.route("/expenses/<int:id>/delete", methods=["POST"])
@login_required
def expense_delete(id):
    exp = db.session.get(Expense, id) or abort(404)
    if exp.invoice_id:
        flash("This expense is on an invoice and cannot be deleted. Void the invoice first.", "error")
        return redirect(url_for("time.expense_edit", id=id))
    matter_id = exp.matter_id
    audit("delete", "expense", exp.id, f"{exp.category} {exp.amount_cents}c", current_user().id)
    db.session.delete(exp)
    db.session.commit()
    flash("Expense deleted.", "ok")
    return redirect(url_for("time.expenses", matter_id=matter_id))


@bp.route("/expenses/<int:id>/receipt")
@login_required
def expense_receipt(id):
    exp = db.session.get(Expense, id) or abort(404)
    if not exp.receipt_path:
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], exp.receipt_path)
    if not os.path.isfile(path):
        abort(404)
    inline = request.args.get("inline") == "1"
    return send_file(path, as_attachment=not inline, download_name=os.path.basename(exp.receipt_path))
