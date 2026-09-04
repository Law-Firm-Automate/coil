"""Click-to-sign on any uploaded document, mirroring the engagement-letter flow.

Staff routes live at /signatures/... Public routes are mounted under /sign/doc/<token>... because /sign/ is already
in CSRF_EXEMPT_PREFIXES (helpers.py may not be edited by this module), and the open pixel at /track/docsign/<token>.gif.

Mounting: app/__init__.py registers blueprints from a fixed list that does not include "signatures", so portal.py
registers this blueprint on the app through a record_once hook. If "signatures" is later added to that list,
remove the hook in portal.py.
"""
import hashlib
import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, send_file, Response
from markupsafe import escape
from ..extensions import db
from ..models import (Firm, Contact, Matter, MatterParty, Document, DocumentSignature, DocumentSignatureEvent,
                      new_token, audit, now)
from ..helpers import login_required, current_user, client_ip
from ..services.mail import send_email
from ..services import pdf as pdfsvc
from ..i18n import t, lang_for
from .documents import abs_path

bp = Blueprint("signatures", __name__)

ONE_PX_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
              b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")
VIEW_DEDUPE_SECONDS = 60
STATUSES = ("draft", "sent", "viewed", "signed", "declined", "void")
INLINE_MIMES = ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sign_url(s):
    return f"{current_app.config['BASE_URL']}/sign/doc/{s.token}"


def pixel_url(s):
    return f"{current_app.config['BASE_URL']}/track/docsign/{s.token}.gif"


def _file_bytes(doc):
    p = abs_path(doc)
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as fh:
        return fh.read()


def _display_title(s):
    return s.title or (s.document.name if s.document else "") or "Document"


def signer_choices(doc):
    """The matter's client plus any party that is linked to a contact."""
    out, seen = [], set()
    m = doc.matter
    if m and m.client and m.client.id not in seen:
        out.append((m.client, "Client"))
        seen.add(m.client.id)
    for p in (m.parties if m else []):
        if p.contact and p.contact.id not in seen:
            out.append((p.contact, p.role.replace("_", " ")))
            seen.add(p.contact.id)
    return out


def _email_html(title, paragraphs, button_text, button_url, lang="en", pixel=None):
    f = Firm.get()
    ps = "".join(f"<p style='margin:0 0 12px'>{escape(p)}</p>" for p in paragraphs)
    btn = (f"<p style='margin:20px 0'><a href='{button_url}' style='background:#1f5f8b;color:#fff;padding:10px 18px;"
           f"border-radius:6px;text-decoration:none;display:inline-block'>{escape(button_text)}</a></p>"
           f"<p style='font-size:12px;color:#666'>{escape(t('email.fallback_link', lang, url=button_url))}</p>")
    px = f"<img src='{pixel}' width='1' height='1' alt=''>" if pixel else ""
    return (f"<div style='font-family:Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1c2430'>"
            f"<h2 style='font-size:18px'>{escape(title)}</h2>{ps}{btn}"
            f"<p style='font-size:13px;color:#666'>{escape(f.name or '')}<br>{escape(f.phone or '')}</p>{px}</div>")


def send_signature(s, user=None):
    """Hash the file, mark sent, email the signer. Does not commit. Returns an error string or None."""
    data = _file_bytes(s.document)
    if data is None:
        return "The file for this document is missing on disk."
    s.document_hash = hashlib.sha256(data).hexdigest()
    s.status = "sent"
    s.sent_at = now()
    s.sent_to = (s.contact.email or "").strip()
    f = Firm.get()
    lang = lang_for(s.contact)
    title = _display_title(s)
    detail = f"to {s.sent_to}" if s.sent_to else "no email on file, link not emailed"
    if s.sent_to:
        paragraphs = [t("email.hello", lang, name=s.contact.first_name or s.contact.display_name),
                      t("email.sig_request.body", lang, firm=f.name, title=title)]
        if (s.message or "").strip():
            paragraphs.append(t("email.sig_request.message_from", lang, firm=f.name))
            paragraphs.append(s.message.strip())
        subj = t("email.sig_request.subject", lang, title=title)
        send_email(s.sent_to, subj, _email_html(subj, paragraphs, t("email.sig_request.button", lang), sign_url(s),
                                                lang=lang, pixel=pixel_url(s)),
                   text=t("email.sig_request.text", lang, title=title, url=sign_url(s)), reply_to=f.email or None)
    db.session.add(DocumentSignatureEvent(signature_id=s.id, event="sent", detail=detail))
    audit("send", "document_signature", s.id, f"{title} {detail}", user.id if user else None)
    return None


def send_signature_reminder(s, user=None, detail="reminder"):
    f = Firm.get()
    lang = lang_for(s.contact)
    title = _display_title(s)
    to = s.sent_to or (s.contact.email or "")
    if to:
        subj = t("email.sig_reminder.subject", lang, title=title)
        send_email(to, subj, _email_html(subj, [t("email.hello", lang, name=s.contact.first_name or s.contact.display_name),
                                                t("email.sig_reminder.body", lang, title=title, firm=f.name)],
                                         t("email.sig_request.button", lang), sign_url(s), lang=lang, pixel=pixel_url(s)),
                   text=t("email.sig_request.text", lang, title=title, url=sign_url(s)), reply_to=f.email or None)
    db.session.add(DocumentSignatureEvent(signature_id=s.id, event="reminder",
                                          detail=f"{detail} to {to}" if to else "no email"))
    audit("remind", "document_signature", s.id, detail, user.id if user else None)


def _log_view(s, detail=""):
    """Count a view unless the last one was under a minute ago. Commits."""
    last = DocumentSignatureEvent.query.filter_by(signature_id=s.id, event="viewed").order_by(
        DocumentSignatureEvent.created_at.desc(), DocumentSignatureEvent.id.desc()).first()
    if last and (now() - last.created_at).total_seconds() < VIEW_DEDUPE_SECONDS:
        return False
    s.view_count = (s.view_count or 0) + 1
    if not s.first_viewed_at:
        s.first_viewed_at = now()
    if s.status == "sent":
        s.status = "viewed"
    db.session.add(DocumentSignatureEvent(signature_id=s.id, event="viewed", ip=client_ip(),
                                          ua=request.headers.get("User-Agent", "")[:300], detail=detail))
    db.session.commit()
    return True


def build_certificate_pdf(s):
    """Certificate of electronic signature: document identity, signer block, event log. Saved to PDF_DIR."""
    f = Firm.get()
    doc = s.document
    pdf = pdfsvc.DocPDF(f, title="Signature certificate")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "Certificate of electronic signature", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    def rows(section, items):
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 6, section, new_x="LMARGIN", new_y="NEXT")
        for k, v in items:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(42, 5, k, new_x="RIGHT")
            pdf.set_font("Helvetica", "", 9)
            pdf.multi_cell(0, 5, pdfsvc._clean(str(v or "")), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    matter = doc.matter if doc else None
    rows("Document", [
        ("Title", _display_title(s)), ("File name", doc.name if doc else ""),
        ("File size", f"{doc.size or 0} bytes" if doc else ""), ("Type", doc.mime if doc else ""),
        ("Matter", f"{matter.number} {matter.name}" if matter else ""),
        ("Document SHA-256", s.document_hash),
    ])
    rows("Signer", [
        ("Signed by", s.signer_name), ("Email", s.signer_email), ("IP address", s.signer_ip),
        ("User agent", s.signer_ua),
        ("Signed at (UTC)", s.signed_at.strftime("%Y-%m-%d %H:%M:%S") if s.signed_at else ""),
        ("Signature SHA-256", s.signature_hash),
    ])
    rows("Request", [
        ("Requested by", s.created_by.name if s.created_by else ""),
        ("Sent to", s.sent_to), ("Sent at (UTC)", s.sent_at.strftime("%Y-%m-%d %H:%M:%S") if s.sent_at else ""),
        ("Views", s.view_count or 0),
    ])
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "Events", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for ev in s.events:
        line = f"{ev.created_at:%Y-%m-%d %H:%M:%S} UTC  {ev.event}"
        if ev.ip:
            line += f"  from {ev.ip}"
        if ev.detail:
            line += f"  {ev.detail}"
        pdf.multi_cell(0, 5, pdfsvc._clean(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, pdfsvc._clean(
        f"{s.signer_name} signed this document electronically by typing their name, confirming they had read it, "
        f"and clicking Sign. The document hash was computed from the file bytes when the request was sent. The "
        f"signature hash is SHA-256 over the document hash, the typed name, the signer's IP address, and the ISO "
        f"timestamp of signing, which binds the signer to that exact file."))
    fname = f"docsign-{s.id}-{(s.token or 'x')[:8]}-certificate.pdf"
    return pdfsvc.save_pdf(pdf, fname)


def _certificate_bytes(s):
    if not s.certificate_pdf_path or not os.path.exists(s.certificate_pdf_path):
        s.certificate_pdf_path = build_certificate_pdf(s)
        db.session.commit()
    with open(s.certificate_pdf_path, "rb") as fh:
        return fh.read()


def _email_signed_copies(s):
    f = Firm.get()
    lang = lang_for(s.contact)
    title = _display_title(s)
    att = []
    try:
        att.append((f"signature-certificate-{s.id}.pdf", _certificate_bytes(s), "application/pdf"))
    except OSError:
        pass
    data = _file_bytes(s.document)
    if data is not None:
        att.append((s.document.name, data, s.document.mime or "application/octet-stream"))
    client_to = s.signer_email or s.contact.email
    if client_to:
        subj = t("email.signed.subject", lang, title=title)
        send_email(client_to, subj, _email_html(subj, [t("email.signed.body", lang, name=s.signer_name, title=title)],
                                                t("email.signed.button", lang), f"{sign_url(s)}/certificate", lang=lang),
                   text=t("email.signed.text", lang, title=title), attachments=att, reply_to=f.email or None)
    firm_to = f.email or current_app.config["MAIL_FROM"]
    subj = f"Signed: {title}"
    matter = s.document.matter if s.document else None
    send_email(firm_to, subj, _email_html(subj, [
        f"{s.signer_name} signed {title}" + (f" for {matter.name} ({matter.number})" if matter else "") +
        f" from {s.signer_ip} at {s.signed_at:%Y-%m-%d %H:%M} UTC. The certificate and the file are attached."],
        "Open in the app", f"{current_app.config['BASE_URL']}/signatures/{s.id}"),
        text=f"{s.signer_name} signed {title}.", attachments=att)


def pending_for_contact(contact_id):
    """Documents awaiting this contact's signature (portal home uses this)."""
    return DocumentSignature.query.filter(DocumentSignature.contact_id == contact_id,
                                         DocumentSignature.status.in_(["sent", "viewed"])) \
        .order_by(DocumentSignature.sent_at.desc()).all()


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------
@bp.route("/signatures")
@login_required
def index():
    status = request.args.get("status", "")
    q = DocumentSignature.query
    if status == "open":
        q = q.filter(DocumentSignature.status.in_(["sent", "viewed"]))
    elif status:
        q = q.filter_by(status=status)
    rows = q.order_by(DocumentSignature.created_at.desc()).all()
    counts = {s: DocumentSignature.query.filter_by(status=s).count() for s in STATUSES}
    return render_template("signatures/index.html", rows=rows, status=status, counts=counts)


@bp.route("/signatures/new", methods=["GET", "POST"])
@login_required
def new():
    u = current_user()
    document_id = request.values.get("document_id", type=int)
    doc = db.session.get(Document, document_id) if document_id else None
    if not doc:
        docs = Document.query.order_by(Document.created_at.desc()).limit(200).all()
        return render_template("signatures/new.html", doc=None, docs=docs, choices=[], contact_id=None)
    choices = signer_choices(doc)
    contact_id = request.values.get("contact_id", type=int) or (choices[0][0].id if choices else None)
    if request.method == "POST":
        title = request.form.get("title", "").strip() or doc.name
        message = request.form.get("message", "").strip()
        contact = db.session.get(Contact, contact_id) if contact_id else None
        allowed = {c.id for c, _ in choices}
        if not contact or contact.id not in allowed:
            flash("Pick a signer from the matter's client or parties.", "error")
            return render_template("signatures/new.html", doc=doc, docs=None, choices=choices, contact_id=contact_id,
                                   title=title, message=message)
        s = DocumentSignature(document_id=doc.id, contact_id=contact.id, token=new_token(), title=title[:300],
                              message=message, status="draft", created_by_id=u.id)
        db.session.add(s)
        db.session.flush()
        audit("create", "document_signature", s.id, f"{title} for {contact.display_name}", u.id)
        if request.form.get("action") == "send":
            err = send_signature(s, u)
            if err:
                db.session.commit()
                flash(err, "error")
                return redirect(url_for("signatures.detail", id=s.id))
            db.session.commit()
            flash(f"Signature request sent to {s.sent_to or 'nobody (no email on file)'}.", "ok")
        else:
            db.session.commit()
            flash("Signature request saved as a draft.", "ok")
        return redirect(url_for("signatures.detail", id=s.id))
    return render_template("signatures/new.html", doc=doc, docs=None, choices=choices, contact_id=contact_id,
                           title=doc.name, message="")


@bp.route("/signatures/<int:id>")
@login_required
def detail(id):
    s = db.session.get(DocumentSignature, id) or abort(404)
    views = [ev for ev in s.events if ev.event == "viewed"]
    return render_template("signatures/detail.html", s=s, sign_url=sign_url(s), title=_display_title(s),
                           summary=dict(views=len(views), first_view=views[0].created_at if views else None))


@bp.route("/signatures/<int:id>/send", methods=["POST"])
@login_required
def send(id):
    s = db.session.get(DocumentSignature, id) or abort(404)
    if s.status not in ("draft", "sent", "viewed"):
        flash(f"Cannot send a request with status {s.status}.", "error")
        return redirect(url_for("signatures.detail", id=s.id))
    if s.status == "draft":
        err = send_signature(s, current_user())
        if err:
            db.session.rollback()
            flash(err, "error")
            return redirect(url_for("signatures.detail", id=s.id))
        db.session.commit()
        flash(f"Sent to {s.sent_to or 'nobody (no email on file)'}.", "ok")
    else:
        send_signature_reminder(s, current_user(), detail="resent by staff")
        db.session.commit()
        flash("Sign link re-sent.", "ok")
    return redirect(url_for("signatures.detail", id=s.id))


@bp.route("/signatures/<int:id>/remind", methods=["POST"])
@login_required
def remind(id):
    s = db.session.get(DocumentSignature, id) or abort(404)
    if s.status not in ("sent", "viewed"):
        flash("Only sent requests can be reminded.", "error")
        return redirect(url_for("signatures.detail", id=s.id))
    send_signature_reminder(s, current_user(), detail="manual reminder")
    db.session.commit()
    flash("Reminder sent.", "ok")
    return redirect(url_for("signatures.detail", id=s.id))


@bp.route("/signatures/<int:id>/void", methods=["POST"])
@login_required
def void(id):
    s = db.session.get(DocumentSignature, id) or abort(404)
    if s.status == "signed":
        flash("A signed document cannot be voided.", "error")
        return redirect(url_for("signatures.detail", id=s.id))
    s.status = "void"
    db.session.add(DocumentSignatureEvent(signature_id=s.id, event="void", detail=f"voided by {current_user().name}"))
    audit("void", "document_signature", s.id, "", current_user().id)
    db.session.commit()
    flash("Signature request voided.", "ok")
    return redirect(url_for("signatures.detail", id=s.id))


@bp.route("/signatures/<int:id>/certificate")
@login_required
def certificate(id):
    s = db.session.get(DocumentSignature, id) or abort(404)
    if s.status != "signed":
        abort(404)
    _certificate_bytes(s)
    return send_file(s.certificate_pdf_path, mimetype="application/pdf", as_attachment=False,
                     download_name=f"signature-certificate-{s.id}.pdf")


# ---------------------------------------------------------------------------
# Public: /sign/doc/<token> (CSRF-exempt by the /sign/ prefix)
# ---------------------------------------------------------------------------
def _by_token(token):
    return DocumentSignature.query.filter_by(token=token).first() or abort(404)


def _ctx(s):
    lang = lang_for(s.contact)
    return dict(s=s, lang=lang, t=t, title=_display_title(s), file_url=f"/sign/doc/{s.token}/file",
                inline=(s.document.mime or "") in INLINE_MIMES if s.document else False)


@bp.route("/sign/doc/<token>", methods=["GET", "POST"])
def sign(token):
    s = _by_token(token)
    if s.status in ("signed", "void", "declined", "draft"):
        return render_template("signatures/sign_status.html", **_ctx(s))
    if request.method == "GET":
        _log_view(s, detail="page")
        return render_template("signatures/sign.html", name="", email=s.contact.email or "", error=None, **_ctx(s))
    name = request.form.get("signer_name", "").strip()
    email = request.form.get("signer_email", "").strip()
    agree = request.form.get("agree") == "1"
    if not name or not agree:
        lang = lang_for(s.contact)
        error = t("sign.err_name", lang) if not name else t("sign.err_agree_doc", lang)
        return render_template("signatures/sign.html", name=name, email=email, error=error, **_ctx(s)), 400
    ts = now()
    ip = client_ip()
    if not s.document_hash:
        data = _file_bytes(s.document)
        s.document_hash = hashlib.sha256(data or b"").hexdigest()
    s.signature_hash = hashlib.sha256(f"{s.document_hash}{name}{ip}{ts.isoformat()}".encode("utf-8")).hexdigest()
    s.signer_name = name[:200]
    s.signer_email = email[:200]
    s.signer_ip = ip
    s.signer_ua = request.headers.get("User-Agent", "")[:300]
    s.signed_at = ts
    s.status = "signed"
    db.session.add(DocumentSignatureEvent(signature_id=s.id, event="signed", ip=ip, ua=s.signer_ua,
                                          detail=f"signed by {name}"))
    audit("sign", "document_signature", s.id, f"{name} from {ip}")
    db.session.flush()
    s.certificate_pdf_path = build_certificate_pdf(s)
    db.session.commit()
    _email_signed_copies(s)
    return render_template("signatures/sign_done.html", **_ctx(s))


@bp.route("/sign/doc/<token>/file")
def sign_file(token):
    s = _by_token(token)
    if s.status not in ("sent", "viewed", "signed"):
        abort(404)
    p = abs_path(s.document)
    if not os.path.isfile(p):
        abort(404)
    inline = (s.document.mime or "") in INLINE_MIMES
    return send_file(p, as_attachment=not inline, download_name=s.document.name, mimetype=s.document.mime or None)


@bp.route("/sign/doc/<token>/decline", methods=["POST"])
def decline(token):
    s = _by_token(token)
    if s.status in ("sent", "viewed"):
        s.status = "declined"
        reason = request.form.get("reason", "")[:300]
        db.session.add(DocumentSignatureEvent(signature_id=s.id, event="declined", ip=client_ip(),
                                              ua=request.headers.get("User-Agent", "")[:300], detail=reason))
        audit("decline", "document_signature", s.id, reason[:200])
        db.session.commit()
        f = Firm.get()
        title = _display_title(s)
        send_email(f.email or current_app.config["MAIL_FROM"], f"Declined: {title}",
                   _email_html("Signature declined",
                               [f"{s.contact.display_name} declined to sign {title}." + (f" Reason: {reason}" if reason else "")],
                               "Open in the app", f"{current_app.config['BASE_URL']}/signatures/{s.id}"))
    return render_template("signatures/sign_status.html", **_ctx(s))


@bp.route("/sign/doc/<token>/certificate")
def sign_certificate(token):
    s = _by_token(token)
    if s.status != "signed":
        abort(404)
    _certificate_bytes(s)
    return send_file(s.certificate_pdf_path, mimetype="application/pdf", as_attachment=True,
                     download_name=f"signature-certificate-{s.id}.pdf")


@bp.route("/track/docsign/<token>.gif")
def track(token):
    s = DocumentSignature.query.filter_by(token=token).first()
    if s and s.status in ("sent", "viewed"):
        _log_view(s, detail="email pixel")
    resp = Response(ONE_PX_GIF, mimetype="image/gif")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp
