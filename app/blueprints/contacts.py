"""Contacts: people and companies, clients and everyone else."""
import csv
import io
import json
import os
import re
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort, current_app, Response
from sqlalchemy import or_, func
from ..extensions import db
from ..models import Contact, Matter, Invoice, Note, audit
from ..helpers import login_required, current_user

bp = Blueprint("contacts", __name__, url_prefix="/contacts")

OPEN_INVOICE_STATUSES = ("sent", "viewed", "partial")


def _fill(c, form):
    c.kind = "company" if form.get("kind") == "company" else "person"
    c.first_name = form.get("first_name", "").strip()
    c.last_name = form.get("last_name", "").strip()
    c.company_name = form.get("company_name", "").strip()
    c.email = form.get("email", "").strip()
    c.phone = form.get("phone", "").strip()
    c.address = form.get("address", "").strip()
    c.tags = form.get("tags", "").strip()
    c.aliases = "\n".join(a.strip() for a in form.get("aliases", "").splitlines() if a.strip())
    c.is_client = bool(form.get("is_client"))
    c.notes = form.get("notes", "").strip()


def _valid(c):
    if c.kind == "company":
        return bool(c.company_name)
    return bool(c.first_name or c.last_name)


def _search_filter(q):
    like = f"%{q}%"
    return or_(Contact.first_name.ilike(like), Contact.last_name.ilike(like), Contact.company_name.ilike(like),
               Contact.email.ilike(like), Contact.phone.ilike(like), Contact.aliases.ilike(like),
               (Contact.first_name + " " + Contact.last_name).ilike(like))


@bp.route("")
@login_required
def index():
    q = request.args.get("q", "").strip()
    only = request.args.get("only", "")
    query = Contact.query
    if q:
        query = query.filter(_search_filter(q))
    if only == "clients":
        query = query.filter_by(is_client=True)
    contacts = query.order_by(Contact.company_name, Contact.last_name, Contact.first_name).all()
    counts = dict(db.session.query(Matter.client_id, func.count(Matter.id)).group_by(Matter.client_id).all())
    return render_template("contacts/index.html", contacts=contacts, q=q, only=only, counts=counts)


@bp.route("/search.json")
@login_required
def search_json():
    q = request.args.get("q", "").strip()
    query = Contact.query
    if q:
        query = query.filter(_search_filter(q))
    rows = query.order_by(Contact.last_name, Contact.first_name, Contact.company_name).limit(20).all()
    return jsonify([{"id": c.id, "name": c.display_name, "email": c.email or ""} for c in rows])


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    c = Contact()
    if request.method == "POST":
        _fill(c, request.form)
        if not _valid(c):
            flash("A name is required.", "error")
            return render_template("contacts/form.html", c=c, is_new=True)
        db.session.add(c)
        db.session.flush()
        audit("create", "contact", c.id, c.display_name, current_user().id)
        db.session.commit()
        flash("Contact created.", "ok")
        return redirect(url_for("contacts.detail", id=c.id))
    c.kind = request.args.get("kind", "person")
    c.is_client = request.args.get("is_client") == "1"
    return render_template("contacts/form.html", c=c, is_new=True)


@bp.route("/<int:id>")
@login_required
def detail(id):
    c = db.session.get(Contact, id) or abort(404)
    matters = Matter.query.filter_by(client_id=c.id).order_by(Matter.status, Matter.created_at.desc()).all()
    invoices = Invoice.query.filter(Invoice.client_id == c.id, Invoice.status.in_(OPEN_INVOICE_STATUSES)).order_by(
        Invoice.due_on).all()
    notes = Note.query.filter_by(contact_id=c.id).order_by(Note.created_at.desc()).all()
    return render_template("contacts/detail.html", c=c, matters=matters, invoices=invoices, notes=notes,
                           trust=c.trust_balance_cents(),
                           outstanding=sum(i.balance_cents for i in invoices))


@bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit(id):
    c = db.session.get(Contact, id) or abort(404)
    if request.method == "POST":
        _fill(c, request.form)
        if not _valid(c):
            flash("A name is required.", "error")
            return render_template("contacts/form.html", c=c, is_new=False)
        db.session.commit()
        flash("Contact saved.", "ok")
        return redirect(url_for("contacts.detail", id=c.id))
    return render_template("contacts/form.html", c=c, is_new=False)


@bp.route("/<int:id>/notes", methods=["POST"])
@login_required
def add_note(id):
    c = db.session.get(Contact, id) or abort(404)
    body = request.form.get("body", "").strip()
    if body:
        db.session.add(Note(contact_id=c.id, user_id=current_user().id, body=body))
        db.session.commit()
    return redirect(url_for("contacts.detail", id=c.id))


@bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def delete(id):
    c = db.session.get(Contact, id) or abort(404)
    if Matter.query.filter_by(client_id=c.id).count():
        flash("This contact has matters and cannot be deleted. Close the matters or reassign them first.", "error")
        return redirect(url_for("contacts.detail", id=c.id))
    name = c.display_name
    Note.query.filter_by(contact_id=c.id).delete()
    db.session.delete(c)
    audit("delete", "contact", id, name, current_user().id)
    db.session.commit()
    flash(f"Deleted {name}.", "ok")
    return redirect(url_for("contacts.index"))



# ---------------------------------------------------------------- CSV import
IMPORT_COLUMNS = ["first_name", "last_name", "company_name", "email", "phone", "address", "tags", "aliases", "notes"]

# Header aliases seen in exports from Clio, MyCase, PracticePanther, Outlook and Google Contacts.
HEADER_MAP = {
    "first_name": ["first name", "first", "given name", "firstname", "given"],
    "last_name": ["last name", "last", "surname", "family name", "lastname", "family"],
    "name": ["name", "full name", "contact name", "contact", "display name", "client name", "client"],
    "company_name": ["company", "company name", "organization", "organisation", "firm", "business", "org"],
    "email": ["email", "e-mail", "email address", "e-mail address", "primary email", "email 1 - value"],
    "phone": ["phone", "phone number", "mobile", "cell", "telephone", "primary phone", "phone 1 - value", "work phone", "mobile phone"],
    "address": ["address", "street", "mailing address", "home address", "address 1 - formatted", "billing address"],
    "tags": ["tags", "labels", "groups", "group membership", "category"],
    "aliases": ["aliases", "aka", "also known as", "other names", "nickname"],
    "notes": ["notes", "note", "comments", "description"],
}


def _norm_header(h):
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def _detect_columns(headers):
    """Map CSV headers to our fields. Returns {our_field: csv_header}."""
    found = {}
    normed = {_norm_header(h): h for h in headers}
    for field, names in HEADER_MAP.items():
        for n in names:
            if n in normed and field not in found:
                found[field] = normed[n]
                break
    return found


def _split_name(full):
    full = " ".join((full or "").split())
    if not full:
        return "", ""
    if "," in full:
        last, _, first = full.partition(",")
        return first.strip(), last.strip()
    parts = full.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def parse_contacts_csv(raw_bytes):
    """Parse CSV bytes into (rows, columns_used, errors). Each row is a dict of our fields."""
    text = raw_bytes.decode("utf-8-sig", "ignore")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    cols = _detect_columns(headers)
    if not cols:
        return [], cols, ["No recognisable columns. Expected headers like Name, First Name, Last Name, Company, Email, Phone."]
    rows, errors = [], []
    for i, r in enumerate(reader, start=2):
        row = {k: "" for k in IMPORT_COLUMNS}
        for field, header in cols.items():
            val = (r.get(header) or "").strip()
            if field == "name":
                if not (cols.get("first_name") or cols.get("last_name")):
                    row["first_name"], row["last_name"] = _split_name(val)
            elif field in row:
                row[field] = val
        if row["aliases"]:
            row["aliases"] = "\n".join(a.strip() for a in re.split(r"[;|\n]", row["aliases"]) if a.strip())
        row["kind"] = "company" if (row["company_name"] and not (row["first_name"] or row["last_name"])) else "person"
        if not (row["first_name"] or row["last_name"] or row["company_name"]):
            errors.append(f"Line {i}: no name or company, skipped.")
            continue
        rows.append(row)
    return rows, cols, errors


def _find_duplicate(row):
    """Existing contact with the same email, or the same full name / company name (case-insensitive)."""
    if row.get("email"):
        c = Contact.query.filter(func.lower(Contact.email) == row["email"].lower()).first()
        if c:
            return c
    if row["kind"] == "company":
        return Contact.query.filter(func.lower(Contact.company_name) == row["company_name"].lower()).first()
    return Contact.query.filter(func.lower(Contact.first_name) == row["first_name"].lower(),
                                func.lower(Contact.last_name) == row["last_name"].lower()).first()


def _import_dir():
    d = os.path.join(current_app.config["UPLOAD_DIR"], "imports")
    os.makedirs(d, exist_ok=True)
    return d


@bp.route("/import", methods=["GET", "POST"])
@login_required
def import_csv():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a CSV file.", "error")
            return redirect(url_for("contacts.import_csv"))
        raw = f.read(10 * 1024 * 1024)
        rows, cols, errors = parse_contacts_csv(raw)
        if not rows:
            for e in errors[:5]:
                flash(e, "error")
            return redirect(url_for("contacts.import_csv"))
        for r in rows:
            dup = _find_duplicate(r)
            r["duplicate_id"] = dup.id if dup else None
            r["duplicate_name"] = dup.display_name if dup else ""
        token = uuid.uuid4().hex
        with open(os.path.join(_import_dir(), f"{token}.json"), "w") as fh:
            json.dump({"rows": rows, "cols": cols, "errors": errors, "filename": f.filename}, fh)
        return render_template("contacts/import_preview.html", rows=rows, cols=cols, errors=errors, token=token,
                               filename=f.filename, dupes=sum(1 for r in rows if r["duplicate_id"]))
    return render_template("contacts/import.html")


@bp.route("/import/template.csv")
@login_required
def import_template():
    body = "First Name,Last Name,Company,Email,Phone,Address,Tags,Aliases,Notes\r\n" \
           "Maria,Alvarez,,maria@example.com,(512) 555-0111,\"12 Oak Lane, Austin, TX 78702\",estate,Maria Gomez,\r\n" \
           ",,Bluebonnet Logistics LLC,ap@bluebonnet.test,(512) 555-0122,,,Bluebonnet Trucking,\r\n"
    return Response(body, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=contacts-template.csv"})


@bp.route("/import/commit", methods=["POST"])
@login_required
def import_commit():
    token = re.sub(r"[^a-f0-9]", "", request.form.get("token", ""))
    path = os.path.join(_import_dir(), f"{token}.json")
    if not token or not os.path.isfile(path):
        flash("That import has expired. Upload the file again.", "error")
        return redirect(url_for("contacts.import_csv"))
    with open(path) as fh:
        data = json.load(fh)
    mode = request.form.get("duplicates", "skip")  # skip | update | create
    mark_clients = bool(request.form.get("is_client"))
    created = updated = skipped = 0
    for r in data["rows"]:
        dup = db.session.get(Contact, r["duplicate_id"]) if r.get("duplicate_id") else None
        if dup and mode == "skip":
            skipped += 1
            continue
        c = dup if (dup and mode == "update") else Contact()
        c.kind = r["kind"]
        for k in IMPORT_COLUMNS:
            v = r.get(k, "")
            if v or c.id is None:
                setattr(c, k, v if v else getattr(c, k, "") or "")
        if mark_clients:
            c.is_client = True
        if c.id is None:
            db.session.add(c)
            created += 1
        else:
            updated += 1
    audit("import", "contact", None, f"{data.get('filename','csv')}: {created} created, {updated} updated, {skipped} skipped",
          current_user().id)
    db.session.commit()
    os.remove(path)
    flash(f"Imported {created} new contacts, updated {updated}, skipped {skipped} duplicates.", "ok")
    return redirect(url_for("contacts.index"))
