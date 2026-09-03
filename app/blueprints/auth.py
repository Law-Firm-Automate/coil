from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from ..extensions import db
from ..models import User, Firm, audit
from ..helpers import login_required, current_user

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if User.query.count() == 0:
        return redirect(url_for("auth.setup"))
    if request.method == "POST":
        u = User.query.filter(db.func.lower(User.email) == request.form.get("email", "").strip().lower()).first()
        if u and u.is_active and u.check_password(request.form.get("password", "")):
            session.clear()
            session["user_id"] = u.id
            session.permanent = True
            audit("login", "user", u.id, user_id=u.id)
            db.session.commit()
            nxt = request.args.get("next") or url_for("dashboard.index")
            return redirect(nxt if nxt.startswith("/") else url_for("dashboard.index"))
        flash("Email or password did not match.", "error")
    return render_template("auth/login.html")


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run: create the owner account and firm profile."""
    if User.query.count() > 0:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        f = Firm.get()
        f.name = request.form.get("firm_name", "").strip() or f.name
        u = User(email=request.form.get("email", "").strip().lower(), name=request.form.get("name", "").strip(),
                 role="owner")
        pw = request.form.get("password", "")
        if not u.email or not u.name or len(pw) < 8:
            flash("Name, email, and a password of at least 8 characters are required.", "error")
            return render_template("auth/setup.html")
        u.set_password(pw)
        u.initials = "".join(p[0] for p in u.name.split()[:2]).upper()
        db.session.add(u)
        db.session.commit()
        session["user_id"] = u.id
        return redirect(url_for("settings.index"))
    return render_template("auth/setup.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    session.pop("user_id", None)
    return redirect(url_for("auth.login"))
