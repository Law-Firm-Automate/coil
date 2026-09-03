"""Client portal: magic-link sign in, a calm home page, document upload, and guarded downloads.
All routes live under /portal so POSTs are CSRF-exempt by prefix (public.html forms still include csrf())."""
import os
import time
from datetime import datetime, timedelta
from flask import (Blueprint, render_template, request, redirect, url_for, flash, session, current_app, abort,
                   send_file)
from sqlalchemy import func
from werkzeug.utils import secure_filename
from ..extensions import db
from ..models import Contact, Matter, Invoice, Document, Engagement, PortalToken, Firm, audit
from ..helpers import portal_required, portal_contact
from ..services.mail import send_email

bp = Blueprint("portal", __name__, url_prefix="/portal")

TOKEN_TTL_MIN = 30
RATE_LIMIT_COUNT = 3
RATE_LIMIT_MIN = 15
NEUTRAL_MSG = "If we have that email on file, we sent you a sign-in link. It works for 30 minutes."


@bp.route("/login", methods=["GET", "POST"])
def login():
    if portal_contact():
        return redirect(url_for("portal.home"))
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
                    link = f"{current_app.config['BASE_URL']}{url_for('portal.auth', token=tok.token)}"
                    html = (f"<p>Hello {contact.display_name},</p>"
                            f"<p>Use this link to sign in to your client portal at {firm.name}. It works once and "
                            f"expires in {TOKEN_TTL_MIN} minutes.</p><p><a href=\"{link}\">{link}</a></p>"
                            f"<p>If you did not ask for this, you can ignore this email.</p>")
                    text = (f"Sign in to your client portal at {firm.name} with this link (one use, "
                            f"{TOKEN_TTL_MIN} minutes):\n{link}\n\nIf you did not ask for this, ignore this email.")
                    send_email(contact.email, f"Your sign-in link for {firm.name}", html, text)
                    audit("portal_link_sent", "contact", contact.id, contact.email)
                    db.session.commit()
        flash(NEUTRAL_MSG, "ok")
    return render_template("portal/login.html")


@bp.route("/auth/<token>")
def auth(token):
    tok = PortalToken.query.filter_by(token=token).first()
    if not tok or tok.used_at or tok.expires_at < datetime.utcnow():
        return render_template("portal/expired.html"), 410
    tok.used_at = datetime.utcnow()
    session["portal_contact_id"] = tok.contact_id
    session.permanent = True
    audit("portal_login", "contact", tok.contact_id)
    db.session.commit()
    return redirect(url_for("portal.home"))


@bp.route("/logout", methods=["POST"])
def logout():
    session.pop("portal_contact_id", None)
    flash("You are signed out.", "ok")
    return redirect(url_for("portal.login"))


def _my_matters(contact):
    return Matter.query.filter_by(client_id=contact.id).order_by(Matter.status, Matter.number).all()


@bp.route("")
@portal_required
def home():
    c = portal_contact()
    matters = _my_matters(c)
    matter_ids = [m.id for m in matters]
    active = [m for m in matters if m.status in ("open", "pending")]
    invoices = Invoice.query.filter(Invoice.client_id == c.id, Invoice.status.in_(["sent", "viewed", "partial"])) \
        .order_by(Invoice.due_on.asc().nulls_last(), Invoice.issued_on.desc()).all()
    invoices = [i for i in invoices if i.balance_cents > 0]
    docs = Document.query.filter(Document.matter_id.in_(matter_ids or [0]), Document.shared_to_portal == True) \
        .order_by(Document.created_at.desc()).all()  # noqa: E712
    letters = Engagement.query.filter(Engagement.contact_id == c.id, Engagement.status.in_(["sent", "viewed"])) \
        .order_by(Engagement.sent_at.desc()).all()
    trust = c.trust_balance_cents()
    return render_template("portal/home.html", c=c, matters=active, all_matters=matters, invoices=invoices,
                           docs=docs, letters=letters, trust=trust)


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
    mid = request.form.get("matter_id", "")
    matter = db.session.get(Matter, int(mid)) if mid.isdigit() else None
    if not matter or matter.client_id != c.id:
        flash("Pick one of your matters.", "error")
        return redirect(url_for("portal.home"))
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a file to upload.", "error")
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
    flash(f"Uploaded {f.filename}. We will take a look.", "ok")
    return redirect(url_for("portal.home"))
