"""Engagement letters: templates, click-to-sign, open tracking, signed PDF with an audit block.

Also exposes build_engagement() and send_engagement() for the intake module.
Public routes (/sign/..., /track/engagement/...) live here too, so the blueprint has no url_prefix.
"""
import hashlib
import io
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, send_file, Response
from jinja2 import Environment, TemplateSyntaxError
from markupsafe import Markup, escape
from ..extensions import db
from ..models import (Firm, Matter, LetterTemplate, Engagement, EngagementEvent, new_token, audit, now)
from ..helpers import login_required, current_user, client_ip, cents_to_str
from ..services.mail import send_email
from ..services import pdf as pdfsvc

bp = Blueprint("engagements", __name__)

MERGE_FIELDS = [
    ("firm_name", "Firm name"), ("firm_address", "Firm address (line breaks kept)"), ("firm_phone", "Firm phone"),
    ("firm_email", "Firm email"), ("attorney_name", "Responsible attorney, or the sender"),
    ("date", "Today's date, e.g. September 3, 2026"), ("client_name", "Client full or company name"),
    ("client_first_name", "Client first name"), ("client_address", "Client address (line breaks kept)"),
    ("client_email", "Client email"), ("matter_name", "Matter name"), ("matter_number", "Matter number"),
    ("practice_area", "Practice area"), ("scope", "Scope of work typed when the letter is created"),
    ("fee_summary", "Built from the matter's billing type: flat fee with milestones, hourly rate, contingency, or both"),
    ("retainer_amount", "First milestone amount, or the flat fee"),
]

FALLBACK_BODY = """<p>{{ date }}</p><p>{{ client_name }}<br>{{ client_address }}</p>
<p>Re: {{ matter_name }}</p><p>Dear {{ client_first_name }},</p>
<p>Thank you for choosing {{ firm_name }}. This letter confirms the terms on which we will represent you.</p>
<h3>Scope</h3><p>{{ scope }}</p><h3>Fees</h3><p>{{ fee_summary }}</p>
<p>By signing below you confirm that you have read this letter and agree to its terms.</p>
<p>Sincerely,<br>{{ attorney_name }}<br>{{ firm_name }}</p>"""

ONE_PX_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
              b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")

VIEW_DEDUPE_SECONDS = 60


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _html_env():
    return Environment(autoescape=True)


def _text_env():
    return Environment(autoescape=False)


def _nl2br(s):
    return Markup("<br>".join(str(escape(line)) for line in (s or "").splitlines()))


def fee_summary(matter):
    parts = []
    bt = matter.billing_type or "flat"
    if bt in ("flat", "hybrid") and (matter.flat_fee_cents or matter.milestones):
        s = f"A flat fee of {cents_to_str(matter.flat_fee_cents)}"
        if matter.milestones:
            ms = "; ".join(f"{m.description} {cents_to_str(m.amount_cents)}" +
                           (f" (due {m.due_on.strftime('%B %-d, %Y')})" if m.due_on else "") for m in matter.milestones)
            s += f", payable as follows: {ms}"
        parts.append(s + ".")
    if bt in ("hourly", "hybrid"):
        parts.append(f"{cents_to_str(matter.effective_rate_cents())} per hour billed in 0.1 hour increments, "
                     f"invoiced monthly.")
    if bt in ("contingency", "hybrid") and matter.contingency_pct:
        pct = matter.contingency_pct
        pct_s = f"{pct:g}"
        parts.append(f"{pct_s}% of any recovery.")
    if not parts:
        parts.append("Fees as agreed in writing between the firm and the client.")
    return " ".join(parts)


def retainer_cents(matter):
    if matter.milestones:
        return matter.milestones[0].amount_cents or 0
    return matter.flat_fee_cents or 0


def merge_context(matter, scope="", user=None, extra=None):
    f = Firm.get()
    c = matter.client
    attorney = matter.responsible or user
    ctx = dict(
        firm_name=f.name or "", firm_address=_nl2br(f.address), firm_phone=f.phone or "", firm_email=f.email or "",
        attorney_name=(attorney.name if attorney else f.name) or "",
        date=date.today().strftime("%B %-d, %Y"),
        client_name=c.display_name, client_first_name=(c.first_name or c.display_name),
        client_address=_nl2br(c.address), client_email=c.email or "",
        matter_name=matter.name or "", matter_number=matter.number or "", practice_area=matter.practice_area or "",
        scope=scope or matter.description or "", fee_summary=fee_summary(matter),
        retainer_amount=cents_to_str(retainer_cents(matter)),
    )
    if extra:
        ctx.update(extra)
    return ctx


def default_template():
    t = LetterTemplate.query.filter_by(kind="engagement", is_default=True).first()
    if not t:
        t = LetterTemplate.query.filter_by(kind="engagement").order_by(LetterTemplate.id).first()
    return t


def render_letter(template, matter, scope="", user=None, extra=None):
    """Returns (subject, body_html). template may be None (built-in fallback)."""
    ctx = merge_context(matter, scope, user, extra)
    subject_src = (template.subject if template and template.subject else "Engagement letter: {{ matter_name }}")
    body_src = template.body_html if template and template.body_html else FALLBACK_BODY
    subject = _text_env().from_string(subject_src).render(**ctx)
    body = _html_env().from_string(body_src).render(**ctx)
    return subject.strip(), body


def build_engagement(matter, template=None, scope="", extra=None, user=None):
    """Create a draft Engagement for the matter's client. Does not commit."""
    if template is None:
        template = default_template()
    subject, body = render_letter(template, matter, scope, user, extra)
    e = Engagement(matter_id=matter.id, contact_id=matter.client_id, template_id=template.id if template else None,
                   subject=subject, body_html=body, token=new_token(), status="draft")
    db.session.add(e)
    db.session.flush()
    audit("create", "engagement", e.id, f"{matter.number} {matter.name}", user.id if user else None)
    return e


def _sign_url(e):
    return f"{current_app.config['BASE_URL']}/sign/{e.token}"


def _pixel_url(e):
    return f"{current_app.config['BASE_URL']}/track/engagement/{e.token}.gif"


def _email_html(title, paragraphs, button_text, button_url, pixel=None):
    f = Firm.get()
    ps = "".join(f"<p style='margin:0 0 12px'>{escape(p)}</p>" for p in paragraphs)
    btn = (f"<p style='margin:20px 0'><a href='{button_url}' style='background:#1f5f8b;color:#fff;padding:10px 18px;"
           f"border-radius:6px;text-decoration:none;display:inline-block'>{escape(button_text)}</a></p>"
           f"<p style='font-size:12px;color:#666'>If the button does not work, open this link: {button_url}</p>")
    px = f"<img src='{pixel}' width='1' height='1' alt=''>" if pixel else ""
    return (f"<div style='font-family:Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1c2430'>"
            f"<h2 style='font-size:18px'>{escape(title)}</h2>{ps}{btn}"
            f"<p style='font-size:13px;color:#666'>{escape(f.name or '')}<br>{escape(f.phone or '')}</p>{px}</div>")


def send_engagement(engagement, user=None):
    """Hash the body, mark sent, email the client a sign link with an open pixel. Does not commit."""
    e = engagement
    e.document_hash = hashlib.sha256((e.body_html or "").encode("utf-8")).hexdigest()
    e.status = "sent"
    e.sent_at = now()
    e.sent_to = (e.contact.email or "").strip()
    f = Firm.get()
    detail = f"to {e.sent_to}" if e.sent_to else "no email on file, link not emailed"
    if e.sent_to:
        html = _email_html(e.subject or "Engagement letter",
                           [f"Hello {e.contact.first_name or e.contact.display_name},",
                            f"{f.name} has prepared an engagement letter for {e.matter.name}. "
                            f"Please review it and sign electronically using the button below."],
                           "Review and sign", _sign_url(e), pixel=_pixel_url(e))
        send_email(e.sent_to, e.subject or "Engagement letter", html,
                   text=f"Please review and sign your engagement letter: {_sign_url(e)}", reply_to=f.email or None)
    db.session.add(EngagementEvent(engagement_id=e.id, event="sent", detail=detail))
    audit("send", "engagement", e.id, detail, user.id if user else None)
    return e


def send_engagement_reminder(engagement, user=None, detail="reminder"):
    """Re-email the sign link. Used by the remind button and the CLI. Does not commit."""
    e = engagement
    f = Firm.get()
    to = e.sent_to or (e.contact.email or "")
    if to:
        html = _email_html(f"Reminder: {e.subject or 'Engagement letter'}",
                           [f"Hello {e.contact.first_name or e.contact.display_name},",
                            f"This is a reminder that the engagement letter from {f.name} for {e.matter.name} "
                            f"is waiting for your signature."],
                           "Review and sign", _sign_url(e), pixel=_pixel_url(e))
        send_email(to, f"Reminder: {e.subject or 'Engagement letter'}", html,
                   text=f"Reminder: please review and sign your engagement letter: {_sign_url(e)}",
                   reply_to=f.email or None)
    db.session.add(EngagementEvent(engagement_id=e.id, event="reminder", detail=f"{detail} to {to}" if to else "no email"))
    audit("remind", "engagement", e.id, detail, user.id if user else None)
    return e


def _log_view(e, detail=""):
    """Count a view unless the last one was under a minute ago. Commits."""
    last = EngagementEvent.query.filter_by(engagement_id=e.id, event="viewed").order_by(
        EngagementEvent.created_at.desc(), EngagementEvent.id.desc()).first()
    if last and (now() - last.created_at).total_seconds() < VIEW_DEDUPE_SECONDS:
        return False
    e.view_count = (e.view_count or 0) + 1
    if not e.first_viewed_at:
        e.first_viewed_at = now()
    if e.status == "sent":
        e.status = "viewed"
    db.session.add(EngagementEvent(engagement_id=e.id, event="viewed", ip=client_ip(),
                                   ua=request.headers.get("User-Agent", "")[:300], detail=detail))
    db.session.commit()
    return True


def _signature_block(pdf, e):
    pdf.ln(6)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "Electronic signature record", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    rows = [
        ("Signed by", e.signer_name), ("Email", e.signer_email), ("IP address", e.signer_ip),
        ("User agent", e.signer_ua), ("Signed at (UTC)", e.signed_at.strftime("%Y-%m-%d %H:%M:%S") if e.signed_at else ""),
        ("Document SHA-256", e.document_hash), ("Signature SHA-256", e.signature_hash),
    ]
    for k, v in rows:
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(38, 5, k, new_x="RIGHT")
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, pdfsvc._clean(v or ""), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, pdfsvc._clean(
        f"{e.signer_name} agreed to sign this letter electronically by typing their name, confirming they had read "
        f"the letter, and clicking Sign. The document hash was computed when the letter was sent and the signature "
        f"hash binds the signer's name, IP address, and timestamp to that exact document."))


def build_signed_pdf(e):
    f = Firm.get()
    pdf = pdfsvc.DocPDF(f, title=e.subject or "Engagement letter")
    pdf.add_page()
    pdfsvc.html_to_pdf_body(pdf, e.body_html)
    _signature_block(pdf, e)
    fname = f"engagement-{e.id}-{(e.token or 'x')[:8]}-signed.pdf"
    return pdfsvc.save_pdf(pdf, fname)


def build_draft_pdf_bytes(e):
    f = Firm.get()
    pdf = pdfsvc.DocPDF(f, title=f"DRAFT {e.subject or 'Engagement letter'}")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(179, 38, 30)
    pdf.cell(0, 7, f"DRAFT, not signed (status: {e.status})", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)
    pdfsvc.html_to_pdf_body(pdf, e.body_html)
    return bytes(pdf.output())


def _events_summary(e):
    views = [ev for ev in e.events if ev.event == "viewed"]
    return dict(views=len(views), first_view=views[0].created_at if views else None)


# ---------------------------------------------------------------------------
# Staff: engagements
# ---------------------------------------------------------------------------
@bp.route("/engagements")
@login_required
def index():
    status = request.args.get("status", "")
    q = Engagement.query
    if status == "open":
        q = q.filter(Engagement.status.in_(["sent", "viewed"]))
    elif status:
        q = q.filter_by(status=status)
    rows = q.order_by(Engagement.created_at.desc()).all()
    counts = {s: Engagement.query.filter_by(status=s).count() for s in
              ("draft", "sent", "viewed", "signed", "declined", "void")}
    return render_template("engagements/index.html", rows=rows, status=status, counts=counts)


@bp.route("/engagements/new", methods=["GET", "POST"])
@login_required
def new():
    u = current_user()
    matter_id = request.values.get("matter_id", type=int)
    matter = db.session.get(Matter, matter_id) if matter_id else None
    templates = LetterTemplate.query.filter_by(kind="engagement").order_by(LetterTemplate.is_default.desc(),
                                                                          LetterTemplate.name).all()
    if not matter:
        matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.created_at.desc()).all()
        return render_template("engagements/new.html", matter=None, matters=matters, templates=templates)
    template_id = request.values.get("template_id", type=int)
    template = db.session.get(LetterTemplate, template_id) if template_id else default_template()
    scope = request.values.get("scope", "")
    action = request.form.get("action", "preview")
    subject, body = "", ""
    try:
        subject, body = render_letter(template, matter, scope, u)
    except TemplateSyntaxError as ex:
        flash(f"Template error: {ex}", "error")
    if request.method == "POST" and action in ("draft", "send"):
        e = build_engagement(matter, template, scope, user=u)
        custom_body = request.form.get("body_html", "")
        if custom_body.strip():
            e.body_html = custom_body
        custom_subject = request.form.get("subject", "").strip()
        if custom_subject:
            e.subject = custom_subject
        if action == "send":
            send_engagement(e, u)
            db.session.commit()
            flash(f"Engagement letter sent to {e.sent_to or 'nobody (no email on file)'}.", "ok")
        else:
            db.session.commit()
            flash("Draft saved.", "ok")
        return redirect(url_for("engagements.detail", id=e.id))
    if request.method == "POST":
        # preview: keep staff edits to subject if any
        subject = request.form.get("subject", "").strip() or subject
    else:
        scope = scope or matter.description or ""
    return render_template("engagements/new.html", matter=matter, templates=templates, template=template,
                           scope=scope, subject=subject, body=body, matters=None)


@bp.route("/engagements/<int:id>")
@login_required
def detail(id):
    e = db.session.get(Engagement, id) or abort(404)
    return render_template("engagements/detail.html", e=e, summary=_events_summary(e), sign_url=_sign_url(e))


@bp.route("/engagements/<int:id>/send", methods=["POST"])
@login_required
def send(id):
    e = db.session.get(Engagement, id) or abort(404)
    if e.status not in ("draft", "sent", "viewed"):
        flash(f"Cannot send a letter with status {e.status}.", "error")
        return redirect(url_for("engagements.detail", id=e.id))
    if e.status == "draft":
        send_engagement(e, current_user())
        db.session.commit()
        flash(f"Sent to {e.sent_to or 'nobody (no email on file)'}.", "ok")
    else:
        send_engagement_reminder(e, current_user(), detail="resent by staff")
        db.session.commit()
        flash("Sign link re-sent.", "ok")
    return redirect(url_for("engagements.detail", id=e.id))


@bp.route("/engagements/<int:id>/remind", methods=["POST"])
@login_required
def remind(id):
    e = db.session.get(Engagement, id) or abort(404)
    if e.status not in ("sent", "viewed"):
        flash("Only sent letters can be reminded.", "error")
        return redirect(url_for("engagements.detail", id=e.id))
    send_engagement_reminder(e, current_user(), detail="manual reminder")
    db.session.commit()
    flash("Reminder sent.", "ok")
    return redirect(url_for("engagements.detail", id=e.id))


@bp.route("/engagements/<int:id>/void", methods=["POST"])
@login_required
def void(id):
    e = db.session.get(Engagement, id) or abort(404)
    if e.status == "signed":
        flash("A signed letter cannot be voided.", "error")
        return redirect(url_for("engagements.detail", id=e.id))
    e.status = "void"
    db.session.add(EngagementEvent(engagement_id=e.id, event="void", detail=f"voided by {current_user().name}"))
    audit("void", "engagement", e.id, "", current_user().id)
    db.session.commit()
    flash("Letter voided.", "ok")
    return redirect(url_for("engagements.detail", id=e.id))


@bp.route("/engagements/<int:id>/pdf")
@login_required
def pdf(id):
    import os
    e = db.session.get(Engagement, id) or abort(404)
    if e.status == "signed":
        if not e.pdf_path or not os.path.exists(e.pdf_path):
            e.pdf_path = build_signed_pdf(e)
            db.session.commit()
        return send_file(e.pdf_path, mimetype="application/pdf", as_attachment=False,
                         download_name=f"engagement-{e.id}-signed.pdf")
    data = build_draft_pdf_bytes(e)
    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=False,
                     download_name=f"engagement-{e.id}-draft.pdf")


# ---------------------------------------------------------------------------
# Staff: templates
# ---------------------------------------------------------------------------
@bp.route("/engagements/templates")
@login_required
def templates():
    rows = LetterTemplate.query.order_by(LetterTemplate.kind, LetterTemplate.is_default.desc(),
                                         LetterTemplate.name).all()
    return render_template("engagements/templates.html", rows=rows)


def _fill_template(t, form):
    t.name = form.get("name", "").strip()
    t.kind = form.get("kind", "engagement") if form.get("kind") in ("engagement", "declination", "general") else "engagement"
    t.subject = form.get("subject", "").strip()
    t.body_html = form.get("body_html", "")
    t.is_default = form.get("is_default") == "1"


def _validate_template(t):
    if not t.name:
        return "A name is required."
    try:
        _html_env().from_string(t.body_html or "")
        _text_env().from_string(t.subject or "")
    except TemplateSyntaxError as ex:
        return f"Template syntax error: {ex}"
    return None


def _apply_default(t):
    if t.is_default:
        for other in LetterTemplate.query.filter(LetterTemplate.kind == t.kind, LetterTemplate.id != t.id).all():
            other.is_default = False


@bp.route("/engagements/templates/new", methods=["GET", "POST"])
@login_required
def template_new():
    t = LetterTemplate(kind="engagement", body_html=FALLBACK_BODY, subject="Engagement letter: {{ matter_name }}")
    if request.method == "POST":
        _fill_template(t, request.form)
        err = _validate_template(t)
        if err:
            flash(err, "error")
            return render_template("engagements/template_form.html", t=t, is_new=True, fields=MERGE_FIELDS)
        if LetterTemplate.query.filter_by(kind=t.kind).count() == 0:
            t.is_default = True
        db.session.add(t)
        db.session.flush()
        _apply_default(t)
        audit("create", "letter_template", t.id, t.name, current_user().id)
        db.session.commit()
        flash("Template created.", "ok")
        return redirect(url_for("engagements.templates"))
    return render_template("engagements/template_form.html", t=t, is_new=True, fields=MERGE_FIELDS)


@bp.route("/engagements/templates/<int:id>/edit", methods=["GET", "POST"])
@login_required
def template_edit(id):
    t = db.session.get(LetterTemplate, id) or abort(404)
    if request.method == "POST":
        _fill_template(t, request.form)
        err = _validate_template(t)
        if err:
            flash(err, "error")
            return render_template("engagements/template_form.html", t=t, is_new=False, fields=MERGE_FIELDS)
        _apply_default(t)
        db.session.commit()
        flash("Template saved.", "ok")
        return redirect(url_for("engagements.templates"))
    return render_template("engagements/template_form.html", t=t, is_new=False, fields=MERGE_FIELDS)


@bp.route("/engagements/templates/<int:id>/delete", methods=["POST"])
@login_required
def template_delete(id):
    t = db.session.get(LetterTemplate, id) or abort(404)
    name = t.name
    for e in Engagement.query.filter_by(template_id=t.id).all():
        e.template_id = None
    db.session.delete(t)
    audit("delete", "letter_template", id, name, current_user().id)
    db.session.commit()
    flash(f"Deleted template {name}.", "ok")
    return redirect(url_for("engagements.templates"))


# ---------------------------------------------------------------------------
# Public: sign, decline, tracking pixel
# ---------------------------------------------------------------------------
def _by_token(token):
    return Engagement.query.filter_by(token=token).first() or abort(404)


@bp.route("/sign/<token>", methods=["GET", "POST"])
def sign(token):
    e = _by_token(token)
    if e.status in ("signed", "void", "declined", "draft"):
        return render_template("engagements/sign_status.html", e=e)
    if request.method == "GET":
        _log_view(e, detail="page")
        return render_template("engagements/sign.html", e=e, name="", email=e.contact.email or "", error=None)
    name = request.form.get("signer_name", "").strip()
    email = request.form.get("signer_email", "").strip()
    agree = request.form.get("agree") == "1"
    if not name or not agree:
        error = "Type your full name and tick the box to confirm you agree." if not name else \
            "Please tick the box to confirm you have read the letter and agree to its terms."
        return render_template("engagements/sign.html", e=e, name=name, email=email, error=error), 400
    ts = now()
    ip = client_ip()
    if not e.document_hash:
        e.document_hash = hashlib.sha256((e.body_html or "").encode("utf-8")).hexdigest()
    e.signature_hash = hashlib.sha256(f"{e.document_hash}{name}{ip}{ts.isoformat()}".encode("utf-8")).hexdigest()
    e.signer_name = name
    e.signer_email = email
    e.signer_ip = ip
    e.signer_ua = request.headers.get("User-Agent", "")[:300]
    e.signed_at = ts
    e.status = "signed"
    db.session.add(EngagementEvent(engagement_id=e.id, event="signed", ip=ip, ua=e.signer_ua,
                                   detail=f"signed by {name}"))
    e.pdf_path = build_signed_pdf(e)
    audit("sign", "engagement", e.id, f"{name} from {ip}")
    db.session.commit()
    _email_signed_copies(e)
    return render_template("engagements/sign_done.html", e=e)


def _email_signed_copies(e):
    f = Firm.get()
    try:
        with open(e.pdf_path, "rb") as fh:
            data = fh.read()
    except OSError:
        data = None
    att = [(f"engagement-{e.id}-signed.pdf", data, "application/pdf")] if data else []
    subj = f"Signed: {e.subject or 'Engagement letter'}"
    client_to = e.signer_email or e.contact.email
    if client_to:
        send_email(client_to, subj, _email_html(subj, [
            f"Thank you, {e.signer_name}. Your signed engagement letter with {f.name} is attached for your records."],
            "View the signed letter", f"{current_app.config['BASE_URL']}/sign/{e.token}/pdf"),
            text=f"Your signed engagement letter is attached.", attachments=att, reply_to=f.email or None)
    firm_to = f.email or current_app.config["MAIL_FROM"]
    send_email(firm_to, subj, _email_html(subj, [
        f"{e.signer_name} signed the engagement letter for {e.matter.name} ({e.matter.number}) "
        f"from {e.signer_ip} at {e.signed_at:%Y-%m-%d %H:%M} UTC."],
        "Open in the app", f"{current_app.config['BASE_URL']}/engagements/{e.id}"),
        text=f"{e.signer_name} signed {e.matter.name}.", attachments=att)


@bp.route("/sign/<token>/decline", methods=["POST"])
def decline(token):
    e = _by_token(token)
    if e.status in ("sent", "viewed"):
        e.status = "declined"
        db.session.add(EngagementEvent(engagement_id=e.id, event="declined", ip=client_ip(),
                                       ua=request.headers.get("User-Agent", "")[:300],
                                       detail=request.form.get("reason", "")[:300]))
        audit("decline", "engagement", e.id, request.form.get("reason", "")[:200])
        db.session.commit()
        f = Firm.get()
        send_email(f.email or current_app.config["MAIL_FROM"], f"Declined: {e.subject or 'Engagement letter'}",
                   _email_html("Engagement letter declined",
                               [f"{e.contact.display_name} declined the engagement letter for {e.matter.name}."],
                               "Open in the app", f"{current_app.config['BASE_URL']}/engagements/{e.id}"))
    return render_template("engagements/sign_status.html", e=e)


@bp.route("/sign/<token>/pdf")
def sign_pdf(token):
    import os
    e = _by_token(token)
    if e.status != "signed":
        abort(404)
    if not e.pdf_path or not os.path.exists(e.pdf_path):
        e.pdf_path = build_signed_pdf(e)
        db.session.commit()
    return send_file(e.pdf_path, mimetype="application/pdf", as_attachment=True,
                     download_name=f"engagement-{e.id}-signed.pdf")


@bp.route("/track/engagement/<token>.gif")
def track(token):
    e = Engagement.query.filter_by(token=token).first()
    if e and e.status in ("sent", "viewed"):
        _log_view(e, detail="email pixel")
    resp = Response(ONE_PX_GIF, mimetype="image/gif")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp
