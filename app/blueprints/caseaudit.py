"""Nightly case audit and case scoring (Eve Legal lane).

Audit: `run_case_audit()` (also `python -m app.cli case_audit`) walks every open matter. Matters with a PiCase get
the full rule set (records, bills, treatment gaps, imaging, limitations, activity, liens, demand, policy limits);
every other open matter gets the lighter pass (limitations and activity). Each rule writes a CaseAuditFinding keyed
on (matter_id, kind, detail key), so a re-run updates last_seen_on instead of duplicating, and a finding whose
condition no longer holds is marked resolved. When AI is on in Settings and a key is present, one model call per PI
matter asks for possible missed injuries, overlooked imaging and mass tort or product signals; those are stored as
origin="ai" findings (capped at 5 per matter) and are always labelled as AI in the UI. One summary email per day
goes to the firm email (or the owner) when the run produced new high or medium findings; AuditLog
action="case_audit_sent" keeps it to one per day.

Scoring: `score_lead(lead)` is a deterministic 0 to 100 from what is on the intake lead, stored on
IntakeLead.score with the factors in score_json. `score_pi_case(pi)` does the same for a PI case (case_score,
case_score_json). Both are plain arithmetic; the model may add an adjustment of at most 15 points either way when
asked to, and that adjustment is recorded with its reason. Nothing here is legal advice.
"""
import json
import re
from datetime import date, datetime, timedelta
from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort, current_app,
                   has_request_context)
from markupsafe import escape
from ..extensions import db
from ..models import (Firm, User, Matter, PiCase, MedicalProvider, ChronologyEntry, Lien, Task, TimeEntry, Note,
                      Message, Document, IntakeLead, CaseAuditFinding, AuditLog, audit, now)
from ..helpers import login_required, owner_required, current_user
from ..services.mail import send_email
from .. import llm
from ..llm import LLMUnavailable

bp = Blueprint("caseaudit", __name__, url_prefix="/audit")

SEVERITIES = ("high", "medium", "low")
RULE_KINDS = [
    ("missing_records", "Records outstanding"),
    ("bills_missing", "Bills not requested"),
    ("treatment_gap", "Treatment gap"),
    ("imaging_not_obtained", "Imaging mentioned, not obtained"),
    ("sol_near", "Limitations date near"),
    ("no_activity", "No activity"),
    ("lien_no_contact", "Lien without a contact"),
    ("demand_unanswered", "Demand unanswered"),
    ("limits_unknown", "Policy limits unknown"),
]
AI_KINDS = [
    ("injury_mention", "Possible missed injury (AI)"),
    ("imaging_mention", "Imaging in the notes (AI)"),
    ("mass_tort_signal", "Mass tort or product signal (AI)"),
]
KIND_LABELS = dict(RULE_KINDS + AI_KINDS)
AI_KIND_KEYS = [k for k, _ in AI_KINDS]

RECORDS_DAYS = 30
TREATMENT_GAP_DAYS = 45
SOL_DAYS = 90
ACTIVITY_DAYS = 30
DEMAND_DAYS = 30
AI_CAP_PER_MATTER = 5
PI_STAGES_AFTER_DEMAND = ("demand", "negotiation", "litigation")
IMAGING_RE = re.compile(r"\b(mri|ct scan|ct|cat scan|x-?ray|xray|imaging|ultrasound)\b", re.I)

AI_SYSTEM = ("You audit personal injury files for a small law firm. Work only from the material given. Never "
             "invent facts. Each flag is one short sentence of reason. If nothing stands out, return an empty list.")

AI_SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": AI_KIND_KEYS},
            "title": {"type": "string"},
            "reason": {"type": "string"},
            "severity": {"type": "string", "enum": list(SEVERITIES)},
        },
        "required": ["kind", "title", "reason", "severity"], "additionalProperties": False}}},
    "required": ["findings"], "additionalProperties": False,
}

ADJUST_SCHEMA = {
    "type": "object",
    "properties": {"adjustment": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["adjustment", "reason"], "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _uid():
    if not has_request_context():
        return None
    u = current_user()
    return u.id if u else None


def _detail(f):
    try:
        d = json.loads(f.detail_json or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _key_of(f):
    return _detail(f).get("key", "")


def _pi_for(matter):
    return PiCase.query.filter_by(matter_id=matter.id).first()


def _ai_available():
    """AI on in Settings, kill switch not set, a key present. Caps are checked by the call itself."""
    try:
        return bool(Firm.get().ai_enabled) and llm.enabled() and bool(llm.provider())
    except Exception:
        return False


def _slug(s, n=60):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n]


# ---------------------------------------------------------------------------
# deterministic rules. Each returns [(kind, key, severity, message, detail)]
# ---------------------------------------------------------------------------
def _pi_rules(m, pi, today):
    out = []
    providers = MedicalProvider.query.filter_by(matter_id=m.id).order_by(MedicalProvider.id).all()
    for p in providers:
        if p.records_requested_on and not p.records_received_on:
            age = (today - p.records_requested_on).days
            if age > RECORDS_DAYS:
                out.append(("missing_records", f"provider:{p.id}", "high" if age > 2 * RECORDS_DAYS else "medium",
                            f"Records requested from {p.name} {age} days ago and not received.",
                            {"provider_id": p.id, "provider": p.name, "days": age}))
        if p.records_received_on and not p.bills_requested_on:
            out.append(("bills_missing", f"provider:{p.id}", "low",
                        f"Records from {p.name} are in but itemized bills were never requested.",
                        {"provider_id": p.id, "provider": p.name}))

    entries = ChronologyEntry.query.filter(ChronologyEntry.matter_id == m.id, ChronologyEntry.confirmed == True,  # noqa
                                           ChronologyEntry.date != None).order_by(ChronologyEntry.date).all()  # noqa
    if (pi.treatment_status or "treating") == "treating":
        for a, b in zip(entries, entries[1:]):
            gap = (b.date - a.date).days
            if gap > TREATMENT_GAP_DAYS:
                out.append(("treatment_gap", f"{a.date.isoformat()}:{b.date.isoformat()}", "medium",
                            f"{gap}-day gap in treatment between {a.date:%b %-d, %Y} and {b.date:%b %-d, %Y} "
                            f"while the client is still treating.",
                            {"from": a.date.isoformat(), "to": b.date.isoformat(), "days": gap}))

    texts = [pi.incident_description, pi.injuries, pi.liability_notes, pi.overview_text]
    texts += [f"{e.diagnosis} {e.procedure} {e.notes}" for e in entries]
    blob = " ".join(t or "" for t in texts)
    mention = IMAGING_RE.search(blob)
    has_imaging = any((e.visit_type or "").strip().lower() == "imaging" for e in entries)
    if mention and not has_imaging:
        out.append(("imaging_not_obtained", "imaging", "medium",
                    f"The file mentions {mention.group(0).upper()} but the chronology has no imaging visit. "
                    f"Get the study and the report.", {"term": mention.group(0)}))

    liens = Lien.query.filter_by(matter_id=m.id).order_by(Lien.id).all()
    for l in liens:
        if l.status == "open" and not (l.contact or "").strip():
            out.append(("lien_no_contact", f"lien:{l.id}", "low",
                        f"Open lien from {l.holder} has no contact person on file.",
                        {"lien_id": l.id, "holder": l.holder}))

    if pi.demand_sent_on and not pi.offer_cents:
        age = (today - pi.demand_sent_on).days
        if age > DEMAND_DAYS:
            out.append(("demand_unanswered", "demand", "medium",
                        f"Demand sent {age} days ago with no offer recorded. Call the adjuster.",
                        {"sent_on": pi.demand_sent_on.isoformat(), "days": age}))

    if not pi.policy_limits_cents and (pi.stage or "") in PI_STAGES_AFTER_DEMAND:
        out.append(("limits_unknown", "limits", "medium",
                    f"Case is at the {pi.stage} stage and the policy limits are still unknown.",
                    {"stage": pi.stage}))
    return out


def _general_rules(m, today):
    out = []
    if m.sol_date and m.sol_date <= today + timedelta(days=SOL_DAYS):
        tasks = Task.query.filter(Task.matter_id == m.id, Task.kind == "deadline").all()
        covered = any(re.search(r"limitations|statute", t.title or "", re.I) for t in tasks)
        if not covered:
            days = (m.sol_date - today).days
            when = f"in {days} days" if days >= 0 else f"{-days} days ago"
            out.append(("sol_near", "sol", "high",
                        f"Limitations date {m.sol_date:%b %-d, %Y} is {when} and there is no deadline task for it.",
                        {"sol_date": m.sol_date.isoformat(), "days": days}))

    opened = m.opened_on or (m.created_at.date() if m.created_at else today)
    if opened <= today - timedelta(days=ACTIVITY_DAYS):
        cutoff_d = today - timedelta(days=ACTIVITY_DAYS)
        cutoff_dt = datetime.combine(cutoff_d, datetime.min.time())
        active = (TimeEntry.query.filter(TimeEntry.matter_id == m.id, TimeEntry.date >= cutoff_d).first()
                  or Note.query.filter(Note.matter_id == m.id, Note.created_at >= cutoff_dt).first()
                  or Message.query.filter(Message.matter_id == m.id, Message.created_at >= cutoff_dt).first()
                  or Document.query.filter(Document.matter_id == m.id, Document.created_at >= cutoff_dt).first())
        if not active:
            out.append(("no_activity", "activity", "low",
                        f"No time, note, message or document on this matter in the last {ACTIVITY_DAYS} days.",
                        {"days": ACTIVITY_DAYS}))
    return out


# ---------------------------------------------------------------------------
# AI flags
# ---------------------------------------------------------------------------
def _ai_context(m, pi):
    entries = ChronologyEntry.query.filter_by(matter_id=m.id).order_by(ChronologyEntry.date).all()
    chron = "\n".join(f"- {e.date.isoformat() if e.date else '?'} {e.provider_name or ''} [{e.visit_type or ''}] "
                      f"{e.diagnosis or ''} {e.procedure or ''} {e.notes or ''}".strip() for e in entries)
    has_imaging = any((e.visit_type or "").strip().lower() == "imaging" for e in entries)
    parts = [f"Matter: {m.label}", f"Incident type: {pi.incident_type or 'not set'}",
             f"Date of loss: {pi.date_of_loss.isoformat() if pi.date_of_loss else 'not set'}",
             f"Treatment status: {pi.treatment_status or ''}",
             f"Imaging entries in chronology: {'yes' if has_imaging else 'none'}",
             "Facts:\n" + (pi.incident_description or "(none)"),
             "Injuries:\n" + (pi.injuries or "(none)"),
             "Liability notes:\n" + (pi.liability_notes or "(none)"),
             "Overview:\n" + (pi.overview_text or "(none)"),
             "Chronology:\n" + (chron or "(none)")]
    text, _ = llm.clip("\n\n".join(parts), 9000)
    return text


def _ai_findings(m, pi):
    """One model call. Returns [(kind, key, severity, message, detail)] capped at AI_CAP_PER_MATTER, or [] when
    the model is unavailable or answers badly."""
    prompt = ("Read this personal injury file and list anything the team may have missed. Look for: injuries the "
              "facts imply but the injury list does not cover (for example a head strike or loss of consciousness "
              "without a concussion or TBI screen, seat belt bruising without an abdominal check); imaging that is "
              "mentioned in the notes but never appears as an imaging visit in the chronology; and mass tort or "
              "product liability signals (a named drug, device, vehicle defect, recalled product, exposure). "
              "Return JSON {\"findings\": [{\"kind\": \"injury_mention\" | \"imaging_mention\" | "
              "\"mass_tort_signal\", \"title\": \"<short title>\", \"reason\": \"<one sentence>\", "
              "\"severity\": \"low\" | \"medium\" | \"high\"}]}. At most 5 items. Only include items grounded in "
              "the file.\n\n" + _ai_context(m, pi))
    try:
        data = llm.complete_json(prompt, AI_SCHEMA, system=AI_SYSTEM, max_tokens=900, kind="case_audit",
                                 entity="matter", entity_id=m.id)
    except LLMUnavailable:
        return []
    rows = (data.get("findings") if isinstance(data, dict) else data) or []
    out = []
    seen = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("kind") or "").strip()
        if kind not in AI_KIND_KEYS:
            kind = "injury_mention"
        title = str(r.get("title") or "").strip()[:120]
        reason = str(r.get("reason") or "").strip()[:300]
        if not (title or reason):
            continue
        sev = str(r.get("severity") or "medium").lower()
        sev = sev if sev in SEVERITIES else "medium"
        key = _slug(title or reason)
        if (kind, key) in seen:
            continue
        seen.add((kind, key))
        msg = f"{title}. {reason}".strip(". ") if title and reason else (title or reason)
        out.append((kind, key, sev, msg[:400], {"title": title, "reason": reason}))
        if len(out) >= AI_CAP_PER_MATTER:
            break
    return out


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def _upsert(m, origin, rows, today, existing, stats):
    """Create or refresh findings for one matter. existing: {(kind, key): finding} for this matter and origin.
    Returns the set of (kind, key) seen this run."""
    seen = set()
    for kind, key, sev, message, detail in rows:
        seen.add((kind, key))
        f = existing.get((kind, key))
        detail = dict(detail or {}, key=key)
        if f is None:
            f = CaseAuditFinding(matter_id=m.id, kind=kind, severity=sev, message=message,
                                 detail_json=json.dumps(detail), origin=origin, status="open",
                                 first_seen_on=today, last_seen_on=today)
            db.session.add(f)
            stats["new"].append(f)
            existing[(kind, key)] = f
            continue
        f.last_seen_on = today
        if f.status == "resolved":
            # the condition came back after being fixed: reopen it as a fresh finding
            f.status = "open"
            f.first_seen_on = today
            f.severity = sev
            f.message = message
            f.detail_json = json.dumps(detail)
            stats["new"].append(f)
        elif f.status == "open":
            f.severity = sev
            f.message = message
            f.detail_json = json.dumps(detail)
            stats["seen"] += 1
        else:  # dismissed stays dismissed, but we note it was still there
            stats["seen"] += 1
    return seen


def run_case_audit(today=None, with_ai=True):
    """Audit every open matter. Returns dict(matters, new=[findings], seen, resolved, ai, emailed, pi_matters)."""
    today = today or date.today()
    stats = {"matters": 0, "pi_matters": 0, "new": [], "seen": 0, "resolved": 0, "ai": 0, "emailed": False}
    ai_on = with_ai and _ai_available()
    open_matters = Matter.query.filter(Matter.status == "open").order_by(Matter.id).all()
    open_ids = {m.id for m in open_matters}

    all_open = CaseAuditFinding.query.filter(CaseAuditFinding.status != "resolved").all()
    by_matter = {}
    for f in all_open:
        by_matter.setdefault(f.matter_id, {}).setdefault(f.origin, {})[(f.kind, _key_of(f))] = f
    # also index resolved rows so a recurring condition reopens instead of duplicating
    for f in CaseAuditFinding.query.filter(CaseAuditFinding.status == "resolved").all():
        by_matter.setdefault(f.matter_id, {}).setdefault(f.origin, {}).setdefault((f.kind, _key_of(f)), f)

    for m in open_matters:
        stats["matters"] += 1
        pi = _pi_for(m)
        rows = _general_rules(m, today)
        if pi:
            stats["pi_matters"] += 1
            rows += _pi_rules(m, pi, today)
        rule_existing = by_matter.setdefault(m.id, {}).setdefault("rule", {})
        seen = _upsert(m, "rule", rows, today, rule_existing, stats)
        for (kind, key), f in rule_existing.items():
            if (kind, key) not in seen and f.status == "open":
                f.status = "resolved"
                f.last_seen_on = today
                stats["resolved"] += 1
        if pi and ai_on:
            ai_rows = _ai_findings(m, pi)
            ai_existing = by_matter[m.id].setdefault("ai", {})
            before = len(stats["new"])
            _upsert(m, "ai", ai_rows, today, ai_existing, stats)
            stats["ai"] += len(stats["new"]) - before
        db.session.commit()

    # matters that are no longer open: close out whatever is still open on them
    for f in all_open:
        if f.matter_id not in open_ids and f.status == "open":
            f.status = "resolved"
            f.last_seen_on = today
            stats["resolved"] += 1
    n_new = len(stats["new"])
    audit("case_audit_run", "firm", 1,
          f"{stats['matters']} matters ({stats['pi_matters']} PI), {n_new} new, {stats['seen']} seen, "
          f"{stats['resolved']} resolved, {stats['ai']} AI")
    db.session.commit()
    stats["emailed"] = _send_summary(stats, today)
    return stats


def last_run():
    return AuditLog.query.filter_by(action="case_audit_run").order_by(AuditLog.id.desc()).first()


def _send_summary(stats, today):
    """One email per day listing new high and medium findings grouped by matter. Nothing new, nothing sent."""
    rows = [f for f in stats["new"] if f.severity in ("high", "medium")]
    if not rows:
        return False
    key = today.isoformat()
    if AuditLog.query.filter_by(action="case_audit_sent", detail=key).first():
        return False
    firm = Firm.get()
    owner = (User.query.filter_by(role="owner", is_active=True).order_by(User.id).first()
             or User.query.filter_by(is_active=True).order_by(User.id).first())
    to = (firm.email or (owner.email if owner else "") or "").strip()
    if not to:
        return False
    base = current_app.config.get("BASE_URL", "")
    groups = {}
    for f in rows:
        groups.setdefault(f.matter_id, []).append(f)
    parts = []
    for mid, fs in groups.items():
        m = fs[0].matter
        items = "".join(f"<li><strong>{escape(f.severity)}</strong> {escape(f.message)}"
                        f"{' <em>(AI flag, review before acting)</em>' if f.origin == 'ai' else ''}</li>" for f in fs)
        parts.append(f"<h3 style='margin:14px 0 4px;font-size:15px'><a href='{base}/audit/{mid}'>"
                     f"{escape(m.label if m else f'matter {mid}')}</a></h3><ul style='margin:0;padding-left:18px'>{items}</ul>")
    n_high = sum(1 for f in rows if f.severity == "high")
    title = f"Case audit for {today:%A, %B %-d}"
    summary = f"{len(rows)} new finding{'s' if len(rows) != 1 else ''}, {n_high} high"
    html = (f"<div style='font-family:Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;color:#1c2430'>"
            f"<h2 style='font-size:18px'>{escape(title)}</h2><p>{summary}. Open the <a href='{base}/audit'>audit "
            f"page</a> to dismiss or resolve them.</p>{''.join(parts)}</div>")
    send_email(to, f"{title}: {summary}", html, text=f"{summary}. Review at {base}/audit")
    audit("case_audit_sent", "firm", 1, key)
    db.session.commit()
    return True


# ---------------------------------------------------------------------------
# summaries other pages use
# ---------------------------------------------------------------------------
def open_count(matter_id):
    return CaseAuditFinding.query.filter_by(matter_id=matter_id, status="open").count()


def open_counts_by_matter():
    """{matter_id: open finding count} in one query, for boards."""
    rows = db.session.query(CaseAuditFinding.matter_id, db.func.count(CaseAuditFinding.id)).filter(
        CaseAuditFinding.status == "open").group_by(CaseAuditFinding.matter_id).all()
    return {mid: n for mid, n in rows}


def case_audit_summary(matter):
    """For the PI case page card: open count, high count, last run date, newest few."""
    q = CaseAuditFinding.query.filter_by(matter_id=matter.id, status="open")
    run = last_run()
    return {"open": q.count(), "high": q.filter_by(severity="high").count(),
            "last_run": run.created_at if run else None,
            "newest": q.order_by(CaseAuditFinding.first_seen_on.desc(), CaseAuditFinding.id.desc()).limit(3).all()}


# ---------------------------------------------------------------------------
# lead scoring
# ---------------------------------------------------------------------------
LEAD_TYPE_WEIGHTS = {
    "personal injury": 30, "litigation": 22, "employment": 18, "business formation": 15, "estate planning": 15,
    "real estate": 15, "family law": 15, "criminal defense": 15, "immigration": 12, "other": 8,
}
LEAD_KEYWORDS = ("hospital", "surgery", "police report", "insurance", "signed", "deadline", "accident", "injur",
                 "contract", "court", "served", "arrest")


def score_lead(lead, today=None):
    """Deterministic 0 to 100 from what is on the lead. Stores score and score_json; caller commits.
    Returns (score, factors)."""
    today = today or date.today()
    factors = []

    def add(label, pts):
        if pts:
            factors.append({"label": label, "points": int(pts)})

    mtype = (lead.matter_type or "").strip().lower()
    w = LEAD_TYPE_WEIGHTS.get(mtype)
    if w is None and mtype:
        w = next((v for k, v in LEAD_TYPE_WEIGHTS.items() if k in mtype or mtype in k), 10)
    add(f"Matter type: {lead.matter_type or 'not given'}", w or 0)
    add("Phone given", 10 if (lead.phone or "").strip() else 0)
    add("Email given", 10 if (lead.email or "").strip() else 0)
    desc = (lead.description or "").strip()
    n = len(desc)
    add(f"Description length ({n} characters)", 0 if not n else 4 if n < 50 else 8 if n < 200 else 12)
    low = desc.lower()
    hits = [k for k in LEAD_KEYWORDS if k in low]
    if hits:
        add("Key words: " + ", ".join(hits[:6]), min(20, 5 * len(hits)))
    add("Other party named", 8 if (lead.adverse_party or "").strip() else 0)
    created = lead.created_at or now()
    age = (today - created.date()).days
    add("Fresh lead" if age <= 2 else "Recent lead", 8 if age <= 2 else 5 if age <= 7 else 2 if age <= 30 else 0)
    score = max(0, min(100, sum(f["points"] for f in factors)))
    lead.score = score
    lead.score_json = json.dumps({"score": score, "factors": factors, "computed_at": now().isoformat(timespec="seconds")})
    return score, factors


# ---------------------------------------------------------------------------
# PI case scoring
# ---------------------------------------------------------------------------
def score_pi_case(pi, today=None, refine=False):
    """Deterministic 0 to 100 for a PI case. refine=True asks the model (when available) for an adjustment of at
    most 15 points either way, recorded with its reason. Stores case_score and case_score_json; caller commits.
    Returns (score, data)."""
    today = today or date.today()
    m = pi.matter
    factors = []

    def add(label, pts):
        if pts:
            factors.append({"label": label, "points": int(pts)})

    add("Liability notes on file", 15 if (pi.liability_notes or "").strip() else 0)
    add("Policy limits known", 10 if pi.policy_limits_cents else 0)
    ts = pi.treatment_status or "treating"
    add("Treatment " + ("complete" if ts in ("mmi", "released") else "ongoing"), 10 if ts in ("mmi", "released") else 5)
    providers = MedicalProvider.query.filter_by(matter_id=pi.matter_id).all()
    add(f"{len(providers)} treating provider{'s' if len(providers) != 1 else ''}", min(4, len(providers)) * 4)
    specials = sum(int(p.total_billed_cents or 0) for p in providers)
    add(f"Medical specials ${specials // 100:,}", 0 if not specials else 5 if specials < 500000 else
        10 if specials < 2500000 else 15)
    entries = ChronologyEntry.query.filter_by(matter_id=pi.matter_id).all()
    has_imaging = any((e.visit_type or "").strip().lower() == "imaging" for e in entries)
    if not has_imaging:
        blob = " ".join([pi.injuries or "", pi.incident_description or ""] +
                        [f"{e.diagnosis} {e.procedure} {e.notes}" for e in entries])
        has_imaging = bool(IMAGING_RE.search(blob))
    add("Imaging on file", 8 if has_imaging else 0)
    add("Demand sent", 8 if pi.demand_sent_on else 0)
    if pi.date_of_loss:
        days = (today - pi.date_of_loss).days
        add(f"{days} days since the loss", 10 if days < 365 else 5 if days < 730 else 0)
    base = max(0, min(100, sum(f["points"] for f in factors)))
    data = {"score": base, "base": base, "factors": factors, "computed_at": now().isoformat(timespec="seconds")}
    if refine:
        adj = _ai_adjustment(pi, m, base, factors)
        if adj:
            data["ai_adjustment"] = adj
            data["score"] = max(0, min(100, base + adj["delta"]))
    pi.case_score = data["score"]
    pi.case_score_json = json.dumps(data)
    return data["score"], data


def _ai_adjustment(pi, m, base, factors):
    """Optional model refinement: {"delta": -15..15, "reason": str} or None when unavailable."""
    if not _ai_available():
        return None
    prompt = (f"A rules-based score gave this personal injury case {base}/100 from these factors: "
              + "; ".join(f"{f['label']} (+{f['points']})" for f in factors) +
              ". Reading the file below, say whether the score should move, by how much (an integer from -15 to "
              "15) and why in one sentence. Return JSON {\"adjustment\": <int>, \"reason\": \"<sentence>\"}.\n\n"
              + _ai_context(m, pi))
    try:
        data = llm.complete_json(prompt, ADJUST_SCHEMA, system=AI_SYSTEM, max_tokens=300, kind="case_score",
                                 entity="matter", entity_id=m.id, user_id=_uid())
    except LLMUnavailable:
        return None
    try:
        delta = int(data.get("adjustment", 0))
    except (TypeError, ValueError, AttributeError):
        return None
    delta = max(-15, min(15, delta))
    return {"delta": delta, "reason": str(data.get("reason") or "").strip()[:300], "origin": "ai"}


@bp.app_template_filter("score_factors")
def score_factors_filter(raw):
    """score_json / case_score_json -> dict with factors list (empty when unset)."""
    try:
        d = json.loads(raw or "{}")
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("factors", [])
    return d


@bp.app_template_filter("kind_label")
def kind_label_filter(kind):
    return KIND_LABELS.get(kind, (kind or "").replace("_", " "))


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
def _matter_or_404(matter_id):
    return db.session.get(Matter, matter_id) or abort(404)


@bp.route("")
@login_required
def index():
    sev = request.args.get("severity", "")
    kind = request.args.get("kind", "")
    q = CaseAuditFinding.query.filter_by(status="open")
    if sev in SEVERITIES:
        q = q.filter_by(severity=sev)
    if kind in KIND_LABELS:
        q = q.filter_by(kind=kind)
    rows = q.order_by(CaseAuditFinding.matter_id, CaseAuditFinding.severity, CaseAuditFinding.id.desc()).all()
    groups = []
    for f in rows:
        if groups and groups[-1][0].id == f.matter_id:
            groups[-1][1].append(f)
        else:
            groups.append((f.matter, [f]))
    order = {"high": 0, "medium": 1, "low": 2}
    for _, fs in groups:
        fs.sort(key=lambda f: (order.get(f.severity, 3), -f.id))
    all_open = CaseAuditFinding.query.filter_by(status="open").all()
    counts = {"total": len(all_open),
              "severity": {s: sum(1 for f in all_open if f.severity == s) for s in SEVERITIES},
              "kind": {k: sum(1 for f in all_open if f.kind == k) for k, _ in RULE_KINDS + AI_KINDS},
              "ai": sum(1 for f in all_open if f.origin == "ai")}
    return render_template("audit/index.html", groups=groups, counts=counts, sev=sev, kind=kind,
                           kinds=RULE_KINDS + AI_KINDS, severities=SEVERITIES, run=last_run(),
                           ai_on=_ai_available())


@bp.route("/<int:matter_id>")
@login_required
def matter(matter_id):
    m = _matter_or_404(matter_id)
    rows = CaseAuditFinding.query.filter_by(matter_id=m.id).order_by(CaseAuditFinding.id.desc()).all()
    order = {"open": 0, "dismissed": 1, "resolved": 2}
    sev = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda f: (order.get(f.status, 3), sev.get(f.severity, 3), -f.id))
    return render_template("audit/matter.html", m=m, rows=rows, pi=_pi_for(m), run=last_run(),
                           open_n=sum(1 for f in rows if f.status == "open"))


def _set_status(fid, status):
    f = db.session.get(CaseAuditFinding, fid) or abort(404)
    old = f.status
    f.status = status
    audit("case_audit_" + status, "case_audit_finding", f.id, f"{f.kind} on matter {f.matter_id}: {old} -> {status}",
          _uid())
    db.session.commit()
    flash(f"Finding {status}.", "ok")
    nxt = request.form.get("next") or ""
    if nxt.startswith("/"):
        return redirect(nxt)
    return redirect(url_for("caseaudit.matter", matter_id=f.matter_id))


@bp.route("/finding/<int:id>/dismiss", methods=["POST"])
@login_required
def dismiss(id):
    return _set_status(id, "dismissed")


@bp.route("/finding/<int:id>/resolve", methods=["POST"])
@login_required
def resolve(id):
    return _set_status(id, "resolved")


@bp.route("/finding/<int:id>/reopen", methods=["POST"])
@login_required
def reopen(id):
    return _set_status(id, "open")


@bp.route("/run", methods=["POST"])
@owner_required
def run_now():
    r = run_case_audit()
    flash(f"Audit ran over {r['matters']} open matter{'s' if r['matters'] != 1 else ''}: {len(r['new'])} new, "
          f"{r['seen']} still open, {r['resolved']} resolved, {r['ai']} AI flag{'s' if r['ai'] != 1 else ''}"
          f"{', summary emailed' if r['emailed'] else ''}.", "ok")
    nxt = request.form.get("next") or ""
    return redirect(nxt if nxt.startswith("/") else url_for("caseaudit.index"))


@bp.route("/pi/<int:matter_id>/rescore", methods=["POST"])
@login_required
def rescore_pi(matter_id):
    m = _matter_or_404(matter_id)
    pi = _pi_for(m) or abort(404)
    refine = request.form.get("refine") == "1"
    score, data = score_pi_case(pi, refine=refine)
    audit("case_score", "matter", m.id, f"{score}/100" + (" with AI adjustment" if data.get("ai_adjustment") else ""),
          _uid())
    db.session.commit()
    note = ""
    if refine and not data.get("ai_adjustment"):
        note = " The model was not available, so the score is rules only."
    flash(f"Case score {score}/100.{note}", "ok")
    return redirect(url_for("pi.case", matter_id=m.id) + "#audit")


@bp.route("/lead/<int:id>/rescore", methods=["POST"])
@login_required
def rescore_lead(id):
    lead = db.session.get(IntakeLead, id) or abort(404)
    score, _ = score_lead(lead)
    db.session.commit()
    flash(f"Lead score {score}/100.", "ok")
    return redirect(url_for("intake.detail", id=lead.id))
