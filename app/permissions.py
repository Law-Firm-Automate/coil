"""Role matrix and the before_request gate that maps URL prefixes to permissions.

Roles: owner | attorney | paralegal | billing | readonly. The legacy value "staff" is an alias for attorney.

Permissions come in pairs: "<area>" (read and write) and "<area>_view" (read only). Holding "<area>" implies
"<area>_view".

The rule the matrix expresses:
  owner      everything, including firm settings and anything that reveals a secret.
  attorney   every area a lawyer works in, except firm settings and firm administration.
  paralegal  case work, no money.
  billing    money and reports, read-only on case work.
  readonly   a view-only mirror of the attorney and never wider. It holds no settings, trust, payments,
             accounting or exports permission, so it can never open a page that prints a signing secret,
             a full trust ledger or a whole-firm export.

An earlier version built the readonly set as "<area>_view" for EVERY area, which inverted the matrix: the least
privileged role read owner-only settings pages (including the webhook signing secret) and downloaded the trust
and contact exports, while attorney, paralegal and billing were refused. The readonly set is now written out by
hand and a test asserts it is a subset of the attorney's areas.

Prefix map (first match wins, longest prefixes listed first inside PREFIX_PERMS):
  /settings/users/<id>/edit  -> self-edit allowed for anyone (settings.py limits which fields a non-owner may change)
  /settings/api              -> anyone who can track time, for their OWN tokens (the Chrome extension is per user)
  /settings/templates GET    -> matters_view (anyone who opens matters can read the template list)
  /settings, /dev, /import   -> settings (owner)
  /trust, /statements, /accounting -> trust (a client statement carries the trust running balance)
  /payments, /money          -> payments
  /invoices                  -> billing
  /exports                   -> exports
  /reports                   -> reports
  /time                      -> time
  /documents, /signatures, /doctemplates -> documents
  /calendar                  -> calendar
  /messages                  -> messages
  case work (/contacts, /matters, /conflicts, /tasks, /intake, /engagements, /pi, /criminal, /discovery,
             /records, /audit, /voice, /research, /ai, /rules) -> matters
  /, /dashboard, /features   -> dashboard (every role)
Anything else that is not in the map is allowed for any signed-in user, except that readonly still cannot POST.
Unauthenticated requests are left alone here; @login_required on the route handles them.

Routes carry their own @permission_required as well, so a future blueprint that lands outside the prefix map
still refuses the wrong role.
"""
import re
from flask import request, render_template
from .helpers import current_user, CSRF_EXEMPT_PREFIXES

ROLES = ["owner", "attorney", "paralegal", "billing", "readonly"]

ROLE_DESCRIPTIONS = {
    "owner": "Everything, including settings, users, trust accounting and the audit log.",
    "attorney": "Matters, contacts, time, invoices, documents, calendar, messages and reports. No trust, payments, exports or settings.",
    "paralegal": "Matters, contacts, time, documents, calendar and messages. No billing, trust, reports or settings.",
    "billing": "Invoices, payments, trust, client statements, the firm books, reports and exports. Can read matters, contacts and time but not change them.",
    "readonly": "Read-only on the pages an attorney works in. No settings, no trust, no payments, no exports, and nothing that shows a signing secret.",
}

AREAS = ["dashboard", "matters", "time", "billing", "trust", "payments", "accounting", "documents", "calendar",
         "messages", "reports", "exports", "settings"]

_ALL = set(AREAS)

# The attorney set is the widest non-owner set. readonly is derived from it below, view-only, so the two can
# never drift apart the way they did before.
_ATTORNEY = {"dashboard", "matters", "time", "billing", "documents", "calendar", "messages", "reports"}

ROLE_PERMS = {
    "owner": set(_ALL),
    # Attorneys hold "billing" (not just billing_view) so they can draft invoices and submit them for approval;
    # approve/reject stay owner|billing inside the invoices module.
    "attorney": set(_ATTORNEY),
    "paralegal": {"dashboard", "matters", "time", "documents", "calendar", "messages"},
    "billing": {"dashboard", "matters_view", "time_view", "billing", "trust", "payments", "accounting", "reports",
                "exports"},
    # View-only mirror of the attorney. Deliberately NOT "every area, view-only": settings, trust, payments,
    # accounting and exports are absent, so readonly cannot read a signing secret, a full trust ledger or a
    # whole-firm export.
    "readonly": {a + "_view" for a in _ATTORNEY},
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
    ("/import", "settings", "settings"),
    # Money. A client statement is a full trust running balance, and the firm books are the firm's own ledger,
    # so both sit with trust rather than with invoicing.
    ("/trust", "trust", "trust"),
    ("/statements", "trust", "trust"),
    ("/accounting", "accounting", "accounting"),
    ("/payments", "payments", "payments"),
    ("/money", "payments", "payments"),
    ("/invoices", "billing", "billing"),
    ("/exports", "exports", "exports"),
    ("/reports", "reports", "reports"),
    ("/time", "time", "time"),
    ("/documents", "documents", "documents"),
    ("/signatures", "documents", "documents"),
    ("/doctemplates", "documents", "documents"),
    ("/calendar", "calendar", "calendar"),
    ("/messages", "messages", "messages"),
    ("/contacts", "matters", "matters"),
    ("/matters", "matters", "matters"),
    ("/conflicts", "matters", "matters"),
    ("/tasks", "matters", "matters"),
    ("/intake", "matters", "matters"),
    ("/engagements", "matters", "matters"),
    # Practice-area case work. All of these read and write matter data, so they follow the matters area:
    # owner, attorney and paralegal work in them, billing stays out, readonly reads.
    ("/pi", "matters", "matters"),
    ("/criminal", "matters", "matters"),
    ("/discovery", "matters", "matters"),
    ("/records", "matters", "matters"),
    ("/litigation", "matters", "matters"),  # blueprint is optional; mapped so it cannot land unguarded
    ("/audit", "matters", "matters"),       # case audit findings, not /settings/audit which is owner-only above
    ("/voice", "matters", "matters"),       # call log and transcripts
    ("/research", "matters", "matters"),
    ("/ai", "matters", "matters"),
    ("/rules", "matters", "matters"),       # /rules/matters/<id>/apply; /settings/rules is owner-only above
    ("/dashboard", "dashboard", "dashboard"),
    ("/features", "dashboard", "dashboard"),
]

ALWAYS_ALLOW = tuple(CSRF_EXEMPT_PREFIXES) + ("/login", "/setup", "/static", "/portal", "/logout")

READ_METHODS = ("GET", "HEAD", "OPTIONS")

_SELF_EDIT = re.compile(r"^/settings/users/(\d+)/edit/?$")
# API tokens are per user: the Chrome extension runs the signed-in user's timer and files capture suggestions
# to that user, so anyone who can track time mints their OWN. settings.py and settings/api.html scope the list.
_API_TOKENS = re.compile(r"^/settings/api(?:/(\d+)/revoke)?/?$")

AREA_LABELS = {
    "settings": "firm settings", "trust": "trust accounting", "payments": "payments", "billing": "invoicing",
    "accounting": "the firm books", "exports": "exports", "reports": "reports", "time": "time and expenses",
    "documents": "documents", "calendar": "the calendar", "messages": "messages",
    "matters": "matters and contacts", "dashboard": "the dashboard",
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
    # Own API tokens: anyone who can track time. Readonly cannot track time, so it still falls through to the
    # settings check below and is refused.
    if _API_TOKENS.match(path) and has_permission(user, "time"):
        return None
    perm = required_permission(path, request.method)
    if perm is None:
        if role == "readonly" and request.method not in READ_METHODS:
            return _deny(user, "dashboard")
        return None
    if has_permission(user, perm):
        return None
    return _deny(user, perm)
