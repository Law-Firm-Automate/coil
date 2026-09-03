"""Matters: the hub of the app. Every other module links back here."""
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from ..extensions import db
from ..models import (Matter, MatterParty, FlatFeeMilestone, Contact, User, Firm, Note, TimeEntry, Expense,
                      Invoice, TrustTransaction, Task, Document, Engagement, AuditLog, audit)
from ..helpers import login_required, current_user, parse_money, parse_date

bp = Blueprint("matters", __name__, url_prefix="/matters")

PRACTICE_AREAS = ["Estate Planning", "Litigation", "Business", "Real Estate", "Family", "Criminal Defense",
                  "Personal Injury", "Immigration", "Employment", "Bankruptcy", "Other"]
BILLING_TYPES = ["flat", "hourly", "contingency", "hybrid"]
STATUSES = ["pending", "open", "closed"]
PARTY_ROLES = ["adverse", "witness", "co_counsel", "opposing_counsel", "other"]
TABS = [("overview", "Overview"), ("time", "Time & expenses"), ("invoices", "Invoices"), ("trust", "Trust"),
        ("tasks", "Tasks"), ("documents", "Documents"), ("engagements", "Engagement letters"),
        ("activity", "Activity")]


def assign_number(m):
    """Take the next matter number from the firm counter. Caller commits so the bump and the matter land together."""
    f = Firm.get()
    while True:
        n = f"{f.matter_prefix}{f.next_matter_number}"
        f.next_matter_number += 1
        if not Matter.query.filter_by(number=n).first():
            m.number = n
            return


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fill(m, form):
    m.client_id = _int(form.get("client_id"))
    m.name = form.get("name", "").strip()
    m.practice_area = form.get("practice_area", "").strip()
    st = form.get("status", "open")
    m.status = st if st in STATUSES else "open"
    m.responsible_user_id = _int(form.get("responsible_user_id"))
    bt = form.get("billing_type", "flat")
    m.billing_type = bt if bt in BILLING_TYPES else "flat"
    m.hourly_rate_cents = parse_money(form.get("hourly_rate")) if m.billing_type in ("hourly", "hybrid") else 0
    m.flat_fee_cents = parse_money(form.get("flat_fee")) if m.billing_type in ("flat", "hybrid") else 0
    try:
        m.contingency_pct = float(form.get("contingency_pct") or 0) if m.billing_type in ("contingency", "hybrid") else 0.0
    except ValueError:
        m.contingency_pct = 0.0
    m.opened_on = parse_date(form.get("opened_on"), m.opened_on or date.today())
    m.sol_date = parse_date(form.get("sol_date"))
    m.sol_basis = form.get("sol_basis", "").strip()
    m.court = form.get("court", "").strip()
    m.case_number = form.get("case_number", "").strip()
    m.description = form.get("description", "").strip()
    keys = form.getlist("cf_key")
    vals = form.getlist("cf_value")
    cf = {}
    for i, k in enumerate(keys):
        k = k.strip()
        if k:
            cf[k] = (vals[i] if i < len(vals) else "").strip()
    m.custom_fields = cf


def _save_milestones(m, form):
    """Rows come in as parallel lists. Existing rows carry ms_id; blank description removes a row."""
    ids = form.getlist("ms_id")
    descs = form.getlist("ms_description")
    amts = form.getlist("ms_amount")
    dues = form.getlist("ms_due")
    existing = {ms.id: ms for ms in m.milestones}
    seen = set()
    sort = 0
    for i, desc in enumerate(descs):
        desc = desc.strip()
        mid = _int(ids[i]) if i < len(ids) else None
        ms = existing.get(mid) if mid else None
        if ms is None:
            if not desc:
                continue
            ms = FlatFeeMilestone(matter=m)
            db.session.add(ms)
        else:
            seen.add(ms.id)
            if ms.invoiced:
                ms.sort = sort
                sort += 1
                continue
            if not desc:
                db.session.delete(ms)
                continue
        ms.description = desc
        ms.amount_cents = parse_money(amts[i] if i < len(amts) else "")
        ms.due_on = parse_date(dues[i] if i < len(dues) else "")
        ms.sort = sort
        sort += 1
    for ms in existing.values():
        if ms.id not in seen and not ms.invoiced:
            db.session.delete(ms)


def _form_context(m):
    clients = Contact.query.order_by(Contact.is_client.desc(), Contact.last_name, Contact.first_name,
                                     Contact.company_name).all()
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    areas = sorted(set(PRACTICE_AREAS) | {a for (a,) in db.session.query(Matter.practice_area).distinct() if a})
    return dict(m=m, clients=clients, users=users, areas=areas, billing_types=BILLING_TYPES, statuses=STATUSES)


@bp.route("")
@login_required
def index():
    status = request.args.get("status", "")
    area = request.args.get("practice_area", "")
    bt = request.args.get("billing_type", "")
    q = Matter.query
    if status:
        q = q.filter_by(status=status)
    if area:
        q = q.filter_by(practice_area=area)
    if bt:
        q = q.filter_by(billing_type=bt)
    matters = q.order_by(Matter.status, Matter.created_at.desc()).all()
    areas = sorted({a for (a,) in db.session.query(Matter.practice_area).distinct() if a})
    return render_template("matters/index.html", matters=matters, status=status, area=area, bt=bt, areas=areas,
                           billing_types=BILLING_TYPES, statuses=STATUSES)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    m = Matter(status="open", billing_type="flat", opened_on=date.today())
    if request.method == "POST":
        _fill(m, request.form)
        if not m.client_id or not db.session.get(Contact, m.client_id):
            flash("Pick a client.", "error")
            return render_template("matters/form.html", is_new=True, **_form_context(m))
        if not m.name:
            flash("A matter name is required.", "error")
            return render_template("matters/form.html", is_new=True, **_form_context(m))
        assign_number(m)
        db.session.add(m)
        db.session.flush()
        _save_milestones(m, request.form)
        client = db.session.get(Contact, m.client_id)
        if client and not client.is_client:
            client.is_client = True
        audit("create", "matter", m.id, f"{m.number} {m.name}", current_user().id)
        db.session.commit()
        flash(f"Matter {m.number} opened.", "ok")
        return redirect(url_for("matters.detail", id=m.id))
    m.client_id = _int(request.args.get("contact_id"))
    m.responsible_user_id = current_user().id
    return render_template("matters/form.html", is_new=True, **_form_context(m))


@bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit(id):
    m = db.session.get(Matter, id) or abort(404)
    if request.method == "POST":
        _fill(m, request.form)
        if not m.client_id or not db.session.get(Contact, m.client_id):
            flash("Pick a client.", "error")
            return render_template("matters/form.html", is_new=False, **_form_context(m))
        if not m.name:
            flash("A matter name is required.", "error")
            return render_template("matters/form.html", is_new=False, **_form_context(m))
        if m.status == "closed" and not m.closed_on:
            m.closed_on = date.today()
        if m.status != "closed":
            m.closed_on = None
        _save_milestones(m, request.form)
        audit("update", "matter", m.id, "edited", current_user().id)
        db.session.commit()
        flash("Matter saved.", "ok")
        return redirect(url_for("matters.detail", id=m.id))
    return render_template("matters/form.html", is_new=False, **_form_context(m))


@bp.route("/<int:id>")
@login_required
def detail(id):
    m = db.session.get(Matter, id) or abort(404)
    tab = request.args.get("tab", "overview")
    if tab not in dict(TABS):
        tab = "overview"
    entries = sorted(
        [("time", t.date, t) for t in m.time_entries] + [("expense", e.date, e) for e in m.expenses],
        key=lambda r: (r[1] or date.min, r[2].id), reverse=True)[:20]
    invoices = sorted(m.invoices, key=lambda i: (i.issued_on or date.min, i.id), reverse=True)
    trust_rows = TrustTransaction.query.filter_by(matter_id=m.id).order_by(TrustTransaction.date.desc(),
                                                                             TrustTransaction.id.desc()).all()
    tasks = Task.query.filter_by(matter_id=m.id, done=False).order_by(Task.due_on.asc().nulls_last()).all()
    done_tasks = Task.query.filter_by(matter_id=m.id, done=True).order_by(Task.done_at.desc()).limit(10).all()
    documents = Document.query.filter_by(matter_id=m.id).order_by(Document.created_at.desc()).all()
    engagements = Engagement.query.filter_by(matter_id=m.id).order_by(Engagement.created_at.desc()).all()
    activity = AuditLog.query.filter_by(entity="matter", entity_id=m.id).order_by(AuditLog.created_at.desc()).limit(
        50).all()
    milestones_total = sum(ms.amount_cents for ms in m.milestones)
    milestones_invoiced = sum(ms.amount_cents for ms in m.milestones if ms.invoiced)
    return render_template("matters/detail.html", m=m, tab=tab, tabs=TABS, entries=entries, invoices=invoices,
                           trust_rows=trust_rows, tasks=tasks, done_tasks=done_tasks, documents=documents,
                           engagements=engagements, activity=activity, party_roles=PARTY_ROLES,
                           unbilled_time=m.unbilled_time_cents(), unbilled_exp=m.unbilled_expense_cents(),
                           trust=m.trust_balance_cents(), outstanding=m.outstanding_cents(),
                           milestones_total=milestones_total, milestones_invoiced=milestones_invoiced,
                           contacts=Contact.query.order_by(Contact.last_name, Contact.first_name,
                                                           Contact.company_name).all())


@bp.route("/<int:id>/close", methods=["POST"])
@login_required
def close(id):
    m = db.session.get(Matter, id) or abort(404)
    m.status = "closed"
    m.closed_on = date.today()
    audit("close", "matter", m.id, "", current_user().id)
    db.session.commit()
    flash(f"{m.number} closed.", "ok")
    return redirect(url_for("matters.detail", id=m.id))


@bp.route("/<int:id>/reopen", methods=["POST"])
@login_required
def reopen(id):
    m = db.session.get(Matter, id) or abort(404)
    m.status = "open"
    m.closed_on = None
    audit("reopen", "matter", m.id, "", current_user().id)
    db.session.commit()
    flash(f"{m.number} reopened.", "ok")
    return redirect(url_for("matters.detail", id=m.id))


@bp.route("/<int:id>/parties", methods=["POST"])
@login_required
def add_party(id):
    m = db.session.get(Matter, id) or abort(404)
    name = request.form.get("name", "").strip()
    contact_id = _int(request.form.get("contact_id"))
    contact = db.session.get(Contact, contact_id) if contact_id else None
    if contact and not name:
        name = contact.display_name
    if not name:
        flash("A party name is required.", "error")
        return redirect(url_for("matters.detail", id=m.id))
    role = request.form.get("role", "adverse")
    p = MatterParty(matter_id=m.id, contact_id=contact.id if contact else None, name=name,
                    role=role if role in PARTY_ROLES else "other", notes=request.form.get("notes", "").strip()[:300])
    db.session.add(p)
    audit("add_party", "matter", m.id, f"{name} ({p.role})", current_user().id)
    db.session.commit()
    return redirect(url_for("matters.detail", id=m.id))


@bp.route("/<int:id>/parties/<int:pid>/delete", methods=["POST"])
@login_required
def delete_party(id, pid):
    p = db.session.get(MatterParty, pid)
    if not p or p.matter_id != id:
        abort(404)
    audit("remove_party", "matter", id, p.name, current_user().id)
    db.session.delete(p)
    db.session.commit()
    return redirect(url_for("matters.detail", id=id))


@bp.route("/<int:id>/milestones", methods=["POST"])
@login_required
def add_milestone(id):
    m = db.session.get(Matter, id) or abort(404)
    desc = request.form.get("description", "").strip()
    if not desc:
        flash("A milestone description is required.", "error")
        return redirect(url_for("matters.detail", id=m.id))
    ms = FlatFeeMilestone(matter_id=m.id, description=desc, amount_cents=parse_money(request.form.get("amount")),
                          due_on=parse_date(request.form.get("due_on")), sort=len(m.milestones))
    db.session.add(ms)
    audit("add_milestone", "matter", m.id, desc, current_user().id)
    db.session.commit()
    return redirect(url_for("matters.detail", id=m.id))


@bp.route("/<int:id>/milestones/<int:mid>/delete", methods=["POST"])
@login_required
def delete_milestone(id, mid):
    ms = db.session.get(FlatFeeMilestone, mid)
    if not ms or ms.matter_id != id:
        abort(404)
    if ms.invoiced:
        flash("That milestone has been invoiced and cannot be removed.", "error")
        return redirect(url_for("matters.detail", id=id))
    audit("remove_milestone", "matter", id, ms.description, current_user().id)
    db.session.delete(ms)
    db.session.commit()
    return redirect(url_for("matters.detail", id=id))


@bp.route("/<int:id>/notes", methods=["POST"])
@login_required
def add_note(id):
    m = db.session.get(Matter, id) or abort(404)
    body = request.form.get("body", "").strip()
    if body:
        db.session.add(Note(matter_id=m.id, user_id=current_user().id, body=body))
        db.session.commit()
    return redirect(url_for("matters.detail", id=m.id))
