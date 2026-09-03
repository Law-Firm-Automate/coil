"""Intake: public lead form, staff lead list, and one-step conversion to Contact + Matter + ConflictCheck + Engagement."""
import json
import time
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app
from rapidfuzz import fuzz
from ..extensions import db
from ..models import (Firm, User, Contact, Matter, MatterParty, FlatFeeMilestone, ConflictCheck, IntakeLead,
                      LetterTemplate, audit, now)
from ..helpers import login_required, current_user, client_ip, parse_money, parse_date
from ..services.mail import send_email
from .engagements import build_engagement, send_engagement

bp = Blueprint("intake", __name__, url_prefix="/intake")

MATTER_TYPES = ["Estate Planning", "Business formation", "Litigation", "Family law", "Real estate",
                "Criminal defense", "Personal injury", "Employment", "Immigration", "Other"]
STATUSES = ("new", "contacted", "converted", "declined")
FUZZ_THRESHOLD = 80

RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 600  # seconds
_submissions = {}  # ip -> [timestamps]


def _rate_limited(ip):
    t = time.time()
    hits = [h for h in _submissions.get(ip, []) if t - h < RATE_LIMIT_WINDOW]
    if len(hits) >= RATE_LIMIT_MAX:
        _submissions[ip] = hits
        return True
    hits.append(t)
    _submissions[ip] = hits
    return False


def age_str(dt):
    if not dt:
        return ""
    s = int((now() - dt).total_seconds())
    if s < 3600:
        return f"{max(1, s // 60)}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


# ---------------------------------------------------------------------------
# Fuzzy conflict preview (small local copy of what the conflicts module does)
# ---------------------------------------------------------------------------
def _candidates(exclude_contact_id=None):
    out = []
    for c in Contact.query.all():
        if exclude_contact_id and c.id == exclude_contact_id:
            continue
        names = [c.display_name] + [a.strip() for a in (c.aliases or "").splitlines() if a.strip()]
        for n in names:
            out.append(dict(name=n, kind="contact", id=c.id, url=f"/contacts/{c.id}",
                            label=f"{c.display_name}" + (" (client)" if c.is_client else "")))
    for p in MatterParty.query.all():
        out.append(dict(name=p.name, kind="party", id=p.matter_id, url=f"/matters/{p.matter_id}",
                        label=f"{p.role} on {p.matter.label if p.matter else 'matter'}"))
    for m in Matter.query.all():
        out.append(dict(name=m.name, kind="matter", id=m.id, url=f"/matters/{m.id}", label=m.label))
    return out


def fuzzy_hits(names, exclude_contact_id=None):
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return []
    cands = _candidates(exclude_contact_id)
    hits = []
    seen = set()
    for q in names:
        for c in cands:
            score = fuzz.token_set_ratio(q.lower(), (c["name"] or "").lower())
            if score >= FUZZ_THRESHOLD:
                key = (q, c["kind"], c["id"], c["name"])
                if key in seen:
                    continue
                seen.add(key)
                hits.append(dict(query=q, match=c["name"], kind=c["kind"], id=c["id"], url=c["url"],
                                 label=c["label"], score=int(score)))
    hits.sort(key=lambda h: -h["score"])
    return hits


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
@bp.route("/form")
def form():
    return render_template("intake/form.html", types=MATTER_TYPES, source=request.args.get("source", "web")[:100],
                           values={})


@bp.route("/submit", methods=["POST"])
def submit():
    f = request.form
    if f.get("website", "").strip():
        # honeypot filled: bots get a thank-you and nothing is stored
        return render_template("intake/thanks.html")
    ip = client_ip()
    if _rate_limited(ip):
        return render_template("intake/thanks.html", limited=True), 429
    name = f.get("name", "").strip()
    if not name:
        flash("Please tell us your name.", "error")
        return render_template("intake/form.html", types=MATTER_TYPES, source=f.get("source", "web")[:100],
                               values=f), 400
    mtype = f.get("matter_type", "").strip()
    if mtype == "Other" and f.get("matter_type_other", "").strip():
        mtype = f.get("matter_type_other", "").strip()[:100]
    lead = IntakeLead(name=name[:200], email=f.get("email", "").strip()[:200], phone=f.get("phone", "").strip()[:50],
                      matter_type=mtype[:100], description=f.get("description", "").strip(),
                      adverse_party=f.get("adverse_party", "").strip()[:300],
                      source=(f.get("source", "web").strip() or "web")[:100])
    db.session.add(lead)
    db.session.flush()
    audit("create", "intake_lead", lead.id, f"{lead.name} via {lead.source} from {ip}")
    db.session.commit()
    firm = Firm.get()
    to = firm.email or current_app.config["MAIL_FROM"]
    link = f"{current_app.config['BASE_URL']}/intake/{lead.id}"
    html = (f"<p>New intake lead from the {lead.source} form.</p>"
            f"<p><strong>{_esc(lead.name)}</strong><br>{_esc(lead.email)}<br>{_esc(lead.phone)}</p>"
            f"<p>Matter type: {_esc(lead.matter_type) or 'not given'}<br>Other party: {_esc(lead.adverse_party) or 'none given'}</p>"
            f"<p>{_esc(lead.description).replace(chr(10), '<br>')}</p><p><a href='{link}'>Open the lead</a></p>")
    send_email(to, f"New intake lead: {lead.name} ({lead.matter_type or 'no type'})", html,
               text=f"New lead {lead.name}. Open: {link}", reply_to=lead.email or None)
    return render_template("intake/thanks.html")


def _esc(s):
    from markupsafe import escape
    return str(escape(s or ""))


@bp.route("/embed")
@login_required
def embed():
    url = f"{current_app.config['BASE_URL']}/intake/form"
    snippet = (f'<iframe src="{url}?source=website" style="width:100%;min-height:760px;border:0" '
               f'title="Contact {Firm.get().name}"></iframe>')
    return render_template("intake/embed.html", url=url, snippet=snippet)


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------
@bp.route("")
@login_required
def index():
    status = request.args.get("status", "new")
    q = IntakeLead.query
    if status in STATUSES:
        q = q.filter_by(status=status)
    rows = q.order_by(IntakeLead.created_at.desc()).all()
    counts = {s: IntakeLead.query.filter_by(status=s).count() for s in STATUSES}
    return render_template("intake/index.html", rows=rows, status=status, counts=counts, age=age_str)


def _next_matter_number():
    """Take Firm.next_matter_number, skipping any number already in use (seed data hardcodes a few)."""
    firm = Firm.get()
    n = firm.next_matter_number or 1001
    prefix = firm.matter_prefix or ""
    while Matter.query.filter_by(number=f"{prefix}{n}").first():
        n += 1
    firm.next_matter_number = n + 1
    return f"{prefix}{n}"


def _split_name(name):
    parts = (name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


@bp.route("/<int:id>")
@login_required
def detail(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    hits = fuzzy_hits([lead.name, lead.adverse_party])
    first, last = _split_name(lead.name)
    email_match = Contact.query.filter(db.func.lower(Contact.email) == lead.email.lower()).first() if lead.email else None
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    templates = LetterTemplate.query.filter_by(kind="engagement").order_by(LetterTemplate.is_default.desc(),
                                                                          LetterTemplate.name).all()
    firm = Firm.get()
    defaults = dict(
        first_name=first, last_name=last,
        matter_name=f"{last or first} - {lead.matter_type}".strip(" -") if lead.matter_type else (last or first),
        practice_area=lead.matter_type, responsible_user_id=current_user().id,
        hourly_rate=f"{firm.default_rate_cents / 100:.2f}", scope=lead.description,
    )
    return render_template("intake/detail.html", lead=lead, hits=hits, email_match=email_match, users=users,
                           templates=templates, defaults=defaults, age=age_str, types=MATTER_TYPES)


@bp.route("/<int:id>/convert", methods=["POST"])
@login_required
def convert(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    u = current_user()
    f = request.form
    if lead.status == "converted":
        flash("This lead was already converted.", "error")
        return redirect(url_for("intake.detail", id=lead.id))

    # 1. contact
    mode = f.get("contact_mode", "new")
    if mode == "existing":
        contact = db.session.get(Contact, f.get("contact_id", type=int) or 0)
        if not contact:
            flash("Pick an existing contact or create a new one.", "error")
            return redirect(url_for("intake.detail", id=lead.id))
        contact.is_client = True
        if not contact.email and lead.email:
            contact.email = lead.email
        if not contact.phone and lead.phone:
            contact.phone = lead.phone
        contact_created = False
    else:
        first, last = f.get("first_name", "").strip(), f.get("last_name", "").strip()
        if not (first or last):
            first, last = _split_name(lead.name)
        contact = Contact(kind="person", first_name=first, last_name=last, email=f.get("email", lead.email).strip(),
                          phone=f.get("phone", lead.phone).strip(), address=f.get("address", "").strip(),
                          is_client=True, notes=f"From intake lead #{lead.id} ({lead.source})")
        db.session.add(contact)
        db.session.flush()
        contact_created = True

    # 2. conflict search, before the new matter and party exist
    adverse = f.get("adverse_party", lead.adverse_party).strip()
    query_names = [lead.name, contact.display_name, adverse]
    hits = fuzzy_hits(query_names, exclude_contact_id=contact.id)

    # 3. matter
    number = _next_matter_number()
    billing = f.get("billing_type", "flat")
    if billing not in ("flat", "hourly", "contingency", "hybrid"):
        billing = "flat"
    matter = Matter(number=number, client_id=contact.id, name=f.get("matter_name", "").strip() or lead.name,
                    practice_area=f.get("practice_area", "").strip() or lead.matter_type, status="open",
                    opened_on=date.today(), responsible_user_id=f.get("responsible_user_id", type=int) or u.id,
                    billing_type=billing, description=f.get("description", lead.description).strip(),
                    sol_date=parse_date(f.get("sol_date")), sol_basis=f.get("sol_basis", "").strip())
    flat = parse_money(f.get("flat_fee")) if billing in ("flat", "hybrid") else 0
    matter.flat_fee_cents = flat
    matter.hourly_rate_cents = parse_money(f.get("hourly_rate")) if billing in ("hourly", "hybrid") else 0
    try:
        matter.contingency_pct = float(f.get("contingency_pct") or 0) if billing in ("contingency", "hybrid") else 0.0
    except ValueError:
        matter.contingency_pct = 0.0
    db.session.add(matter)
    db.session.flush()

    # 4. milestones
    milestones_made = 0
    if flat and f.get("split_milestones") == "1":
        m1 = parse_money(f.get("milestone1_amount"))
        m2 = parse_money(f.get("milestone2_amount"))
        if not m1 and not m2:
            m1 = flat // 2
            m2 = flat - m1
        elif not m2:
            m2 = max(0, flat - m1)
        elif not m1:
            m1 = max(0, flat - m2)
        db.session.add(FlatFeeMilestone(matter_id=matter.id, description=f.get("milestone1_desc", "").strip() or "Retainer on signing",
                                        amount_cents=m1, sort=0, due_on=parse_date(f.get("milestone1_due"))))
        db.session.add(FlatFeeMilestone(matter_id=matter.id, description=f.get("milestone2_desc", "").strip() or "Balance",
                                        amount_cents=m2, sort=1, due_on=parse_date(f.get("milestone2_due"))))
        milestones_made = 2

    # 5. adverse party
    if adverse:
        db.session.add(MatterParty(matter_id=matter.id, name=adverse, role="adverse", notes="From intake"))

    # 6. conflict check record
    check = ConflictCheck(run_by_id=u.id, query="\n".join(n for n in [lead.name, adverse] if n),
                          results_json=json.dumps(hits), matter_id=matter.id, contact_id=contact.id,
                          outcome="unresolved" if hits else "clear",
                          notes=f"Run automatically when converting intake lead #{lead.id}.")
    db.session.add(check)
    db.session.flush()

    # 7. lead
    lead.status = "converted"
    lead.contact_id = contact.id
    lead.matter_id = matter.id
    lead.conflict_check_id = check.id

    audit("create", "contact" if contact_created else "contact_link", contact.id, f"intake lead #{lead.id}", u.id)
    audit("create", "matter", matter.id, f"{matter.number} {matter.name} from intake lead #{lead.id}", u.id)
    audit("create", "conflict_check", check.id, f"{check.outcome}, {len(hits)} hit(s)", u.id)
    audit("convert", "intake_lead", lead.id, f"contact {contact.id}, matter {matter.id}", u.id)

    # 8. engagement letter
    engagement = None
    if f.get("send_engagement") == "1":
        db.session.expire(matter, ["milestones", "client", "responsible"])
        template = db.session.get(LetterTemplate, f.get("template_id", type=int) or 0)
        engagement = build_engagement(matter, template, scope=f.get("scope", "").strip(), user=u)
        send_engagement(engagement, u)

    db.session.commit()

    bits = [f"{'Created' if contact_created else 'Linked'} client {contact.display_name}",
            f"opened matter {matter.number}"]
    if milestones_made:
        bits.append(f"{milestones_made} fee milestones")
    if adverse:
        bits.append(f"adverse party {adverse}")
    bits.append("conflict check " + ("found %d possible hit(s), review it" % len(hits) if hits else "clear"))
    if engagement:
        bits.append("engagement letter sent to " + (engagement.sent_to or "nobody (no email)"))
    flash(". ".join(bits) + ".", "ok" if not hits else "")
    return redirect(f"/matters/{matter.id}")


@bp.route("/<int:id>/decline", methods=["POST"])
@login_required
def decline(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    lead.status = "declined"
    audit("decline", "intake_lead", lead.id, request.form.get("reason", "")[:200], current_user().id)
    db.session.commit()
    flash(f"Declined {lead.name}.", "ok")
    return redirect(url_for("intake.index"))


@bp.route("/<int:id>/status", methods=["POST"])
@login_required
def status(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    s = request.form.get("status", "")
    if s not in ("new", "contacted", "declined"):
        flash("Unknown status.", "error")
        return redirect(url_for("intake.detail", id=lead.id))
    if lead.status == "converted":
        flash("Converted leads keep their status.", "error")
        return redirect(url_for("intake.detail", id=lead.id))
    lead.status = s
    db.session.commit()
    flash(f"Marked {lead.name} as {s}.", "ok")
    return redirect(url_for("intake.detail", id=lead.id))
