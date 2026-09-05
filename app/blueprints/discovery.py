"""Discovery drafting and deposition summaries (Eve Legal lane).

Discovery: propound a set (interrogatories, requests for production, requests for admission) from a built-in
starter set for the matter's practice area, optionally tailored by the model from the matter facts, PI facts and
the confirmed chronology; or respond to a served set by parsing the served document's extracted text into numbered
requests and drafting a response, objections from a fixed library, and a flag for each one. Every item is
editable, the set can be exported as a PDF filed under Documents in the "Discovery" folder, and one click creates
the 30-day response deadline task.

Depositions: pick a transcript document, and the model reads it in chunks of about 10,000 characters (page and
line markers kept) to produce a summary, key testimony with page:line cites, and contradictions against the
confirmed chronology and PI facts. Without the model the record still exists so notes can be added.

Every model call goes through app.llm (module reference so tests can monkeypatch app.llm.complete). Callers catch
LLMUnavailable and fall back. All AI output is a draft for attorney review and the pages say so.
"""
import json
import re
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from ..extensions import db
from ..models import (Matter, Document, Task, Note, PiCase, ChronologyEntry, DiscoverySet, DepositionSummary, Firm,
                      audit)
from ..helpers import login_required, current_user, parse_date
from ..services.pdf import DocPDF
from .. import llm
from ..llm import LLMUnavailable
from .documents import store_bytes

bp = Blueprint("discovery", __name__, url_prefix="/discovery")

SYSTEM = ("You are drafting litigation discovery for a small law firm. Be precise and plain. Never invent facts, "
          "names, dates or amounts that are not in the material you are given. Every answer you write is a draft "
          "an attorney will review.")
DRAFT_NOTE = "Draft prepared for attorney review. Not legal advice."

DIRECTIONS = [("propound", "Propound (we serve it)"), ("respond", "Respond (served on us)")]
KINDS = [("interrogatories", "Interrogatories"), ("rfp", "Requests for production"),
         ("rfa", "Requests for admission")]
KIND_LABELS = dict(KINDS)
ITEM_LABELS = {"interrogatories": "INTERROGATORY", "rfp": "REQUEST FOR PRODUCTION", "rfa": "REQUEST FOR ADMISSION"}
STATUSES = ["draft", "review", "final"]
RESPONSE_DAYS = 30
FALLBACK_RESPONSE = "Objection: [none]. Response: [ATTORNEY TO COMPLETE]"

# The fixed objection library. Keys are what the model may pick and what the JSON stores.
OBJECTIONS = [
    ("overbroad", "Overbroad"),
    ("unduly_burdensome", "Unduly burdensome"),
    ("vague", "Vague and ambiguous"),
    ("privileged", "Privileged (attorney-client, work product)"),
    ("not_proportional", "Not proportional to the needs of the case"),
    ("legal_conclusion", "Calls for a legal conclusion"),
    ("equally_available", "Equally available to the requesting party"),
]
OBJECTION_KEYS = [k for k, _ in OBJECTIONS]
OBJECTION_LABELS = dict(OBJECTIONS)

# ---------------------------------------------------------------------------
# starter sets: area -> kind -> requests. Plain, generic, no jurisdiction-specific rules.
# ---------------------------------------------------------------------------
STARTER_SETS = {
    "personal_injury": {
        "interrogatories": [
            "State your full name, every other name you have used, your date of birth, and your current address.",
            "Identify every person who witnessed the incident described in the pleadings or who has knowledge of how it occurred, with each person's address and telephone number.",
            "Describe in detail how you contend the incident occurred, including the sequence of events, the speed and position of every vehicle or person involved, and every act or omission you contend caused it.",
            "State whether you contend that any person or entity other than the parties caused or contributed to the incident, and if so, identify each and describe the basis for your contention.",
            "Identify every statement, written or recorded, taken from any party or witness concerning the incident, including who took it, when, and who now has it.",
            "Identify every photograph, video, diagram, or other depiction of the scene, the vehicles, the property, or the injuries, and state who has custody of each.",
            "State whether you were cited, charged, or ticketed in connection with the incident, and if so, describe the charge and its disposition.",
            "Identify every insurance policy that may provide coverage for the claims in this lawsuit, including the insurer, policy number, and limits of coverage.",
            "State whether you consumed any alcohol, drug, or medication in the 24 hours before the incident, and if so, describe what, how much, and when.",
            "Identify every mobile phone or other electronic device in your possession at the time of the incident and state whether you were using it in the ten minutes before the incident.",
            "Describe every injury you claim resulted from the incident, and for each state the part of the body affected and whether you claim it is permanent.",
            "Identify every physician, hospital, clinic, therapist, or other health care provider who has examined or treated you for the injuries you claim, with the dates of treatment and the charges billed.",
            "Identify every health care provider who examined or treated you in the ten years before the incident for any condition involving the same parts of the body you claim were injured.",
            "State whether you have ever been involved in any other accident, incident, or lawsuit in which you claimed a personal injury, and describe each.",
            "Itemize every element of damages you claim, including medical expenses, lost earnings, property damage, and any other loss, with the amount of each and how it was calculated.",
            "State your employer, job title, and rate of pay at the time of the incident, and describe every period of work you missed as a result of the incident.",
            "Describe every activity you contend you can no longer perform, or can perform only with difficulty, because of the injuries claimed.",
            "State whether any medical expense you claim has been paid or written off by any insurer, government program, or lienholder, and identify each payer and the amount.",
            "Identify every person you expect to call as an expert witness at trial, the subject matter on which each is expected to testify, and the substance of each opinion.",
            "Identify every document, report, or record you relied on in answering these interrogatories.",
        ],
        "rfp": [
            "All photographs, video recordings, and diagrams of the scene of the incident, the vehicles or property involved, and the injuries claimed.",
            "All written or recorded statements of any party or witness concerning the incident.",
            "All police reports, incident reports, and accident reports concerning the incident.",
            "All insurance policies, declarations pages, and reservation of rights letters that may provide coverage for the claims in this lawsuit.",
            "All medical records, charts, and reports from every health care provider who has examined or treated you for the injuries you claim.",
            "All medical bills, invoices, statements, and explanations of benefits for treatment of the injuries you claim.",
            "All medical records for the ten years before the incident from any provider who treated you for any condition involving the same parts of the body you claim were injured.",
            "All records of prescription medication filled in the two years before the incident and at any time since.",
            "All documents supporting your claim for lost earnings, including pay stubs, W-2 forms, tax returns for the three years before the incident and every year since, and employer records of missed work.",
            "All estimates, invoices, and receipts for repair or replacement of any property damaged in the incident.",
            "All mobile phone records, including call and text logs, for the period beginning one hour before and ending one hour after the incident.",
            "All correspondence between you and any insurer concerning the incident or the injuries claimed.",
            "All documents relating to any lien, subrogation claim, or right of reimbursement asserted against any recovery in this lawsuit.",
            "All diaries, journals, calendars, and social media posts that describe the incident, your injuries, your treatment, or your activities since the incident.",
            "All documents you provided to, or received from, any expert witness you expect to call at trial.",
            "All documents relating to any prior accident, injury, or claim involving the parts of the body you claim were injured.",
            "All documents you identified or relied on in answering the interrogatories served with these requests.",
        ],
        "rfa": [
            "Admit that the incident described in the pleadings occurred on the date and at the location alleged.",
            "Admit that you were operating the vehicle described in the pleadings at the time of the incident.",
            "Admit that at the time of the incident you were acting within the course and scope of your employment.",
            "Admit that you failed to keep a proper lookout immediately before the incident.",
            "Admit that you were traveling above the posted speed limit at the time of the incident.",
            "Admit that you failed to yield the right of way immediately before the incident.",
            "Admit that you were using a mobile phone in the five minutes before the incident.",
            "Admit that you were cited by law enforcement in connection with the incident.",
            "Admit that no act or omission of the plaintiff caused or contributed to the incident.",
            "Admit that the plaintiff sought medical treatment within 72 hours after the incident.",
            "Admit that the medical treatment the plaintiff received was reasonable and necessary as a result of the incident.",
            "Admit that the charges for the plaintiff's medical treatment were reasonable and customary.",
            "Admit that the plaintiff missed work as a result of the injuries sustained in the incident.",
            "Admit that the vehicle or property described in the pleadings was damaged in the incident.",
            "Admit that you were insured under a liability policy in force on the date of the incident.",
            "Admit that the document attached as Exhibit A is a true and correct copy of the police report of the incident.",
        ],
    },
    "contract": {
        "interrogatories": [
            "Identify every person who participated in negotiating, drafting, or signing the contract at issue, and describe each person's role.",
            "Identify every document that you contend forms part of the agreement between the parties, including every amendment, addendum, purchase order, and course-of-dealing document.",
            "State every term of the contract you contend the opposing party breached, and describe each act or omission that constitutes the breach and when it occurred.",
            "State whether you contend that any condition precedent to the opposing party's performance did not occur, and if so, identify each condition and the facts supporting your contention.",
            "Describe every communication between the parties concerning performance, non-performance, or termination of the contract, with the date, participants, and substance of each.",
            "State whether you gave, or received, any written notice of breach, default, or termination, and identify each notice and the date it was sent or received.",
            "Identify every payment made under the contract, including the date, amount, payer, and payee.",
            "Itemize every element of damages you claim, including the amount of each and the method used to calculate it.",
            "Describe every step you took to mitigate the damages you claim.",
            "State whether you contend the contract is ambiguous in any respect, and if so, identify each ambiguous term and state your interpretation of it.",
            "Identify every affirmative defense you assert and state every fact supporting each.",
            "State whether you performed all of your obligations under the contract, and if not, identify each obligation you did not perform and the reason.",
            "Identify every person with knowledge of the negotiation, performance, or breach of the contract, and summarize the knowledge of each.",
            "Identify every other contract, proposal, or bid you entered into or considered for the same goods or services during the term of the contract.",
            "Identify every person you expect to call as an expert witness at trial, the subject matter on which each is expected to testify, and the substance of each opinion.",
            "Identify every document you relied on in answering these interrogatories.",
        ],
        "rfp": [
            "All drafts, versions, and executed copies of the contract at issue, with every amendment, addendum, exhibit, and schedule.",
            "All correspondence, emails, text messages, and notes exchanged between the parties concerning the negotiation of the contract.",
            "All correspondence, emails, text messages, and notes exchanged between the parties concerning performance, non-performance, or termination of the contract.",
            "All internal communications, memoranda, and notes concerning the contract, its performance, or the dispute.",
            "All invoices, statements, purchase orders, delivery records, and receipts issued under the contract.",
            "All records of payment made or received under the contract, including cancelled checks, wire confirmations, and ledger entries.",
            "All notices of breach, default, cure, or termination sent or received concerning the contract.",
            "All documents supporting each element of damages you claim, including calculations, financial statements, and projections.",
            "All documents relating to your efforts to mitigate damages, including communications with substitute vendors, buyers, or contractors.",
            "All documents relating to any other contract, bid, or proposal for the same goods or services during the term of the contract at issue.",
            "All documents reflecting the parties' course of dealing before the contract at issue, including prior contracts and invoices.",
            "All documents you provided to, or received from, any expert witness you expect to call at trial.",
            "All documents identified or relied on in answering the interrogatories served with these requests.",
            "All insurance policies that may provide coverage for the claims asserted in this lawsuit.",
            "All organizational documents, board minutes, and resolutions authorizing entry into or termination of the contract.",
        ],
        "rfa": [
            "Admit that you signed the contract at issue on the date it bears.",
            "Admit that the document attached as Exhibit A is a true and correct copy of the contract at issue.",
            "Admit that the contract was supported by consideration.",
            "Admit that you received the goods or services described in the contract.",
            "Admit that you did not pay the full amount due under the contract.",
            "Admit that you received written notice of breach before this lawsuit was filed.",
            "Admit that you did not cure the breach within the time provided in the contract.",
            "Admit that the plaintiff performed all of its obligations under the contract.",
            "Admit that you did not give written notice of any defect in the goods or services within the time required by the contract.",
            "Admit that the amount stated in the invoice attached as Exhibit B is accurate.",
            "Admit that you have no evidence that the plaintiff breached the contract.",
            "Admit that the contract contains a provision for recovery of attorney's fees by the prevailing party.",
            "Admit that you did not terminate the contract in the manner it requires.",
            "Admit that the contract was not modified in writing after it was signed.",
            "Admit that you were acting on behalf of the company named in the contract when you signed it.",
        ],
    },
    "general": {
        "interrogatories": [
            "State your full name, every other name you have used, your date of birth, and your current address.",
            "Identify every person who has knowledge of the facts alleged in the pleadings, and summarize the knowledge of each.",
            "Describe in detail every fact supporting each claim or defense you assert in this lawsuit.",
            "Identify every document that supports, refutes, or relates to any claim or defense you assert.",
            "Identify every written or recorded statement taken from any party or witness concerning the facts of this lawsuit.",
            "Describe every communication between you and the opposing party concerning the subject matter of this lawsuit, with the date, participants, and substance of each.",
            "Itemize every element of damages you claim, with the amount of each and how it was calculated.",
            "Describe every step you took to mitigate the damages you claim.",
            "Identify every affirmative defense you assert and state every fact supporting each.",
            "Identify every insurance policy that may provide coverage for the claims in this lawsuit.",
            "State whether you have ever been a party to any other lawsuit, arbitration, or administrative proceeding, and describe each.",
            "State whether you have ever been convicted of a felony or a crime involving dishonesty, and if so, describe the offense and the disposition.",
            "Identify every person you expect to call as an expert witness at trial, the subject matter on which each is expected to testify, and the substance of each opinion.",
            "Identify every person you expect to call as a fact witness at trial and summarize the expected testimony of each.",
            "Identify every document you relied on in answering these interrogatories.",
        ],
        "rfp": [
            "All documents that support, refute, or relate to any claim or defense asserted in this lawsuit.",
            "All correspondence, emails, text messages, and notes exchanged between the parties concerning the subject matter of this lawsuit.",
            "All written or recorded statements of any party or witness concerning the facts of this lawsuit.",
            "All photographs, video recordings, and other depictions relating to the subject matter of this lawsuit.",
            "All documents supporting each element of damages you claim, including calculations and financial records.",
            "All documents relating to your efforts to mitigate damages.",
            "All insurance policies that may provide coverage for the claims asserted in this lawsuit.",
            "All documents you provided to, or received from, any expert witness you expect to call at trial.",
            "All documents identified in your initial disclosures or in your answers to interrogatories.",
            "All diaries, journals, calendars, and social media posts that describe the events at issue.",
            "All contracts, agreements, and other writings between the parties.",
            "All documents relating to any prior lawsuit, claim, or complaint involving the same subject matter.",
            "All documents you intend to introduce as exhibits at trial.",
            "All documents relied on in answering the interrogatories served with these requests.",
            "All organizational documents identifying the owners, officers, and managers of any entity party to this lawsuit.",
        ],
        "rfa": [
            "Admit that the court has jurisdiction over you in this lawsuit.",
            "Admit that venue is proper in this court.",
            "Admit that you were properly served with the pleadings in this lawsuit.",
            "Admit that the document attached as Exhibit A is a true and correct copy of what it purports to be.",
            "Admit that the document attached as Exhibit A is a business record kept in the ordinary course of business.",
            "Admit that you received the correspondence attached as Exhibit B on or about the date it bears.",
            "Admit that you did not respond in writing to the correspondence attached as Exhibit B.",
            "Admit that you have no documents that support your denial of the allegations in the pleadings.",
            "Admit that you have no witnesses, other than yourself, who support your denial of the allegations in the pleadings.",
            "Admit that the plaintiff has been damaged by the conduct alleged in the pleadings.",
            "Admit that you were acting on behalf of the entity named in the pleadings at all relevant times.",
            "Admit that you have not identified any expert witness who will testify on your behalf at trial.",
            "Admit that the amount of damages stated in the pleadings is accurate.",
            "Admit that you did not attempt to resolve this dispute before the lawsuit was filed.",
            "Admit that you are not entitled to recover attorney's fees in this lawsuit.",
        ],
    },
}
AREA_LABELS = {"personal_injury": "personal injury", "contract": "contract", "general": "general civil"}


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


def _matter_or_404(mid):
    m = db.session.get(Matter, mid) if mid else None
    return m or abort(404)


def area_for(matter):
    """Which starter set fits the matter: PI when it has a PI case or a PI-sounding practice area, contract when
    the practice area says contract, business or commercial, else general civil."""
    pa = (matter.practice_area or "").lower()
    if PiCase.query.filter_by(matter_id=matter.id).first():
        return "personal_injury"
    if "injur" in pa or "accident" in pa or pa.strip() in ("pi", "p.i."):
        return "personal_injury"
    if "contract" in pa or "commercial" in pa or "business" in pa:
        return "contract"
    return "general"


def starter_items(area, kind):
    reqs = STARTER_SETS.get(area, STARTER_SETS["general"]).get(kind) or STARTER_SETS["general"][kind]
    return [_item(i + 1, r) for i, r in enumerate(reqs)]


def _item(n, request_text="", response="", objections=None, flag=""):
    return {"n": int(n), "request": str(request_text or "").strip(), "response": str(response or "").strip(),
            "objections": [o for o in (objections or []) if o in OBJECTION_KEYS], "flag": str(flag or "").strip()}


def _renumber(items):
    return [_item(i + 1, it.get("request"), it.get("response"), it.get("objections"), it.get("flag"))
            for i, it in enumerate(items)]


def _set_items(ds, items):
    ds.items_json = json.dumps(_renumber(items), ensure_ascii=False)


def _default_title(direction, kind, party):
    base = KIND_LABELS.get(kind, kind)
    if direction == "respond":
        return f"Responses to {base.lower()} from {party}" if party else f"Responses to {base.lower()}"
    return f"{base} to {party}" if party else base


def task_title(ds):
    return f"Discovery responses due: {ds.title}"


# ---------------------------------------------------------------------------
# matter facts for the prompts
# ---------------------------------------------------------------------------
def matter_facts(m, limit=4000):
    """Plain-text facts about the matter: description, parties, PI facts and the confirmed chronology."""
    lines = [f"Matter: {m.label}", f"Practice area: {m.practice_area or 'not set'}",
             f"Client: {m.client.display_name if m.client else ''}"]
    if m.court or m.case_number:
        lines.append(f"Court: {m.court or ''} {('No. ' + m.case_number) if m.case_number else ''}".strip())
    if m.description:
        lines.append(f"Description: {m.description.strip()}")
    for p in m.parties:
        lines.append(f"Party: {p.name} ({(p.role or '').replace('_', ' ')})")
    pi = PiCase.query.filter_by(matter_id=m.id).first()
    if pi:
        lines.append("PI facts:")
        if pi.date_of_loss:
            lines.append(f"  Date of loss: {pi.date_of_loss.isoformat()}")
        if pi.incident_type:
            lines.append(f"  Incident type: {pi.incident_type}")
        if pi.incident_description:
            lines.append(f"  What happened: {pi.incident_description.strip()}")
        if pi.injuries:
            lines.append(f"  Injuries: {pi.injuries.strip()}")
        if pi.treatment_status:
            lines.append(f"  Treatment status: {pi.treatment_status}")
        if pi.insurer:
            lines.append(f"  Insurer: {pi.insurer} claim {pi.claim_number or ''}".strip())
        if pi.liability_notes:
            lines.append(f"  Liability notes: {pi.liability_notes.strip()}")
    chrono = ChronologyEntry.query.filter_by(matter_id=m.id, confirmed=True).order_by(
        ChronologyEntry.date.asc().nulls_last(), ChronologyEntry.id).limit(60).all()
    if chrono:
        lines.append("Confirmed chronology:")
        for e in chrono:
            bits = [e.date.isoformat() if e.date else "undated", e.provider_name or "", e.visit_type or "",
                    e.diagnosis or "", e.procedure or "", e.notes or ""]
            lines.append("  " + " | ".join(b.strip() for b in bits if b and b.strip()))
    text = "\n".join(lines)
    return text[:limit]


# ---------------------------------------------------------------------------
# parsing a served set
# ---------------------------------------------------------------------------
_LABEL_RE = re.compile(
    r"(?:INTERROGATOR(?:Y|IES)|REQUESTS?\s+FOR\s+(?:PRODUCTION|ADMISSIONS?|INSPECTION)|REQUESTS?|RFP|RFA|ROG)"
    r"\s*(?:NO\.?|NUMBER|#)\s*(\d{1,3})\s*[:.\-]?", re.I)
_PARA_RE = re.compile(r"(?<![\w.$])(\d{1,3})\s*[.)]\s+(?=[A-Z\"'(])")
_TAIL_RE = re.compile(r"\b(?:ANSWER|RESPONSE|OBJECTION)S?\s*[:.]", re.I)
MAX_REQUEST_CHARS = 1500


def _sequential(matches):
    """Keep the matches whose number continues the sequence 1, 2, 3, ... (skips stray numbers)."""
    out, expect = [], 1
    for mt in matches:
        if int(mt.group(1)) == expect:
            out.append(mt)
            expect += 1
    return out


def parse_requests(text):
    """Split a served set's text into numbered requests. Labelled headings ("INTERROGATORY NO. 3") win; otherwise
    numbered paragraphs ("3. State ..."). Returns a list of request strings in order."""
    text = " ".join((text or "").split())
    if not text:
        return []
    for rx in (_LABEL_RE, _PARA_RE):
        marks = _sequential(list(rx.finditer(text)))
        if len(marks) >= 2 or (len(marks) == 1 and rx is _LABEL_RE):
            out = []
            for i, mt in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
                body = text[mt.end():end].strip()
                tail = _TAIL_RE.search(body)
                if tail and tail.start() > 0:
                    body = body[:tail.start()].strip()
                if i + 1 == len(marks):
                    body = _trim_tail(body)
                if body:
                    out.append(body[:MAX_REQUEST_CHARS])
            if out:
                return out
    return []


def _trim_tail(body):
    """Cut signature blocks and certificates of service off the last request."""
    m = re.search(r"\b(?:Respectfully submitted|CERTIFICATE OF SERVICE|Dated:|/s/)", body, re.I)
    return body[:m.start()].strip() if m and m.start() > 0 else body


# ---------------------------------------------------------------------------
# model calls
# ---------------------------------------------------------------------------
TAILOR_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {"request": {"type": "string"}},
        "required": ["request"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "response": {"type": "string"},
                       "objections": {"type": "array", "items": {"type": "string", "enum": OBJECTION_KEYS}},
                       "flag": {"type": "string"}},
        "required": ["n", "response", "objections", "flag"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def tailor_with_ai(ds):
    """Rewrite and extend a propounded set from the matter facts. Returns the new item list. Raises LLMUnavailable."""
    m = ds.matter
    kind_word = KIND_LABELS[ds.kind].lower()
    current = "\n".join(f"{it['n']}. {it['request']}" for it in ds.items)
    facts, _ = llm.clip(matter_facts(m), 3500)
    prompt = (f"Below are generic {kind_word} to be served on {ds.party or 'the opposing party'} in this matter, "
              f"followed by the matter facts. Rewrite each request so it fits these facts (name the people, dates, "
              f"places, vehicles, documents and injuries that appear in the facts), drop any request that cannot "
              f"apply, and add requests the facts call for. Keep each request a single, clear sentence or two. "
              f"Keep the total between 15 and 30. Return JSON {{\"items\": [{{\"request\": \"...\"}}]}} in the "
              f"order to serve.\n\nMatter facts:\n{facts}\n\nCurrent requests:\n{current}")
    data = llm.complete_json(prompt, TAILOR_SCHEMA, system=SYSTEM, max_tokens=3000, kind="discovery_tailor",
                             entity="discovery_set", entity_id=ds.id, user_id=_uid())
    rows = (data.get("items") if isinstance(data, dict) else data) or []
    items = []
    for r in rows:
        text = (r.get("request") if isinstance(r, dict) else r) if r else ""
        text = str(text or "").strip()
        if text:
            items.append(_item(len(items) + 1, text))
    if not items:
        raise llm.LLMBadOutput("The AI returned no requests. The set was left as it was.")
    return items


def draft_responses_with_ai(ds):
    """Fill response, objections and flag for every item, in batches. Returns the updated items. Raises
    LLMUnavailable (a batch that fails raises; earlier batches are still applied by the caller)."""
    m = ds.matter
    items = ds.items
    facts, _ = llm.clip(matter_facts(m), 3000)
    kind_word = KIND_LABELS[ds.kind].lower()
    lib = "\n".join(f"  {k}: {v}" for k, v in OBJECTIONS)
    by_n = {it["n"]: it for it in items}
    party = ds.party or "the opposing party"
    batch = 8
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        listing = "\n".join(f"{it['n']}. {it['request']}" for it in chunk)
        prompt = (f"Draft responses on behalf of our client to the {kind_word} below, served by {party}. "
                  f"For each item return a response written in the client's voice, a list of "
                  f"objection keys from the library (empty list when none applies; do not object reflexively), and "
                  f"a flag: a short note when the answer needs a fact only the client has (for example a date, a "
                  f"name, or what happened) or an empty string. Use only the matter facts given. When you do not "
                  f"know a fact, write the response around a bracketed placeholder like [CLIENT TO CONFIRM] and "
                  f"set the flag. For requests for admission answer Admitted, Denied, or a qualified answer with "
                  f"the reason. Return JSON {{\"items\": [{{\"n\": <same number>, \"response\": \"...\", "
                  f"\"objections\": [keys], \"flag\": \"...\"}}]}}.\n\nObjection library:\n{lib}\n\n"
                  f"Matter facts:\n{facts}\n\nRequests:\n{listing}")
        data = llm.complete_json(prompt, DRAFT_SCHEMA, system=SYSTEM, max_tokens=3500, kind="discovery_draft",
                                 entity="discovery_set", entity_id=ds.id, user_id=_uid())
        rows = (data.get("items") if isinstance(data, dict) else data) or []
        for r in rows:
            if not isinstance(r, dict):
                continue
            it = by_n.get(_int(r.get("n")))
            if not it:
                continue
            it["response"] = str(r.get("response") or "").strip()
            objs = r.get("objections") or []
            it["objections"] = [o for o in objs if isinstance(o, str) and o in OBJECTION_KEYS]
            it["flag"] = str(r.get("flag") or "").strip()
    return items


def fill_placeholders(items):
    n = 0
    for it in items:
        if not (it.get("response") or "").strip():
            it["response"] = FALLBACK_RESPONSE
            n += 1
    return n


# ---------------------------------------------------------------------------
# discovery pages
# ---------------------------------------------------------------------------
@bp.route("")
@login_required
def index():
    matter_id = _int(request.args.get("matter_id"))
    q = DiscoverySet.query
    if matter_id:
        q = q.filter_by(matter_id=matter_id)
    sets = q.order_by(DiscoverySet.created_at.desc()).all()
    matter = db.session.get(Matter, matter_id) if matter_id else None
    matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    return render_template("discovery/index.html", sets=sets, matter=matter, matter_id=matter_id, matters=matters,
                           kind_labels=KIND_LABELS, task_titles=_task_titles(sets))


def _task_titles(sets):
    titles = {task_title(s) for s in sets}
    if not titles:
        return set()
    return {t.title for t in Task.query.filter(Task.title.in_(list(titles))).all()}


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    matter_id = _int(request.values.get("matter_id"))
    m = _matter_or_404(matter_id)
    docs = Document.query.filter_by(matter_id=m.id, is_current=True).order_by(Document.created_at.desc()).all()
    area = area_for(m)
    if request.method == "POST":
        direction = request.form.get("direction", "propound")
        direction = direction if direction in dict(DIRECTIONS) else "propound"
        kind = request.form.get("kind", "interrogatories")
        kind = kind if kind in KIND_LABELS else "interrogatories"
        party = request.form.get("party", "").strip()[:200]
        served_on = parse_date(request.form.get("served_on"))
        title = request.form.get("title", "").strip()[:300] or _default_title(direction, kind, party)
        ds = DiscoverySet(matter_id=m.id, direction=direction, kind=kind, party=party, served_on=served_on,
                          title=title, created_by_id=_uid())
        if served_on:
            ds.due_on = served_on + timedelta(days=RESPONSE_DAYS)
        if direction == "propound":
            _set_items(ds, starter_items(area, kind))
            msg = f"Started from the {AREA_LABELS[area]} starter set ({len(ds.items)} items)."
        else:
            doc = db.session.get(Document, _int(request.form.get("source_document_id")) or 0)
            if not doc or doc.matter_id != m.id:
                flash("Pick the served set from the matter's documents.", "error")
                return render_template("discovery/new.html", m=m, docs=docs, directions=DIRECTIONS, kinds=KINDS,
                                       area=AREA_LABELS[area], form=request.form)
            ds.source_document_id = doc.id
            reqs = parse_requests(doc.extracted_text)
            _set_items(ds, [_item(i + 1, r) for i, r in enumerate(reqs)])
            if reqs:
                msg = f"Found {len(reqs)} numbered requests in {doc.name}."
            else:
                msg = (f"No numbered requests were found in {doc.name} (it may have no extracted text). Add the "
                       f"items by hand below.")
        db.session.add(ds)
        db.session.flush()
        audit("create", "discovery_set", ds.id, f"{ds.title} on {m.number}", _uid())
        db.session.commit()
        flash(msg, "ok")
        return redirect(url_for("discovery.detail", id=ds.id))
    return render_template("discovery/new.html", m=m, docs=docs, directions=DIRECTIONS, kinds=KINDS,
                           area=AREA_LABELS[area], form={})


def _set_or_404(id):
    return db.session.get(DiscoverySet, id) or abort(404)


def _task_for(ds):
    return Task.query.filter_by(matter_id=ds.matter_id, title=task_title(ds)).first()


@bp.route("/<int:id>")
@login_required
def detail(id):
    ds = _set_or_404(id)
    out_doc = db.session.get(Document, ds.output_document_id) if ds.output_document_id else None
    src_doc = db.session.get(Document, ds.source_document_id) if ds.source_document_id else None
    return render_template("discovery/detail.html", ds=ds, items=ds.items, objections=OBJECTIONS, statuses=STATUSES,
                           kind_labels=KIND_LABELS, item_label=ITEM_LABELS[ds.kind], out_doc=out_doc,
                           src_doc=src_doc, task=_task_for(ds), ai=llm.status(), fallback=FALLBACK_RESPONSE)


def _apply_edits(ds, form):
    """Read every item's fields from the editor form. Missing items (removed rows) are dropped."""
    ds.title = form.get("title", ds.title).strip()[:300] or ds.title
    ds.party = form.get("party", ds.party or "").strip()[:200]
    ds.served_on = parse_date(form.get("served_on"))
    ds.due_on = parse_date(form.get("due_on"))
    st = form.get("status", ds.status)
    ds.status = st if st in STATUSES else ds.status
    ns = [_int(x) for x in form.getlist("item_n")]
    items = []
    for n in ns:
        if n is None:
            continue
        objs = [o for o in form.getlist(f"item_{n}_objections") if o in OBJECTION_KEYS]
        items.append(_item(n, form.get(f"item_{n}_request", ""), form.get(f"item_{n}_response", ""), objs,
                           form.get(f"item_{n}_flag", "")))
    return items


@bp.route("/<int:id>/save", methods=["POST"])
@login_required
def save(id):
    ds = _set_or_404(id)
    items = _apply_edits(ds, request.form) if request.form.getlist("item_n") else ds.items
    action = request.form.get("action", "")
    if action == "add":
        items.append(_item(len(items) + 1, request.form.get("new_request", "")))
    elif action.startswith("delete:"):
        n = _int(action.split(":", 1)[1])
        items = [it for it in items if it["n"] != n]
    elif action.startswith("up:") or action.startswith("down:"):
        n = _int(action.split(":", 1)[1])
        idx = next((i for i, it in enumerate(items) if it["n"] == n), None)
        if idx is not None:
            j = idx - 1 if action.startswith("up:") else idx + 1
            if 0 <= j < len(items):
                items[idx], items[j] = items[j], items[idx]
    _set_items(ds, items)
    db.session.commit()
    if not action:
        flash("Saved.", "ok")
    return redirect(url_for("discovery.detail", id=ds.id))


@bp.route("/<int:id>/tailor", methods=["POST"])
@login_required
def tailor(id):
    ds = _set_or_404(id)
    if ds.direction != "propound":
        flash("Tailoring is for sets we propound. Use Draft responses on a served set.", "error")
        return redirect(url_for("discovery.detail", id=ds.id))
    try:
        items = tailor_with_ai(ds)
    except LLMUnavailable as e:
        flash(f"The set was not changed. {e}", "error")
        return redirect(url_for("discovery.detail", id=ds.id))
    _set_items(ds, items)
    if ds.status == "final":
        ds.status = "review"
    audit("ai_tailor", "discovery_set", ds.id, f"{len(items)} items", _uid())
    db.session.commit()
    flash(f"Tailored to the matter facts: {len(items)} requests. This is a draft for attorney review.", "ok")
    return redirect(url_for("discovery.detail", id=ds.id))


@bp.route("/<int:id>/draft", methods=["POST"])
@login_required
def draft(id):
    ds = _set_or_404(id)
    if not ds.items:
        flash("Add the requests first.", "error")
        return redirect(url_for("discovery.detail", id=ds.id))
    items = ds.items
    try:
        items = draft_responses_with_ai(ds)
        n_ph = fill_placeholders(items)
        _set_items(ds, items)
        audit("ai_draft", "discovery_set", ds.id, f"{len(items)} responses", _uid())
        db.session.commit()
        flagged = sum(1 for it in items if it.get("flag"))
        flash(f"Drafted {len(items)} responses ({flagged} need a fact from the client"
              f"{', ' + str(n_ph) + ' left as placeholders' if n_ph else ''}). "
              f"This is a draft for attorney review.", "ok")
    except LLMUnavailable as e:
        n_ph = fill_placeholders(items)
        _set_items(ds, items)
        db.session.commit()
        flash(f"The AI is not available ({e}) so {n_ph} placeholder responses were filled in for you to complete.",
              "error")
    return redirect(url_for("discovery.detail", id=ds.id))


@bp.route("/<int:id>/task", methods=["POST"])
@login_required
def due_task(id):
    ds = _set_or_404(id)
    if not ds.served_on:
        flash("Set the served date first. The deadline is the served date plus 30 days.", "error")
        return redirect(url_for("discovery.detail", id=ds.id))
    existing = _task_for(ds)
    due = ds.served_on + timedelta(days=RESPONSE_DAYS)
    if existing:
        flash(f"That deadline task already exists (due {due.strftime('%b %-d, %Y')}).", "ok")
        return redirect(url_for("discovery.detail", id=ds.id))
    t = Task(matter_id=ds.matter_id, title=task_title(ds), kind="deadline", due_on=due, priority="high",
             assignee_id=ds.matter.responsible_user_id, notes=f"Served {ds.served_on.isoformat()} by {ds.party}. "
             f"Discovery set /discovery/{ds.id}")
    db.session.add(t)
    if not ds.due_on:
        ds.due_on = due
    db.session.flush()
    audit("create", "task", t.id, t.title, _uid())
    db.session.commit()
    flash(f"Deadline task created, due {due.strftime('%b %-d, %Y')}.", "ok")
    return redirect(url_for("discovery.detail", id=ds.id))


@bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def delete(id):
    ds = _set_or_404(id)
    mid = ds.matter_id
    audit("delete", "discovery_set", ds.id, ds.title, _uid())
    db.session.delete(ds)
    db.session.commit()
    flash("Discovery set deleted.", "ok")
    return redirect(url_for("discovery.index", matter_id=mid))


# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------
class DraftPDF(DocPDF):
    """Firm letterhead plus a footer that says the document is a draft for attorney review."""

    def footer(self):
        self.set_y(-18)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 4.5, _txt(DRAFT_NOTE), align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4.5, f"{_txt(self.doc_title)}   Page {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0, 0, 0)


def _txt(s):
    return (str(s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
            .replace("–", "-").replace("\u2014", "-").replace("•", "-")
            .encode("latin-1", "replace").decode("latin-1"))


def _para(pdf, text, size=10.5, style="", gap=2.5):
    pdf.set_font("Helvetica", style, size)
    pdf.multi_cell(0, 5.2, _txt(text), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(gap)


def _caption(pdf, m, extra_lines):
    """Caption block: court, case number, matter name, then the lines the caller passes."""
    _para(pdf, (m.court or "[COURT]").upper(), size=11, style="B", gap=1)
    _para(pdf, f"Cause No. {m.case_number or '[CASE NUMBER]'}", size=10.5, gap=1)
    _para(pdf, m.name, size=11, style="B", gap=1)
    for l in extra_lines:
        _para(pdf, l, size=10.5, gap=1)
    pdf.ln(2)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(4)


def build_set_pdf(ds):
    m = ds.matter
    firm = Firm.get()
    label = KIND_LABELS[ds.kind]
    head = f"{label} to {ds.party}" if ds.direction == "propound" else f"Responses to {label} from {ds.party}"
    pdf = DraftPDF(firm, title=head)
    pdf.alias_nb_pages()
    pdf.add_page()
    _caption(pdf, m, [("Propounded to: " if ds.direction == "propound" else "Served by: ") + (ds.party or ""),
                      f"Kind: {label}",
                      f"Served on: {ds.served_on.strftime('%b %-d, %Y') if ds.served_on else 'not set'}"
                      + (f"   Responses due: {ds.due_on.strftime('%b %-d, %Y')}" if ds.due_on else "")])
    _para(pdf, head.upper(), size=12, style="B", gap=4)
    item_label = ITEM_LABELS[ds.kind]
    for it in ds.items:
        _para(pdf, f"{item_label} NO. {it['n']}:", size=10.5, style="B", gap=1)
        _para(pdf, it["request"], gap=2)
        if ds.direction == "respond" or it.get("response") or it.get("objections"):
            objs = [OBJECTION_LABELS[o] for o in it.get("objections", []) if o in OBJECTION_LABELS]
            if objs:
                _para(pdf, "OBJECTIONS: " + "; ".join(objs) + ".", gap=1)
            _para(pdf, "RESPONSE:", style="B", gap=1)
            _para(pdf, it.get("response") or "[ATTORNEY TO COMPLETE]", gap=2)
        if it.get("flag"):
            _para(pdf, f"[Needs client input: {it['flag']}]", size=9, style="I", gap=2)
        pdf.ln(1)
    return bytes(pdf.output())


@bp.route("/<int:id>/export", methods=["POST"])
@login_required
def export(id):
    ds = _set_or_404(id)
    if request.form.getlist("item_n"):
        _set_items(ds, _apply_edits(ds, request.form))
    data = build_set_pdf(ds)
    name = f"{ds.title} {date.today().isoformat()}.pdf"
    doc, err = store_bytes(ds.matter_id, name, data, mime="application/pdf", user_id=_uid(), folder="Discovery",
                           tags="discovery")
    if err:
        flash(err, "error")
        return redirect(url_for("discovery.detail", id=ds.id))
    db.session.flush()
    ds.output_document_id = doc.id
    audit("generate", "document", doc.id, f"{name} for {ds.matter.number}", _uid())
    db.session.commit()
    flash(f"PDF filed under Documents in the Discovery folder: {name}", "ok")
    return redirect(url_for("discovery.detail", id=ds.id))


# ---------------------------------------------------------------------------
# depositions
# ---------------------------------------------------------------------------
CHUNK_CHARS = 10_000
_PAGE_RE = re.compile(r"\b(?:Page|PAGE)\s+(\d{1,4})\b")  # case-sensitive so "of page 3" in prose is not a marker
_PL_RE = re.compile(r"\b(\d{1,4}):(\d{1,2})\b")

DEPO_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_testimony": {"type": "array", "items": {
            "type": "object",
            "properties": {"page": {"type": "integer"}, "line": {"type": "integer"}, "quote": {"type": "string"},
                           "topic": {"type": "string"}},
            "required": ["page", "line", "quote", "topic"], "additionalProperties": False}},
        "contradictions": {"type": "array", "items": {
            "type": "object",
            "properties": {"testimony": {"type": "string"}, "conflicts_with": {"type": "string"},
                           "source": {"type": "string"}},
            "required": ["testimony", "conflicts_with", "source"], "additionalProperties": False}},
    },
    "required": ["summary", "key_testimony", "contradictions"], "additionalProperties": False,
}
CONDENSE_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
                   "additionalProperties": False}


def has_markers(text):
    return bool(_PAGE_RE.search(text or "") or _PL_RE.search(text or ""))


def chunk_transcript(text, size=CHUNK_CHARS):
    """Split into pieces of about `size` characters, breaking at a "Page N" marker when one sits in the last
    fifth of the piece, else at whitespace. Markers stay attached to the text that follows them."""
    text = text or ""
    out, pos = [], 0
    while pos < len(text):
        end = min(len(text), pos + size)
        if end < len(text):
            window_start = pos + int(size * 0.8)
            cut = None
            for mt in _PAGE_RE.finditer(text, window_start, end):
                cut = mt.start()
            if cut is None:
                sp = text.rfind(" ", window_start, end)
                cut = sp if sp > pos else end
            end = cut
        piece = text[pos:end].strip()
        if piece:
            out.append(piece)
        pos = end if end > pos else pos + size
    return out


def summarize_transcript(dep, text):
    """Run the model over the transcript in chunks. Returns (summary, key_testimony, contradictions).
    Raises LLMUnavailable when nothing could be produced."""
    m = dep.matter
    facts, _ = llm.clip(matter_facts(m), 1500)
    marked = has_markers(text)
    chunks = chunk_transcript(text)
    summaries, key, contras = [], [], []
    marker_note = ("The text keeps the transcript's page and line markers (lines like \"Page 12\" or \"12:5\" meaning "
                   "page 12 line 5). Cite the page and line each quote comes from."
                   if marked else "The text has no page or line markers, so use 0 for page and line.")
    for i, chunk in enumerate(chunks, 1):
        prompt = (f"This is part {i} of {len(chunks)} of the deposition transcript of {dep.deponent or 'the deponent'}"
                  f"{', taken ' + dep.taken_on.strftime('%b %-d, %Y') if dep.taken_on else ''}. {marker_note}\n"
                  f"Return JSON with: \"summary\" (a plain summary of this part, about 150 words), \"key_testimony\" "
                  f"(up to 10 items: page, line, a short verbatim quote, topic), and \"contradictions\" (each place "
                  f"the testimony conflicts with the confirmed chronology or PI facts below: the testimony, what it "
                  f"conflicts with, and the source named as \"chronology <date> <provider>\" or \"PI facts: "
                  f"<field>\"). Only report contradictions the facts below actually support.\n\n"
                  f"Matter facts:\n{facts}\n\nTranscript part {i}:\n{chunk}")
        data = llm.complete_json(prompt, DEPO_SCHEMA, system=SYSTEM, max_tokens=2500, kind="deposition_summary",
                                 entity="deposition_summary", entity_id=dep.id, user_id=_uid())
        if not isinstance(data, dict):
            continue
        if data.get("summary"):
            summaries.append(str(data["summary"]).strip())
        for k in data.get("key_testimony") or []:
            if isinstance(k, dict) and (k.get("quote") or "").strip():
                key.append({"page": _int(k.get("page")) or 0, "line": _int(k.get("line")) or 0,
                            "quote": str(k.get("quote")).strip(), "topic": str(k.get("topic") or "").strip()})
        for c in data.get("contradictions") or []:
            if isinstance(c, dict) and (c.get("testimony") or "").strip():
                contras.append({"testimony": str(c.get("testimony")).strip(),
                                "conflicts_with": str(c.get("conflicts_with") or "").strip(),
                                "source": str(c.get("source") or "").strip()})
    if not summaries:
        raise llm.LLMBadOutput("The AI returned nothing usable for this transcript.")
    summary = summaries[0]
    if len(summaries) > 1:
        joined = "\n\n".join(summaries)
        try:
            data = llm.complete_json(
                f"Combine these part summaries of the deposition of {dep.deponent or 'the deponent'} into one summary "
                f"of about 300 words, in order, keeping every fact. Return JSON {{\"summary\": \"...\"}}.\n\n{joined}",
                CONDENSE_SCHEMA, system=SYSTEM, max_tokens=1200, kind="deposition_condense",
                entity="deposition_summary", entity_id=dep.id, user_id=_uid())
            summary = str(data.get("summary") or "").strip() if isinstance(data, dict) else ""
        except LLMUnavailable:
            summary = ""
        summary = summary or joined
    return summary, key, contras


def cite(dep, k):
    """Copy-ready citation: "Depo. Tr. 12:5", or "Depo. Tr. p. 12" when there is no line."""
    p, l = _int(k.get("page")) or 0, _int(k.get("line")) or 0
    if p and l:
        return f"Depo. Tr. {p}:{l}"
    if p:
        return f"Depo. Tr. p. {p}"
    return "Depo. Tr. (no page ref)"


@bp.route("/depositions")
@login_required
def depositions():
    matter_id = _int(request.args.get("matter_id"))
    q = DepositionSummary.query
    if matter_id:
        q = q.filter_by(matter_id=matter_id)
    deps = q.order_by(DepositionSummary.created_at.desc()).all()
    matter = db.session.get(Matter, matter_id) if matter_id else None
    matters = Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
    counts = {}
    for d in deps:
        try:
            counts[d.id] = len(json.loads(d.key_testimony_json or "[]"))
        except ValueError:
            counts[d.id] = 0
    return render_template("discovery/depositions.html", deps=deps, matter=matter, matter_id=matter_id,
                           matters=matters, counts=counts)


@bp.route("/depositions/new", methods=["GET", "POST"])
@login_required
def deposition_new():
    matter_id = _int(request.values.get("matter_id"))
    m = _matter_or_404(matter_id)
    docs = Document.query.filter_by(matter_id=m.id, is_current=True).order_by(Document.created_at.desc()).all()
    if request.method == "POST":
        doc = db.session.get(Document, _int(request.form.get("document_id")) or 0)
        if not doc or doc.matter_id != m.id:
            flash("Pick the transcript from the matter's documents.", "error")
            return render_template("discovery/deposition_new.html", m=m, docs=docs, form=request.form)
        dep = DepositionSummary(matter_id=m.id, document_id=doc.id,
                                deponent=request.form.get("deponent", "").strip()[:200],
                                taken_on=parse_date(request.form.get("taken_on")), created_by_id=_uid())
        db.session.add(dep)
        db.session.flush()
        text = (doc.extracted_text or "").strip()
        if not text:
            dep.summary_text = (f"No text could be read from {doc.name}, so nothing was summarised. Upload a text, "
                                f"DOCX or text-based PDF transcript, or type the summary here.")
        else:
            try:
                summary, key, contras = summarize_transcript(dep, text)
                dep.summary_text = summary
                dep.key_testimony_json = json.dumps(key, ensure_ascii=False)
                dep.contradictions_json = json.dumps(contras, ensure_ascii=False)
                flash(f"Summary drafted: {len(key)} key passages, {len(contras)} contradictions. "
                      f"This is a draft for attorney review.", "ok")
            except LLMUnavailable as e:
                dep.summary_text = (f"The AI model is not configured, so no summary was generated. ({e}) "
                                    f"Type the summary here or add notes on the matter.")
                flash("The record was created without an AI summary. " + str(e), "error")
        audit("create", "deposition_summary", dep.id, f"{dep.deponent} on {m.number}", _uid())
        db.session.commit()
        return redirect(url_for("discovery.deposition_detail", id=dep.id))
    return render_template("discovery/deposition_new.html", m=m, docs=docs, form={})


def _dep_or_404(id):
    return db.session.get(DepositionSummary, id) or abort(404)


def _dep_lists(dep):
    try:
        key = json.loads(dep.key_testimony_json or "[]")
    except ValueError:
        key = []
    try:
        contras = json.loads(dep.contradictions_json or "[]")
    except ValueError:
        contras = []
    return key, contras


@bp.route("/depositions/<int:id>")
@login_required
def deposition_detail(id):
    dep = _dep_or_404(id)
    key, contras = _dep_lists(dep)
    rows = [(k, cite(dep, k)) for k in key]
    return render_template("discovery/deposition_detail.html", dep=dep, rows=rows, contras=contras,
                           statuses=STATUSES, ai=llm.status())


@bp.route("/depositions/<int:id>/save", methods=["POST"])
@login_required
def deposition_save(id):
    dep = _dep_or_404(id)
    dep.summary_text = request.form.get("summary_text", dep.summary_text or "").strip()
    dep.deponent = request.form.get("deponent", dep.deponent or "").strip()[:200]
    dep.taken_on = parse_date(request.form.get("taken_on"), dep.taken_on)
    st = request.form.get("status", dep.status)
    dep.status = st if st in STATUSES else dep.status
    db.session.commit()
    flash("Saved.", "ok")
    return redirect(url_for("discovery.deposition_detail", id=dep.id))


@bp.route("/depositions/<int:id>/rerun", methods=["POST"])
@login_required
def deposition_rerun(id):
    dep = _dep_or_404(id)
    text = (dep.document.extracted_text if dep.document else "") or ""
    if not text.strip():
        flash("The transcript has no readable text.", "error")
        return redirect(url_for("discovery.deposition_detail", id=dep.id))
    try:
        summary, key, contras = summarize_transcript(dep, text)
    except LLMUnavailable as e:
        flash(f"Nothing changed. {e}", "error")
        return redirect(url_for("discovery.deposition_detail", id=dep.id))
    dep.summary_text, dep.key_testimony_json = summary, json.dumps(key, ensure_ascii=False)
    dep.contradictions_json = json.dumps(contras, ensure_ascii=False)
    audit("ai_rerun", "deposition_summary", dep.id, "", _uid())
    db.session.commit()
    flash(f"Summary redrafted: {len(key)} key passages, {len(contras)} contradictions. Draft for attorney review.", "ok")
    return redirect(url_for("discovery.deposition_detail", id=dep.id))


def deposition_note_text(dep):
    key, contras = _dep_lists(dep)
    lines = [f"Deposition summary: {dep.deponent or 'deponent'}"
             + (f", taken {dep.taken_on.strftime('%b %-d, %Y')}" if dep.taken_on else ""), "",
             (dep.summary_text or "").strip()]
    if key:
        lines += ["", "Key testimony:"]
        lines += [f"- {cite(dep, k)}: \"{k.get('quote', '')}\"" + (f" ({k['topic']})" if k.get("topic") else "")
                  for k in key]
    if contras:
        lines += ["", "Possible contradictions:"]
        lines += [f"- {c.get('testimony', '')} vs {c.get('conflicts_with', '')}"
                  + (f" [{c['source']}]" if c.get("source") else "") for c in contras]
    lines += ["", "AI draft for attorney review."]
    return "\n".join(lines)


@bp.route("/depositions/<int:id>/note", methods=["POST"])
@login_required
def deposition_note(id):
    dep = _dep_or_404(id)
    n = Note(matter_id=dep.matter_id, user_id=_uid(), body=deposition_note_text(dep))
    db.session.add(n)
    db.session.flush()
    audit("create", "note", n.id, f"deposition summary of {dep.deponent}", _uid())
    db.session.commit()
    flash("Saved as a note on the matter.", "ok")
    return redirect(url_for("discovery.deposition_detail", id=dep.id))


def build_deposition_pdf(dep):
    m = dep.matter
    firm = Firm.get()
    key, contras = _dep_lists(dep)
    title = f"Deposition summary: {dep.deponent or 'deponent'}"
    pdf = DraftPDF(firm, title=title)
    pdf.alias_nb_pages()
    pdf.add_page()
    _caption(pdf, m, [f"Deponent: {dep.deponent or ''}",
                      f"Taken: {dep.taken_on.strftime('%b %-d, %Y') if dep.taken_on else 'not set'}",
                      f"Transcript: {dep.document.name if dep.document else ''}"])
    _para(pdf, "SUMMARY", size=12, style="B", gap=2)
    for para in re.split(r"\n\s*\n", dep.summary_text or ""):
        if para.strip():
            _para(pdf, para.strip())
    if key:
        _para(pdf, "KEY TESTIMONY", size=12, style="B", gap=2)
        for k in key:
            _para(pdf, f"{cite(dep, k)}" + (f"  [{k['topic']}]" if k.get("topic") else ""), style="B", gap=1)
            _para(pdf, f"\"{k.get('quote', '')}\"", gap=2)
    if contras:
        _para(pdf, "POSSIBLE CONTRADICTIONS", size=12, style="B", gap=2)
        for c in contras:
            _para(pdf, f"Testimony: {c.get('testimony', '')}", gap=1)
            _para(pdf, f"Conflicts with: {c.get('conflicts_with', '')}"
                  + (f" (source: {c['source']})" if c.get("source") else ""), gap=3)
    return bytes(pdf.output())


@bp.route("/depositions/<int:id>/export", methods=["POST"])
@login_required
def deposition_export(id):
    dep = _dep_or_404(id)
    data = build_deposition_pdf(dep)
    name = f"Deposition summary {dep.deponent or 'deponent'} {date.today().isoformat()}.pdf"
    doc, err = store_bytes(dep.matter_id, name, data, mime="application/pdf", user_id=_uid(), folder="Depositions",
                           tags="deposition")
    if err:
        flash(err, "error")
        return redirect(url_for("discovery.deposition_detail", id=dep.id))
    db.session.flush()
    audit("generate", "document", doc.id, f"{name} for {dep.matter.number}", _uid())
    db.session.commit()
    flash(f"PDF filed under Documents in the Depositions folder: {name}", "ok")
    return redirect(url_for("discovery.deposition_detail", id=dep.id))


@bp.route("/depositions/<int:id>/delete", methods=["POST"])
@login_required
def deposition_delete(id):
    dep = _dep_or_404(id)
    mid = dep.matter_id
    audit("delete", "deposition_summary", dep.id, dep.deponent, _uid())
    db.session.delete(dep)
    db.session.commit()
    flash("Deposition summary deleted.", "ok")
    return redirect(url_for("discovery.depositions", matter_id=mid))
