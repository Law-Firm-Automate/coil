"""Email filing: pull unseen mail from an IMAP mailbox and attach it to matters.

Run with `python -m app.cli emailin`. Matching order:
  1. a matter number in square brackets anywhere in the subject or body, like [M-1002];
  2. the sender's email belongs to a client contact with exactly one open matter.
Matched mail becomes a Message (channel=email, direction=in) on the matter and each attachment becomes a Document in
the "Email" folder. Unmatched mail is stored with no contact or matter and listed at /messages/unfiled, where staff pick
a matter; attachments wait under UPLOAD_DIR/unfiled/<message id>/ until then. Everything is idempotent on Message-ID.

`fetch_unseen()` talks to IMAP and returns plain dicts; `file_email(msg)` is pure filing logic, so tests monkeypatch
the fetch and feed dicts straight in.

This blueprint has no url_prefix: it also serves the PWA files at the site root (/manifest.webmanifest, /sw.js,
/offline) because a service worker only controls the scope it is served from.
"""
import email
import email.policy
import imaplib
import os
import re
import shutil
from email.utils import parseaddr
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, send_from_directory
from werkzeug.utils import secure_filename
from ..extensions import db
from ..models import Contact, Matter, Message, Document, audit
from ..helpers import login_required, current_user
from .documents import store_bytes, BLOCKED_EXT

bp = Blueprint("emailin", __name__)

MATTER_RE = re.compile(r"\[\s*([A-Za-z]{1,6}-?\d{1,10})\s*\]")
BODY_CAP = 100_000


# ---------------------------------------------------------------------------
# parsing (pure)
# ---------------------------------------------------------------------------
def _strip_html(html):
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()


def parse_email(raw):
    """RFC 822 bytes -> dict(message_id, from_addr, from_name, to_addr, subject, body, date, attachments=[(name, bytes, mime)])."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    from_name, from_addr = parseaddr(str(msg.get("From", "")))
    _, to_addr = parseaddr(str(msg.get("To", "")))
    text = html = ""
    attachments = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "")
        if fname or disp == "attachment":
            try:
                data = part.get_payload(decode=True) or b""
            except Exception:  # noqa: BLE001
                data = b""
            if data:
                attachments.append((fname or f"attachment.{ctype.split('/')[-1] or 'bin'}", data, ctype))
            continue
        if ctype == "text/plain" and not text:
            try:
                text = part.get_content()
            except Exception:  # noqa: BLE001
                text = (part.get_payload(decode=True) or b"").decode("utf-8", "ignore")
        elif ctype == "text/html" and not html:
            try:
                html = part.get_content()
            except Exception:  # noqa: BLE001
                html = (part.get_payload(decode=True) or b"").decode("utf-8", "ignore")
    body = (text or _strip_html(html) or "").strip()[:BODY_CAP]
    return dict(message_id=(str(msg.get("Message-ID", "")) or "").strip()[:300], from_addr=(from_addr or "").lower()[:200],
                from_name=from_name or "", to_addr=(to_addr or "").lower()[:200], subject=str(msg.get("Subject", "") or "")[:300],
                body=body, date=str(msg.get("Date", "") or ""), attachments=attachments)


# ---------------------------------------------------------------------------
# IMAP (the only part that touches the network)
# ---------------------------------------------------------------------------
def imap_configured(cfg=None):
    cfg = cfg or current_app.config
    return bool(cfg.get("IMAP_HOST") and cfg.get("IMAP_USER"))


def fetch_unseen(cfg=None, limit=200):
    """Connect, pull every UNSEEN message in IMAP_FOLDER, mark it seen, and return a list of parsed dicts.
    Returns [] when IMAP is not configured. Tests replace this function."""
    cfg = cfg or current_app.config
    if not imap_configured(cfg):
        return []
    port = int(cfg.get("IMAP_PORT") or 993)
    cls = imaplib.IMAP4_SSL if port == 993 else imaplib.IMAP4
    conn = cls(cfg["IMAP_HOST"], port)
    out = []
    try:
        if cls is imaplib.IMAP4:
            try:
                conn.starttls()
            except Exception:  # noqa: BLE001
                pass
        conn.login(cfg["IMAP_USER"], cfg.get("IMAP_PASS") or "")
        conn.select(cfg.get("IMAP_FOLDER") or "INBOX")
        status, data = conn.search(None, "UNSEEN")
        ids = (data[0].split() if status == "OK" and data and data[0] else [])[:limit]
        for num in ids:
            status, parts = conn.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not parts:
                continue
            raw = next((p[1] for p in parts if isinstance(p, tuple) and len(p) > 1), None)
            if not raw:
                continue
            parsed = parse_email(raw)
            parsed["uid"] = num.decode() if isinstance(num, bytes) else str(num)
            out.append(parsed)
            conn.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
# filing (pure: dict in, rows out)
# ---------------------------------------------------------------------------
def find_matter(msg):
    """Matter for a parsed email, or None. Bracketed number first, then sender email -> client with one open matter."""
    hay = f"{msg.get('subject', '')}\n{msg.get('body', '')}"
    for m in MATTER_RE.finditer(hay):
        token = m.group(1).upper()
        matter = Matter.query.filter(db.func.upper(Matter.number) == token).first()
        if not matter and "-" not in token:
            matter = Matter.query.filter(db.func.upper(Matter.number) == re.sub(r"^([A-Z]+)(\d+)$", r"\1-\2", token)).first()
        if matter:
            return matter
    sender = (msg.get("from_addr") or "").strip().lower()
    if not sender:
        return None
    contacts = Contact.query.filter(db.func.lower(Contact.email) == sender).all()
    if not contacts:
        return None
    open_matters = Matter.query.filter(Matter.client_id.in_([c.id for c in contacts]), Matter.status == "open").all()
    return open_matters[0] if len(open_matters) == 1 else None


def _unfiled_dir(message_row_id):
    return os.path.join(current_app.config["UPLOAD_DIR"], "unfiled", str(message_row_id))


def held_attachments(message_row):
    """Files parked for an unfiled email: [(filename, absolute path)]."""
    d = _unfiled_dir(message_row.id)
    if not os.path.isdir(d):
        return []
    return sorted((f, os.path.join(d, f)) for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))


def attach_to_matter(message_row, matter, attachments, user_id=None):
    """Save (name, bytes, mime) tuples as Documents in the Email folder. Returns the Documents created."""
    docs = []
    for name, data, mime in attachments:
        doc, err = store_bytes(matter.id, name, data, mime=mime or "", user_id=user_id, folder="Email",
                               tags="email")
        if err:
            audit("email_attachment_skipped", "message", message_row.id, f"{name}: {err}")
            continue
        docs.append(doc)
    return docs


def file_email(msg, user_id=None):
    """File one parsed email. Returns (Message, created). Idempotent on message_id."""
    mid = (msg.get("message_id") or "").strip()
    if mid:
        existing = Message.query.filter_by(message_id=mid).first()
        if existing:
            return existing, False
    matter = find_matter(msg)
    attachments = list(msg.get("attachments") or [])
    row = Message(contact_id=matter.client_id if matter else None, matter_id=matter.id if matter else None,
                  direction="in", channel="email", to_addr=msg.get("to_addr") or "", from_addr=msg.get("from_addr") or "",
                  body=(msg.get("body") or "")[:BODY_CAP], subject=(msg.get("subject") or "")[:300], message_id=mid[:300],
                  has_attachments=bool(attachments), status="received" if matter else "unfiled")
    db.session.add(row)
    db.session.flush()
    if matter:
        docs = attach_to_matter(row, matter, attachments, user_id=user_id)
        audit("email_filed", "message", row.id,
              f"from {row.from_addr} to {matter.number}: {row.subject[:120]} ({len(docs)} attachment(s))", user_id)
        audit("email_filed", "matter", matter.id, f"{row.subject[:120]} from {row.from_addr}", user_id)
    else:
        if attachments:
            d = _unfiled_dir(row.id)
            os.makedirs(d, exist_ok=True)
            for name, data, _mime in attachments:
                safe = secure_filename(name) or "attachment"
                target = os.path.join(d, safe)
                n = 1
                while os.path.exists(target):
                    n += 1
                    stem, _, ext = safe.rpartition(".")
                    target = os.path.join(d, f"{stem or safe}-{n}.{ext}" if ext and stem else f"{safe}-{n}")
                with open(target, "wb") as f:
                    f.write(data)
        audit("email_unfiled", "message", row.id, f"from {row.from_addr}: {row.subject[:120]} (no matter matched)", user_id)
    db.session.commit()
    return row, True


def run_emailin(fetch=None):
    """Fetch and file everything unseen. Returns dict(filed, unfiled, skipped)."""
    fetch = fetch or fetch_unseen
    counts = dict(filed=0, unfiled=0, skipped=0)
    for msg in fetch():
        row, created = file_email(msg)
        if not created:
            counts["skipped"] += 1
        elif row.matter_id:
            counts["filed"] += 1
        else:
            counts["unfiled"] += 1
    return counts


# ---------------------------------------------------------------------------
# review list for unmatched mail
# ---------------------------------------------------------------------------
def unfiled_query():
    return Message.query.filter(Message.channel == "email", Message.direction == "in", Message.matter_id.is_(None)) \
        .order_by(Message.created_at.desc())


@bp.route("/messages/unfiled")
@login_required
def unfiled():
    rows = unfiled_query().all()
    matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    held = {r.id: [name for name, _p in held_attachments(r)] for r in rows}
    return render_template("documents/unfiled.html", rows=rows, matters=matters, held=held,
                           imap_user=current_app.config.get("IMAP_USER") or "", configured=imap_configured())


@bp.route("/messages/unfiled/<int:id>/file", methods=["POST"])
@login_required
def file_unfiled(id):
    row = db.session.get(Message, id) or abort(404)
    if row.matter_id:
        flash("That email is already filed.", "")
        return redirect(url_for("emailin.unfiled"))
    matter = db.session.get(Matter, request.form.get("matter_id", type=int) or 0)
    if not matter:
        flash("Pick a matter.", "error")
        return redirect(url_for("emailin.unfiled"))
    u = current_user()
    row.matter_id = matter.id
    row.contact_id = matter.client_id
    row.status = "received"
    attachments = []
    for name, path in held_attachments(row):
        with open(path, "rb") as f:
            attachments.append((name, f.read(), ""))
    docs = attach_to_matter(row, matter, attachments, user_id=u.id)
    shutil.rmtree(_unfiled_dir(row.id), ignore_errors=True)
    audit("email_filed", "message", row.id, f"filed to {matter.number} by staff ({len(docs)} attachment(s))", u.id)
    audit("email_filed", "matter", matter.id, f"{row.subject[:120]} from {row.from_addr}", u.id)
    db.session.commit()
    flash(f"Filed to {matter.label}" + (f" with {len(docs)} attachment(s)" if docs else "") + ".", "ok")
    return redirect(url_for("emailin.unfiled"))


@bp.route("/messages/unfiled/<int:id>/delete", methods=["POST"])
@login_required
def delete_unfiled(id):
    """Drop junk that will never belong to a matter. The Message-ID stays out of the database, so a re-fetch of the
    same mail would come back; IMAP has already marked it seen, so in practice it does not."""
    row = db.session.get(Message, id) or abort(404)
    if row.matter_id or row.channel != "email":
        abort(400)
    shutil.rmtree(_unfiled_dir(row.id), ignore_errors=True)
    audit("email_discarded", "message", row.id, f"from {row.from_addr}: {row.subject[:120]}", current_user().id)
    db.session.delete(row)
    db.session.commit()
    flash("Discarded.", "ok")
    return redirect(url_for("emailin.unfiled"))


# ---------------------------------------------------------------------------
# PWA files at the site root
# ---------------------------------------------------------------------------
def _static_dir():
    return current_app.static_folder


@bp.route("/manifest.webmanifest")
def manifest():
    r = send_from_directory(_static_dir(), "manifest.webmanifest", mimetype="application/manifest+json")
    r.headers["Cache-Control"] = "public, max-age=3600"
    return r


@bp.route("/sw.js")
def service_worker():
    r = send_from_directory(_static_dir(), "sw.js", mimetype="text/javascript")
    r.headers["Service-Worker-Allowed"] = "/"
    r.headers["Cache-Control"] = "no-cache"
    return r


@bp.route("/offline")
def offline():
    return send_from_directory(_static_dir(), "offline.html", mimetype="text/html")
