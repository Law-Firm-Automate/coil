import re
import time
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from ..extensions import db
from ..models import User, Firm, audit
from ..helpers import login_required, current_user

bp = Blueprint("auth", __name__)
KEY_SHAPE = re.compile(r"^COIL-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if User.query.count() == 0:
        return redirect(url_for("auth.setup"))

    # Rate limit FAILED login attempts per IP. Counting successes too would lock out a
    # whole firm behind one office IP after ten normal sign-ins in five minutes.
    ip = request.remote_addr or "unknown"
    key = f"login:{ip}"
    attempts = current_app.config.setdefault("_login_attempts", {})
    now = time.time()
    attempts[key] = [t for t in attempts.get(key, []) if now - t < 300]  # 5 minute window
    if len(attempts[key]) >= 10:
        flash("Too many login attempts. Try again in a few minutes.", "error")
        return render_template("auth/login.html")

    if request.method == "POST":
        u = User.query.filter(db.func.lower(User.email) == request.form.get("email", "").strip().lower()).first()
        if u and u.is_active and u.check_password(request.form.get("password", "")):
            attempts.pop(key, None)  # a good password clears the run of failures
            session.clear()
            session["user_id"] = u.id
            session.permanent = True
            audit("login", "user", u.id, user_id=u.id)
            db.session.commit()
            nxt = request.args.get("next") or url_for("dashboard.index")
            return redirect(nxt if nxt.startswith("/") else url_for("dashboard.index"))
        attempts[key].append(now)
        flash("Email or password did not match.", "error")
    return render_template("auth/login.html")


def verify_install_key(key, email, firm_name):
    """Check a free install key against coil.legal. Returns (ok, message)."""
    import requests
    from flask import current_app
    key = (key or "").strip().upper()
    if not key:
        return False, "Enter your install key. It is free: get one at https://coil.legal/download."
    # Shape first, locally. A key is COIL- and three groups of four. Anything else is a typo or
    # a paste gone wrong, and telling the person "that key does not match this email" sends them
    # to check their email address when the problem is in front of them. This also means a
    # malformed key never makes the network call.
    if not KEY_SHAPE.match(key):
        return False, ("That is not the shape of an install key. Keys look like COIL-XXXX-XXXX-XXXX. "
                       "Check for a missing group or a stray character, or get one at https://coil.legal/download.")
    try:
        r = requests.post(current_app.config["COIL_KEY_VERIFY_URL"], json={
            "key": key, "email": email, "firm": firm_name, "base_url": current_app.config["BASE_URL"],
            "version": current_app.config["COIL_VERSION"]}, timeout=12)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return True, ""
        return False, data.get("error") or ("That key was not issued for this email address. Either the key is not "
                                            "one we issued, or it was issued to a different address. Keys are free "
                                            "and tied to the email you request them with at https://coil.legal/download.")
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("install key check failed: %s", e)
        return False, "Could not reach coil.legal to check the key. Check your internet connection and try again."


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run: create the owner account and firm profile. Self-hosted installs also register their free install key."""
    from flask import current_app
    if User.query.count() > 0:
        return redirect(url_for("auth.login"))
    need_key = not current_app.config.get("COIL_SKIP_INSTALL_KEY") and not current_app.config.get("TESTING")
    if request.method == "POST":
        f = Firm.get()
        f.name = request.form.get("firm_name", "").strip() or f.name
        u = User(email=request.form.get("email", "").strip().lower(), name=request.form.get("name", "").strip(),
                 role="owner")
        pw = request.form.get("password", "")
        if not u.email or not u.name or len(pw) < 8:
            flash("Name, email, and a password of at least 8 characters are required.", "error")
            return render_template("auth/setup.html", need_key=need_key)
        if need_key:
            ok, msg = verify_install_key(request.form.get("install_key"), u.email, f.name)
            if not ok:
                flash(msg, "error")
                return render_template("auth/setup.html", need_key=need_key)
            f.install_key = request.form.get("install_key", "").strip().upper()
        u.set_password(pw)
        u.initials = "".join(p[0] for p in u.name.split()[:2]).upper()
        db.session.add(u)
        db.session.commit()
        session["user_id"] = u.id
        return redirect(url_for("settings.index"))
    return render_template("auth/setup.html", need_key=need_key)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    session.pop("user_id", None)
    return redirect(url_for("auth.login"))
