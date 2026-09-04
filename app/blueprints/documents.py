"""Documents: files attached to matters, optionally shared to the client portal."""
import mimetypes
import os
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, send_file
from werkzeug.utils import secure_filename
from ..extensions import db
from ..models import Document, Matter, audit
from ..helpers import login_required, current_user

bp = Blueprint("documents", __name__, url_prefix="/documents")

MAX_BYTES = 25 * 1024 * 1024
BLOCKED_EXT = {"exe", "bat", "cmd", "com", "msi", "scr", "pif", "cpl", "dll", "sys", "sh", "bash", "zsh", "ps1",
               "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta", "jar", "app", "dmg", "pkg", "deb", "rpm", "apk",
               "py", "pyc", "rb", "pl", "php", "reg", "lnk"}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _next(default):
    n = request.form.get("next") or request.args.get("next") or ""
    return n if n.startswith("/") and not n.startswith("//") else default


def abs_path(doc):
    return os.path.join(current_app.config["UPLOAD_DIR"], doc.path)


TEXT_CAP = 200_000


def extract_text(path, ext):
    """Best-effort plain text from a stored file for conflict searching. Never raises."""
    try:
        if ext in ("txt", "md", "csv", "tsv", "log", "json", "html", "htm", "xml", "eml"):
            with open(path, "rb") as f:
                raw = f.read(TEXT_CAP * 2)
            text = raw.decode("utf-8", "ignore")
            if ext in ("html", "htm", "xml"):
                import re
                text = re.sub(r"<[^>]+>", " ", text)
            return " ".join(text.split())[:TEXT_CAP]
        if ext == "docx":
            import re
            import zipfile
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            return " ".join(re.sub(r"<[^>]+>", " ", xml).split())[:TEXT_CAP]
        if ext == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(path)
            parts = []
            for page in reader.pages[:200]:
                parts.append(page.extract_text() or "")
                if sum(len(x) for x in parts) > TEXT_CAP:
                    break
            return " ".join(" ".join(parts).split())[:TEXT_CAP]
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("text extraction failed for %s: %s", path, e)
    return ""


def store_upload(matter_id, file, user_id=None, shared=False, by_client=False):
    """Validate and save an uploaded file. Returns (Document, error)."""
    name = (file.filename or "").strip()
    if not name:
        return None, "Choose a file."
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in BLOCKED_EXT:
        return None, f"Files of type .{ext} are not allowed."
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size > MAX_BYTES:
        return None, "That file is over 25 MB."
    if size == 0:
        return None, "That file is empty."
    safe = secure_filename(name) or "file"
    rel_dir = str(matter_id)
    folder = os.path.join(current_app.config["UPLOAD_DIR"], rel_dir)
    os.makedirs(folder, exist_ok=True)
    fname = f"{uuid.uuid4().hex}_{safe}"
    full = os.path.join(folder, fname)
    file.save(full)
    mime = file.mimetype or mimetypes.guess_type(name)[0] or "application/octet-stream"
    doc = Document(matter_id=matter_id, name=name[:300], path=f"{rel_dir}/{fname}", size=size, mime=mime,
                   uploaded_by_id=user_id, shared_to_portal=shared, uploaded_by_client=by_client,
                   extracted_text=extract_text(full, ext))
    db.session.add(doc)
    return doc, None


@bp.route("")
@login_required
def index():
    matter_id = _int(request.args.get("matter_id"))
    q = Document.query
    if matter_id:
        q = q.filter_by(matter_id=matter_id)
    docs = q.order_by(Document.created_at.desc()).all()
    matter = db.session.get(Matter, matter_id) if matter_id else None
    matters = Matter.query.order_by(Matter.status, Matter.number).all()
    return render_template("documents/index.html", docs=docs, matter=matter, matter_id=matter_id, matters=matters,
                           total=sum(d.size or 0 for d in docs))


@bp.route("/upload", methods=["POST"])
@login_required
def upload():
    matter_id = _int(request.form.get("matter_id"))
    m = db.session.get(Matter, matter_id) if matter_id else None
    if not m:
        flash("Pick a matter to attach the file to.", "error")
        return redirect(_next(url_for("documents.index")))
    file = request.files.get("file")
    if not file:
        flash("Choose a file.", "error")
        return redirect(_next(url_for("documents.index", matter_id=m.id)))
    doc, err = store_upload(m.id, file, user_id=current_user().id, shared=bool(request.form.get("shared_to_portal")))
    if err:
        flash(err, "error")
        return redirect(_next(url_for("documents.index", matter_id=m.id)))
    db.session.flush()
    audit("upload", "document", doc.id, doc.name, current_user().id)
    audit("upload_document", "matter", m.id, doc.name, current_user().id)
    db.session.commit()
    flash(f"Uploaded {doc.name}.", "ok")
    return redirect(_next(url_for("documents.index", matter_id=m.id)))


@bp.route("/<int:id>/download")
@login_required
def download(id):
    doc = db.session.get(Document, id) or abort(404)
    p = abs_path(doc)
    if not os.path.isfile(p):
        abort(404)
    return send_file(p, as_attachment=True, download_name=doc.name, mimetype=doc.mime or None)


@bp.route("/<int:id>/share", methods=["POST"])
@login_required
def share(id):
    doc = db.session.get(Document, id) or abort(404)
    doc.shared_to_portal = not doc.shared_to_portal
    audit("share" if doc.shared_to_portal else "unshare", "document", doc.id, doc.name, current_user().id)
    db.session.commit()
    flash(f"{doc.name} is {'now visible' if doc.shared_to_portal else 'no longer visible'} in the client portal.", "ok")
    return redirect(_next(url_for("documents.index", matter_id=doc.matter_id)))


@bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def delete(id):
    doc = db.session.get(Document, id) or abort(404)
    p = abs_path(doc)
    try:
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        current_app.logger.warning("could not remove %s", p)
    matter_id, name = doc.matter_id, doc.name
    audit("delete", "document", doc.id, name, current_user().id)
    db.session.delete(doc)
    db.session.commit()
    flash(f"Deleted {name}.", "ok")
    return redirect(_next(url_for("documents.index", matter_id=matter_id)))
