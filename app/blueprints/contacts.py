"""Contacts: people and companies, clients and everyone else."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort
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
