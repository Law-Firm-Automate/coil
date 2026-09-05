"""Firm profile, users, offices, matter templates, the audit log, integration status, and the dev outbox.
/dev/outbox is outside /settings, so no url_prefix. Access is gated by app.permissions.enforce (owner for
everything here except a user editing their own account, and reading the template list)."""
import json
import os
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, session, Response
from sqlalchemy import func
from ..extensions import db
from ..models import Firm, User, Office, Matter, MatterTemplate, AuditLog, audit
from ..helpers import login_required, owner_required, current_user, parse_money, parse_date, CURRENCIES
from ..permissions import ROLES, ROLE_DESCRIPTIONS, canonical_role
from ..services.mail import dev_outbox
from ..services import sms as smssvc
from .invoices import (invoice_settings, sample_pdf_bytes, COLUMN_KEYS, COLUMN_TITLES, COLUMN_HELP, DEFAULT_COLUMNS,
                       LABEL_KEYS, DEFAULT_LABELS, DEFAULT_ACCENT, DEFAULT_TITLE, LOGO_EXTS, LOGO_DIR, valid_hex,
                       logo_abs_path)

bp = Blueprint("settings", __name__)

TEXT_FIELDS = ("name", "address", "phone", "email", "website", "timezone", "invoice_prefix", "matter_prefix",
               "invoice_footer", "trust_bank_name", "operating_bank_name", "trust_account_last4", "ledes_firm_id",
               "courtlistener_token")
INT_FIELDS = ("invoice_terms_days", "next_invoice_number", "next_matter_number", "interest_grace_days")
LANGUAGES = [("en", "English"), ("es", "Spanish")]
TASK_KINDS = ["task", "deadline", "court_date"]
PRIORITIES = ["low", "normal", "high"]
BILLING_TYPES = ["flat", "hourly", "contingency", "hybrid"]
AUDIT_PAGE = 100


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def index():
    f = Firm.get()
    if request.method == "POST":
        if current_user().role != "owner":
            abort(403)
        form = request.form
        for k in TEXT_FIELDS:
            if k in form:
                setattr(f, k, form.get(k, "").strip())
        f.trust_account_last4 = (f.trust_account_last4 or "")[-4:]
        for k in INT_FIELDS:
            if k in form:
                try:
                    setattr(f, k, int(form.get(k) or 0))
                except ValueError:
                    flash(f"{k.replace('_', ' ')} must be a whole number.", "error")
                    return render_template("settings/index.html", f=f, currencies=CURRENCIES, languages=LANGUAGES)
        if "default_rate" in form:
            f.default_rate_cents = parse_money(form.get("default_rate"))
        if "surcharge_pct" in form:
            try:
                pct = float(str(form.get("surcharge_pct") or "0").replace("%", "").strip() or 0)
            except ValueError:
                flash("Surcharge must be a percentage like 3 or 2.5.", "error")
                return render_template("settings/index.html", f=f, currencies=CURRENCIES, languages=LANGUAGES)
            f.surcharge_bps = int(round(pct * 100))
        if "interest_apr" in form:
            try:
                apr = float(str(form.get("interest_apr") or "0").replace("%", "").strip() or 0)
            except ValueError:
                flash("Interest rate must be a percentage like 12 or 1.5.", "error")
                return render_template("settings/index.html", f=f, currencies=CURRENCIES, languages=LANGUAGES)
            f.interest_apr_bps = int(round(apr * 100))
        if "currency" in form:
            cur = form.get("currency", "USD").upper()
            f.currency = cur if cur in CURRENCIES else "USD"
        if "default_language" in form:
            lang = form.get("default_language", "en")
            f.default_language = lang if lang in dict(LANGUAGES) else "en"
        if "_form" in form:
            # checkboxes only arrive when ticked; _form marks a full submission so unticked means off
            f.surcharge_enabled = form.get("surcharge_enabled") == "1"
            f.daily_agenda_email = form.get("daily_agenda_email") == "1"
            f.require_invoice_approval = form.get("require_invoice_approval") == "1"
            f.ai_enabled = form.get("ai_enabled") == "1"
            f.sequences_auto_send = form.get("sequences_auto_send") == "1"
        else:
            for k in ("surcharge_enabled", "daily_agenda_email", "require_invoice_approval", "ai_enabled",
                      "sequences_auto_send"):
                if k in form:
                    setattr(f, k, form.get(k) == "1")
        audit("update", "firm", f.id, "settings saved", current_user().id)
        db.session.commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("settings.index"))
    return render_template("settings/index.html", f=f, currencies=CURRENCIES, languages=LANGUAGES)


# ---- invoice template (Agent I) ----
def _template_form(form):
    """Unsaved template settings from the editor form, as the override dict invoice_settings() accepts."""
    picked = []
    for key in COLUMN_KEYS:
        if form.get(f"col_{key}") == "1":
            picked.append((_int(form.get(f"ord_{key}"), 99) or 99, COLUMN_KEYS.index(key), key))
    columns = [k for _, _, k in sorted(picked)]
    labels = {k: (form.get(f"label_{k}") or "").strip()[:60] for k in LABEL_KEYS}
    return {
        "columns": columns or list(DEFAULT_COLUMNS),
        "labels": {k: v for k, v in labels.items() if v},
        "accent": valid_hex(form.get("invoice_accent")) or DEFAULT_ACCENT,
        "title": (form.get("invoice_title") or "").strip()[:60] or DEFAULT_TITLE,
        "show_timekeeper": form.get("invoice_show_timekeeper") == "1",
        "show_codes": form.get("invoice_show_activity_codes") == "1",
        "payment_instructions": (form.get("invoice_payment_instructions") or "").strip()[:4000],
        "statement_footer": (form.get("statement_footer") or "").strip()[:2000],
    }


def _save_logo(file, name="logo"):
    """Store an uploaded png/jpg under UPLOAD_DIR/firm/. Returns the relative path, or an error string."""
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in LOGO_EXTS:
        return "Logo must be a PNG or JPG file."
    data = file.read()
    if not data:
        return "The logo file is empty."
    if len(data) > 2 * 1024 * 1024:
        return "Logo must be under 2 MB."
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except Exception:
        return "That file is not a readable image."
    folder = os.path.join(current_app.config["UPLOAD_DIR"], LOGO_DIR)
    os.makedirs(folder, exist_ok=True)
    rel = f"{LOGO_DIR}/{name}.{'jpg' if ext == 'jpeg' else ext}"
    for old in LOGO_EXTS:  # one logo at a time
        stale = os.path.join(folder, f"{name}.{old}")
        if os.path.exists(stale):
            os.remove(stale)
    with open(os.path.join(current_app.config["UPLOAD_DIR"], rel), "wb") as fh:
        fh.write(data)
    return rel


def _template_page(f, tpl, error=None):
    order = {k: i + 1 for i, k in enumerate(tpl.columns)}
    return render_template("settings/invoice_template.html", f=f, tpl=tpl, column_keys=COLUMN_KEYS,
                           column_titles=COLUMN_TITLES, column_help=COLUMN_HELP, order=order, label_keys=LABEL_KEYS,
                           default_labels=DEFAULT_LABELS, has_logo=bool(logo_abs_path(f.invoice_logo_path)),
                           error=error)


@bp.route("/settings/invoice-template", methods=["GET", "POST"])
@owner_required
def invoice_template():
    f = Firm.get()
    if request.method == "GET":
        return _template_page(f, invoice_settings(f))
    form = request.form
    o = _template_form(form)
    if form.get("action") == "preview":
        override = dict(o)
        logo = request.files.get("logo")
        if logo and logo.filename:
            r = _save_logo(logo, name="logo-preview")
            if r and "/" in r:
                override["logo_path"] = r
            elif r:
                return _template_page(f, invoice_settings(f, o), error=r)
        elif form.get("remove_logo") == "1":
            override["logo_path"] = ""
        data = sample_pdf_bytes(invoice_settings(f, override))
        return Response(data, mimetype="application/pdf",
                        headers={"Content-Disposition": 'inline; filename="invoice-preview-SAMPLE.pdf"'})
    # save
    logo = request.files.get("logo")
    if logo and logo.filename:
        r = _save_logo(logo)
        if r and "/" not in r:
            return _template_page(f, invoice_settings(f, o), error=r)
        f.invoice_logo_path = r
    elif form.get("remove_logo") == "1":
        path = logo_abs_path(f.invoice_logo_path)
        if path:
            os.remove(path)
        f.invoice_logo_path = ""
    f.invoice_columns_json = json.dumps(o["columns"])
    f.invoice_labels_json = json.dumps(o["labels"])
    f.invoice_accent = o["accent"]
    f.invoice_title = o["title"]
    f.invoice_show_timekeeper = o["show_timekeeper"]
    f.invoice_show_activity_codes = o["show_codes"]
    f.invoice_payment_instructions = o["payment_instructions"]
    f.statement_footer = o["statement_footer"]
    day = _int(form.get("monthly_billing_day"), 0) or 0
    f.monthly_billing_day = day if 0 <= day <= 28 else 0
    f.monthly_billing_send = form.get("monthly_billing_send") == "1"
    audit("update", "firm", f.id, "invoice template saved", current_user().id)
    db.session.commit()
    flash("Invoice template saved. Existing invoice PDFs are rebuilt with it the next time they are sent or opened.", "ok")
    return redirect(url_for("settings.invoice_template"))


# ---- users ----
def _role_options():
    return [(r, r.capitalize(), ROLE_DESCRIPTIONS[r]) for r in ROLES]


def _user_form_context(u, is_new, self_only=False):
    return dict(u=u, is_new=is_new, roles=_role_options(), self_only=self_only,
                offices=Office.query.order_by(Office.is_default.desc(), Office.name).all(),
                display_role=canonical_role(u.role) if u.role else "attorney")


@bp.route("/settings/users")
@owner_required
def users():
    rows = User.query.order_by(User.is_active.desc(), User.name).all()
    return render_template("settings/users.html", rows=rows, canonical_role=canonical_role)


def _fill_user(u, form, is_new, self_only=False):
    u.name = form.get("name", "").strip()
    u.email = form.get("email", "").strip().lower()
    u.initials = form.get("initials", "").strip().upper()[:6] or "".join(p[0] for p in u.name.split()[:2]).upper()
    if not self_only:
        role = (form.get("role") or "attorney").strip().lower()
        # "staff" is the legacy spelling of attorney and is still accepted as posted.
        u.role = role if role in ROLES or role == "staff" else "attorney"
        u.hourly_rate_cents = parse_money(form.get("hourly_rate"))
        if "cost_rate" in form:
            u.cost_rate_cents = parse_money(form.get("cost_rate"))
        if "office_id" in form:
            oid = _int(form.get("office_id"))
            u.office_id = oid if oid and db.session.get(Office, oid) else None
        if not is_new:
            u.is_active = form.get("is_active") == "1"
    pw = form.get("password", "")
    if not u.name or not u.email:
        return "Name and email are required."
    if is_new and len(pw) < 8:
        return "A password of at least 8 characters is required for a new user."
    if pw and len(pw) < 8:
        return "New password must be at least 8 characters."
    other = User.query.filter(db.func.lower(User.email) == u.email, User.id != (u.id or 0)).first()
    if other:
        return "Another user already has that email."
    if pw:
        u.set_password(pw)
    return None


@bp.route("/settings/users/new", methods=["GET", "POST"])
@owner_required
def user_new():
    default_office = Office.query.filter_by(is_default=True).first()
    u = User(role="attorney", is_active=True, hourly_rate_cents=Firm.get().default_rate_cents,
             office_id=default_office.id if default_office else None)
    if request.method == "POST":
        err = _fill_user(u, request.form, True)
        if err:
            flash(err, "error")
            return render_template("settings/user_form.html", **_user_form_context(u, True))
        db.session.add(u)
        db.session.flush()
        audit("create", "user", u.id, f"{u.email} ({u.role})", current_user().id)
        db.session.commit()
        flash(f"Added {u.name}.", "ok")
        return redirect(url_for("settings.users"))
    return render_template("settings/user_form.html", **_user_form_context(u, True))


@bp.route("/settings/users/<int:id>/edit", methods=["GET", "POST"])
@login_required
def user_edit(id):
    """Owners edit anyone. Other roles may open only their own account and change name, email, initials, password."""
    me = current_user()
    u = db.session.get(User, id) or abort(404)
    self_only = me.role != "owner"
    if self_only and u.id != me.id:
        abort(403)
    if request.method == "POST":
        err = _fill_user(u, request.form, False, self_only=self_only)
        if not err and u.id == me.id and not self_only and (not u.is_active or u.role != "owner"):
            err = "You cannot deactivate or demote your own account."
        if not err and not u.is_active and User.query.filter_by(role="owner", is_active=True).filter(
                User.id != u.id).count() == 0 and u.role == "owner":
            err = "At least one active owner is required."
        if err:
            db.session.rollback()
            flash(err, "error")
            return render_template("settings/user_form.html", **_user_form_context(db.session.get(User, id), False, self_only))
        audit("update", "user", u.id, u.email + (" (own account)" if self_only else ""), me.id)
        db.session.commit()
        flash("User saved.", "ok")
        return redirect(url_for("settings.users") if not self_only else url_for("dashboard.index"))
    return render_template("settings/user_form.html", **_user_form_context(u, False, self_only))


# ---- offices ----
def _office_refs(o):
    return (User.query.filter_by(office_id=o.id).count(), Matter.query.filter_by(office_id=o.id).count())


def _fill_office(o, form):
    o.name = form.get("name", "").strip()
    o.address = form.get("address", "").strip()
    o.phone = form.get("phone", "").strip()
    o.email = form.get("email", "").strip()
    return None if o.name else "An office name is required."


def _set_default(o):
    for other in Office.query.all():
        other.is_default = other.id == o.id


@bp.route("/settings/offices")
@owner_required
def offices():
    rows = Office.query.order_by(Office.is_default.desc(), Office.name).all()
    refs = {o.id: _office_refs(o) for o in rows}
    return render_template("settings/offices.html", rows=rows, refs=refs)


@bp.route("/settings/offices/new", methods=["GET", "POST"])
@owner_required
def office_new():
    o = Office()
    if request.method == "POST":
        err = _fill_office(o, request.form)
        if err:
            flash(err, "error")
            return render_template("settings/office_form.html", o=o, is_new=True)
        db.session.add(o)
        db.session.flush()
        if request.form.get("is_default") == "1" or Office.query.count() == 1:
            _set_default(o)
        audit("create", "office", o.id, o.name, current_user().id)
        db.session.commit()
        flash(f"Added office {o.name}.", "ok")
        return redirect(url_for("settings.offices"))
    o.is_default = Office.query.count() == 0
    return render_template("settings/office_form.html", o=o, is_new=True)


@bp.route("/settings/offices/<int:id>/edit", methods=["GET", "POST"])
@owner_required
def office_edit(id):
    o = db.session.get(Office, id) or abort(404)
    if request.method == "POST":
        err = _fill_office(o, request.form)
        if err:
            flash(err, "error")
            return render_template("settings/office_form.html", o=o, is_new=False)
        if request.form.get("is_default") == "1":
            _set_default(o)
        audit("update", "office", o.id, o.name, current_user().id)
        db.session.commit()
        flash("Office saved.", "ok")
        return redirect(url_for("settings.offices"))
    return render_template("settings/office_form.html", o=o, is_new=False)


@bp.route("/settings/offices/<int:id>/default", methods=["POST"])
@owner_required
def office_default(id):
    o = db.session.get(Office, id) or abort(404)
    _set_default(o)
    audit("update", "office", o.id, f"{o.name} made default", current_user().id)
    db.session.commit()
    flash(f"{o.name} is now the default office.", "ok")
    return redirect(url_for("settings.offices"))


@bp.route("/settings/offices/<int:id>/delete", methods=["POST"])
@owner_required
def office_delete(id):
    o = db.session.get(Office, id) or abort(404)
    users_n, matters_n = _office_refs(o)
    if users_n or matters_n:
        flash(f"{o.name} is still used by {users_n} user(s) and {matters_n} matter(s). Reassign them first.", "error")
        return redirect(url_for("settings.offices"))
    name = o.name
    was_default = o.is_default
    db.session.delete(o)
    db.session.flush()
    if was_default:
        nxt = Office.query.order_by(Office.name).first()
        if nxt:
            nxt.is_default = True
    audit("delete", "office", id, name, current_user().id)
    db.session.commit()
    flash(f"Deleted office {name}.", "ok")
    return redirect(url_for("settings.offices"))


# ---- audit log ----
@bp.route("/settings/audit")
@owner_required
def audit_log():
    a = request.args
    user_id = _int(a.get("user_id"))
    action = a.get("action", "").strip()
    entity = a.get("entity", "").strip()
    entity_id = _int(a.get("entity_id"))
    d_from = parse_date(a.get("from"))
    d_to = parse_date(a.get("to"))
    q = a.get("q", "").strip()
    page = max(1, _int(a.get("page"), 1) or 1)
    query = AuditLog.query
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if entity:
        query = query.filter(AuditLog.entity == entity)
    if entity_id:
        query = query.filter(AuditLog.entity_id == entity_id)
    if d_from:
        query = query.filter(AuditLog.created_at >= datetime.combine(d_from, datetime.min.time()))
    if d_to:
        query = query.filter(AuditLog.created_at < datetime.combine(d_to + timedelta(days=1), datetime.min.time()))
    if q:
        query = query.filter(AuditLog.detail.ilike(f"%{q}%"))
    total = query.count()
    rows = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset((page - 1) * AUDIT_PAGE).limit(
        AUDIT_PAGE).all()
    pages = max(1, (total + AUDIT_PAGE - 1) // AUDIT_PAGE)
    actions = [r for (r,) in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action) if r]
    entities = [r for (r,) in db.session.query(AuditLog.entity).distinct().order_by(AuditLog.entity) if r]
    users = User.query.order_by(User.name).all()
    filters = dict(user_id=user_id or "", action=action, entity=entity, entity_id=entity_id or "",
                   **{"from": a.get("from", ""), "to": a.get("to", "")}, q=q)
    return render_template("settings/audit.html", rows=rows, total=total, page=page, pages=pages, per_page=AUDIT_PAGE,
                           actions=actions, entities=entities, users=users, f=filters)


# ---- matter templates ----
SAMPLE_TEMPLATES = [
    dict(name="Personal injury (contingency)", practice_area="Personal Injury",
         description="Auto or premises injury claim on a one-third contingency. Two-year Texas limitations period.",
         billing_type="contingency", contingency_pct=33.33, sol_years=2.0,
         sol_basis="2-year personal injury, TX CPRC 16.003",
         milestones=[],
         tasks=[
             dict(title="Send letters of representation to insurers", kind="task", offset_days=3, priority="high", assignee="responsible"),
             dict(title="Request police report and medical records", kind="task", offset_days=7, priority="normal", assignee="responsible"),
             dict(title="Send spoliation letter", kind="deadline", offset_days=14, priority="high", assignee="responsible"),
             dict(title="Treatment status check-in with client", kind="task", offset_days=45, priority="normal", assignee="responsible"),
             dict(title="Demand package due", kind="deadline", offset_days=180, priority="high", assignee="responsible"),
             dict(title="Calendar limitations deadline", kind="deadline", offset_days=640, priority="high", assignee="responsible"),
         ],
         custom_fields={"Date of injury": "", "Insurance carrier": "", "Claim number": "", "Policy limits": ""},
         trust_minimum_cents=0, trust_replenish_to_cents=0),
    dict(name="Estate plan (flat fee)", practice_area="Estate Planning",
         description="Will, powers of attorney and directives for an individual or couple. Half on signing, half at execution.",
         billing_type="flat", flat_fee_cents=250000, sol_years=0.0, sol_basis="",
         milestones=[
             dict(description="Retainer on signing", amount_cents=125000, due_offset_days=0),
             dict(description="Balance on document execution", amount_cents=125000, due_offset_days=45),
         ],
         tasks=[
             dict(title="Send intake questionnaire", kind="task", offset_days=1, priority="normal", assignee="responsible"),
             dict(title="Design meeting", kind="task", offset_days=14, priority="normal", assignee="responsible"),
             dict(title="Draft documents for review", kind="task", offset_days=28, priority="normal", assignee="responsible"),
             dict(title="Signing appointment", kind="court_date", offset_days=45, priority="high", assignee="responsible"),
         ],
         custom_fields={"Spouse name": "", "Executor": "", "Guardian for minors": ""},
         trust_minimum_cents=0, trust_replenish_to_cents=0),
]


def _ensure_sample_templates():
    if MatterTemplate.query.count() > 0:
        return
    for s in SAMPLE_TEMPLATES:
        t = MatterTemplate(name=s["name"], practice_area=s["practice_area"], description=s["description"],
                           billing_type=s["billing_type"], flat_fee_cents=s.get("flat_fee_cents", 0),
                           hourly_rate_cents=s.get("hourly_rate_cents", 0), contingency_pct=s.get("contingency_pct", 0.0),
                           sol_years=s["sol_years"], sol_basis=s["sol_basis"],
                           trust_minimum_cents=s["trust_minimum_cents"], trust_replenish_to_cents=s["trust_replenish_to_cents"],
                           milestones_json=json.dumps(s["milestones"]), tasks_json=json.dumps(s["tasks"]),
                           custom_fields_json=json.dumps(s["custom_fields"]), is_active=True)
        db.session.add(t)
    db.session.commit()


def _fill_template(t, form):
    t.name = form.get("name", "").strip()
    t.practice_area = form.get("practice_area", "").strip()
    t.description = form.get("description", "").strip()
    bt = form.get("billing_type", "flat")
    t.billing_type = bt if bt in BILLING_TYPES else "flat"
    t.hourly_rate_cents = parse_money(form.get("hourly_rate"))
    t.flat_fee_cents = parse_money(form.get("flat_fee"))
    try:
        t.contingency_pct = float(form.get("contingency_pct") or 0)
    except ValueError:
        t.contingency_pct = 0.0
    try:
        t.sol_years = float(form.get("sol_years") or 0)
    except ValueError:
        t.sol_years = 0.0
    t.sol_basis = form.get("sol_basis", "").strip()
    t.trust_minimum_cents = parse_money(form.get("trust_minimum"))
    t.trust_replenish_to_cents = parse_money(form.get("trust_replenish_to"))
    t.is_active = form.get("is_active") == "1"

    ms = []
    descs, amts, offs = form.getlist("ms_description"), form.getlist("ms_amount"), form.getlist("ms_offset")
    for i, d in enumerate(descs):
        d = d.strip()
        if not d:
            continue
        ms.append(dict(description=d, amount_cents=parse_money(amts[i] if i < len(amts) else ""),
                       due_offset_days=_int(offs[i] if i < len(offs) else "")))
    t.milestones_json = json.dumps(ms)

    tasks = []
    titles, kinds, toffs = form.getlist("t_title"), form.getlist("t_kind"), form.getlist("t_offset")
    prios, assignees = form.getlist("t_priority"), form.getlist("t_assignee")
    for i, title in enumerate(titles):
        title = title.strip()
        if not title:
            continue
        kind = kinds[i] if i < len(kinds) else "task"
        prio = prios[i] if i < len(prios) else "normal"
        who = assignees[i] if i < len(assignees) else "responsible"
        tasks.append(dict(title=title, kind=kind if kind in TASK_KINDS else "task",
                          offset_days=_int(toffs[i] if i < len(toffs) else "", 0) or 0,
                          priority=prio if prio in PRIORITIES else "normal",
                          assignee="none" if who == "none" else "responsible"))
    t.tasks_json = json.dumps(tasks)

    cf = {}
    keys, vals = form.getlist("cf_key"), form.getlist("cf_value")
    for i, k in enumerate(keys):
        k = k.strip()
        if k:
            cf[k] = (vals[i] if i < len(vals) else "").strip()
    t.custom_fields_json = json.dumps(cf)
    return None if t.name else "A template name is required."


def _template_form_context(t, is_new):
    from .matters import PRACTICE_AREAS
    areas = sorted(set(PRACTICE_AREAS) | {a for (a,) in db.session.query(Matter.practice_area).distinct() if a})
    return dict(t=t, is_new=is_new, areas=areas, billing_types=BILLING_TYPES, kinds=TASK_KINDS, priorities=PRIORITIES)


@bp.route("/settings/templates")
@login_required
def templates():
    _ensure_sample_templates()
    rows = MatterTemplate.query.order_by(MatterTemplate.is_active.desc(), MatterTemplate.name).all()
    usage = dict(db.session.query(Matter.template_id, func.count(Matter.id)).filter(
        Matter.template_id.isnot(None)).group_by(Matter.template_id).all())
    return render_template("settings/templates.html", rows=rows, usage=usage, is_owner=current_user().role == "owner")


@bp.route("/settings/templates/new", methods=["GET", "POST"])
@owner_required
def template_new():
    t = MatterTemplate(billing_type="flat", is_active=True)
    if request.method == "POST":
        err = _fill_template(t, request.form)
        if err:
            flash(err, "error")
            return render_template("settings/template_form.html", **_template_form_context(t, True))
        db.session.add(t)
        db.session.flush()
        audit("create", "matter_template", t.id, t.name, current_user().id)
        db.session.commit()
        flash(f"Template {t.name} created.", "ok")
        return redirect(url_for("settings.templates"))
    return render_template("settings/template_form.html", **_template_form_context(t, True))


@bp.route("/settings/templates/<int:id>/edit", methods=["GET", "POST"])
@owner_required
def template_edit(id):
    t = db.session.get(MatterTemplate, id) or abort(404)
    if request.method == "POST":
        err = _fill_template(t, request.form)
        if err:
            flash(err, "error")
            return render_template("settings/template_form.html", **_template_form_context(t, False))
        audit("update", "matter_template", t.id, t.name, current_user().id)
        db.session.commit()
        flash("Template saved.", "ok")
        return redirect(url_for("settings.templates"))
    return render_template("settings/template_form.html", **_template_form_context(t, False))


@bp.route("/settings/templates/<int:id>/duplicate", methods=["POST"])
@owner_required
def template_duplicate(id):
    src = db.session.get(MatterTemplate, id) or abort(404)
    t = MatterTemplate(name=f"{src.name} (copy)", practice_area=src.practice_area, description=src.description,
                       billing_type=src.billing_type, hourly_rate_cents=src.hourly_rate_cents,
                       flat_fee_cents=src.flat_fee_cents, contingency_pct=src.contingency_pct,
                       milestones_json=src.milestones_json, tasks_json=src.tasks_json,
                       custom_fields_json=src.custom_fields_json, sol_years=src.sol_years, sol_basis=src.sol_basis,
                       trust_minimum_cents=src.trust_minimum_cents, trust_replenish_to_cents=src.trust_replenish_to_cents,
                       is_active=src.is_active)
    db.session.add(t)
    db.session.flush()
    audit("duplicate", "matter_template", t.id, f"copied from {src.name}", current_user().id)
    db.session.commit()
    flash(f"Duplicated as {t.name}.", "ok")
    return redirect(url_for("settings.template_edit", id=t.id))


@bp.route("/settings/templates/<int:id>/delete", methods=["POST"])
@owner_required
def template_delete(id):
    t = db.session.get(MatterTemplate, id) or abort(404)
    used = Matter.query.filter_by(template_id=t.id).count()
    if used:
        t.is_active = False
        audit("update", "matter_template", t.id, f"{t.name} deactivated ({used} matters use it)", current_user().id)
        db.session.commit()
        flash(f"{t.name} is used by {used} matter(s), so it was deactivated instead of deleted.", "ok")
        return redirect(url_for("settings.templates"))
    name = t.name
    db.session.delete(t)
    audit("delete", "matter_template", id, name, current_user().id)
    db.session.commit()
    flash(f"Deleted template {name}.", "ok")
    return redirect(url_for("settings.templates"))


# ---- integrations ----
@bp.route("/settings/integrations")
@login_required
def integrations():
    c = current_app.config
    base = c["BASE_URL"]
    cards = [
        dict(name="Email (SMTP)", ok=bool(c.get("SMTP_HOST")),
             detail=f"Sending from {c.get('MAIL_FROM')} via {c.get('SMTP_HOST')}:{c.get('SMTP_PORT')}" if c.get("SMTP_HOST")
             else "SMTP_HOST is empty. Emails are logged to the dev outbox instead of being delivered.",
             env="SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM",
             link=("/dev/outbox", "Open dev outbox") if current_user().role == "owner" and not c.get("SMTP_HOST") else None),
        dict(name="Stripe (card and ACH payments)", ok=bool(c.get("STRIPE_SECRET_KEY")),
             detail=("Secret key set. " + ("Webhook secret set." if c.get("STRIPE_WEBHOOK_SECRET") else
                                          "STRIPE_WEBHOOK_SECRET is empty, so webhook signatures are not verified."))
             if c.get("STRIPE_SECRET_KEY") else "STRIPE_SECRET_KEY is empty. Pay links show mailing instructions instead.",
             env="STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET",
             webhook=f"{base}/webhooks/stripe", webhook_note="Stripe Dashboard > Developers > Webhooks. Event: checkout.session.completed"),
        dict(name="Twilio (two-way texting)", ok=smssvc.configured(),
             detail=f"Sending from {c.get('TWILIO_FROM_NUMBER')}." if smssvc.configured()
             else "Twilio is not configured. Messages are stored but not delivered.",
             env="TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER",
             webhook=f"{base}/webhooks/twilio", webhook_note="Twilio Console > Phone number > Messaging > A message comes in (HTTP POST)"),
        # Agent J: research on CourtListener. The token is optional; the Firm field in Settings wins over the env.
        dict(name="Research (CourtListener)", ok=bool((Firm.get().courtlistener_token or "").strip() or c.get("COURTLISTENER_TOKEN")),
             detail=("Token set" + (" in Settings." if (Firm.get().courtlistener_token or "").strip() else " from the environment.")
                     + " Searches, full opinion text and the citation check use it.")
             if ((Firm.get().courtlistener_token or "").strip() or c.get("COURTLISTENER_TOKEN"))
             else "No token. Case law search still works with a lower rate limit; full opinion text and the citation check "
                  "need a token, which is optional and free from courtlistener.com. Paste it under Settings, Research.",
             env="COURTLISTENER_TOKEN (or the Research field on the firm settings form)",
             link=("/research", "Open research")),
    ]
    return render_template("settings/integrations.html", cards=cards, base=base,
                           intake_url=f"{base}/intake/form")


@bp.route("/dev/outbox")
@owner_required
def outbox():
    return render_template("settings/outbox.html", rows=dev_outbox(),
                           smtp=bool(current_app.config.get("SMTP_HOST")))


# ---------------------------------------------------------------- API tokens (Agent G)
@bp.route("/settings/api", methods=["GET", "POST"])
@login_required
def api_tokens():
    from ..models import ApiToken
    from .api import create_token, RATE_LIMIT
    u = current_user()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Give the token a name so you know what to revoke later.", "error")
            return redirect(url_for("settings.api_tokens"))
        t, raw = create_token(u, name, request.form.get("scopes") or "read")
        db.session.flush()
        audit("api_token_create", "api_token", t.id, f"{name} ({t.scopes})", u.id)
        db.session.commit()
        session["_new_api_token"] = raw
        return redirect(url_for("settings.api_tokens"))
    new_token = session.pop("_new_api_token", None)
    rows = ApiToken.query.order_by(ApiToken.revoked_at.isnot(None), ApiToken.created_at.desc()).all()
    return render_template("settings/api.html", rows=rows, new_token=new_token, base=current_app.config["BASE_URL"],
                           rate_limit=current_app.config.get("API_RATE_LIMIT", RATE_LIMIT))


@bp.route("/settings/api/<int:id>/revoke", methods=["POST"])
@login_required
def api_token_revoke(id):
    from ..models import ApiToken
    t = db.session.get(ApiToken, id) or abort(404)
    u = current_user()
    if t.user_id != u.id and u.role != "owner":
        abort(403)
    if not t.revoked_at:
        t.revoked_at = datetime.utcnow()
        audit("api_token_revoke", "api_token", t.id, t.name, u.id)
        db.session.commit()
    flash(f"Token {t.name} revoked.", "ok")
    return redirect(url_for("settings.api_tokens"))


# ---------------------------------------------------------------- outgoing webhooks (Agent G)
@bp.route("/settings/webhooks")
@login_required
def webhooks():
    from ..models import Webhook, WebhookDelivery
    from .webhooks_out import EVENTS
    hooks = Webhook.query.order_by(Webhook.id).all()
    deliveries = WebhookDelivery.query.order_by(WebhookDelivery.id.desc()).limit(50).all()
    return render_template("settings/webhooks.html", hooks=hooks, deliveries=deliveries, events=EVENTS)


@bp.route("/settings/webhooks/new", methods=["POST"])
@login_required
def webhook_new():
    from ..models import Webhook, new_token
    from .webhooks_out import EVENT_NAMES
    url = (request.form.get("url") or "").strip()
    events = [e for e in request.form.getlist("events") if e in EVENT_NAMES]
    if not url.lower().startswith(("http://", "https://")):
        flash("Enter a full http(s) URL.", "error")
        return redirect(url_for("settings.webhooks"))
    if not events:
        flash("Pick at least one event.", "error")
        return redirect(url_for("settings.webhooks"))
    h = Webhook(url=url[:500], events=",".join(events), secret=(request.form.get("secret") or "").strip()[:120] or new_token(24),
                is_active=True)
    db.session.add(h)
    db.session.flush()
    audit("webhook_create", "webhook", h.id, f"{url} [{h.events}]", current_user().id)
    db.session.commit()
    flash("Webhook added.", "ok")
    return redirect(url_for("settings.webhooks"))


@bp.route("/settings/webhooks/<int:id>/toggle", methods=["POST"])
@login_required
def webhook_toggle(id):
    from ..models import Webhook
    h = db.session.get(Webhook, id) or abort(404)
    h.is_active = not h.is_active
    db.session.commit()
    flash("Webhook resumed." if h.is_active else "Webhook paused.", "ok")
    return redirect(url_for("settings.webhooks"))


@bp.route("/settings/webhooks/<int:id>/delete", methods=["POST"])
@login_required
def webhook_delete(id):
    from ..models import Webhook, WebhookDelivery
    h = db.session.get(Webhook, id) or abort(404)
    WebhookDelivery.query.filter_by(webhook_id=h.id).delete()
    audit("webhook_delete", "webhook", h.id, h.url, current_user().id)
    db.session.delete(h)
    db.session.commit()
    flash("Webhook deleted.", "ok")
    return redirect(url_for("settings.webhooks"))


@bp.route("/settings/webhooks/<int:id>/test", methods=["POST"])
@login_required
def webhook_test(id):
    from ..models import Webhook, WebhookDelivery
    from .webhooks_out import attempt_delivery
    import json as _json
    h = db.session.get(Webhook, id) or abort(404)
    d = WebhookDelivery(webhook_id=h.id, event="ping", status="pending", attempts=0,
                        payload_json=_json.dumps({"event": "ping", "created_at": datetime.utcnow().isoformat(),
                                                  "data": {"message": "Test delivery from Coil", "webhook_id": h.id}}))
    db.session.add(d)
    db.session.flush()
    ok = attempt_delivery(d, h)
    db.session.commit()
    flash("Test delivered." if ok else f"Test failed: {d.last_error}", "ok" if ok else "error")
    return redirect(url_for("settings.webhooks"))


@bp.route("/settings/webhooks/deliveries/<int:id>/retry", methods=["POST"])
@login_required
def webhook_retry(id):
    from ..models import WebhookDelivery
    from .webhooks_out import attempt_delivery
    d = db.session.get(WebhookDelivery, id) or abort(404)
    if not d.webhook:
        abort(404)
    ok = attempt_delivery(d, d.webhook)
    db.session.commit()
    flash("Delivered." if ok else f"Still failing: {d.last_error}", "ok" if ok else "error")
    return redirect(url_for("settings.webhooks"))
