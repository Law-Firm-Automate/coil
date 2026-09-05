"""Eve Legal lane, part 1: medical records into a treatment chronology, a case overview written from the
documents, and a narrative demand letter drafted in the firm's voice.

Every model call goes through app.llm (complete_json). When the model is unavailable each feature has a plain
fallback: a regex pass over the records for dates and dollar amounts near known providers, an overview assembled
from the structured data, and a templated demand letter. Everything the model writes is a draft for attorney
review and may contain errors; every page that shows model output says so once.

Money is integer cents throughout. Dates are naive UTC. The current narrative demand draft is kept as a small JSON
file under UPLOAD_DIR/<matter_id>/ (no schema change); saving it builds a PDF Document in the Demand folder.
"""
import json
import os
import re
from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app

from ..extensions import db
from ..models import (Matter, PiCase, ChronologyEntry, MedicalProvider, Lien, Document, Note, Firm, audit, now)
from ..helpers import login_required, current_user, parse_money, parse_date, cents_to_str
from .. import llm
from ..llm import LLMUnavailable
from ..services.pdf import money as pdf_money
from .documents import parse_tags
from .pi import (PiPDF, _para, _line, _table, _letter_head, _signature, save_pdf_document, _fmt_date,
                 TREATMENT_STATUSES)

bp = Blueprint("records", __name__, url_prefix="/records")

SYSTEM = ("You help the staff of a small plaintiff-side law firm. Be accurate and plain. Never invent facts, "
          "names, dates, diagnoses or amounts that are not in the material you are given. No marketing language.")
DRAFT_LINE = "This is a draft for attorney review and may contain errors."

VISIT_TYPES = ["ER", "office", "imaging", "surgery", "PT", "pharmacy", "other"]
CHUNK_CHARS = 9000
MAX_CHUNKS = 12
DOC_TEXT_BUDGET = 12000
OVERVIEW_FOLDERS = ("Medical records", "Demand", "Email", "")
STYLE_TAG = "style-example"
MAX_STYLE_EXAMPLES = 3
DRAFT_FILE = "narrative_demand_draft.json"

OVERVIEW_SECTIONS = [("facts", "Facts"), ("parties", "Parties"), ("injuries_and_treatment", "Injuries and treatment"),
                     ("liability", "Liability"), ("damages_summary", "Damages summary"),
                     ("open_questions", "Open questions")]
DEMAND_SECTIONS = [("intro", "Introduction"), ("facts", "Facts"), ("liability", "Liability"),
                   ("injuries_and_treatment", "Injuries and treatment"), ("damages", "Damages"),
                   ("demand_and_deadline", "Demand and deadline"), ("closing", "Closing")]

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"entries": {"type": "array", "items": {
        "type": "object",
        "properties": {"date": {"type": "string"}, "provider": {"type": "string"}, "visit_type": {"type": "string"},
                       "diagnosis": {"type": "string"}, "procedure": {"type": "string"}, "charges": {"type": "string"},
                       "page_ref": {"type": "string"}, "notes": {"type": "string"}},
        "required": ["date", "provider", "visit_type", "diagnosis", "procedure", "charges", "page_ref", "notes"],
        "additionalProperties": False}}},
    "required": ["entries"], "additionalProperties": False,
}
OVERVIEW_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "string"}, "parties": {"type": "string"},
                   "injuries_and_treatment": {"type": "string"}, "liability": {"type": "string"},
                   "damages_summary": {"type": "string"},
                   "open_questions": {"type": "array", "items": {"type": "string"}}},
    "required": [k for k, _ in OVERVIEW_SECTIONS], "additionalProperties": False,
}
DEMAND_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "string"} for k, _ in DEMAND_SECTIONS},
    "required": [k for k, _ in DEMAND_SECTIONS], "additionalProperties": False,
}

MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|(?:" + MONTHS + r")\.?\s+\d{1,2},?\s+\d{4})\b",
                     re.I)
MONEY_RE = re.compile(r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\$\s?\d+(?:\.\d{2})?")
PAGE_RE = re.compile(r"\f|(?<![A-Za-z])Page\s+(\d{1,4})(?:\s+of\s+\d{1,4})?(?![A-Za-z0-9])", re.I)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _uid():
    u = current_user()
    return u.id if u else None


def _load(matter_id):
    """Matter plus its PI case. Visiting the chronology starts the PI case when the matter has none yet."""
    m = db.session.get(Matter, matter_id) or abort(404)
    c = PiCase.query.filter_by(matter_id=m.id).first()
    if not c:
        c = PiCase(matter_id=m.id, stage="intake", treatment_status="treating")
        db.session.add(c)
        db.session.flush()
        audit("pi_start", "matter", m.id, "personal injury case started from records", _uid())
        db.session.commit()
    return m, c


def _entries(m):
    return ChronologyEntry.query.filter_by(matter_id=m.id).order_by(
        ChronologyEntry.date.asc().nulls_last(), ChronologyEntry.id).all()


def _providers(m):
    return MedicalProvider.query.filter_by(matter_id=m.id).order_by(MedicalProvider.name).all()


def _liens(m):
    return Lien.query.filter_by(matter_id=m.id).order_by(Lien.id).all()


def _matter_docs(matter_id):
    return Document.query.filter(Document.matter_id == matter_id, Document.is_current == True).order_by(  # noqa: E712
        Document.created_at.desc(), Document.id.desc()).all()


def parse_any_date(s):
    """'2024-03-05', '3/5/2024', '3/5/24', 'March 5, 2024', 'Mar 5 2024' -> date, else None."""
    s = str(s or "").strip().rstrip(".")
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%b. %d, %Y",
                "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return parse_date(s)


def _charges_cents(v):
    """Model output may be '1,250.00', '$1250', 1250.5 or ''. Never negative."""
    if v is None:
        return 0
    try:
        return max(0, parse_money(str(v)))
    except (TypeError, ValueError):
        return 0


def _s(v, limit=None):
    t = str(v or "").strip() if v is not None else ""
    return t[:limit] if limit else t


def _draft_path(matter_id):
    return os.path.join(current_app.config["UPLOAD_DIR"], str(matter_id), DRAFT_FILE)


def _load_draft(matter_id):
    p = _draft_path(matter_id)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save_draft(matter_id, draft):
    p = _draft_path(matter_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(draft, fh, ensure_ascii=False, indent=1)


def records_summary(matter_id):
    """Counts and lists the PI case page needs for its Chronology, Overview and Demand cards."""
    entries = ChronologyEntry.query.filter_by(matter_id=matter_id).all()
    docs = _matter_docs(matter_id)
    demand_doc = next((d for d in docs if d.folder == "Demand" and d.name.startswith("Narrative demand")), None)
    return dict(total=len(entries), unconfirmed=sum(1 for e in entries if not e.confirmed), docs=docs,
                demand_doc=demand_doc, has_draft=os.path.isfile(_draft_path(matter_id)))


@bp.app_context_processor
def _template_globals():
    return dict(records_summary=records_summary)


# ---------------------------------------------------------------------------
# 1. chronology page and hand edits
# ---------------------------------------------------------------------------
@bp.route("/<int:matter_id>")
@login_required
def chronology(matter_id):
    m, c = _load(matter_id)
    entries = _entries(m)
    return render_template("records/chronology.html", m=m, c=c, entries=entries, providers=_providers(m),
                           docs=_matter_docs(m.id), visit_types=VISIT_TYPES,
                           unconfirmed=sum(1 for e in entries if not e.confirmed),
                           total_charges=sum(int(e.charges_cents or 0) for e in entries),
                           confirmed_charges=sum(int(e.charges_cents or 0) for e in entries if e.confirmed))


def _fill_entry(e, f, m):
    e.date = parse_any_date(f.get("date"))
    pid = _int(f.get("provider_id"))
    prov = db.session.get(MedicalProvider, pid) if pid else None
    if prov and prov.matter_id != m.id:
        prov = None
    e.provider_id = prov.id if prov else None
    e.provider_name = _s(f.get("provider_name"), 200) or (prov.name if prov else "")
    e.visit_type = _s(f.get("visit_type"), 60)
    e.diagnosis = _s(f.get("diagnosis"))
    e.procedure = _s(f.get("procedure"))
    e.charges_cents = _charges_cents(f.get("charges"))
    e.page_ref = _s(f.get("page_ref"), 40)
    e.notes = _s(f.get("notes"))


@bp.route("/<int:matter_id>/entries", methods=["POST"])
@login_required
def entry_add(matter_id):
    m, c = _load(matter_id)
    e = ChronologyEntry(matter_id=m.id, origin="user", confirmed=True)
    _fill_entry(e, request.form, m)
    if not (e.date or e.provider_name or e.procedure or e.diagnosis):
        flash("Enter at least a date, a provider or what was done.", "error")
        return redirect(url_for("records.chronology", matter_id=m.id))
    db.session.add(e)
    db.session.flush()
    audit("create", "chronology_entry", e.id, f"{e.date or 'no date'} {e.provider_name}", _uid())
    db.session.commit()
    flash("Entry added.", "ok")
    return redirect(url_for("records.chronology", matter_id=m.id))


def _entry(m, eid):
    e = db.session.get(ChronologyEntry, eid) or abort(404)
    if e.matter_id != m.id:
        abort(404)
    return e


@bp.route("/<int:matter_id>/entries/<int:eid>/edit", methods=["GET", "POST"])
@login_required
def entry_edit(matter_id, eid):
    m, c = _load(matter_id)
    e = _entry(m, eid)
    if request.method == "POST":
        _fill_entry(e, request.form, m)
        if request.form.get("confirm"):
            e.confirmed = True
        audit("update", "chronology_entry", e.id, f"{e.date or 'no date'} {e.provider_name}", _uid())
        db.session.commit()
        flash("Entry saved.", "ok")
        return redirect(url_for("records.chronology", matter_id=m.id))
    return render_template("records/entry_form.html", m=m, e=e, providers=_providers(m), visit_types=VISIT_TYPES)


@bp.route("/<int:matter_id>/entries/<int:eid>/delete", methods=["POST"])
@login_required
def entry_delete(matter_id, eid):
    m, c = _load(matter_id)
    e = _entry(m, eid)
    audit("delete", "chronology_entry", e.id, f"{e.date or 'no date'} {e.provider_name}", _uid())
    db.session.delete(e)
    db.session.commit()
    flash("Entry deleted.", "ok")
    return redirect(url_for("records.chronology", matter_id=m.id))


@bp.route("/<int:matter_id>/entries/<int:eid>/confirm", methods=["POST"])
@login_required
def entry_confirm(matter_id, eid):
    m, c = _load(matter_id)
    e = _entry(m, eid)
    e.confirmed = True
    db.session.commit()
    flash("Entry confirmed.", "ok")
    return redirect(url_for("records.chronology", matter_id=m.id))


@bp.route("/<int:matter_id>/entries/<int:eid>/link", methods=["POST"])
@login_required
def entry_link(matter_id, eid):
    """Link (or unlink) an entry to one of the matter's providers. The typed provider name is kept."""
    m, c = _load(matter_id)
    e = _entry(m, eid)
    pid = _int(request.form.get("provider_id"))
    prov = db.session.get(MedicalProvider, pid) if pid else None
    if prov and prov.matter_id != m.id:
        prov = None
    e.provider_id = prov.id if prov else None
    if prov and not e.provider_name:
        e.provider_name = prov.name
    db.session.commit()
    flash(f"Linked to {prov.name}." if prov else "Provider link removed.", "ok")
    return redirect(url_for("records.chronology", matter_id=m.id))


@bp.route("/<int:matter_id>/confirm-all", methods=["POST"])
@login_required
def confirm_all(matter_id):
    m, c = _load(matter_id)
    n = 0
    for e in _entries(m):
        if not e.confirmed:
            e.confirmed = True
            n += 1
    if n:
        audit("confirm_all", "chronology", m.id, f"{n} entries confirmed", _uid())
    db.session.commit()
    flash(f"Confirmed {n} entr{'y' if n == 1 else 'ies'}.", "ok")
    return redirect(url_for("records.chronology", matter_id=m.id))


@bp.route("/<int:matter_id>/recalc-specials", methods=["POST"])
@login_required
def recalc_specials(matter_id):
    """Provider.total_billed_cents = sum of confirmed, linked entries. Providers with no confirmed entries keep
    whatever was typed on the provider form. First and last visit dates widen to cover the confirmed entries."""
    m, c = _load(matter_id)
    entries = [e for e in _entries(m) if e.confirmed and e.provider_id]
    touched = []
    for p in _providers(m):
        rows = [e for e in entries if e.provider_id == p.id]
        if not rows:
            continue
        p.total_billed_cents = sum(int(e.charges_cents or 0) for e in rows)
        dates = [e.date for e in rows if e.date]
        if dates:
            p.first_visit_on = min([d for d in (p.first_visit_on, min(dates)) if d])
            p.last_visit_on = max([d for d in (p.last_visit_on, max(dates)) if d])
        touched.append(p)
    audit("recalc_specials", "matter", m.id, "; ".join(f"{p.name} {cents_to_str(p.total_billed_cents)}" for p in touched),
          _uid())
    db.session.commit()
    if touched:
        total = sum(int(p.total_billed_cents or 0) for p in touched)
        flash(f"Specials recalculated for {len(touched)} provider{'' if len(touched) == 1 else 's'} from confirmed "
              f"entries: {cents_to_str(total)}. Providers with no confirmed linked entries were left alone.", "ok")
    else:
        flash("No confirmed entries are linked to a provider yet. Confirm entries and link them first.", "error")
    return redirect(url_for("records.chronology", matter_id=m.id))


# ---------------------------------------------------------------------------
# 1b. extraction from a document
# ---------------------------------------------------------------------------
def _pages(text):
    """[(page label, segment)]. Splits on form feeds or 'Page N' text; one unlabeled segment when there are no
    markers. A marker is taken to start the page it names."""
    marks = list(PAGE_RE.finditer(text))
    if len(marks) < 2:
        return [("", text)]
    out, prev_end, label = [], 0, ""
    first_no = _int(marks[0].group(1))
    if first_no is None or first_no > 1:  # a form feed, or a record that starts mid-way: the lead-in is page 1
        label = "1"
    for mk in marks:
        seg = text[prev_end:mk.start()]
        if seg.strip():
            out.append((label, seg))
        label = mk.group(1) or str(len(out) + 1)
        prev_end = mk.end()
    tail = text[prev_end:]
    if tail.strip():
        out.append((label, tail))
    return out


def chunk_text(text, size=CHUNK_CHARS):
    """Pieces of about `size` characters, each carrying [Page N] markers where the source had them."""
    chunks, cur = [], ""
    for label, seg in _pages(text):
        head = f"[Page {label}]\n" if label else ""
        seg = seg.strip()
        while len(seg) > size:
            cut = seg.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(head + seg[:cut])
            seg = seg[cut:].lstrip()
        block = head + seg + "\n"
        if cur and len(cur) + len(block) > size:
            chunks.append(cur)
            cur = ""
        cur += block
    if cur.strip():
        chunks.append(cur)
    return chunks


def _extract_prompt(m, chunk):
    return ("Below is part of a medical record for our client " + (m.client.display_name if m.client else "") +
            ". List every dated visit, treatment, test, procedure or charge as one entry. Use the date as "
            "written (YYYY-MM-DD when you can), the provider or facility name, a visit type from: " +
            ", ".join(VISIT_TYPES) + ", the diagnosis, the procedure or service, the charge as dollars "
            "('1,250.00'; blank when the record shows none), the page reference from the [Page N] markers when "
            "present, and any short note worth a lawyer's attention. Leave a field blank rather than guessing. "
            "Return JSON {\"entries\": [{\"date\", \"provider\", \"visit_type\", \"diagnosis\", \"procedure\", "
            "\"charges\", \"page_ref\", \"notes\"}]}. An empty list is fine when there is nothing dated.\n\n" +
            "Record text:\n" + chunk)


def _rows_from_model(data):
    rows = data.get("entries") if isinstance(data, dict) else data
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        out.append(dict(date=_s(r.get("date"), 40), provider=_s(r.get("provider"), 200),
                        visit_type=_s(r.get("visit_type"), 60), diagnosis=_s(r.get("diagnosis")),
                        procedure=_s(r.get("procedure")), charges=r.get("charges"), page_ref=_s(r.get("page_ref"), 40),
                        notes=_s(r.get("notes"))))
    return out


def regex_extract(text, m):
    """Fallback without the model: dates and dollar amounts near provider names already on the matter."""
    marks = [(mk.start(), mk.group(1) or str(i + 1)) for i, mk in enumerate(PAGE_RE.finditer(text))]

    def page_at(idx):
        label = ""
        for pos, lab in marks:
            if pos <= idx:
                label = lab
            else:
                break
        return label

    rows, seen = [], set()
    for p in _providers(m):
        name = (p.name or "").strip()
        if len(name) < 3:
            continue
        for hit in re.finditer(re.escape(name), text, re.I):
            # what follows the name belongs to it; a date just before it is a fallback, an amount before it is not
            after = text[hit.end(): hit.end() + 400]
            before = text[max(0, hit.start() - 120): hit.start()]
            dates = DATE_RE.findall(after) or DATE_RE.findall(before)
            amounts = MONEY_RE.findall(after)
            if not dates and not amounts:
                continue
            d = dates[0] if dates else ""
            key = (d, name.lower())
            if key in seen:
                continue
            seen.add(key)
            rows.append(dict(date=d, provider=name, visit_type="", diagnosis="", procedure="",
                             charges=amounts[0] if amounts else "", page_ref=page_at(hit.start()),
                             notes="Found by the fallback text scan (no AI). Verify against the record."))
    return rows


def _provider_lookup(m):
    return {(p.name or "").strip().lower(): p for p in _providers(m)}


def _match_provider(name, lookup):
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in lookup:
        return lookup[key]
    for k, p in lookup.items():
        if len(k) >= 5 and (k in key or key in k):
            return p
    return None


def _add_entries(m, doc, rows, origin="ai"):
    """Create ChronologyEntry rows, deduping on (date, provider, procedure) against what the matter already has.
    Providers not yet on the matter are added by name. Returns (added, skipped, new provider names)."""
    existing = {((e.date.isoformat() if e.date else ""), (e.provider_name or "").strip().lower(),
                 (e.procedure or "").strip().lower()) for e in _entries(m)}
    lookup = _provider_lookup(m)
    added, skipped, new_names = 0, 0, []
    for r in rows:
        d = parse_any_date(r.get("date"))
        pname = _s(r.get("provider"), 200)
        proc = _s(r.get("procedure"))
        key = ((d.isoformat() if d else ""), pname.lower(), proc.lower())
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        prov = _match_provider(pname, lookup)
        if pname and not prov:
            prov = MedicalProvider(matter_id=m.id, name=pname, notes="added from records extraction")
            db.session.add(prov)
            db.session.flush()
            lookup[pname.lower()] = prov
            new_names.append(pname)
        e = ChronologyEntry(matter_id=m.id, provider_id=prov.id if prov else None, date=d,
                            provider_name=pname or (prov.name if prov else ""), visit_type=_s(r.get("visit_type"), 60),
                            diagnosis=r.get("diagnosis") or "", procedure=proc, charges_cents=_charges_cents(r.get("charges")),
                            source_document_id=doc.id if doc else None, page_ref=_s(r.get("page_ref"), 40),
                            notes=r.get("notes") or "", origin=origin, confirmed=False)
        db.session.add(e)
        added += 1
    return added, skipped, new_names


@bp.route("/<int:matter_id>/extract", methods=["POST"])
@login_required
def extract(matter_id):
    m, c = _load(matter_id)
    doc = db.session.get(Document, _int(request.form.get("document_id")) or 0)
    if not doc or doc.matter_id != m.id:
        flash("Pick one of this matter's documents to extract from.", "error")
        return redirect(url_for("records.chronology", matter_id=m.id))
    text = (doc.extracted_text or "").strip()
    if not text:
        flash(f"{doc.name} has no readable text. Scanned PDFs need OCR before extraction; type the entries by hand "
              "for now.", "error")
        return redirect(url_for("records.chronology", matter_id=m.id))
    chunks = chunk_text(text)
    rows, fallback, reason, done = [], False, "", 0
    for i, chunk in enumerate(chunks[:MAX_CHUNKS]):
        try:
            data = llm.complete_json(_extract_prompt(m, chunk), EXTRACT_SCHEMA, system=SYSTEM, max_tokens=3000,
                                     kind="records_extract", entity="document", entity_id=doc.id, user_id=_uid())
        except LLMUnavailable as e:
            reason = str(e)
            if i == 0:
                rows = regex_extract(text, m)
                fallback = True
            break
        rows.extend(_rows_from_model(data))
        done += 1
    added, skipped, new_names = _add_entries(m, doc, rows)
    audit("records_extract", "document", doc.id,
          f"{added} added, {skipped} duplicates skipped{', fallback scan' if fallback else ''}", _uid())
    db.session.commit()
    msg = f"Added {added} unconfirmed entr{'y' if added == 1 else 'ies'} from {doc.name}"
    if skipped:
        msg += f" ({skipped} already on the chronology)"
    msg += "."
    if new_names:
        msg += f" New provider{'' if len(new_names) == 1 else 's'} added: {', '.join(new_names[:6])}."
    if fallback:
        msg += (f" The AI was not available ({reason}) so this was a plain text scan for dates and dollar amounts "
                "near the providers already on the matter. Diagnoses and procedures were not read.")
    elif reason:
        msg += f" The AI stopped after {done} of {len(chunks)} parts ({reason}). Run it again later to finish."
    elif len(chunks) > MAX_CHUNKS:
        msg += (f" The document has {len(chunks)} parts and only the first {MAX_CHUNKS} were read this run. "
                "Run it again to read the rest; duplicates are skipped.")
    flash(msg, "ok" if added or not fallback else "error")
    return redirect(url_for("records.chronology", matter_id=m.id))


# ---------------------------------------------------------------------------
# 2. case overview from the documents
# ---------------------------------------------------------------------------
def _chron_lines(entries, limit=40):
    out = []
    for e in entries[:limit]:
        bits = [e.date.isoformat() if e.date else "undated", e.provider_name or "", e.visit_type or ""]
        if e.diagnosis:
            bits.append("dx: " + e.diagnosis.strip()[:120])
        if e.procedure:
            bits.append(e.procedure.strip()[:120])
        if e.charges_cents:
            bits.append(cents_to_str(e.charges_cents))
        out.append("- " + " | ".join(b for b in bits if b))
    if len(entries) > limit:
        out.append(f"- ({len(entries) - limit} more entries not shown)")
    return "\n".join(out)


def _structured_context(m, c, entries, providers, liens):
    """The facts, chronology, specials, liens and notes as plain text. Shared by the overview and the demand."""
    parts = [f"Matter {m.number}: {m.name}. Client: {m.client.display_name if m.client else ''}.",
             f"Date of loss: {c.date_of_loss or 'not entered'}. Incident type: {c.incident_type or 'not entered'}. "
             f"Stage: {c.stage}. Treatment status: {dict(TREATMENT_STATUSES).get(c.treatment_status, c.treatment_status)}."]
    if c.incident_description:
        parts.append("What happened: " + c.incident_description.strip())
    if c.injuries:
        parts.append("Injuries: " + c.injuries.strip())
    if c.liability_notes:
        parts.append("Liability notes: " + c.liability_notes.strip())
    ins = [x for x in (c.insurer, c.claim_number and f"claim {c.claim_number}", c.adjuster_name and f"adjuster {c.adjuster_name}") if x]
    if ins:
        parts.append("Insurer: " + ", ".join(ins) + ".")
    parts.append(f"Policy limits: {cents_to_str(c.policy_limits_cents) if c.policy_limits_cents else 'unknown'}. "
                 f"UM/UIM: {cents_to_str(c.um_uim_limits_cents) if c.um_uim_limits_cents else 'unknown'}. "
                 f"Demand: {cents_to_str(c.demand_amount_cents) if c.demand_amount_cents else 'none yet'}"
                 + (f" sent {c.demand_sent_on}" if c.demand_sent_on else "") +
                 f". Offer: {cents_to_str(c.offer_cents) if c.offer_cents else 'none'}.")
    if m.parties:
        parts.append("Parties: " + "; ".join(f"{p.name} ({p.role})" for p in m.parties))
    if providers:
        total = sum(int(p.total_billed_cents or 0) for p in providers)
        parts.append("Providers and billed amounts (specials):\n" + "\n".join(
            f"- {p.name}{(' (' + p.specialty + ')') if p.specialty else ''}: {cents_to_str(p.total_billed_cents or 0)}"
            f"{'' if p.records_received_on else ', records not yet received'}" for p in providers) +
            f"\nTotal specials: {cents_to_str(total)}")
    if liens:
        payable = sum(int(l.payable_cents or 0) for l in liens if l.status != "paid")
        parts.append("Liens:\n" + "\n".join(
            f"- {l.holder} ({l.type.replace('_', ' ')}): {cents_to_str(l.original_cents)} {l.status}"
            + (f", reduced to {cents_to_str(l.reduced_cents)}" if l.reduced_cents is not None else "") for l in liens)
            + f"\nLiens payable: {cents_to_str(payable)}")
    confirmed = [e for e in entries if e.confirmed]
    if confirmed:
        parts.append("Confirmed treatment chronology:\n" + _chron_lines(confirmed))
    unconfirmed = len(entries) - len(confirmed)
    if unconfirmed:
        parts.append(f"({unconfirmed} unconfirmed chronology entries were left out.)")
    return "\n\n".join(parts)


def _notes_text(m, limit=10):
    notes = sorted(m.notes, key=lambda n: n.created_at or datetime.min, reverse=True)[:limit]
    if not notes:
        return ""
    return "Notes (newest first):\n" + "\n".join(
        f"- {n.created_at:%Y-%m-%d} {n.user.name if n.user else ''}: {n.body.strip()[:500]}" for n in notes)


def _documents_text(m, budget):
    """Extracted text from the documents in the overview folders, most recent first, up to `budget` characters.
    Returns (text, list of document names used, whether anything was cut)."""
    docs = [d for d in _matter_docs(m.id) if (d.folder or "") in OVERVIEW_FOLDERS and (d.extracted_text or "").strip()]
    parts, used, cut, left = [], [], False, budget
    for d in docs:
        if left < 300:
            cut = True
            break
        head = f"--- {d.name} ({d.folder or 'root'}, {d.created_at:%Y-%m-%d}) ---\n"
        body, was_cut = llm.clip(d.extracted_text.strip(), max(0, left - len(head)))
        cut = cut or was_cut
        parts.append(head + body)
        used.append(d.name)
        left -= len(head) + len(body) + 2
    return "\n\n".join(parts), used, cut


def _overview_text(sections):
    out = []
    for key, label in OVERVIEW_SECTIONS:
        v = sections.get(key)
        if isinstance(v, list):
            v = "\n".join("- " + str(x).strip() for x in v if str(x).strip())
        v = str(v or "").strip()
        if v:
            out.append(f"{label}\n{v}")
    return "\n\n".join(out)


def _structured_overview(m, c, entries, providers, liens):
    """Overview assembled from the structured data alone, for when the model is not available."""
    confirmed = [e for e in entries if e.confirmed]
    facts = [f"Date of loss: {_fmt_date(c.date_of_loss) or 'not entered'}.",
             f"Incident type: {c.incident_type or 'not entered'}."]
    if c.incident_description:
        facts.append(c.incident_description.strip())
    parties = [f"Client: {m.client.display_name if m.client else ''}."]
    parties += [f"{p.name} ({p.role})." for p in m.parties]
    if c.insurer:
        parties.append(f"Insurer: {c.insurer}" + (f", claim {c.claim_number}" if c.claim_number else "") +
                       (f", adjuster {c.adjuster_name}" if c.adjuster_name else "") + ".")
    inj = [c.injuries.strip()] if c.injuries else ["No injuries entered."]
    inj.append(f"Treatment status: {dict(TREATMENT_STATUSES).get(c.treatment_status, c.treatment_status)}.")
    if providers:
        inj.append("Providers: " + ", ".join(p.name for p in providers) + ".")
    if confirmed:
        dates = [e.date for e in confirmed if e.date]
        if dates:
            inj.append(f"{len(confirmed)} confirmed chronology entries from {_fmt_date(min(dates))} to "
                       f"{_fmt_date(max(dates))}.")
    total = sum(int(p.total_billed_cents or 0) for p in providers)
    payable = sum(int(l.payable_cents or 0) for l in liens if l.status != "paid")
    damages = [f"Medical specials: {cents_to_str(total)} across {len(providers)} provider{'' if len(providers) == 1 else 's'}.",
               f"Liens payable: {cents_to_str(payable)} ({len(liens)} lien{'' if len(liens) == 1 else 's'}).",
               f"Policy limits: {cents_to_str(c.policy_limits_cents) if c.policy_limits_cents else 'unknown'}; "
               f"UM/UIM: {cents_to_str(c.um_uim_limits_cents) if c.um_uim_limits_cents else 'unknown'}."]
    if c.demand_amount_cents:
        damages.append(f"Demand: {cents_to_str(c.demand_amount_cents)}" +
                       (f", sent {_fmt_date(c.demand_sent_on)}" if c.demand_sent_on else "") +
                       (f"; offer {cents_to_str(c.offer_cents)}" if c.offer_cents else "") + ".")
    questions = []
    if not c.date_of_loss:
        questions.append("Date of loss is not entered.")
    if not c.incident_description:
        questions.append("No description of what happened.")
    if not providers:
        questions.append("No treating providers on the matter.")
    for p in providers:
        if not p.records_received_on:
            questions.append(f"Records not received from {p.name}.")
        if not p.bills_received_on:
            questions.append(f"Bills not received from {p.name}.")
    if len(entries) - len(confirmed):
        questions.append(f"{len(entries) - len(confirmed)} chronology entries are unconfirmed.")
    if not c.policy_limits_cents:
        questions.append("Policy limits unknown.")
    if c.treatment_status == "treating":
        questions.append("Client is still treating; specials are not final.")
    if m.sol_date:
        questions.append(f"Limitations date {m.sol_date}.")
    return dict(facts="\n".join(facts), parties="\n".join(parties), injuries_and_treatment="\n".join(inj),
                liability=c.liability_notes.strip() if c.liability_notes else "No liability notes entered.",
                damages_summary="\n".join(damages), open_questions=questions)


@bp.route("/<int:matter_id>/overview", methods=["POST"])
@login_required
def overview(matter_id):
    m, c = _load(matter_id)
    entries, providers, liens = _entries(m), _providers(m), _liens(m)
    head = _structured_context(m, c, entries, providers, liens)
    notes = _notes_text(m)
    instr = ("Write a case overview for the attorney handling this personal injury matter, from the material "
             "below only. Sections: facts (what happened, when, where), parties (client, defendants, insurers, "
             "adjusters), injuries_and_treatment (injuries, providers, course of treatment, current status), "
             "liability (who is at fault and the evidence), damages_summary (specials, liens, limits, demand "
             "and offer so far), open_questions (a list of gaps, missing records, contradictions and things to "
             "check). Plain prose, past tense for events. Say 'not in the file' rather than guessing. Return JSON "
             "with exactly those six keys; open_questions is a list of strings.\n\n")
    budget = min(DOC_TEXT_BUDGET, llm.MAX_CONTEXT_CHARS - len(instr) - len(head) - len(notes) - 100)
    doc_text, used, cut = _documents_text(m, budget) if budget > 300 else ("", [], True)
    prompt = instr + head + ("\n\n" + notes if notes else "") + ("\n\nDocuments:\n" + doc_text if doc_text else "")
    source = ""
    try:
        data = llm.complete_json(prompt, OVERVIEW_SCHEMA, system=SYSTEM, max_tokens=2500, kind="case_overview",
                                 entity="matter", entity_id=m.id, user_id=_uid())
        sections = data if isinstance(data, dict) else {}
        if not any(str(sections.get(k) or "").strip() for k, _ in OVERVIEW_SECTIONS):
            raise LLMUnavailable("The AI answered in an unexpected format.")
        source = "Written by the AI from the facts, chronology, providers, liens, notes" + \
                 (f" and {len(used)} document{'' if len(used) == 1 else 's'} ({', '.join(used[:5])})" if used else "") + "."
        if cut:
            source += " Some document text did not fit and was left out."
        flash("Overview generated. " + DRAFT_LINE, "ok")
    except LLMUnavailable as e:
        sections = _structured_overview(m, c, entries, providers, liens)
        source = f"Assembled from the structured data without the model ({e})"
        flash(f"The AI was not available ({e}). The overview was assembled from the case data without the model, "
              "so it lists facts and totals but does not read the documents.", "error")
    c.overview_text = _overview_text(sections) + "\n\n" + source
    c.overview_at = now()
    audit("case_overview", "matter", m.id, source[:200], _uid())
    db.session.commit()
    return redirect(url_for("pi.case", matter_id=m.id) + "#overview")


@bp.route("/<int:matter_id>/overview/note", methods=["POST"])
@login_required
def overview_note(matter_id):
    m, c = _load(matter_id)
    if not (c.overview_text or "").strip():
        flash("Generate the overview first.", "error")
        return redirect(url_for("pi.case", matter_id=m.id) + "#overview")
    n = Note(matter_id=m.id, user_id=_uid(),
             body=f"Case overview ({(c.overview_at or now()):%b %-d, %Y}). {DRAFT_LINE}\n\n{c.overview_text.strip()}")
    db.session.add(n)
    db.session.flush()
    audit("create", "note", n.id, f"case overview saved on {m.number}", _uid())
    db.session.commit()
    flash("Overview saved as a note on the matter.", "ok")
    return redirect(url_for("pi.case", matter_id=m.id) + "#overview")


# ---------------------------------------------------------------------------
# 3. narrative demand drafting
# ---------------------------------------------------------------------------
def _style_examples(budget):
    """Up to three documents tagged style-example anywhere in the firm, newest first, text clipped to fit."""
    docs = Document.query.filter(Document.is_current == True, Document.tags.ilike(f"%{STYLE_TAG}%")).order_by(  # noqa: E712
        Document.created_at.desc()).limit(20).all()
    docs = [d for d in docs if STYLE_TAG in parse_tags(d.tags) and (d.extracted_text or "").strip()][:MAX_STYLE_EXAMPLES]
    if not docs or budget < 400:
        return "", []
    each = budget // len(docs)
    parts = []
    for d in docs:
        body, _ = llm.clip(d.extracted_text.strip(), each - 60)
        parts.append(f"--- Example: {d.name} ---\n{body}")
    return "\n\n".join(parts), [d.name for d in docs]


def _demand_inputs(m, c):
    entries, providers, liens = _entries(m), _providers(m), _liens(m)
    return entries, providers, liens, sum(int(p.total_billed_cents or 0) for p in providers)


def _demand_prompt(m, c, head, demand_cents, style_notes, examples):
    instr = ("Draft a narrative settlement demand letter to the insurer for this personal injury claim, from the "
             "material below only. Write in the first person plural for the firm ('our client', 'we'). Sections: "
             "intro (who we represent, the claim, purpose of the letter), facts (what happened), liability (why "
             "the insured is at fault), injuries_and_treatment (the injuries and the course of care, in order, "
             "with provider names), damages (specials by provider with the total, liens, the effect on the "
             "client's life as far as the file supports it), demand_and_deadline (the demand of "
             f"{cents_to_str(demand_cents)}"
             + (f" against policy limits of {cents_to_str(c.policy_limits_cents)}" if c.policy_limits_cents else "")
             + ", a 30 day response deadline, and that this is a settlement communication), closing (a courteous "
             "close, no signature block). Plain, confident, specific. No headings inside the text, no bullet "
             "lists, no invented facts or figures. Return JSON with exactly those seven keys.\n\n")
    if style_notes:
        instr += "Tone notes from the attorney for this letter: " + style_notes.strip() + "\n\n"
    if examples:
        instr += ("Match the voice, sentence rhythm and level of formality of these earlier letters from the firm. "
                  "Do not copy their facts.\n\n" + examples + "\n\n")
    return instr + "Case material:\n" + head


def _template_demand(m, c, entries, providers, liens, total, demand_cents):
    client = m.client.display_name if m.client else "our client"
    confirmed = [e for e in entries if e.confirmed]
    intro = (f"[Template text. The AI was not available, so this letter was assembled from the case data. Edit "
             f"every section before sending.]\n\nThis office represents {client} for injuries sustained on "
             f"{_fmt_date(c.date_of_loss) or 'the date of loss'}"
             + (f" in a claim against your insured, claim number {c.claim_number}" if c.claim_number else "") +
             ". This letter sets out the facts, liability, the injuries and treatment, the damages, and our "
             "client's demand for settlement.")
    facts = c.incident_description.strip() if c.incident_description else "(Enter what happened on the PI case page.)"
    liability = c.liability_notes.strip() if c.liability_notes else \
        "(Enter the liability notes on the PI case page: fault, citations, witnesses, photographs.)"
    inj = [c.injuries.strip() if c.injuries else "(Enter the injuries on the PI case page.)"]
    if confirmed:
        inj.append("The course of treatment, from the records, was as follows:")
        for e in confirmed[:30]:
            bits = [_fmt_date(e.date) or "Undated", e.provider_name or "", e.visit_type or ""]
            if e.diagnosis:
                bits.append(e.diagnosis.strip())
            if e.procedure:
                bits.append(e.procedure.strip())
            inj.append(", ".join(b for b in bits if b) + ".")
    elif providers:
        inj.append("Treatment was provided by " + ", ".join(p.name for p in providers) + ".")
    status = dict(TREATMENT_STATUSES).get(c.treatment_status, c.treatment_status or "")
    if status:
        inj.append(f"Treatment status: {status}.")
    dmg = [f"Medical specials to date total {cents_to_str(total)}:"]
    dmg += [f"{p.name}: {cents_to_str(p.total_billed_cents or 0)}." for p in providers]
    if liens:
        payable = sum(int(l.payable_cents or 0) for l in liens if l.status != "paid")
        dmg.append(f"Liens asserted against the recovery total {cents_to_str(payable)}.")
    demand = (f"In light of the liability facts, the injuries described above and medical specials of "
              f"{cents_to_str(total)}, our client demands {cents_to_str(demand_cents)} in full settlement of all "
              f"claims arising from this loss."
              + (f" We understand the applicable policy limits to be {cents_to_str(c.policy_limits_cents)}."
                 if c.policy_limits_cents else "") +
              " Please respond within 30 days of the date of this letter. This letter is a settlement "
              "communication and is not admissible for any other purpose. It does not waive any claim, right or remedy.")
    closing = "We look forward to your prompt response. Please direct all communication about this claim to this office."
    return dict(intro=intro, facts=facts, liability=liability, injuries_and_treatment="\n".join(inj),
                damages="\n".join(dmg), demand_and_deadline=demand, closing=closing)


@bp.route("/<int:matter_id>/demand-draft", methods=["GET", "POST"])
@login_required
def demand_draft(matter_id):
    m, c = _load(matter_id)
    entries, providers, liens, total = _demand_inputs(m, c)
    if request.method == "GET":
        return render_template("records/demand_draft.html", m=m, c=c, draft=_load_draft(m.id), sections=DEMAND_SECTIONS,
                               providers=providers, total=total, liens=liens,
                               unconfirmed=sum(1 for e in entries if not e.confirmed), style_tag=STYLE_TAG,
                               example_names=_style_examples(6000)[1])
    style_notes = _s(request.form.get("demand_style_notes"))
    c.demand_style_notes = style_notes
    demand_cents = parse_money(request.form.get("demand_amount")) or int(c.demand_amount_cents or 0)
    if demand_cents <= 0:
        db.session.commit()
        flash("Enter the demand amount first.", "error")
        return redirect(url_for("records.demand_draft", matter_id=m.id))
    c.demand_amount_cents = demand_cents
    head = _structured_context(m, c, entries, providers, liens)
    budget = llm.MAX_CONTEXT_CHARS - len(head) - 1400 - len(style_notes)
    examples, example_names = _style_examples(budget)
    draft = dict(generated_at=now().isoformat(timespec="seconds"), demand_cents=demand_cents, template=False,
                 examples=example_names)
    try:
        data = llm.complete_json(_demand_prompt(m, c, head, demand_cents, style_notes, examples), DEMAND_SCHEMA,
                                 system=SYSTEM, max_tokens=4000, kind="demand_draft", entity="matter",
                                 entity_id=m.id, user_id=_uid())
        sections = {k: _s((data or {}).get(k)) for k, _ in DEMAND_SECTIONS} if isinstance(data, dict) else {}
        if not any(sections.values()):
            raise LLMUnavailable("The AI answered in an unexpected format.")
        draft["sections"] = sections
        flash("Demand letter drafted. " + DRAFT_LINE, "ok")
    except LLMUnavailable as e:
        draft["sections"] = _template_demand(m, c, entries, providers, liens, total, demand_cents)
        draft["template"] = True
        draft["reason"] = str(e)
        flash(f"The AI was not available ({e}). A template letter was filled in from the case data instead; "
              "it is marked as template text.", "error")
    _save_draft(m.id, draft)
    audit("demand_draft", "matter", m.id, f"{cents_to_str(demand_cents)}{' template' if draft['template'] else ''}",
          _uid())
    db.session.commit()
    return redirect(url_for("records.demand_draft", matter_id=m.id))


def build_narrative_demand(m, c, providers, sections, demand_cents):
    f = Firm.get()
    pdf = PiPDF(f, title="Narrative demand")
    pdf.add_page()
    to_lines = [c.insurer or "Claims department"]
    if c.adjuster_name:
        to_lines.append(f"Attn: {c.adjuster_name}")
    for x in (c.adjuster_email, c.adjuster_phone):
        if x:
            to_lines.append(x)
    re_lines = [f"Claimant: {m.client.display_name if m.client else ''}"]
    if c.claim_number:
        re_lines.append(f"Claim number: {c.claim_number}")
    if c.date_of_loss:
        re_lines.append(f"Date of loss: {_fmt_date(c.date_of_loss)}")
    re_lines.append(f"Demand: {cents_to_str(demand_cents)}")
    re_lines.append("FOR SETTLEMENT PURPOSES ONLY")
    _letter_head(pdf, to_lines, re_lines)
    _line(pdf, "Dear " + (c.adjuster_name or "Claims representative") + ":")
    pdf.ln(2)
    total = sum(int(p.total_billed_cents or 0) for p in providers)
    for key, label in DEMAND_SECTIONS:
        text = (sections.get(key) or "").strip()
        if key == "damages":
            if text:
                _line(pdf, label, size=12, style="B")
                pdf.ln(1)
                for para in [p for p in text.split("\n") if p.strip()]:
                    _para(pdf, para)
            _line(pdf, "Medical specials", size=12, style="B")
            pdf.ln(2)
            srows = [(p.name, pdf_money(p.total_billed_cents or 0)) for p in providers]
            srows.append(("Total medical specials", pdf_money(total)))
            _table(pdf, ("Provider", "Billed"), srows, (130, 44), ("LEFT", "RIGHT"))
            continue
        if not text:
            continue
        if key not in ("intro", "closing"):
            _line(pdf, label, size=12, style="B")
            pdf.ln(1)
        for para in [p for p in text.split("\n") if p.strip()]:
            _para(pdf, para)
    _signature(pdf, m)
    return pdf


@bp.route("/<int:matter_id>/demand-draft/save", methods=["POST"])
@login_required
def demand_save(matter_id):
    m, c = _load(matter_id)
    sections = {k: _s(request.form.get(k)) for k, _ in DEMAND_SECTIONS}
    if not any(sections.values()):
        flash("There is nothing to save. Generate or type the letter first.", "error")
        return redirect(url_for("records.demand_draft", matter_id=m.id))
    draft = _load_draft(m.id) or dict(generated_at="", template=False)
    draft["sections"] = sections
    draft["saved_at"] = now().isoformat(timespec="seconds")
    demand_cents = parse_money(request.form.get("demand_amount")) or int(draft.get("demand_cents") or 0) \
        or int(c.demand_amount_cents or 0)
    draft["demand_cents"] = demand_cents
    _save_draft(m.id, draft)
    providers = _providers(m)
    pdf = build_narrative_demand(m, c, providers, sections, demand_cents)
    name = f"Narrative demand - {m.number}.pdf"
    doc = save_pdf_document(m, pdf, name, "Demand", current_user())
    audit("demand_draft_saved", "matter", m.id, f"{name} {cents_to_str(demand_cents)}", _uid())
    db.session.commit()
    flash(f"Saved {name} to Documents (Demand). " + DRAFT_LINE, "ok")
    return redirect(url_for("pi.case", matter_id=m.id) + "#demand")
