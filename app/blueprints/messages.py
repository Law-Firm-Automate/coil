"""Two-way SMS threads per contact via Twilio. Inbound webhook at /webhooks/twilio, so no url_prefix."""
import re
from datetime import timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, Response
from ..extensions import db
from ..models import Contact, Matter, Message, audit, now
from ..helpers import login_required, current_user
from ..services.sms import send_sms

bp = Blueprint("messages", __name__)


def _digits(s):
    return re.sub(r"\D", "", s or "")


def match_contact_by_phone(number):
    """Compare the last 10 digits so +1 prefixes and formatting do not matter."""
    d = _digits(number)[-10:]
    if not d:
        return None
    for c in Contact.query.filter(Contact.phone != "").all():
        if _digits(c.phone)[-10:] == d:
            return c
    return None


@bp.route("/messages")
@login_required
def index():
    cutoff = now() - timedelta(hours=24)
    rows = Message.query.order_by(Message.created_at.desc()).all()
    threads = []
    seen = set()
    unmatched = []
    for m in rows:
        if m.contact_id is None:
            if m.direction == "in":
                unmatched.append(m)
            continue
        if m.contact_id in seen:
            continue
        seen.add(m.contact_id)
        recent_in = Message.query.filter(Message.contact_id == m.contact_id, Message.direction == "in",
                                         Message.created_at >= cutoff).count()
        threads.append(dict(contact=m.contact, latest=m, recent_in=recent_in))
    contacts = Contact.query.filter(Contact.phone != "").order_by(Contact.last_name, Contact.first_name,
                                                                Contact.company_name).all()
    return render_template("messages/index.html", threads=threads, unmatched=unmatched, contacts=contacts)


@bp.route("/messages/<int:contact_id>")
@login_required
def thread(contact_id):
    c = db.session.get(Contact, contact_id) or abort(404)
    msgs = Message.query.filter_by(contact_id=c.id).order_by(Message.created_at, Message.id).all()
    matters = Matter.query.filter_by(client_id=c.id).order_by(Matter.status, Matter.created_at.desc()).all()
    return render_template("messages/thread.html", c=c, msgs=msgs, matters=matters,
                           matter_id=request.args.get("matter_id", type=int))


@bp.route("/messages/send", methods=["POST"])
@login_required
def send():
    c = db.session.get(Contact, request.form.get("contact_id", type=int) or 0) or abort(404)
    body = request.form.get("body", "").strip()
    matter_id = request.form.get("matter_id", type=int) or None
    if not body:
        flash("Type a message first.", "error")
        return redirect(url_for("messages.thread", contact_id=c.id))
    if not c.phone:
        flash(f"{c.display_name} has no phone number on file.", "error")
        return redirect(url_for("messages.thread", contact_id=c.id))
    provider_id, status = send_sms(c.phone, body)
    m = Message(contact_id=c.id, matter_id=matter_id, direction="out", channel="sms", to_addr=c.phone,
                from_addr=current_app.config.get("TWILIO_FROM_NUMBER", "") or "", body=body,
                provider_id=provider_id or "", status=status or "queued")
    db.session.add(m)
    db.session.flush()
    audit("send", "message", m.id, f"sms to {c.phone} ({status})", current_user().id)
    db.session.commit()
    if status == "unconfigured":
        flash("Twilio is not configured, so the message was stored but not delivered. See Settings > Integrations.", "")
    elif str(status).startswith("error"):
        flash(f"Twilio rejected the message ({status}). It was stored for the record.", "error")
    return redirect(url_for("messages.thread", contact_id=c.id))


@bp.route("/webhooks/twilio", methods=["POST"])
def twilio_inbound():
    frm = request.form.get("From", "")
    body = request.form.get("Body", "")
    sid = request.form.get("MessageSid", "")
    to = request.form.get("To", "")
    c = match_contact_by_phone(frm)
    if sid and Message.query.filter_by(provider_id=sid).first():
        return _twiml()
    m = Message(contact_id=c.id if c else None, direction="in", channel="sms", from_addr=frm, to_addr=to,
                body=body, provider_id=sid, status="received")
    db.session.add(m)
    db.session.flush()
    audit("receive", "message", m.id, f"sms from {frm}" + (f" ({c.display_name})" if c else " (no match)"))
    db.session.commit()
    return _twiml()


def _twiml():
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', mimetype="text/xml")
