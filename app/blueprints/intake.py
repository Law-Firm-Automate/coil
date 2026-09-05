"""Intake: public lead form, staff lead list, one-step conversion to Contact + Matter + ConflictCheck + Engagement,
the CRM pipeline (stages, value, owner, follow-up date), and follow-up sequences.

Sequences: FollowUpSequence.steps_json is [{"day": 0, "subject": "...", "body": "..."}] with Jinja merge fields
(name, first_name, firm_name, firm_phone, firm_email, matter_type, attorney_name, booking_url). `python -m app.cli
sequences` runs process_lead_sequence() for every active LeadSequence. A due step is emailed only when
Firm.sequences_auto_send is on; otherwise it becomes a draft Message (channel=email, direction=out, status=draft)
listed at /intake/drafts with a Send button. Each step is keyed by Message.provider_id = "lead-seq:<ls>:<step>",
so a re-run never sends or drafts the same step twice.
"""
import json
import time
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, jsonify
from markupsafe import escape
from jinja2.sandbox import SandboxedEnvironment
from jinja2 import TemplateError
from rapidfuzz import fuzz
from ..extensions import db
from ..models import (Firm, User, Contact, Matter, MatterParty, FlatFeeMilestone, ConflictCheck, IntakeLead,
                      LetterTemplate, FollowUpSequence, LeadSequence, Message, audit, now)
from ..helpers import login_required, current_user, client_ip, parse_money, parse_date
from ..services.mail import send_email
from .engagements import build_engagement, send_engagement

bp = Blueprint("intake", __name__, url_prefix="/intake")

MATTER_TYPES = ["Estate Planning", "Business formation", "Litigation", "Family law", "Real estate",
                "Criminal defense", "Personal injury", "Employment", "Immigration", "Other"]
STATUSES = ("new", "contacted", "converted", "declined")
STAGES = [("new", "New"), ("contacted", "Contacted"), ("consult_scheduled", "Consult scheduled"),
          ("proposal", "Proposal"), ("won", "Won"), ("lost", "Lost")]
STAGE_KEYS = [k for k, _ in STAGES]
MERGE_FIELDS = ("name", "first_name", "firm_name", "firm_phone", "firm_email", "matter_type", "attorney_name",
                "booking_url")
MAX_STEPS = 20
FUZZ_THRESHOLD = 80

RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 600  # seconds
_submissions = {}  # ip -> [timestamps]


def _rate_limited(ip):
    t = time.time()
    hits = [h for h in _submissions.get(ip, []) if t - h < RATE_LIMIT_WINDOW]
    if len(hits) >= RATE_LIMIT_MAX:
        _submissions[ip] = hits
        return True
    hits.append(t)
    _submissions[ip] = hits
    return False


def age_str(dt):
    if not dt:
        return ""
    s = int((now() - dt).total_seconds())
    if s < 3600:
        return f"{max(1, s // 60)}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


# ---------------------------------------------------------------------------
# Fuzzy conflict preview (small local copy of what the conflicts module does)
# ---------------------------------------------------------------------------
def _candidates(exclude_contact_id=None):
    out = []
    for c in Contact.query.all():
        if exclude_contact_id and c.id == exclude_contact_id:
            continue
        names = [c.display_name] + [a.strip() for a in (c.aliases or "").splitlines() if a.strip()]
        for n in names:
            out.append(dict(name=n, kind="contact", id=c.id, url=f"/contacts/{c.id}",
                            label=f"{c.display_name}" + (" (client)" if c.is_client else "")))
    for p in MatterParty.query.all():
        out.append(dict(name=p.name, kind="party", id=p.matter_id, url=f"/matters/{p.matter_id}",
                        label=f"{p.role} on {p.matter.label if p.matter else 'matter'}"))
    for m in Matter.query.all():
        out.append(dict(name=m.name, kind="matter", id=m.id, url=f"/matters/{m.id}", label=m.label))
    return out


def fuzzy_hits(names, exclude_contact_id=None):
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return []
    cands = _candidates(exclude_contact_id)
    hits = []
    seen = set()
    for q in names:
        for c in cands:
            score = fuzz.token_set_ratio(q.lower(), (c["name"] or "").lower())
            if score >= FUZZ_THRESHOLD:
                key = (q, c["kind"], c["id"], c["name"])
                if key in seen:
                    continue
                seen.add(key)
                hits.append(dict(query=q, match=c["name"], kind=c["kind"], id=c["id"], url=c["url"],
                                 label=c["label"], score=int(score)))
    hits.sort(key=lambda h: -h["score"])
    return hits


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
@bp.route("/form")
def form():
    return render_template("intake/form.html", types=MATTER_TYPES, source=request.args.get("source", "web")[:100],
                           values={})


@bp.route("/submit", methods=["POST"])
def submit():
    f = request.form
    if f.get("website", "").strip():
        # honeypot filled: bots get a thank-you and nothing is stored
        return render_template("intake/thanks.html")
    ip = client_ip()
    if _rate_limited(ip):
        return render_template("intake/thanks.html", limited=True), 429
    name = f.get("name", "").strip()
    if not name:
        flash("Please tell us your name.", "error")
        return render_template("intake/form.html", types=MATTER_TYPES, source=f.get("source", "web")[:100],
                               values=f), 400
    mtype = f.get("matter_type", "").strip()
    if mtype == "Other" and f.get("matter_type_other", "").strip():
        mtype = f.get("matter_type_other", "").strip()[:100]
    lead = IntakeLead(name=name[:200], email=f.get("email", "").strip()[:200], phone=f.get("phone", "").strip()[:50],
                      matter_type=mtype[:100], description=f.get("description", "").strip(),
                      adverse_party=f.get("adverse_party", "").strip()[:300],
                      source=(f.get("source", "web").strip() or "web")[:100])
    db.session.add(lead)
    db.session.flush()
    _score(lead)
    audit("create", "intake_lead", lead.id, f"{lead.name} via {lead.source} from {ip}")
    db.session.commit()
    firm = Firm.get()
    to = firm.email or current_app.config["MAIL_FROM"]
    link = f"{current_app.config['BASE_URL']}/intake/{lead.id}"
    html = (f"<p>New intake lead from the {lead.source} form.</p>"
            f"<p><strong>{_esc(lead.name)}</strong><br>{_esc(lead.email)}<br>{_esc(lead.phone)}</p>"
            f"<p>Matter type: {_esc(lead.matter_type) or 'not given'}<br>Other party: {_esc(lead.adverse_party) or 'none given'}</p>"
            f"<p>{_esc(lead.description).replace(chr(10), '<br>')}</p><p><a href='{link}'>Open the lead</a></p>")
    send_email(to, f"New intake lead: {lead.name} ({lead.matter_type or 'no type'})", html,
               text=f"New lead {lead.name}. Open: {link}", reply_to=lead.email or None)
    return render_template("intake/thanks.html", booking_url=current_app.config.get("BOOKING_URL", ""))


def _esc(s):
    from markupsafe import escape
    return str(escape(s or ""))


def _score(lead):
    """Agent N: deterministic case score on create and update. Never blocks the intake flow."""
    try:
        from .caseaudit import score_lead
        score_lead(lead)
    except Exception as e:  # scoring is a convenience; a bug there must not lose a lead
        current_app.logger.warning("lead scoring failed: %s", e)


@bp.route("/embed")
@login_required
def embed():
    url = f"{current_app.config['BASE_URL']}/intake/form"
    snippet = (f'<iframe src="{url}?source=website" style="width:100%;min-height:760px;border:0" '
               f'title="Contact {Firm.get().name}"></iframe>')
    return render_template("intake/embed.html", url=url, snippet=snippet)


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------
@bp.route("")
@login_required
def index():
    status = request.args.get("status", "new")
    q = IntakeLead.query
    if status in STATUSES:
        q = q.filter_by(status=status)
    rows = q.order_by(IntakeLead.created_at.desc()).all()
    counts = {s: IntakeLead.query.filter_by(status=s).count() for s in STATUSES}
    drafts = Message.query.filter_by(status="draft", direction="out", channel="email").count()
    return render_template("intake/index.html", rows=rows, status=status, counts=counts, age=age_str,
                           stages=dict(STAGES), drafts=drafts)


# ---------------------------------------------------------------------------
# CRM pipeline
# ---------------------------------------------------------------------------
@bp.route("/pipeline")
@login_required
def pipeline():
    leads = IntakeLead.query.order_by(IntakeLead.next_follow_up_on.asc().nulls_last(),
                                      IntakeLead.created_at.desc()).all()
    cols = {k: [] for k in STAGE_KEYS}
    for l in leads:
        cols[l.stage if l.stage in cols else "new"].append(l)
    values = {k: sum(l.value_cents or 0 for l in v) for k, v in cols.items()}
    return render_template("intake/pipeline.html", stages=STAGES, cols=cols, values=values, age=age_str,
                           today=date.today())


def _set_stage(lead, stage):
    """Move a lead between pipeline stages, keeping the older status field in step."""
    lead.stage = stage
    if lead.status != "converted":
        if stage == "lost":
            lead.status = "declined"
        elif stage == "new":
            lead.status = "new"
        else:
            lead.status = "contacted"
    if stage != "lost":
        lead.lost_reason = ""


@bp.route("/<int:id>/stage", methods=["POST"])
@login_required
def stage(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    s = request.form.get("stage", "")
    wants_json = request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get(
        "Accept", "")
    if s not in STAGE_KEYS:
        if wants_json:
            return jsonify(ok=False, error="Unknown stage."), 400
        flash("Unknown stage.", "error")
        return redirect(url_for("intake.pipeline"))
    if lead.status == "converted" and s != "won":
        msg = "This lead was converted into a matter, so it stays in Won."
        if wants_json:
            return jsonify(ok=False, error=msg), 400
        flash(msg, "error")
        return redirect(url_for("intake.pipeline"))
    old = lead.stage
    _set_stage(lead, s)
    if s == "lost" and request.form.get("lost_reason"):
        lead.lost_reason = request.form.get("lost_reason", "").strip()[:200]
    audit("stage", "intake_lead", lead.id, f"{old} -> {s}", current_user().id)
    db.session.commit()
    if wants_json:
        return jsonify(ok=True, stage=s, status=lead.status)
    flash(f"Moved {lead.name} to {dict(STAGES)[s]}." + (" Convert the lead to open the matter." if s == "won" and
                                                          lead.status != "converted" else ""), "ok")
    return redirect(request.form.get("next") or url_for("intake.pipeline"))


@bp.route("/<int:id>/fields", methods=["POST"])
@login_required
def fields(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    f = request.form
    lead.value_cents = parse_money(f.get("value"))
    lead.assigned_user_id = f.get("assigned_user_id", type=int) or None
    lead.next_follow_up_on = parse_date(f.get("next_follow_up_on"))
    lead.lost_reason = f.get("lost_reason", "").strip()[:200]
    _score(lead)
    db.session.commit()
    flash("Lead updated.", "ok")
    return redirect(url_for("intake.detail", id=lead.id))


# ---------------------------------------------------------------------------
# Follow-up sequences
# ---------------------------------------------------------------------------
_jinja = SandboxedEnvironment(autoescape=False)

SAMPLE_SEQUENCE = dict(name="New lead follow-up (3 touches)", steps=[
    dict(day=0, subject="Thanks for reaching out to {{ firm_name }}",
         body="Hi {{ first_name }},\n\nThank you for contacting {{ firm_name }} about your {{ matter_type }} "
              "question. I have your details and will review them today.\n\n"
              "{% if booking_url %}If you would like to talk sooner, you can pick a time here: {{ booking_url }}\n\n{% endif %}"
              "{{ attorney_name }}\n{{ firm_name }}{% if firm_phone %}\n{{ firm_phone }}{% endif %}"),
    dict(day=3, subject="Following up on your {{ matter_type }} question",
         body="Hi {{ first_name }},\n\nI wanted to check whether you still need help with your {{ matter_type }} "
              "matter. If so, reply to this email or call {{ firm_phone or 'the office' }} and we will find a time.\n\n"
              "{{ attorney_name }}\n{{ firm_name }}"),
    dict(day=7, subject="Last note from {{ firm_name }}",
         body="Hi {{ first_name }},\n\nThis is my last follow-up. If the timing is not right, no problem at all; "
              "keep this email and reach out whenever you are ready.\n\n{{ attorney_name }}\n{{ firm_name }}"),
])


def merge_context(lead):
    firm = Firm.get()
    first = (lead.name or "").strip().split()[0] if (lead.name or "").strip() else ""
    attorney = lead.assigned_user or (lead.matter.responsible if lead.matter else None)
    return dict(name=lead.name or "", first_name=first, firm_name=firm.name or "", firm_phone=firm.phone or "",
                firm_email=firm.email or "", matter_type=lead.matter_type or "your legal",
                attorney_name=attorney.name if attorney else (firm.name or ""),
                booking_url=current_app.config.get("BOOKING_URL", "") or "")


def render_step(lead, step):
    """Return (subject, body) with merge fields filled in. A broken template falls back to the raw text."""
    ctx = merge_context(lead)
    out = []
    for key in ("subject", "body"):
        raw = str(step.get(key) or "")
        try:
            out.append(_jinja.from_string(raw).render(**ctx).strip())
        except TemplateError:
            out.append(raw)
    return out[0], out[1]


def _validate_steps(steps):
    for s in steps:
        for key in ("subject", "body"):
            try:
                _jinja.from_string(str(s.get(key) or ""))
            except TemplateError as e:
                return f"Step on day {s.get('day')} has a template syntax error in its {key}: {e}"
    return None


def _steps_from_form(f):
    steps = []
    for i in range(MAX_STEPS):
        subj = (f.get(f"step_subject_{i}") or "").strip()
        body = (f.get(f"step_body_{i}") or "").strip()
        if not subj and not body:
            continue
        try:
            day = max(0, int(f.get(f"step_day_{i}") or 0))
        except ValueError:
            day = 0
        steps.append(dict(day=day, subject=subj[:300], body=body[:20000]))
    steps.sort(key=lambda s: s["day"])
    return steps


def _ensure_sample_sequence():
    if FollowUpSequence.query.count() == 0:
        db.session.add(FollowUpSequence(name=SAMPLE_SEQUENCE["name"], steps_json=json.dumps(SAMPLE_SEQUENCE["steps"])))
        db.session.commit()


@bp.route("/sequences")
@login_required
def sequences():
    _ensure_sample_sequence()
    rows = FollowUpSequence.query.order_by(FollowUpSequence.is_active.desc(), FollowUpSequence.name).all()
    running = {r.id: LeadSequence.query.filter_by(sequence_id=r.id, status="active").count() for r in rows}
    return render_template("intake/sequences.html", rows=rows, running=running, firm_settings=Firm.get(),
                           booking_url=current_app.config.get("BOOKING_URL", ""))


def _sequence_form(seq, is_new):
    return render_template("intake/sequence_form.html", seq=seq, is_new=is_new, steps=seq.steps if seq else [],
                           merge_fields=MERGE_FIELDS, max_steps=MAX_STEPS)


@bp.route("/sequences/new", methods=["GET", "POST"])
@login_required
def sequence_new():
    if request.method == "POST":
        f = request.form
        steps = _steps_from_form(f)
        name = f.get("name", "").strip()[:200]
        err = None if name else "Give the sequence a name."
        err = err or (None if steps else "Add at least one step.") or _validate_steps(steps)
        seq = FollowUpSequence(name=name, steps_json=json.dumps(steps), is_active=f.get("is_active", "1") == "1")
        if err:
            flash(err, "error")
            return _sequence_form(seq, True), 400
        db.session.add(seq)
        db.session.flush()
        audit("create", "follow_up_sequence", seq.id, f"{seq.name}, {len(steps)} steps", current_user().id)
        db.session.commit()
        flash(f"Sequence {seq.name} created.", "ok")
        return redirect(url_for("intake.sequences"))
    return _sequence_form(None, True)


@bp.route("/sequences/<int:id>/edit", methods=["GET", "POST"])
@login_required
def sequence_edit(id):
    seq = db.session.get(FollowUpSequence, id) or abort(404)
    if request.method == "POST":
        f = request.form
        steps = _steps_from_form(f)
        name = f.get("name", "").strip()[:200]
        err = None if name else "Give the sequence a name."
        err = err or (None if steps else "Add at least one step.") or _validate_steps(steps)
        if err:
            flash(err, "error")
            seq.name, seq.steps_json = name, json.dumps(steps)
            return _sequence_form(seq, False), 400
        seq.name, seq.steps_json, seq.is_active = name, json.dumps(steps), f.get("is_active") == "1"
        audit("update", "follow_up_sequence", seq.id, f"{seq.name}, {len(steps)} steps", current_user().id)
        db.session.commit()
        flash("Sequence saved. Leads already on it keep counting from their start date.", "ok")
        return redirect(url_for("intake.sequences"))
    return _sequence_form(seq, False)


@bp.route("/sequences/<int:id>/delete", methods=["POST"])
@login_required
def sequence_delete(id):
    seq = db.session.get(FollowUpSequence, id) or abort(404)
    used = LeadSequence.query.filter_by(sequence_id=seq.id).count()
    if used:
        seq.is_active = False
        for ls in LeadSequence.query.filter_by(sequence_id=seq.id, status="active").all():
            ls.status = "stopped"
        audit("update", "follow_up_sequence", seq.id, f"{seq.name} deactivated, {used} lead(s) stopped",
              current_user().id)
        db.session.commit()
        flash(f"{seq.name} was used by {used} lead(s), so it was deactivated and those runs stopped.", "ok")
        return redirect(url_for("intake.sequences"))
    name = seq.name
    db.session.delete(seq)
    audit("delete", "follow_up_sequence", id, name, current_user().id)
    db.session.commit()
    flash(f"Deleted {name}.", "ok")
    return redirect(url_for("intake.sequences"))


@bp.route("/<int:id>/sequence/start", methods=["POST"])
@login_required
def sequence_start(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    seq = db.session.get(FollowUpSequence, request.form.get("sequence_id", type=int) or 0)
    if not seq or not seq.is_active:
        flash("Pick an active sequence.", "error")
        return redirect(url_for("intake.detail", id=lead.id))
    if not lead.email:
        flash("This lead has no email address, so a sequence cannot be started.", "error")
        return redirect(url_for("intake.detail", id=lead.id))
    if LeadSequence.query.filter_by(lead_id=lead.id, sequence_id=seq.id, status="active").first():
        flash(f"{lead.name} is already on {seq.name}.", "error")
        return redirect(url_for("intake.detail", id=lead.id))
    ls = LeadSequence(lead_id=lead.id, sequence_id=seq.id, started_on=parse_date(request.form.get("started_on"),
                                                                               date.today()), next_step=0)
    db.session.add(ls)
    db.session.flush()
    audit("start", "lead_sequence", ls.id, f"{seq.name} on lead #{lead.id}", current_user().id)
    db.session.commit()
    auto = Firm.get().sequences_auto_send
    flash(f"Started {seq.name}. The day-0 step is {'sent' if auto else 'drafted for your review'} the next time "
          f"python -m app.cli sequences runs.", "ok")
    return redirect(url_for("intake.detail", id=lead.id))


@bp.route("/<int:id>/sequence/<int:lsid>/stop", methods=["POST"])
@login_required
def sequence_stop(id, lsid):
    ls = db.session.get(LeadSequence, lsid) or abort(404)
    if ls.lead_id != id:
        abort(404)
    ls.status = "stopped"
    audit("stop", "lead_sequence", ls.id, f"stopped at step {ls.next_step}", current_user().id)
    db.session.commit()
    flash("Sequence stopped.", "ok")
    return redirect(url_for("intake.detail", id=id))


def _step_key(ls, index):
    return f"lead-seq:{ls.id}:{index}"


def _body_html(body):
    return "".join(f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in body.split("\n\n") if p.strip())


def process_lead_sequence(ls, today, auto_send):
    """Send or draft every step of one lead's sequence whose date has arrived. Returns (sent, drafted).

    Idempotent: next_step only moves forward and each step's Message carries a unique provider_id key.
    """
    lead = ls.lead
    steps = sorted(ls.sequence.steps if ls.sequence else [], key=lambda s: int(s.get("day", 0) or 0))
    if ls.status != "active":
        return 0, 0
    if lead is None or lead.stage in ("won", "lost") or lead.status in ("converted", "declined"):
        ls.status = "stopped"
        audit("stop", "lead_sequence", ls.id, f"lead is {lead.stage if lead else 'gone'}")
        db.session.commit()
        return 0, 0
    firm = Firm.get()
    sent = drafted = 0
    while ls.next_step < len(steps):
        step = steps[ls.next_step]
        due = (ls.started_on or today) + timedelta(days=int(step.get("day", 0) or 0))
        if due > today:
            break
        if not lead.email:
            ls.status = "stopped"
            audit("stop", "lead_sequence", ls.id, "lead has no email")
            break
        key = _step_key(ls, ls.next_step)
        if not Message.query.filter_by(provider_id=key).first():
            subject, body = render_step(lead, step)
            msg = Message(contact_id=lead.contact_id, matter_id=lead.matter_id, direction="out", channel="email",
                          to_addr=lead.email, from_addr=firm.email or current_app.config["MAIL_FROM"],
                          subject=subject, body=body, provider_id=key, status="draft")
            db.session.add(msg)
            db.session.flush()
            if auto_send:
                send_email(lead.email, subject, _body_html(body), text=body, reply_to=firm.email or None)
                msg.status = "sent"
                audit("send", "message", msg.id, f"sequence step {ls.next_step} to {lead.email} (auto-send on)")
                sent += 1
            else:
                audit("draft", "message", msg.id, f"sequence step {ls.next_step} drafted for {lead.email}")
                drafted += 1
        ls.next_step += 1
        db.session.commit()
    if ls.status == "active" and ls.next_step >= len(steps):
        ls.status = "done"
    db.session.commit()
    return sent, drafted


# ---------------------------------------------------------------------------
# Drafts (sequence steps waiting for a human to send)
# ---------------------------------------------------------------------------
def _draft_lead(msg):
    """Find the lead behind a sequence draft via its provider_id key."""
    pid = msg.provider_id or ""
    if pid.startswith("lead-seq:"):
        try:
            ls = db.session.get(LeadSequence, int(pid.split(":")[1]))
            return ls.lead if ls else None
        except (ValueError, IndexError):
            return None
    return None


@bp.route("/drafts")
@login_required
def drafts():
    rows = Message.query.filter_by(status="draft", direction="out", channel="email").order_by(
        Message.created_at.desc()).all()
    recent = Message.query.filter(Message.provider_id.like("lead-seq:%"), Message.status == "sent").order_by(
        Message.created_at.desc()).limit(20).all()
    firm = Firm.get()
    return render_template("intake/drafts.html", rows=[(m, _draft_lead(m)) for m in rows],
                           recent=[(m, _draft_lead(m)) for m in recent], auto=firm.sequences_auto_send)


@bp.route("/drafts/<int:id>/send", methods=["POST"])
@login_required
def draft_send(id):
    msg = db.session.get(Message, id) or abort(404)
    if msg.status != "draft":
        flash("This message is not a draft.", "error")
        return redirect(url_for("intake.drafts"))
    subject = request.form.get("subject", msg.subject or "").strip()[:300] or msg.subject
    body = request.form.get("body", msg.body or "").strip() or msg.body
    to = request.form.get("to", msg.to_addr or "").strip()[:200] or msg.to_addr
    if not to:
        flash("No recipient address.", "error")
        return redirect(url_for("intake.drafts"))
    firm = Firm.get()
    send_email(to, subject, _body_html(body), text=body, reply_to=firm.email or None)
    msg.subject, msg.body, msg.to_addr, msg.status = subject, body, to, "sent"
    msg.created_at = now()
    audit("send", "message", msg.id, f"draft sent to {to}", current_user().id)
    db.session.commit()
    flash(f"Sent to {to}.", "ok")
    return redirect(url_for("intake.drafts"))


@bp.route("/drafts/<int:id>/delete", methods=["POST"])
@login_required
def draft_delete(id):
    msg = db.session.get(Message, id) or abort(404)
    if msg.status != "draft":
        flash("This message is not a draft.", "error")
        return redirect(url_for("intake.drafts"))
    audit("delete", "message", msg.id, f"draft to {msg.to_addr} discarded", current_user().id)
    db.session.delete(msg)
    db.session.commit()
    flash("Draft discarded. The sequence moves on to its next step.", "ok")
    return redirect(url_for("intake.drafts"))


def _next_matter_number():
    """Take Firm.next_matter_number, skipping any number already in use (seed data hardcodes a few)."""
    firm = Firm.get()
    n = firm.next_matter_number or 1001
    prefix = firm.matter_prefix or ""
    while Matter.query.filter_by(number=f"{prefix}{n}").first():
        n += 1
    firm.next_matter_number = n + 1
    return f"{prefix}{n}"


def _split_name(name):
    parts = (name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


@bp.route("/<int:id>")
@login_required
def detail(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    hits = fuzzy_hits([lead.name, lead.adverse_party])
    first, last = _split_name(lead.name)
    email_match = Contact.query.filter(db.func.lower(Contact.email) == lead.email.lower()).first() if lead.email else None
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    templates = LetterTemplate.query.filter_by(kind="engagement").order_by(LetterTemplate.is_default.desc(),
                                                                          LetterTemplate.name).all()
    firm = Firm.get()
    defaults = dict(
        first_name=first, last_name=last,
        matter_name=f"{last or first} - {lead.matter_type}".strip(" -") if lead.matter_type else (last or first),
        practice_area=lead.matter_type, responsible_user_id=current_user().id,
        hourly_rate=f"{firm.default_rate_cents / 100:.2f}", scope=lead.description,
    )
    seqs = FollowUpSequence.query.filter_by(is_active=True).order_by(FollowUpSequence.name).all()
    runs = LeadSequence.query.filter_by(lead_id=lead.id).order_by(LeadSequence.created_at.desc()).all()
    step_msgs = {m.provider_id: m for m in Message.query.filter(Message.provider_id.like("lead-seq:%")).all()}
    run_rows = []
    for r in runs:
        steps = r.sequence.steps if r.sequence else []
        run_rows.append(dict(run=r, steps=[(i, s, step_msgs.get(_step_key(r, i))) for i, s in enumerate(steps)]))
    return render_template("intake/detail.html", lead=lead, hits=hits, email_match=email_match, users=users,
                           templates=templates, defaults=defaults, age=age_str, types=MATTER_TYPES,
                           stages=STAGES, sequences=seqs, run_rows=run_rows, today=date.today())


@bp.route("/<int:id>/convert", methods=["POST"])
@login_required
def convert(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    u = current_user()
    f = request.form
    if lead.status == "converted":
        flash("This lead was already converted.", "error")
        return redirect(url_for("intake.detail", id=lead.id))

    # 1. contact
    mode = f.get("contact_mode", "new")
    if mode == "existing":
        contact = db.session.get(Contact, f.get("contact_id", type=int) or 0)
        if not contact:
            flash("Pick an existing contact or create a new one.", "error")
            return redirect(url_for("intake.detail", id=lead.id))
        contact.is_client = True
        if not contact.email and lead.email:
            contact.email = lead.email
        if not contact.phone and lead.phone:
            contact.phone = lead.phone
        contact_created = False
    else:
        first, last = f.get("first_name", "").strip(), f.get("last_name", "").strip()
        if not (first or last):
            first, last = _split_name(lead.name)
        contact = Contact(kind="person", first_name=first, last_name=last, email=f.get("email", lead.email).strip(),
                          phone=f.get("phone", lead.phone).strip(), address=f.get("address", "").strip(),
                          is_client=True, notes=f"From intake lead #{lead.id} ({lead.source})")
        db.session.add(contact)
        db.session.flush()
        contact_created = True

    # 2. conflict search, before the new matter and party exist
    adverse = f.get("adverse_party", lead.adverse_party).strip()
    query_names = [lead.name, contact.display_name, adverse]
    hits = fuzzy_hits(query_names, exclude_contact_id=contact.id)

    # 3. matter
    number = _next_matter_number()
    billing = f.get("billing_type", "flat")
    if billing not in ("flat", "hourly", "contingency", "hybrid"):
        billing = "flat"
    matter = Matter(number=number, client_id=contact.id, name=f.get("matter_name", "").strip() or lead.name,
                    practice_area=f.get("practice_area", "").strip() or lead.matter_type, status="open",
                    opened_on=date.today(), responsible_user_id=f.get("responsible_user_id", type=int) or u.id,
                    billing_type=billing, description=f.get("description", lead.description).strip(),
                    sol_date=parse_date(f.get("sol_date")), sol_basis=f.get("sol_basis", "").strip())
    flat = parse_money(f.get("flat_fee")) if billing in ("flat", "hybrid") else 0
    matter.flat_fee_cents = flat
    matter.hourly_rate_cents = parse_money(f.get("hourly_rate")) if billing in ("hourly", "hybrid") else 0
    try:
        matter.contingency_pct = float(f.get("contingency_pct") or 0) if billing in ("contingency", "hybrid") else 0.0
    except ValueError:
        matter.contingency_pct = 0.0
    db.session.add(matter)
    db.session.flush()

    # 4. milestones
    milestones_made = 0
    if flat and f.get("split_milestones") == "1":
        m1 = parse_money(f.get("milestone1_amount"))
        m2 = parse_money(f.get("milestone2_amount"))
        if not m1 and not m2:
            m1 = flat // 2
            m2 = flat - m1
        elif not m2:
            m2 = max(0, flat - m1)
        elif not m1:
            m1 = max(0, flat - m2)
        db.session.add(FlatFeeMilestone(matter_id=matter.id, description=f.get("milestone1_desc", "").strip() or "Retainer on signing",
                                        amount_cents=m1, sort=0, due_on=parse_date(f.get("milestone1_due"))))
        db.session.add(FlatFeeMilestone(matter_id=matter.id, description=f.get("milestone2_desc", "").strip() or "Balance",
                                        amount_cents=m2, sort=1, due_on=parse_date(f.get("milestone2_due"))))
        milestones_made = 2

    # 5. adverse party
    if adverse:
        db.session.add(MatterParty(matter_id=matter.id, name=adverse, role="adverse", notes="From intake"))

    # 6. conflict check record
    check = ConflictCheck(run_by_id=u.id, query="\n".join(n for n in [lead.name, adverse] if n),
                          results_json=json.dumps(hits), matter_id=matter.id, contact_id=contact.id,
                          outcome="unresolved" if hits else "clear",
                          notes=f"Run automatically when converting intake lead #{lead.id}.")
    db.session.add(check)
    db.session.flush()

    # 7. lead
    lead.status = "converted"
    lead.stage = "won"
    lead.lost_reason = ""
    lead.contact_id = contact.id
    lead.matter_id = matter.id
    lead.conflict_check_id = check.id

    audit("create", "contact" if contact_created else "contact_link", contact.id, f"intake lead #{lead.id}", u.id)
    audit("create", "matter", matter.id, f"{matter.number} {matter.name} from intake lead #{lead.id}", u.id)
    audit("create", "conflict_check", check.id, f"{check.outcome}, {len(hits)} hit(s)", u.id)
    audit("convert", "intake_lead", lead.id, f"contact {contact.id}, matter {matter.id}", u.id)

    # 8. engagement letter
    engagement = None
    if f.get("send_engagement") == "1":
        db.session.expire(matter, ["milestones", "client", "responsible"])
        template = db.session.get(LetterTemplate, f.get("template_id", type=int) or 0)
        engagement = build_engagement(matter, template, scope=f.get("scope", "").strip(), user=u)
        send_engagement(engagement, u)

    db.session.commit()

    bits = [f"{'Created' if contact_created else 'Linked'} client {contact.display_name}",
            f"opened matter {matter.number}"]
    if milestones_made:
        bits.append(f"{milestones_made} fee milestones")
    if adverse:
        bits.append(f"adverse party {adverse}")
    bits.append("conflict check " + ("found %d possible hit(s), review it" % len(hits) if hits else "clear"))
    if engagement:
        bits.append("engagement letter sent to " + (engagement.sent_to or "nobody (no email)"))
    flash(". ".join(bits) + ".", "ok" if not hits else "")
    return redirect(f"/matters/{matter.id}")


@bp.route("/<int:id>/decline", methods=["POST"])
@login_required
def decline(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    reason = request.form.get("reason", "").strip()[:200]
    lead.status = "declined"
    lead.stage = "lost"
    if reason:
        lead.lost_reason = reason
    for ls in LeadSequence.query.filter_by(lead_id=lead.id, status="active").all():
        ls.status = "stopped"
    audit("decline", "intake_lead", lead.id, reason, current_user().id)
    db.session.commit()
    flash(f"Declined {lead.name}.", "ok")
    return redirect(url_for("intake.index"))


@bp.route("/<int:id>/status", methods=["POST"])
@login_required
def status(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    s = request.form.get("status", "")
    if s not in ("new", "contacted", "declined"):
        flash("Unknown status.", "error")
        return redirect(url_for("intake.detail", id=lead.id))
    if lead.status == "converted":
        flash("Converted leads keep their status.", "error")
        return redirect(url_for("intake.detail", id=lead.id))
    lead.status = s
    if s == "declined":
        lead.stage = "lost"
    elif s == "new":
        lead.stage = "new"
    elif lead.stage in ("new", "lost"):
        lead.stage = "contacted"
    db.session.commit()
    flash(f"Marked {lead.name} as {s}.", "ok")
    return redirect(url_for("intake.detail", id=lead.id))
