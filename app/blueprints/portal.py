"""Client portal: magic-link sign in, a calm home page, secure messaging, document upload, and guarded downloads.
All routes live under /portal so POSTs are CSRF-exempt by prefix (public.html forms still include csrf()).

Every client-facing string comes from app.i18n, chosen by lang_for(contact) (contact language, else firm default).
"""
import os
import time
from datetime import datetime, timedelta
from flask import (Blueprint, render_template, request, redirect, url_for, flash, session, current_app, abort,
                   send_file)
from sqlalchemy import func
from werkzeug.utils import secure_filename
from ..extensions import db
from ..models import Contact, Matter, Invoice, Document, Engagement, PortalToken, Firm, Message, audit, now
from ..helpers import portal_required, portal_contact
from ..services.mail import send_email
from ..i18n import t, lang_for
from . import signatures as _signatures

bp = Blueprint("portal", __name__, url_prefix="/portal")

TOKEN_TTL_MIN = 30
RATE_LIMIT_COUNT = 3
RATE_LIMIT_MIN = 15
NEUTRAL_MSG = "If we have that email on file, we sent you a sign-in link. It works for 30 minutes."




@bp.app_context_processor
def _i18n_context():
    """Expose t() and lang_for() to templates rendered by other modules (invoices/public.html, engagements/sign*.html).
    Explicit render_template kwargs always override these."""
    return dict(t=t, lang_for=lang_for)


def _lang():
    return lang_for(portal_contact())


@bp.route("/login", methods=["GET", "POST"])
def login():
    if portal_contact():
        return redirect(url_for("portal.home"))
    lang = lang_for(None)
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if email:
            contact = Contact.query.filter(func.lower(Contact.email) == email).order_by(
                Contact.is_client.desc(), Contact.id).first()
            if contact:
                since = datetime.utcnow() - timedelta(minutes=RATE_LIMIT_MIN)
                recent = PortalToken.query.filter(PortalToken.contact_id == contact.id,
                                                  PortalToken.created_at >= since).count()
                if recent >= RATE_LIMIT_COUNT:
                    current_app.logger.warning("portal login rate limit hit for contact %s", contact.id)
                else:
                    tok = PortalToken(contact_id=contact.id,
                                      expires_at=datetime.utcnow() + timedelta(minutes=TOKEN_TTL_MIN))
                    db.session.add(tok)
                    db.session.flush()
                    firm = Firm.get()
                    cl = lang_for(contact)
                    link = f"{current_app.config['BASE_URL']}{url_for('portal.auth', token=tok.token)}"
                    html = (f"<p>{t('email.hello', cl, name=contact.display_name)}</p>"
                            f"<p>{t('email.portal_link.body', cl, firm=firm.name, minutes=TOKEN_TTL_MIN)}</p>"
                            f"<p><a href=\"{link}\">{link}</a></p>"
                            f"<p>{t('email.portal_link.ignore', cl)}</p>")
                    text = t("email.portal_link.text", cl, firm=firm.name, minutes=TOKEN_TTL_MIN, url=link)
                    send_email(contact.email, t("email.portal_link.subject", cl, firm=firm.name), html, text)
                    audit("portal_link_sent", "contact", contact.id, contact.email)
                    db.session.commit()
        flash(t("portal.login.neutral", lang), "ok")
    return render_template("portal/login.html", lang=lang, t=t)


@bp.route("/auth/<token>")
def auth(token):
    tok = PortalToken.query.filter_by(token=token).first()
    if not tok or tok.used_at or tok.expires_at < datetime.utcnow():
        return render_template("portal/expired.html", lang=lang_for(tok.contact if tok else None), t=t), 410
    tok.used_at = datetime.utcnow()
    session["portal_contact_id"] = tok.contact_id
    session.permanent = True
    audit("portal_login", "contact", tok.contact_id)
    db.session.commit()
    return redirect(url_for("portal.home"))


@bp.route("/logout", methods=["POST"])
def logout():
    lang = _lang()
    session.pop("portal_contact_id", None)
    flash(t("portal.logged_out", lang), "ok")
    return redirect(url_for("portal.login"))


def _my_matters(contact):
    return Matter.query.filter_by(client_id=contact.id).order_by(Matter.status, Matter.number).all()


def unread_count(contact):
    """Staff portal messages the client has not opened yet."""
    return Message.query.filter(Message.contact_id == contact.id, Message.channel == "portal",
                                Message.direction == "out", Message.read_at.is_(None)).count()


@bp.route("")
@portal_required
def home():
    c = portal_contact()
    lang = lang_for(c)
    matters = _my_matters(c)
    matter_ids = [m.id for m in matters]
    active = [m for m in matters if m.status in ("open", "pending")]
    invoices = Invoice.query.filter(Invoice.client_id == c.id, Invoice.status.in_(["sent", "viewed", "partial"])) \
        .order_by(Invoice.due_on.asc().nulls_last(), Invoice.issued_on.desc()).all()
    invoices = [i for i in invoices if i.balance_cents > 0]
    docs = Document.query.filter(Document.matter_id.in_(matter_ids or [0]), Document.shared_to_portal == True,
                                 Document.is_current == True) \
        .order_by(Document.created_at.desc()).all()  # noqa: E712
    letters = Engagement.query.filter(Engagement.contact_id == c.id, Engagement.status.in_(["sent", "viewed"])) \
        .order_by(Engagement.sent_at.desc()).all()
    to_sign = _signatures.pending_for_contact(c.id)
    trust = c.trust_balance_cents()
    return render_template("portal/home.html", c=c, matters=active, all_matters=matters, invoices=invoices,
                           docs=docs, letters=letters, to_sign=to_sign, trust=trust, unread=unread_count(c),
                           lang=lang, t=t)


# ---------------------------------------------------------------------------
# Secure messages
# ---------------------------------------------------------------------------
@bp.route("/messages")
@portal_required
def messages():
    c = portal_contact()
    lang = lang_for(c)
    matters = _my_matters(c)
    thread = request.args.get("thread", "")  # "" = all, "general", or a matter id
    q = Message.query.filter(Message.contact_id == c.id, Message.channel == "portal")
    if thread == "general":
        q = q.filter(Message.matter_id.is_(None))
    elif thread.isdigit():
        q = q.filter(Message.matter_id == int(thread))
    msgs = q.order_by(Message.created_at, Message.id).all()
    changed = False
    for m in msgs:
        if m.direction == "out" and m.read_at is None:
            m.read_at = now()
            changed = True
    if changed:
        db.session.commit()
    counts = {}
    for m in Message.query.filter(Message.contact_id == c.id, Message.channel == "portal", Message.direction == "out",
                                  Message.read_at.is_(None)).all():
        counts[m.matter_id or "general"] = counts.get(m.matter_id or "general", 0) + 1
    selected_matter = int(thread) if thread.isdigit() else None
    return render_template("portal/messages.html", c=c, msgs=msgs, matters=matters, thread=thread,
                           selected_matter=selected_matter, unread_by_thread=counts, lang=lang, t=t)


@bp.route("/messages/send", methods=["POST"])
@portal_required
def messages_send():
    c = portal_contact()
    lang = lang_for(c)
    body = (request.form.get("body") or "").strip()
    mid = request.form.get("matter_id", "")
    matter = db.session.get(Matter, int(mid)) if mid.isdigit() else None
    if matter and matter.client_id != c.id:
        matter = None
    back = url_for("portal.messages", thread=(matter.id if matter else "general"))
    if not body:
        flash(t("portal.msgs.empty_body", lang), "error")
        return redirect(back)
    firm = Firm.get()
    m = Message(contact_id=c.id, matter_id=matter.id if matter else None, direction="in", channel="portal",
                from_addr=c.email or "", to_addr=firm.email or "", body=body[:20000], status="received")
    db.session.add(m)
    db.session.flush()
    audit("receive", "message", m.id, f"portal message from {c.display_name}")
    db.session.commit()
    _notify_staff(m, c, matter, firm)
    flash(t("portal.msgs.sent", lang), "ok")
    return redirect(back)


def _notify_staff(m, c, matter, firm):
    """Tell the responsible attorney (or the firm inbox) a client wrote in the portal. Staff email, so English."""
    to = (matter.responsible.email if matter and matter.responsible and matter.responsible.email else "") or firm.email \
        or current_app.config["MAIL_FROM"]
    link = f"{current_app.config['BASE_URL']}/messages/{c.id}" + (f"?matter_id={matter.id}" if matter else "")
    about = f"{matter.number} {matter.name}" if matter else "no specific matter"
    from markupsafe import escape
    html = (f"<p>{escape(c.display_name)} sent a secure message in the client portal ({escape(about)}).</p>"
            f"<blockquote style='border-left:3px solid #ccc;margin:12px 0;padding:6px 12px;white-space:pre-wrap'>"
            f"{escape(m.body)}</blockquote>"
            f"<p><a href=\"{link}\">Reply in the app</a></p>")
    text = f"{c.display_name} sent a secure message ({about}):\n\n{m.body}\n\nReply: {link}"
    send_email(to, f"New portal message from {c.display_name}", html, text)


@bp.route("/invoices/<int:invoice_id>")
@portal_required
def invoice(invoice_id):
    c = portal_contact()
    inv = db.session.get(Invoice, invoice_id)
    if not inv or inv.client_id != c.id or inv.status == "draft":
        abort(404)
    return redirect(f"/p/{inv.public_token}")


@bp.route("/documents/<int:doc_id>/download")
@portal_required
def download(doc_id):
    c = portal_contact()
    doc = db.session.get(Document, doc_id)
    if not doc or not doc.shared_to_portal or not doc.matter or doc.matter.client_id != c.id:
        abort(404)
    path = doc.path if os.path.isabs(doc.path) else os.path.join(current_app.config["UPLOAD_DIR"], doc.path)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=doc.name, mimetype=doc.mime or None)


@bp.route("/upload", methods=["POST"])
@portal_required
def upload():
    c = portal_contact()
    lang = lang_for(c)
    mid = request.form.get("matter_id", "")
    matter = db.session.get(Matter, int(mid)) if mid.isdigit() else None
    if not matter or matter.client_id != c.id:
        flash(t("portal.upload.pick_matter", lang), "error")
        return redirect(url_for("portal.home"))
    f = request.files.get("file")
    if not f or not f.filename:
        flash(t("portal.upload.choose_file", lang), "error")
        return redirect(url_for("portal.home"))
    name = secure_filename(f.filename) or "upload"
    folder = os.path.join(current_app.config["UPLOAD_DIR"], str(matter.id))
    os.makedirs(folder, exist_ok=True)
    stored = f"{int(time.time())}_{name}"
    path = os.path.join(folder, stored)
    f.save(path)
    doc = Document(matter_id=matter.id, name=f.filename[:300], path=path, size=os.path.getsize(path),
                   mime=f.mimetype or "", uploaded_by_id=None, shared_to_portal=True, uploaded_by_client=True)
    db.session.add(doc)
    db.session.flush()
    audit("document_upload_client", "document", doc.id, f"{c.display_name} uploaded {f.filename} to {matter.number}")
    db.session.commit()
    flash(t("portal.upload.done", lang, name=f.filename), "ok")
    return redirect(url_for("portal.home"))
