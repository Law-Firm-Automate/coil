"""Documents: files attached to matters, optionally shared to the client portal.

Phase 3 additions: versions (new-version upload, history), folders and tags with filters and a bulk move/tag form,
and full-text search over name, tags, folder and the text extracted at upload.
"""
import mimetypes
import os
import re
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
                text = re.sub(r"<[^>]+>", " ", text)
            return " ".join(text.split())[:TEXT_CAP]
        if ext == "docx":
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


# ---- folders and tags ----
def clean_folder(s):
    """'  pleadings / Motions/ ' -> 'pleadings/Motions'. Empty stays empty (root)."""
    parts = [p.strip() for p in str(s or "").replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p)[:300]


def parse_tags(s):
    """'Urgent, filed,urgent' -> ['urgent', 'filed'] (lowercase, deduped, order kept)."""
    out = []
    for t in str(s or "").split(","):
        t = t.strip().lower()
        if t and t not in out:
            out.append(t)
    return out


def tags_str(tags):
    return ", ".join(tags)[:300]


def doc_tags(doc):
    return parse_tags(doc.tags)


def store_bytes(matter_id, name, data, mime="", user_id=None, shared=False, by_client=False, folder="", tags="",
                version=1, version_of_id=None, is_current=True):
    """Write raw bytes as a new Document row (used by uploads, new versions and email attachments).
    Validates the extension and size. Returns (Document, error)."""
    name = (name or "").strip()
    if not name:
        return None, "Choose a file."
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in BLOCKED_EXT:
        return None, f"Files of type .{ext} are not allowed."
    size = len(data)
    if size > MAX_BYTES:
        return None, "That file is over 25 MB."
    if size == 0:
        return None, "That file is empty."
    safe = secure_filename(name) or "file"
    rel_dir = str(matter_id)
    folder_abs = os.path.join(current_app.config["UPLOAD_DIR"], rel_dir)
    os.makedirs(folder_abs, exist_ok=True)
    fname = f"{uuid.uuid4().hex}_{safe}"
    full = os.path.join(folder_abs, fname)
    with open(full, "wb") as f:
        f.write(data)
    mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    doc = Document(matter_id=matter_id, name=name[:300], path=f"{rel_dir}/{fname}", size=size, mime=mime,
                   uploaded_by_id=user_id, shared_to_portal=shared, uploaded_by_client=by_client,
                   extracted_text=extract_text(full, ext), folder=clean_folder(folder), tags=tags_str(parse_tags(tags)),
                   version=version, version_of_id=version_of_id, is_current=is_current)
    db.session.add(doc)
    return doc, None


def store_upload(matter_id, file, user_id=None, shared=False, by_client=False, folder="", tags="", **kw):
    """Validate and save an uploaded file. Returns (Document, error)."""
    name = (file.filename or "").strip()
    if not name:
        return None, "Choose a file."
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size > MAX_BYTES:
        return None, "That file is over 25 MB."
    return store_bytes(matter_id, name, file.stream.read(), mime=file.mimetype or "", user_id=user_id, shared=shared,
                       by_client=by_client, folder=folder, tags=tags, **kw)


# ---- versions ----
def version_family(doc):
    """Every version of a document (root first), newest version first."""
    rid = doc.root_id
    return Document.query.filter(db.or_(Document.id == rid, Document.version_of_id == rid)) \
        .order_by(Document.version.desc(), Document.id.desc()).all()


def _apply_filters(q, matter_id=None, folder=None, tag=None):
    if matter_id:
        q = q.filter(Document.matter_id == matter_id)
    if folder:
        q = q.filter(db.or_(Document.folder == folder, Document.folder.like(folder.replace("%", "") + "/%")))
    if tag:
        # tags are stored lowercase, comma+space separated
        q = q.filter(db.or_(Document.tags == tag, Document.tags.like(f"{tag}, %"), Document.tags.like(f"%, {tag}"),
                            Document.tags.like(f"%, {tag}, %")))
    return q


@bp.route("")
@login_required
def index():
    matter_id = _int(request.args.get("matter_id"))
    folder = clean_folder(request.args.get("folder"))
    tag = (request.args.get("tag") or "").strip().lower()
    q = _apply_filters(Document.query.filter(Document.is_current == True), matter_id, folder, tag)  # noqa: E712
    docs = q.order_by(Document.folder, Document.created_at.desc()).all()
    matter = db.session.get(Matter, matter_id) if matter_id else None
    matters = Matter.query.order_by(Matter.status, Matter.number).all()
    # Group by folder, root folder first, then alphabetical.
    groups = []
    for d in docs:
        if not groups or groups[-1][0] != (d.folder or ""):
            groups.append((d.folder or "", []))
        groups[-1][1].append(d)
    groups.sort(key=lambda g: (g[0] != "", g[0].lower()))
    scope = Document.query.filter(Document.is_current == True)  # noqa: E712
    if matter_id:
        scope = scope.filter(Document.matter_id == matter_id)
    folders = sorted({d.folder for d in scope.with_entities(Document.folder).distinct() if d.folder}, key=str.lower)
    all_tags = sorted({t for (ts,) in scope.with_entities(Document.tags).distinct() for t in parse_tags(ts)})
    return render_template("documents/index.html", docs=docs, groups=groups, matter=matter, matter_id=matter_id,
                           matters=matters, folders=folders, all_tags=all_tags, folder=folder, tag=tag,
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
    doc, err = store_upload(m.id, file, user_id=current_user().id, shared=bool(request.form.get("shared_to_portal")),
                            folder=request.form.get("folder", ""), tags=request.form.get("tags", ""))
    if err:
        flash(err, "error")
        return redirect(_next(url_for("documents.index", matter_id=m.id)))
    db.session.flush()
    audit("upload", "document", doc.id, doc.name, current_user().id)
    audit("upload_document", "matter", m.id, doc.name, current_user().id)
    db.session.commit()
    flash(f"Uploaded {doc.name}.", "ok")
    return redirect(_next(url_for("documents.index", matter_id=m.id)))


@bp.route("/<int:id>/versions")
@login_required
def versions(id):
    doc = db.session.get(Document, id) or abort(404)
    family = version_family(doc)
    current = next((d for d in family if d.is_current), family[0])
    return render_template("documents/versions.html", doc=current, family=family)


@bp.route("/<int:id>/new-version", methods=["POST"])
@login_required
def new_version(id):
    doc = db.session.get(Document, id) or abort(404)
    file = request.files.get("file")
    if not file or not (file.filename or "").strip():
        flash("Choose a file for the new version.", "error")
        return redirect(_next(url_for("documents.versions", id=doc.id)))
    family = version_family(doc)
    root_id = doc.root_id
    next_v = max(d.version or 1 for d in family) + 1
    new, err = store_upload(doc.matter_id, file, user_id=current_user().id, shared=bool(doc.shared_to_portal),
                            folder=doc.folder or "", tags=doc.tags or "", version=next_v, version_of_id=root_id,
                            is_current=True)
    if err:
        flash(err, "error")
        return redirect(_next(url_for("documents.versions", id=doc.id)))
    for d in family:
        d.is_current = False
    db.session.flush()
    audit("new_version", "document", new.id, f"{new.name} v{next_v} (replaces v{doc.version or 1})", current_user().id)
    audit("upload_document", "matter", doc.matter_id, f"{new.name} v{next_v}", current_user().id)
    db.session.commit()
    flash(f"Uploaded version {next_v} of {new.name}.", "ok")
    return redirect(_next(url_for("documents.versions", id=new.id)))


@bp.route("/bulk", methods=["POST"])
@login_required
def bulk():
    """Move and/or tag the checked documents in one go. Applies to the whole version family of each."""
    ids = [i for i in (_int(x) for x in request.form.getlist("doc_id")) if i]
    if not ids:
        flash("Tick at least one document first.", "error")
        return redirect(_next(url_for("documents.index")))
    folder = clean_folder(request.form.get("folder"))
    set_folder = bool(request.form.get("set_folder"))
    tags = parse_tags(request.form.get("tags"))
    replace = request.form.get("tag_mode") == "replace"
    docs = Document.query.filter(Document.id.in_(ids)).all()
    touched = 0
    for d in docs:
        for v in version_family(d):
            if set_folder:
                v.folder = folder
            if tags or replace:
                v.tags = tags_str(tags if replace else parse_tags(v.tags) + [t for t in tags if t not in parse_tags(v.tags)])
        touched += 1
        audit("bulk_update", "document", d.id,
              (f"folder={folder or '(root)'}; " if set_folder else "") + (f"tags={tags_str(tags)}" if tags or replace else ""),
              current_user().id)
    db.session.commit()
    what = []
    if set_folder:
        what.append(f"moved to {folder or 'the root folder'}")
    if tags or replace:
        what.append(f"tags {'set to' if replace else 'added:'} {tags_str(tags) or '(none)'}")
    flash(f"{touched} document{'s' if touched != 1 else ''} {', '.join(what) if what else 'unchanged (nothing to apply)'}.", "ok")
    return redirect(_next(url_for("documents.index")))


# ---- search ----
def _norm(s):
    return " ".join(str(s or "").lower().split())


def snippet(text, q, width=90):
    """A short window of `text` around the first case-insensitive hit of `q`, or the start of the text."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    i = text.lower().find(q.lower())
    if i < 0:
        return text[:width * 2] + ("..." if len(text) > width * 2 else "")
    a = max(0, i - width)
    b = min(len(text), i + len(q) + width)
    return ("..." if a > 0 else "") + text[a:b] + ("..." if b < len(text) else "")


def search_documents(q, matter_id=None, limit=200):
    """Current versions whose name, tags, folder or extracted text contain every word of q. Returns [(doc, where, snippet)]."""
    words = [w for w in _norm(q).split() if w]
    if not words:
        return []
    query = Document.query.filter(Document.is_current == True)  # noqa: E712
    if matter_id:
        query = query.filter(Document.matter_id == matter_id)
    for w in words:
        like = f"%{w}%"
        query = query.filter(db.or_(Document.name.ilike(like), Document.tags.ilike(like), Document.folder.ilike(like),
                                    Document.extracted_text.ilike(like)))
    out = []
    for d in query.order_by(Document.created_at.desc()).limit(limit).all():
        first = words[0]
        if first in _norm(d.name):
            out.append((d, "name", d.name))
        elif first in _norm(d.tags):
            out.append((d, "tag", d.tags))
        elif first in _norm(d.folder):
            out.append((d, "folder", d.folder))
        else:
            out.append((d, "text", snippet(d.extracted_text, first)))
    return out


@bp.route("/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    matter_id = _int(request.args.get("matter_id"))
    matter = db.session.get(Matter, matter_id) if matter_id else None
    results = search_documents(q, matter_id) if q else []
    return render_template("documents/search.html", q=q, results=results, matter=matter, matter_id=matter_id)


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
    # Keep the version chain intact: re-root on the oldest remaining version and promote the newest to current.
    rest = [d for d in version_family(doc) if d.id != doc.id]
    if rest:
        oldest = min(rest, key=lambda d: (d.version or 1, d.id))
        if doc.version_of_id is None:  # deleting the root
            oldest.version_of_id = None
            for d in rest:
                if d.id != oldest.id:
                    d.version_of_id = oldest.id
        if doc.is_current:
            newest = max(rest, key=lambda d: (d.version or 1, d.id))
            newest.is_current = True
    audit("delete", "document", doc.id, f"{name} v{doc.version or 1}", current_user().id)
    db.session.delete(doc)
    db.session.commit()
    flash(f"Deleted {name}.", "ok")
    return redirect(_next(url_for("documents.index", matter_id=matter_id)))
