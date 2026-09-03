"""Firm profile, users, integration status, and the dev outbox. /dev/outbox is outside /settings, so no url_prefix."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app
from ..extensions import db
from ..models import Firm, User, audit
from ..helpers import login_required, owner_required, current_user, parse_money, cents_to_str
from ..services.mail import dev_outbox
from ..services import sms as smssvc

bp = Blueprint("settings", __name__)

TEXT_FIELDS = ("name", "address", "phone", "email", "website", "timezone", "invoice_prefix", "matter_prefix",
               "invoice_footer", "trust_bank_name", "operating_bank_name", "trust_account_last4")
INT_FIELDS = ("invoice_terms_days", "next_invoice_number", "next_matter_number")


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def index():
    f = Firm.get()
    if request.method == "POST":
        if current_user().role != "owner":
            abort(403)
        form = request.form
        for k in TEXT_FIELDS:
            if k in form:
                setattr(f, k, form.get(k, "").strip())
        f.trust_account_last4 = (f.trust_account_last4 or "")[-4:]
        for k in INT_FIELDS:
            if k in form:
                try:
                    setattr(f, k, int(form.get(k) or 0))
                except ValueError:
                    flash(f"{k.replace('_', ' ')} must be a whole number.", "error")
                    return render_template("settings/index.html", f=f)
        if "default_rate" in form:
            f.default_rate_cents = parse_money(form.get("default_rate"))
        if "surcharge_pct" in form:
            try:
                pct = float(str(form.get("surcharge_pct") or "0").replace("%", "").strip() or 0)
            except ValueError:
                flash("Surcharge must be a percentage like 3 or 2.5.", "error")
                return render_template("settings/index.html", f=f)
            f.surcharge_bps = int(round(pct * 100))
        if "_form" in form:
            # checkboxes only arrive when ticked; _form marks a full submission so unticked means off
            f.surcharge_enabled = form.get("surcharge_enabled") == "1"
            f.daily_agenda_email = form.get("daily_agenda_email") == "1"
        else:
            if "surcharge_enabled" in form:
                f.surcharge_enabled = form.get("surcharge_enabled") == "1"
            if "daily_agenda_email" in form:
                f.daily_agenda_email = form.get("daily_agenda_email") == "1"
        audit("update", "firm", f.id, "settings saved", current_user().id)
        db.session.commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("settings.index"))
    return render_template("settings/index.html", f=f)


# ---- users ----
@bp.route("/settings/users")
@owner_required
def users():
    rows = User.query.order_by(User.is_active.desc(), User.name).all()
    return render_template("settings/users.html", rows=rows)


def _fill_user(u, form, is_new):
    u.name = form.get("name", "").strip()
    u.email = form.get("email", "").strip().lower()
    u.role = "owner" if form.get("role") == "owner" else "staff"
    u.hourly_rate_cents = parse_money(form.get("hourly_rate"))
    u.initials = form.get("initials", "").strip().upper()[:6] or "".join(p[0] for p in u.name.split()[:2]).upper()
    if not is_new:
        u.is_active = form.get("is_active") == "1"
    pw = form.get("password", "")
    if not u.name or not u.email:
        return "Name and email are required."
    if is_new and len(pw) < 8:
        return "A password of at least 8 characters is required for a new user."
    if pw and len(pw) < 8:
        return "New password must be at least 8 characters."
    other = User.query.filter(db.func.lower(User.email) == u.email, User.id != (u.id or 0)).first()
    if other:
        return "Another user already has that email."
    if pw:
        u.set_password(pw)
    return None


@bp.route("/settings/users/new", methods=["GET", "POST"])
@owner_required
def user_new():
    u = User(role="staff", is_active=True, hourly_rate_cents=Firm.get().default_rate_cents)
    if request.method == "POST":
        err = _fill_user(u, request.form, True)
        if err:
            flash(err, "error")
            return render_template("settings/user_form.html", u=u, is_new=True)
        db.session.add(u)
        db.session.flush()
        audit("create", "user", u.id, u.email, current_user().id)
        db.session.commit()
        flash(f"Added {u.name}.", "ok")
        return redirect(url_for("settings.users"))
    return render_template("settings/user_form.html", u=u, is_new=True)


@bp.route("/settings/users/<int:id>/edit", methods=["GET", "POST"])
@owner_required
def user_edit(id):
    u = db.session.get(User, id) or abort(404)
    if request.method == "POST":
        me = current_user()
        err = _fill_user(u, request.form, False)
        if not err and u.id == me.id and (not u.is_active or u.role != "owner"):
            err = "You cannot deactivate or demote your own account."
        if not err and not u.is_active and User.query.filter_by(role="owner", is_active=True).filter(
                User.id != u.id).count() == 0 and u.role == "owner":
            err = "At least one active owner is required."
        if err:
            db.session.rollback()
            flash(err, "error")
            return render_template("settings/user_form.html", u=db.session.get(User, id), is_new=False)
        audit("update", "user", u.id, u.email, me.id)
        db.session.commit()
        flash("User saved.", "ok")
        return redirect(url_for("settings.users"))
    return render_template("settings/user_form.html", u=u, is_new=False)


# ---- integrations ----
@bp.route("/settings/integrations")
@login_required
def integrations():
    c = current_app.config
    base = c["BASE_URL"]
    cards = [
        dict(name="Email (SMTP)", ok=bool(c.get("SMTP_HOST")),
             detail=f"Sending from {c.get('MAIL_FROM')} via {c.get('SMTP_HOST')}:{c.get('SMTP_PORT')}" if c.get("SMTP_HOST")
             else "SMTP_HOST is empty. Emails are logged to the dev outbox instead of being delivered.",
             env="SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM",
             link=("/dev/outbox", "Open dev outbox") if current_user().role == "owner" and not c.get("SMTP_HOST") else None),
        dict(name="Stripe (card and ACH payments)", ok=bool(c.get("STRIPE_SECRET_KEY")),
             detail=("Secret key set. " + ("Webhook secret set." if c.get("STRIPE_WEBHOOK_SECRET") else
                                          "STRIPE_WEBHOOK_SECRET is empty, so webhook signatures are not verified."))
             if c.get("STRIPE_SECRET_KEY") else "STRIPE_SECRET_KEY is empty. Pay links show mailing instructions instead.",
             env="STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET",
             webhook=f"{base}/webhooks/stripe", webhook_note="Stripe Dashboard > Developers > Webhooks. Event: checkout.session.completed"),
        dict(name="Twilio (two-way texting)", ok=smssvc.configured(),
             detail=f"Sending from {c.get('TWILIO_FROM_NUMBER')}." if smssvc.configured()
             else "Twilio is not configured. Messages are stored but not delivered.",
             env="TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER",
             webhook=f"{base}/webhooks/twilio", webhook_note="Twilio Console > Phone number > Messaging > A message comes in (HTTP POST)"),
    ]
    return render_template("settings/integrations.html", cards=cards, base=base,
                           intake_url=f"{base}/intake/form")


@bp.route("/dev/outbox")
@owner_required
def outbox():
    return render_template("settings/outbox.html", rows=dev_outbox(),
                           smtp=bool(current_app.config.get("SMTP_HOST")))
