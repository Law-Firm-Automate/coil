"""Criminal defense module (Vertical case types lane, Agent R).

/criminal            board by stage with custody badge and next setting; start a case on a matter
/criminal/<matter>   case facts, charges CRUD, court date chain, speedy-trial check, disposition PDF, client status

Sentencing ranges and fines are typed in by the attorney. Coil ships no statute tables; the module records what
you enter and never looks a range up.
"""
import os
import uuid
from datetime import date, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import Matter, CriminalCase, Charge, Task, Document, Firm, audit, now
from ..helpers import login_required, current_user, parse_money, parse_date
from ..services.pdf import DocPDF

bp = Blueprint("criminal", __name__, url_prefix="/criminal")

STAGES = [("arrest", "Arrest"), ("charged", "Charged"), ("discovery", "Discovery"), ("negotiation", "Negotiation"),
          ("pretrial", "Pretrial"), ("trial", "Trial"), ("disposed", "Disposed")]
STAGE_KEYS = [k for k, _ in STAGES]
STAGE_LABELS = dict(STAGES)
SETTING_TYPES = [("arraignment", "Arraignment"), ("announcement", "Announcement"), ("pretrial", "Pretrial hearing"),
                 ("plea", "Plea"), ("trial", "Trial"), ("other", "Other")]
BOND_STATUSES = [("", "Not set"), ("posted", "Posted"), ("denied", "Denied"), ("pr", "Personal recognizance"),
                 ("held", "Held (no bond)")]
CUSTODY = [("out", "Out of custody"), ("in", "In custody")]
DISPOSITIONS = [("pending", "Pending"), ("dismissed", "Dismissed"), ("plea", "Plea"), ("acquitted", "Acquitted"),
                ("convicted", "Convicted"), ("deferred", "Deferred adjudication")]
SPEEDY_TRIAL_DAYS = 180
SPEEDY_TRIAL_TITLE = "Speedy trial / limitations check"
SPEEDY_TRIAL_NOTE = ("Placeholder at arrest + 180 days. Confirm the jurisdiction's speedy-trial rule and any charging "
                     "limitations period for these charges; Coil does not ship statute tables.")
RANGE_NOTE = ("Sentencing ranges, degrees and maximum fines are entered by the attorney. Coil ships no statute tables "
              "and does not look anything up.")


# ---------------------------------------------------------------- helpers
def _case(matter_id, create=False):
    m = db.session.get(Matter, matter_id) or abort(404)
    c = CriminalCase.query.filter_by(matter_id=m.id).first()
    if not c and create:
        c = CriminalCase(matter_id=m.id, stage="arrest")
        db.session.add(c)
        db.session.flush()
        audit("create", "criminal_case", c.id, f"started on {m.number}", current_user().id)
        db.session.commit()
    return m, c


def _setting_label(c):
    return dict(SETTING_TYPES).get(c.next_setting_type or "", (c.next_setting_type or "").replace("_", " ").title()
                                    or "Court setting")


def sync_matter_stage(m, stage_key):
    """Agent O's client-facing stage timeline: when the matter's StageSet has a stage with the same key, mirror the
    criminal stage onto matter.stage. Small hook; the timeline module owns the rest."""
    try:
        ss = m.stage_set
        if not ss:
            return False
        keys = [s.get("key") for s in ss.stages if isinstance(s, dict)]
        if stage_key in keys and m.stage != stage_key:
            m.stage = stage_key
            m.stage_changed_at = now()
            return True
    except Exception as e:  # the timeline is optional; never break a criminal stage change over it
        current_app.logger.warning("stage sync skipped: %s", e)
    return False


def client_status(c, charges=None):
    """One plain-English paragraph a client could read, for the page and the stage timeline."""
    if charges is None:
        charges = Charge.query.filter_by(matter_id=c.matter_id).order_by(Charge.id).all()
    parts = []
    stage = STAGE_LABELS.get(c.stage, c.stage or "").lower()
    if c.stage == "disposed":
        parts.append("The case has been disposed.")
    elif stage:
        parts.append(f"The case is at the {stage} stage.")
    if c.custody_status == "in":
        parts.append("The client is in custody" + (f" with bond set at ${c.bond_cents // 100:,}" if c.bond_cents else "")
                     + (f" ({dict(BOND_STATUSES).get(c.bond_status, c.bond_status).lower()})" if c.bond_status else "")
                     + ".")
    elif c.bond_status:
        parts.append(f"Bond: {dict(BOND_STATUSES).get(c.bond_status, c.bond_status).lower()}"
                     + (f", ${c.bond_cents // 100:,}" if c.bond_cents else "") + ".")
    open_charges = [ch for ch in charges if (ch.disposition or "pending") == "pending"]
    if charges:
        n = len(charges)
        parts.append(f"{n} charge{'' if n == 1 else 's'} on file, {len(open_charges)} still pending.")
    if c.next_setting_on:
        parts.append(f"Next court setting: {_setting_label(c)} on {c.next_setting_on:%B %-d, %Y}"
                     + (f" in {c.court}" if c.court else "") + ".")
    if c.discovery_received_on:
        parts.append(f"Discovery was received on {c.discovery_received_on:%B %-d, %Y}.")
    elif c.stage in ("charged", "discovery", "negotiation"):
        parts.append("Discovery has not been received yet.")
    if c.plea_offer and c.stage in ("negotiation", "pretrial"):
        parts.append("A plea offer is on the table and under review with the client.")
    return " ".join(parts) or "No case facts recorded yet."


def _task_exists(matter_id, title, due_on):
    return Task.query.filter_by(matter_id=matter_id, title=title, due_on=due_on).first() is not None


def _add_task(m, title, due_on, kind="task", notes="", priority="normal"):
    """Create a task unless one with the same title and date exists on the matter. Returns the task or None."""
    if _task_exists(m.id, title, due_on):
        return None
    t = Task(matter_id=m.id, title=title[:300], kind=kind, due_on=due_on, priority=priority,
             assignee_id=m.responsible_user_id, notes=notes)
    db.session.add(t)
    db.session.flush()
    audit("create", "task", t.id, f"criminal: {title} due {due_on}", current_user().id)
    return t


def court_date_chain(m, c, today=None):
    """Tasks from the next setting: the setting (court_date), prepare 7 days before, confirm client appearance 2
    days before, and a discovery request when none has been received. Idempotent by title + date."""
    today = today or date.today()
    if not c.next_setting_on:
        return []
    label = _setting_label(c)
    made = []
    for t in (
        _add_task(m, f"{label}" + (f" ({c.court})" if c.court else ""), c.next_setting_on, kind="court_date",
                  notes=(f"Cause {c.cause_number}. " if c.cause_number else "") + (f"Judge {c.judge}." if c.judge else ""),
                  priority="high"),
        _add_task(m, f"Prepare for {label.lower()}", c.next_setting_on - timedelta(days=7)),
        _add_task(m, "Confirm client appearance", c.next_setting_on - timedelta(days=2),
                  notes=f"{label} on {c.next_setting_on:%b %-d, %Y}."),
    ):
        if t:
            made.append(t)
    if not c.discovery_received_on:
        base = c.next_setting_on if (c.next_setting_type or "") == "arraignment" else today
        t = _add_task(m, "Request discovery", base + timedelta(days=14),
                      notes=(f"Prosecutor: {c.prosecutor}" + (f" <{c.prosecutor_email}>" if c.prosecutor_email else "")
                             if c.prosecutor else "Send the discovery request to the prosecutor."))
        if t:
            made.append(t)
    return made


def speedy_trial_task(m, c):
    if not c.arrest_on:
        return None
    return _add_task(m, SPEEDY_TRIAL_TITLE, c.arrest_on + timedelta(days=SPEEDY_TRIAL_DAYS), kind="deadline",
                     notes=SPEEDY_TRIAL_NOTE, priority="high")


# ---------------------------------------------------------------- PDF
def _txt(s):
    return (str(s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
            .replace("–", "-").replace("—", "-").replace("•", "-")
            .encode("latin-1", "replace").decode("latin-1"))


def _line(pdf, text, size=10.5, style=""):
    pdf.set_font("Helvetica", style, size)
    pdf.multi_cell(0, 5.2, _txt(text), new_x="LMARGIN", new_y="NEXT")


def _save_pdf_document(matter, pdf, name, folder, user):
    data = bytes(pdf.output())
    rel_dir = str(matter.id)
    out_dir = os.path.join(current_app.config["UPLOAD_DIR"], rel_dir)
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{uuid.uuid4().hex}_{secure_filename(name) or 'document.pdf'}"
    with open(os.path.join(out_dir, fname), "wb") as fh:
        fh.write(data)
    doc = Document(matter_id=matter.id, name=name[:300], path=f"{rel_dir}/{fname}", size=len(data),
                   mime="application/pdf", uploaded_by_id=user.id if user else None, folder=folder, extracted_text="")
    db.session.add(doc)
    db.session.flush()
    audit("generate", "document", doc.id, f"{name} for {matter.number}", user.id if user else None)
    return doc


def build_disposition_pdf(m, c, charges):
    firm = Firm.get()
    pdf = DocPDF(firm, f"Disposition summary {m.number}")
    pdf.alias_nb_pages()
    pdf.add_page()
    _line(pdf, "Disposition summary", 14, "B")
    _line(pdf, f"{m.number} {m.name}", 11, "B")
    _line(pdf, f"Client: {m.client.display_name if m.client else ''}")
    if c.court or c.cause_number:
        _line(pdf, f"Court: {c.court or ''}" + (f"    Cause no. {c.cause_number}" if c.cause_number else ""))
    if c.judge or c.prosecutor:
        _line(pdf, (f"Judge: {c.judge}" if c.judge else "") + ("    " if c.judge and c.prosecutor else "")
              + (f"Prosecutor: {c.prosecutor}" if c.prosecutor else ""))
    if c.arrest_on:
        _line(pdf, f"Arrest date: {c.arrest_on:%B %-d, %Y}")
    _line(pdf, f"Stage: {STAGE_LABELS.get(c.stage, c.stage)}    Prepared {date.today():%B %-d, %Y}")
    pdf.ln(3)
    _line(pdf, "Charges", 12, "B")
    if not charges:
        _line(pdf, "No charges recorded.")
    pdf.set_font("Helvetica", "", 9.5)
    if charges:
        with pdf.table(col_widths=(30, 50, 26, 30, 38), text_align=("LEFT", "LEFT", "LEFT", "LEFT", "LEFT"),
                       line_height=5.5, borders_layout="HORIZONTAL_LINES") as t:
            r = t.row()
            for h in ("Statute", "Charge", "Degree", "Disposition", "Sentence"):
                r.cell(h)
            for ch in charges:
                r = t.row()
                r.cell(_txt(ch.statute))
                r.cell(_txt(ch.description + (f" ({ch.enhancement})" if ch.enhancement else "")))
                r.cell(_txt(ch.degree))
                disp = dict(DISPOSITIONS).get(ch.disposition or "pending", ch.disposition or "pending")
                r.cell(_txt(disp + (f" {ch.disposition_on:%m/%d/%Y}" if ch.disposition_on else "")))
                r.cell(_txt(ch.sentence))
    pdf.ln(3)
    _line(pdf, "Client status", 12, "B")
    _line(pdf, client_status(c, charges))
    if c.notes:
        pdf.ln(2)
        _line(pdf, "Notes", 12, "B")
        _line(pdf, c.notes)
    pdf.ln(4)
    _line(pdf, RANGE_NOTE, 8, "I")
    return pdf


# ---------------------------------------------------------------- board
@bp.route("")
@login_required
def index():
    cases = CriminalCase.query.all()
    cols = {k: [] for k in STAGE_KEYS}
    charge_counts = {}
    for ch in Charge.query.all():
        charge_counts[ch.matter_id] = charge_counts.get(ch.matter_id, 0) + 1
    for c in cases:
        if not c.matter:
            continue
        cols.setdefault(c.stage if c.stage in cols else "arrest", []).append(
            {"case": c, "matter": c.matter, "charges": charge_counts.get(c.matter_id, 0),
             "setting": _setting_label(c) if c.next_setting_on else ""})
    for k in cols:
        cols[k].sort(key=lambda r: (r["case"].next_setting_on or date.max, r["matter"].number or ""))
    have = {c.matter_id for c in cases}
    open_matters = [m for m in Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
                    if m.id not in have]
    today = date.today()
    return render_template("criminal/index.html", stages=STAGES, cols=cols, count=len(cases),
                           open_matters=open_matters, today=today, soon=today + timedelta(days=7))


@bp.route("/start", methods=["POST"])
@login_required
def start():
    mid = request.form.get("matter_id", type=int)
    m = db.session.get(Matter, mid) if mid else None
    if not m:
        flash("Pick a matter to start the criminal case on.", "error")
        return redirect(url_for("criminal.index"))
    _case(m.id, create=True)
    flash(f"Criminal case started on {m.number}.", "ok")
    return redirect(url_for("criminal.case", matter_id=m.id))


# ---------------------------------------------------------------- case page
@bp.route("/<int:matter_id>")
@login_required
def case(matter_id):
    m, c = _case(matter_id, create=True)
    charges = Charge.query.filter_by(matter_id=m.id).order_by(Charge.id).all()
    tasks = Task.query.filter_by(matter_id=m.id, done=False).order_by(Task.due_on.is_(None), Task.due_on).all()
    docs = (Document.query.filter_by(matter_id=m.id, folder="Criminal").order_by(Document.created_at.desc())
            .limit(10).all())
    today = date.today()
    return render_template("criminal/case.html", m=m, c=c, charges=charges, tasks=tasks, docs=docs, stages=STAGES,
                           setting_types=SETTING_TYPES, bond_statuses=BOND_STATUSES, custody=CUSTODY,
                           dispositions=DISPOSITIONS, status_text=client_status(c, charges), range_note=RANGE_NOTE,
                           speedy_note=SPEEDY_TRIAL_NOTE, speedy_days=SPEEDY_TRIAL_DAYS, today=today,
                           speedy_on=(c.arrest_on + timedelta(days=SPEEDY_TRIAL_DAYS)) if c.arrest_on else None,
                           stage_synced=bool(m.stage_set and c.stage in [s.get("key") for s in m.stage_set.stages
                                                                          if isinstance(s, dict)]))


@bp.route("/<int:matter_id>/facts", methods=["POST"])
@login_required
def facts(matter_id):
    m, c = _case(matter_id, create=True)
    f = request.form
    old_stage = c.stage
    c.court = (f.get("court") or "").strip()[:200]
    c.cause_number = (f.get("cause_number") or "").strip()[:100]
    c.arrest_on = parse_date(f.get("arrest_on"))
    c.bond_cents = parse_money(f.get("bond"))
    bs = (f.get("bond_status") or "").strip()
    c.bond_status = bs if bs in dict(BOND_STATUSES) else ""
    cs = (f.get("custody_status") or "out").strip()
    c.custody_status = cs if cs in dict(CUSTODY) else "out"
    c.prosecutor = (f.get("prosecutor") or "").strip()[:200]
    c.prosecutor_email = (f.get("prosecutor_email") or "").strip()[:200]
    c.judge = (f.get("judge") or "").strip()[:200]
    c.next_setting_on = parse_date(f.get("next_setting_on"))
    st = (f.get("next_setting_type") or "").strip()
    c.next_setting_type = st if st in dict(SETTING_TYPES) else ""
    c.discovery_received_on = parse_date(f.get("discovery_received_on"))
    c.plea_offer = (f.get("plea_offer") or "").strip()
    stage = (f.get("stage") or c.stage or "arrest").strip()
    c.stage = stage if stage in STAGE_KEYS else c.stage
    c.notes = (f.get("notes") or "").strip()
    if c.court and not m.court:
        m.court = c.court
    if c.cause_number and not m.case_number:
        m.case_number = c.cause_number
    if c.stage != old_stage:
        audit("stage", "criminal_case", c.id, f"{old_stage} -> {c.stage}", current_user().id)
        sync_matter_stage(m, c.stage)
    audit("update", "criminal_case", c.id, "facts", current_user().id)
    db.session.commit()
    flash("Case facts saved.", "ok")
    return redirect(url_for("criminal.case", matter_id=m.id) + "#facts")


# ---------------------------------------------------------------- charges
def _fill_charge(ch, f):
    ch.statute = (f.get("statute") or "").strip()[:120]
    ch.description = (f.get("description") or "").strip()[:300]
    ch.degree = (f.get("degree") or "").strip()[:60]
    ch.range_text = (f.get("range_text") or "").strip()[:200]
    ch.fine_max_cents = parse_money(f.get("fine_max"))
    ch.enhancement = (f.get("enhancement") or "").strip()[:200]
    d = (f.get("disposition") or "pending").strip()
    ch.disposition = d if d in dict(DISPOSITIONS) else "pending"
    ch.disposition_on = parse_date(f.get("disposition_on"))
    ch.sentence = (f.get("sentence") or "").strip()


@bp.route("/<int:matter_id>/charges/new", methods=["GET", "POST"])
@login_required
def charge_new(matter_id):
    m, c = _case(matter_id, create=True)
    ch = Charge(matter_id=m.id, disposition="pending")
    if request.method == "POST":
        _fill_charge(ch, request.form)
        if not ch.description:
            flash("Describe the charge.", "error")
            return render_template("criminal/charge_form.html", m=m, c=c, ch=ch, dispositions=DISPOSITIONS,
                                   range_note=RANGE_NOTE, is_new=True), 400
        db.session.add(ch)
        db.session.flush()
        audit("create", "charge", ch.id, f"{ch.description} on {m.number}", current_user().id)
        db.session.commit()
        flash("Charge added.", "ok")
        return redirect(url_for("criminal.case", matter_id=m.id) + "#charges")
    return render_template("criminal/charge_form.html", m=m, c=c, ch=ch, dispositions=DISPOSITIONS,
                           range_note=RANGE_NOTE, is_new=True)


@bp.route("/<int:matter_id>/charges/<int:id>/edit", methods=["GET", "POST"])
@login_required
def charge_edit(matter_id, id):
    m, c = _case(matter_id, create=True)
    ch = db.session.get(Charge, id) or abort(404)
    if ch.matter_id != m.id:
        abort(404)
    if request.method == "POST":
        _fill_charge(ch, request.form)
        if not ch.description:
            flash("Describe the charge.", "error")
            return render_template("criminal/charge_form.html", m=m, c=c, ch=ch, dispositions=DISPOSITIONS,
                                   range_note=RANGE_NOTE, is_new=False), 400
        audit("update", "charge", ch.id, f"{ch.description} ({ch.disposition})", current_user().id)
        db.session.commit()
        flash("Charge saved.", "ok")
        return redirect(url_for("criminal.case", matter_id=m.id) + "#charges")
    return render_template("criminal/charge_form.html", m=m, c=c, ch=ch, dispositions=DISPOSITIONS,
                           range_note=RANGE_NOTE, is_new=False)


@bp.route("/<int:matter_id>/charges/<int:id>/delete", methods=["POST"])
@login_required
def charge_delete(matter_id, id):
    ch = db.session.get(Charge, id) or abort(404)
    if ch.matter_id != matter_id:
        abort(404)
    audit("delete", "charge", ch.id, ch.description, current_user().id)
    db.session.delete(ch)
    db.session.commit()
    flash("Charge removed.", "ok")
    return redirect(url_for("criminal.case", matter_id=matter_id) + "#charges")


# ---------------------------------------------------------------- actions
@bp.route("/<int:matter_id>/court-chain", methods=["POST"])
@login_required
def court_chain(matter_id):
    m, c = _case(matter_id, create=True)
    if not c.next_setting_on:
        flash("Set the next setting date first.", "error")
        return redirect(url_for("criminal.case", matter_id=m.id) + "#facts")
    made = court_date_chain(m, c)
    db.session.commit()
    if made:
        flash(f"Created {len(made)} task{'' if len(made) == 1 else 's'} from the {_setting_label(c).lower()} on "
              f"{c.next_setting_on:%b %-d}.", "ok")
    else:
        flash("Those tasks already exist for this setting date.", "ok")
    return redirect(url_for("criminal.case", matter_id=m.id) + "#tasks")


@bp.route("/<int:matter_id>/speedy-trial", methods=["POST"])
@login_required
def speedy_trial(matter_id):
    m, c = _case(matter_id, create=True)
    if not c.arrest_on:
        flash("Enter the arrest date first.", "error")
        return redirect(url_for("criminal.case", matter_id=m.id) + "#facts")
    t = speedy_trial_task(m, c)
    db.session.commit()
    if t:
        flash(f"Deadline added for {t.due_on:%b %-d, %Y} (arrest + {SPEEDY_TRIAL_DAYS} days). Confirm the "
              f"jurisdiction's rule.", "ok")
    else:
        flash("That deadline already exists.", "ok")
    return redirect(url_for("criminal.case", matter_id=m.id) + "#tasks")


@bp.route("/<int:matter_id>/disposition-pdf", methods=["POST"])
@login_required
def disposition_pdf(matter_id):
    m, c = _case(matter_id, create=True)
    charges = Charge.query.filter_by(matter_id=m.id).order_by(Charge.id).all()
    pdf = build_disposition_pdf(m, c, charges)
    name = f"Disposition summary {m.number} {date.today():%Y-%m-%d}.pdf"
    doc = _save_pdf_document(m, pdf, name, "Criminal", current_user())
    db.session.commit()
    flash(f"Disposition summary saved to the Criminal folder: {doc.name}.", "ok")
    return redirect(url_for("criminal.case", matter_id=m.id) + "#documents")
