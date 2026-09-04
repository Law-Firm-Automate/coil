"""Role matrix and the before_request gate that maps URL prefixes to permissions.

Roles: owner | attorney | paralegal | billing | readonly. The legacy value "staff" is an alias for attorney.

Permissions come in pairs: "<area>" (read and write) and "<area>_view" (read only). Holding "<area>" implies
"<area>_view". A readonly user only ever holds *_view permissions, so every non-GET request is refused.

Prefix map (first match wins, longest prefixes listed first inside PREFIX_PERMS):
  /settings/users/<id>/edit  -> self-edit allowed for anyone (settings.py limits which fields a non-owner may change)
  /settings/templates GET    -> matters_view (anyone who opens matters can read the template list)
  /settings, /dev            -> settings (owner)
  /trust                     -> trust
  /payments                  -> payments
  /invoices                  -> billing
  /exports                   -> exports
  /reports                   -> reports
  /time                      -> time
  /documents, /signatures    -> documents
  /calendar                  -> calendar
  /messages                  -> messages
  /contacts, /matters, /conflicts, /tasks, /intake, /engagements -> matters
  /, /dashboard              -> dashboard (every role)
Anything else that is not in the map is allowed for any signed-in user, except that readonly still cannot POST.
Unauthenticated requests are left alone here; @login_required on the route handles them.
"""
import re
from flask import request, render_template
from .helpers import current_user, CSRF_EXEMPT_PREFIXES

ROLES = ["owner", "attorney", "paralegal", "billing", "readonly"]

ROLE_DESCRIPTIONS = {
    "owner": "Everything, including settings, users, trust accounting and the audit log.",
    "attorney": "Matters, contacts, time, invoices, documents, calendar, messages and reports. No trust, payments, exports or settings.",
    "paralegal": "Matters, contacts, time, documents, calendar and messages. No billing, trust, reports or settings.",
    "billing": "Invoices, payments, trust, reports and exports. Can read matters, contacts and time but not change them.",
    "readonly": "Can open every page an attorney can, but cannot change anything.",
}

AREAS = ["dashboard", "matters", "time", "billing", "trust", "payments", "documents", "calendar", "messages",
         "reports", "exports", "settings"]

_ALL = set(AREAS)

ROLE_PERMS = {
    "owner": set(_ALL),
    # Attorneys hold "billing" (not just billing_view) so they can draft invoices and submit them for approval;
    # approve/reject stay owner|billing inside the invoices module.
    "attorney": {"dashboard", "matters", "time", "billing", "documents", "calendar", "messages", "reports"},
    "paralegal": {"dashboard", "matters", "time", "documents", "calendar", "messages"},
    "billing": {"dashboard", "matters_view", "time_view", "billing", "trust", "payments", "reports", "exports"},
    "readonly": {a + "_view" for a in _ALL},
}

ROLE_ALIASES = {"staff": "attorney"}


def canonical_role(role):
    role = (role or "").strip().lower()
    return ROLE_ALIASES.get(role, role) if role else "readonly"


def permissions_for(role):
    return ROLE_PERMS.get(canonical_role(role), set())


def has_permission(user, name):
    """True when the user's role holds `name`. "<area>" implies "<area>_view"."""
    if not user:
        return False
    perms = permissions_for(user.role)
    if name in perms:
        return True
    if name.endswith("_view") and name[:-5] in perms:
        return True
    return False


# Ordered: longer / more specific prefixes first.
PREFIX_PERMS = [
    ("/settings/templates", "matters", "settings"),  # (prefix, view perm area, write perm area)
    ("/settings", "settings", "settings"),
    ("/dev", "settings", "settings"),
    ("/trust", "trust", "trust"),
    ("/payments", "payments", "payments"),
    ("/invoices", "billing", "billing"),
    ("/exports", "exports", "exports"),
    ("/reports", "reports", "reports"),
    ("/time", "time", "time"),
    ("/documents", "documents", "documents"),
    ("/signatures", "documents", "documents"),
    ("/calendar", "calendar", "calendar"),
    ("/messages", "messages", "messages"),
    ("/contacts", "matters", "matters"),
    ("/matters", "matters", "matters"),
    ("/conflicts", "matters", "matters"),
    ("/tasks", "matters", "matters"),
    ("/intake", "matters", "matters"),
    ("/engagements", "matters", "matters"),
    ("/dashboard", "dashboard", "dashboard"),
]

ALWAYS_ALLOW = tuple(CSRF_EXEMPT_PREFIXES) + ("/login", "/setup", "/static", "/portal", "/logout")

READ_METHODS = ("GET", "HEAD", "OPTIONS")

_SELF_EDIT = re.compile(r"^/settings/users/(\d+)/edit/?$")

AREA_LABELS = {
    "settings": "firm settings", "trust": "trust accounting", "payments": "payments", "billing": "invoicing",
    "exports": "exports", "reports": "reports", "time": "time and expenses", "documents": "documents",
    "calendar": "the calendar", "messages": "messages", "matters": "matters and contacts", "dashboard": "the dashboard",
}


def _prefix_match(path):
    for prefix, view_area, write_area in PREFIX_PERMS:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return view_area, write_area
    return None, None


def required_permission(path, method):
    """The permission a request needs, or None when anyone signed in may proceed."""
    view_area, write_area = _prefix_match(path)
    if view_area is None:
        return None
    if method in READ_METHODS:
        return view_area + "_view"
    return write_area


def _deny(user, perm):
    area = perm[:-5] if perm.endswith("_view") else perm
    role = canonical_role(user.role)
    verb = "open" if perm.endswith("_view") else "change"
    msg = (f"Your role ({role}) cannot {verb} {AREA_LABELS.get(area, area)}. "
           f"Ask the firm owner if you need that access.")
    return render_template("error.html", code=403, message=msg), 403


def enforce():
    """before_request: refuse requests the signed-in user's role does not cover."""
    path = request.path or "/"
    if path == "/" and request.method in READ_METHODS:
        return None
    for p in ALWAYS_ALLOW:
        if path == p.rstrip("/") or path.startswith(p if p.endswith("/") else p + "/") or path.startswith(p):
            return None
    user = current_user()
    if not user:
        return None  # @login_required on the route decides what to do with anonymous requests
    role = canonical_role(user.role)
    if role == "owner":
        return None
    # A user may always open their own edit page; settings.py limits which fields they can change.
    m = _SELF_EDIT.match(path)
    if m and int(m.group(1)) == user.id:
        return None
    perm = required_permission(path, request.method)
    if perm is None:
        if role == "readonly" and request.method not in READ_METHODS:
            return _deny(user, "dashboard")
        return None
    if has_permission(user, perm):
        return None
    return _deny(user, perm)
