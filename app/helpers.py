"""Shared helpers: auth decorators, CSRF, money formatting, template globals."""
from functools import wraps
from datetime import date, datetime
import secrets
from flask import session, redirect, url_for, request, abort, g, flash
from .extensions import db
from .models import User, Contact, Firm


# ---- money ----
def cents_to_str(c, symbol="$"):
    c = int(c or 0)
    neg = c < 0
    c = abs(c)
    s = f"{symbol}{c // 100:,}.{c % 100:02d}"
    return f"({s})" if neg else s


def parse_money(s):
    """'1,250.50' -> 125050. Blank -> 0."""
    if s is None:
        return 0
    s = str(s).replace("$", "").replace(",", "").strip()
    if not s:
        return 0
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = s.strip("()-")
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    v = int(whole or 0) * 100 + int(frac or 0)
    return -v if neg else v


CURRENCY_SYMBOLS = {"USD": "$", "CAD": "CA$", "GBP": "\u00a3", "EUR": "\u20ac", "AUD": "A$", "MXN": "MX$"}
CURRENCIES = list(CURRENCY_SYMBOLS)


def fmt_money(cents, code="USD"):
    """Like money() but with the symbol for the given ISO code. fmt_money(123456, "GBP") -> "\u00a31,234.56"."""
    code = (code or "USD").upper()
    symbol = CURRENCY_SYMBOLS.get(code, code + " ")
    return cents_to_str(cents, symbol)


def parse_date(s, default=None):
    if not s:
        return default
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return default


def parse_minutes(s):
    """Accept '1.5' (hours), '1:30', '90m', '0.1'. Returns minutes."""
    s = str(s or "").strip().lower()
    if not s:
        return 0
    if s.endswith("m"):
        return int(float(s[:-1]))
    if ":" in s:
        h, m = s.split(":", 1)
        return int(h or 0) * 60 + int(m or 0)
    if s.endswith("h"):
        s = s[:-1]
    return int(round(float(s) * 60))


# ---- auth ----
def current_user():
    if "user" in g:
        return g.user
    uid = session.get("user_id")
    g.user = db.session.get(User, uid) if uid else None
    return g.user


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not current_user():
            return redirect(url_for("auth.login", next=request.path))
        return f(*a, **kw)
    return wrapper


def owner_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("auth.login", next=request.path))
        if u.role != "owner":
            abort(403)
        return f(*a, **kw)
    return wrapper


def permission_required(name):
    """Explicit check against app.permissions: @permission_required("trust"). Owners always pass."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                return redirect(url_for("auth.login", next=request.path))
            from .permissions import has_permission
            if not has_permission(u, name):
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


def portal_contact():
    if "portal_contact" in g:
        return g.portal_contact
    cid = session.get("portal_contact_id")
    g.portal_contact = db.session.get(Contact, cid) if cid else None
    return g.portal_contact


def portal_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not portal_contact():
            return redirect(url_for("portal.login"))
        return f(*a, **kw)
    return wrapper


# ---- CSRF ----
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]


def csrf_field():
    from markupsafe import Markup
    return Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">')


CSRF_EXEMPT_PREFIXES = ("/webhooks/", "/intake/submit", "/track/", "/sign/", "/pay/", "/p/", "/portal/", "/api/v1/")


def check_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    for p in CSRF_EXEMPT_PREFIXES:
        if request.path.startswith(p):
            return
    tok = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
    if not tok or tok != session.get("_csrf"):
        abort(400, "CSRF token missing or invalid")


def emit_event(name, payload):
    """Queue one outgoing-webhook delivery per active Webhook subscribed to `name` and try to send each
    right away. Returns the WebhookDelivery ids created. Call it after your own commit: it writes through
    its own short-lived session so it is safe from SQLAlchemy after_commit hooks too. Implemented in
    app/blueprints/webhooks_out.py; this is the import-friendly entry point."""
    from .blueprints.webhooks_out import deliver_event
    return deliver_event(name, payload)


def client_ip():
    xf = request.headers.get("X-Forwarded-For", "")
    return (xf.split(",")[0].strip() if xf else request.remote_addr) or ""


def register_template_globals(app):
    app.jinja_env.globals.update(
        money=cents_to_str, csrf=csrf_field, current_user=current_user, portal_contact=portal_contact,
        firm=lambda: Firm.get(), today=date.today, now=datetime.utcnow,
    )
    app.jinja_env.filters["money"] = cents_to_str
    app.jinja_env.filters["cur"] = fmt_money
    app.jinja_env.filters["hours"] = lambda m: f"{(m or 0) / 60:.2f}"
    app.jinja_env.filters["d"] = lambda v: v.strftime("%b %-d, %Y") if v else ""
    app.jinja_env.filters["dt"] = lambda v: v.strftime("%b %-d, %Y %-I:%M %p") if v else ""
    app.jinja_env.filters["iso"] = lambda v: v.isoformat() if v else ""
