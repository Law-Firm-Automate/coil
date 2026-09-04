"""Document automation: .docx or HTML templates with merge fields, generated per matter into a Document."""
import mimetypes
import os
import re
import uuid
import zipfile
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, send_file
from jinja2 import Environment, TemplateSyntaxError, meta
from werkzeug.utils import secure_filename
from ..extensions import db
from ..models import DocTemplate, Document, Matter, Firm, Office, audit
from ..helpers import login_required, current_user

bp = Blueprint("doctemplates", __name__, url_prefix="/doctemplates")

KINDS = ["html", "docx"]
MAX_BYTES = 25 * 1024 * 1024
PRACTICE_AREAS = ["Estate Planning", "Litigation", "Business", "Real Estate", "Family", "Criminal Defense",
                  "Personal Injury", "Immigration", "Employment", "Bankruptcy", "Other"]

# Every field the generate page can prefill, with a one-line description for the help panel.
STANDARD_FIELDS = [
    ("firm_name", "Firm name"), ("firm_address", "Firm address, one line"), ("firm_phone", "Firm phone"),
    ("firm_email", "Firm email"), ("office_address", "Address of the matter's office (falls back to the firm)"),
    ("attorney_name", "Responsible attorney"), ("attorney_email", "Responsible attorney's email"),
    ("today", "Today's date, spelled out"), ("client_name", "Client display name"),
    ("client_first_name", "Client first name"), ("client_last_name", "Client last name"),
    ("client_address", "Client address, one line"), ("client_email", "Client email"), ("client_phone", "Client phone"),
    ("matter_name", "Matter name"), ("matter_number", "Matter number"), ("practice_area", "Practice area"),
    ("court", "Court"), ("case_number", "Case number"), ("adverse_parties", "Adverse parties, comma separated"),
]

SAMPLE_CLOSING_LETTER = """<p>{{ today }}</p>
<p>{{ client_name }}<br>{{ client_address }}</p>
<p>Re: {{ matter_name }} (our file {{ matter_number }})</p>
<p>Dear {{ client_first_name }},</p>
<p>Our work on the matter above is complete and we are closing our file. Thank you for trusting {{ firm_name }} with it.</p>
<p>Please keep this letter with your records. We will retain our file for the period required by our records policy, after which it may be destroyed without further notice. If you would like any original documents returned, or a copy of the file, let us know in writing before then.</p>
<p>Closing the file ends our representation in this matter. If anything new comes up on it, or if we can help with something else, call us and we will open a new file. Until then we will not be monitoring deadlines or developments on this matter.</p>
<p>It has been a pleasure working with you.</p>
<p>Sincerely,</p>
<p>{{ attorney_name }}<br>{{ firm_name }}<br>{{ firm_phone }} &middot; {{ firm_email }}</p>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def snake(s):
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(s or "")).strip("_").lower()
    return s or "field"


def one_line(s):
    return ", ".join(line.strip() for line in str(s or "").splitlines() if line.strip())


def abs_path(rel):
    return os.path.join(current_app.config["UPLOAD_DIR"], rel)


_JINJA_VAR = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)")
_PLAIN_VAR = re.compile(r"(?<!{){([A-Z][A-Z0-9_]{1,60})}(?!})")


def detect_fields_text(text):
    """Merge fields in a block of text: {{ jinja_style }} plus plain {FIELD} style, in order of first appearance."""
    found = []
    for m in _JINJA_VAR.finditer(text):
        if m.group(1) not in found:
            found.append(m.group(1))
    for m in _PLAIN_VAR.finditer(text):
        if m.group(1) not in found:
            found.append(m.group(1))
    return found


def detect_fields_docx(path):
    """Scan document.xml plus any headers and footers, with the XML tags stripped so split runs still match."""
    text_parts = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name == "word/document.xml" or re.match(r"word/(header|footer)\d*\.xml$", name):
                xml = z.read(name).decode("utf-8", "ignore")
                text_parts.append(re.sub(r"<[^>]+>", "", xml))
    return detect_fields_text("\n".join(text_parts))


def detect_fields_html(body):
    try:
        ast = Environment().parse(body or "")
        names = sorted(meta.find_undeclared_variables(ast))
    except TemplateSyntaxError:
        names = []
    # keep source order where we can, then anything jinja found that the regex did not
    ordered = detect_fields_text(body or "")
    return ordered + [n for n in names if n not in ordered]


def build_context(matter, user=None):
    """Prefilled merge values for a matter. Unknown template fields are left for the form."""
    f = Firm.get()
    c = matter.client
    attorney = matter.responsible or user
    office = matter.office or Office.query.filter_by(is_default=True).first()
    adverse = [p.name for p in matter.parties if p.role == "adverse" and p.name]
    ctx = dict(
        firm_name=f.name or "", firm_address=one_line(f.address), firm_phone=f.phone or "", firm_email=f.email or "",
        office_address=one_line(office.address) if office and office.address else one_line(f.address),
        attorney_name=(attorney.name if attorney else f.name) or "",
        attorney_email=(attorney.email if attorney else f.email) or "",
        today=date.today().strftime("%B %-d, %Y"),
        client_name=c.display_name if c else "", client_first_name=(c.first_name or c.display_name) if c else "",
        client_last_name=(c.last_name or "") if c else "", client_address=one_line(c.address) if c else "",
        client_email=(c.email or "") if c else "", client_phone=(c.phone or "") if c else "",
        matter_name=matter.name or "", matter_number=matter.number or "", practice_area=matter.practice_area or "",
        court=matter.court or "", case_number=matter.case_number or "", adverse_parties=", ".join(adverse),
    )
    for k, v in matter.custom_fields.items():
        ctx[f"cf_{snake(k)}"] = "" if v is None else str(v)
    if c:
        for k, v in c.custom_fields.items():
            ctx[f"ccf_{snake(k)}"] = "" if v is None else str(v)
    return ctx


def ensure_sample_template():
    if DocTemplate.query.count():
        return
    t = DocTemplate(name="Closing letter (sample)", kind="html", practice_area="",
                    description="Generic end-of-representation letter. Edit it to match how your firm closes files.",
                    body_html=SAMPLE_CLOSING_LETTER, fields_json=_json(detect_fields_html(SAMPLE_CLOSING_LETTER)),
                    is_active=True)
    db.session.add(t)
    db.session.commit()


def _json(v):
    import json
    return json.dumps(v)


def _store_docx(file):
    """Save an uploaded .docx under UPLOAD_DIR/templates/. Returns (rel_path, error)."""
    name = (file.filename or "").strip()
    if not name.lower().endswith(".docx"):
        return None, "Upload a .docx file (Word format)."
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0:
        return None, "That file is empty."
    if size > MAX_BYTES:
        return None, "That file is over 25 MB."
    folder = os.path.join(current_app.config["UPLOAD_DIR"], "templates")
    os.makedirs(folder, exist_ok=True)
    fname = f"{uuid.uuid4().hex}_{secure_filename(name) or 'template.docx'}"
    full = os.path.join(folder, fname)
    file.save(full)
    try:
        with zipfile.ZipFile(full) as z:
            z.getinfo("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        os.remove(full)
        return None, "That file is not a valid .docx."
    return f"templates/{fname}", None


def _fill(t, form, files):
    """Apply the form to a template. Returns an error string or None."""
    t.name = form.get("name", "").strip()
    t.practice_area = form.get("practice_area", "").strip()
    t.description = form.get("description", "").strip()
    t.is_active = bool(form.get("is_active"))
    kind = form.get("kind", t.kind or "html")
    if kind not in KINDS:
        kind = "html"
    if not t.name:
        return "A name is required."
    file = files.get("file")
    if kind == "docx":
        if file and file.filename:
            rel, err = _store_docx(file)
            if err:
                return err
            if t.path:
                try:
                    os.remove(abs_path(t.path))
                except OSError:
                    pass
            t.path = rel
        if not t.path:
            return "Upload a .docx file for a Word template."
        t.kind = "docx"
        t.fields_json = _json(detect_fields_docx(abs_path(t.path)))
        return None
    body = form.get("body_html", "")
    try:
        Environment(autoescape=True).from_string(body)
    except TemplateSyntaxError as e:
        return f"Template syntax problem on line {e.lineno}: {e.message}"
    t.kind = "html"
    t.body_html = body
    t.fields_json = _json(detect_fields_html(body))
    return None


def _form_context(t, is_new):
    return dict(t=t, is_new=is_new, kinds=KINDS, practice_areas=PRACTICE_AREAS, standard_fields=STANDARD_FIELDS)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@bp.route("")
@login_required
def index():
    ensure_sample_template()
    matter_id = _int(request.args.get("matter_id"))
    matter = db.session.get(Matter, matter_id) if matter_id else None
    rows = DocTemplate.query.order_by(DocTemplate.is_active.desc(), DocTemplate.name).all()
    usage = dict(db.session.query(Document.template_id, db.func.count(Document.id)).filter(
        Document.template_id.isnot(None)).group_by(Document.template_id).all())
    matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    return render_template("doctemplates/index.html", rows=rows, usage=usage, matter=matter, matters=matters,
                           standard_fields=STANDARD_FIELDS)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    t = DocTemplate(kind="html", is_active=True, body_html="")
    if request.method == "POST":
        err = _fill(t, request.form, request.files)
        if err:
            flash(err, "error")
            return render_template("doctemplates/form.html", **_form_context(t, True))
        db.session.add(t)
        db.session.flush()
        audit("create", "doc_template", t.id, t.name, current_user().id)
        db.session.commit()
        flash(f"Template {t.name} saved with {len(t.fields)} merge field(s).", "ok")
        return redirect(url_for("doctemplates.edit", id=t.id))
    k = request.args.get("kind", "html")
    t.kind = k if k in KINDS else "html"
    return render_template("doctemplates/form.html", **_form_context(t, True))


@bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit(id):
    t = db.session.get(DocTemplate, id) or abort(404)
    if request.method == "POST":
        err = _fill(t, request.form, request.files)
        if err:
            flash(err, "error")
            return render_template("doctemplates/form.html", **_form_context(t, False))
        db.session.commit()
        flash(f"Template saved with {len(t.fields)} merge field(s).", "ok")
        return redirect(url_for("doctemplates.edit", id=t.id))
    return render_template("doctemplates/form.html", **_form_context(t, False))


@bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def delete(id):
    t = db.session.get(DocTemplate, id) or abort(404)
    used = Document.query.filter_by(template_id=t.id).count()
    if used:
        t.is_active = False
        db.session.commit()
        flash(f"{t.name} was used for {used} document(s), so it was deactivated instead of deleted.", "ok")
        return redirect(url_for("doctemplates.index"))
    if t.path:
        try:
            os.remove(abs_path(t.path))
        except OSError:
            pass
    audit("delete", "doc_template", t.id, t.name, current_user().id)
    db.session.delete(t)
    db.session.commit()
    flash(f"Deleted template {t.name}.", "ok")
    return redirect(url_for("doctemplates.index"))


@bp.route("/<int:id>/download")
@login_required
def download(id):
    t = db.session.get(DocTemplate, id) or abort(404)
    if t.kind != "docx" or not t.path or not os.path.isfile(abs_path(t.path)):
        abort(404)
    return send_file(abs_path(t.path), as_attachment=True, download_name=f"{t.name}.docx",
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def _write_output(matter_id, name, data):
    """Write generated bytes under UPLOAD_DIR/<matter_id>/ and return (rel_path, size)."""
    rel_dir = str(matter_id)
    folder = os.path.join(current_app.config["UPLOAD_DIR"], rel_dir)
    os.makedirs(folder, exist_ok=True)
    fname = f"{uuid.uuid4().hex}_{secure_filename(name) or 'document'}"
    full = os.path.join(folder, fname)
    with open(full, "wb") as f:
        f.write(data)
    return f"{rel_dir}/{fname}", len(data), full


def render_docx(t, ctx):
    from docxtpl import DocxTemplate
    import io
    doc = DocxTemplate(abs_path(t.path))
    doc.render(ctx, autoescape=True)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def render_html_pdf(t, ctx, title):
    from ..services.pdf import DocPDF, html_to_pdf_body
    html = Environment(autoescape=True).from_string(t.body_html or "").render(**ctx)
    pdf = DocPDF(Firm.get(), title=title)
    pdf.add_page()
    html_to_pdf_body(pdf, html)
    out = pdf.output()
    return bytes(out) if not isinstance(out, (bytes, bytearray)) else bytes(out)


def _extracted_text(full, ext):
    try:
        from .documents import extract_text
        return extract_text(full, ext)
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("text extraction unavailable for %s: %s", full, e)
        return ""


def generate_document(t, matter, values, user):
    """Render template `t` for `matter` with merge values and save a Document. Returns the Document."""
    ctx = build_context(matter, user)
    ctx.update({k: v for k, v in values.items() if k})
    ext = "docx" if t.kind == "docx" else "pdf"
    name = f"{t.name} - {matter.number}.{ext}"
    if t.kind == "docx":
        data = render_docx(t, ctx)
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        data = render_html_pdf(t, ctx, title=t.name)
        mime = "application/pdf"
    rel, size, full = _write_output(matter.id, name, data)
    doc = Document(matter_id=matter.id, name=name[:300], path=rel, size=size,
                   mime=mime or mimetypes.guess_type(name)[0] or "application/octet-stream",
                   uploaded_by_id=user.id if user else None, template_id=t.id, folder="Generated",
                   extracted_text=_extracted_text(full, ext))
    db.session.add(doc)
    db.session.flush()
    audit("generate", "document", doc.id, f"{t.name} for {matter.number}", user.id if user else None)
    audit("generate_document", "matter", matter.id, name, user.id if user else None)
    return doc


@bp.route("/<int:id>/generate", methods=["GET", "POST"])
@login_required
def generate(id):
    t = db.session.get(DocTemplate, id) or abort(404)
    matter_id = _int(request.form.get("matter_id") if request.method == "POST" else request.args.get("matter_id"))
    m = db.session.get(Matter, matter_id) if matter_id else None
    matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    if not m:
        return render_template("doctemplates/generate.html", t=t, m=None, matters=matters, fields=[], ctx={})
    if t.kind == "docx" and (not t.path or not os.path.isfile(abs_path(t.path))):
        flash("The Word file for this template is missing. Upload it again.", "error")
        return redirect(url_for("doctemplates.edit", id=t.id))
    ctx = build_context(m, current_user())
    fields = list(t.fields)
    if request.method == "POST":
        # a field present in the form (even empty) overrides the prefill; one that is absent keeps it
        values = {f: request.form[f"f_{f}"] for f in fields if f"f_{f}" in request.form}
        try:
            doc = generate_document(t, m, values, current_user())
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception("document generation failed")
            db.session.rollback()
            flash(f"Could not generate the document: {e}", "error")
            rows = [(f, values.get(f, ""), f in ctx) for f in fields]
            return render_template("doctemplates/generate.html", t=t, m=m, matters=matters, fields=rows, ctx=ctx)
        db.session.commit()
        flash(f"Generated {doc.name}.", "ok")
        return redirect(url_for("matters.detail", id=m.id, tab="documents"))
    rows = [(f, ctx.get(f, ""), f in ctx) for f in fields]
    return render_template("doctemplates/generate.html", t=t, m=m, matters=matters, fields=rows, ctx=ctx)
