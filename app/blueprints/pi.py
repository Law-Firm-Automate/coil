"""Personal injury module (Filevine lane): case facts and stage board, treating providers with records and
bills tracking, liens with reduction letters, a demand package builder, the settlement worksheet with trust
postings, and a standard PI task set.

Every generated PDF is a draft for attorney review and says so in its footer. Nothing here is legal advice.
Money is integer cents throughout (parse_money in, |money out).
"""
import json
import os
import uuid
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app
from werkzeug.utils import secure_filename
from ..extensions import db
from ..models import (PiCase, MedicalProvider, Lien, SettlementWorksheet, Matter, Document, Task,
                      TrustTransaction, Expense, Firm, audit)
from ..helpers import login_required, current_user, parse_money, parse_date, cents_to_str
from ..services.pdf import DocPDF, money as pdf_money

bp = Blueprint("pi", __name__, url_prefix="/pi")

STAGES = [("intake", "Intake"), ("treating", "Treating"), ("records", "Records"), ("demand", "Demand"),
          ("negotiation", "Negotiation"), ("litigation", "Litigation"), ("settled", "Settled"), ("closed", "Closed")]
STAGE_KEYS = [k for k, _ in STAGES]
INCIDENT_TYPES = [("auto", "Motor vehicle"), ("premises", "Premises liability"), ("dog_bite", "Dog bite"),
                  ("medmal", "Medical malpractice"), ("product", "Product liability"), ("other", "Other")]
TREATMENT_STATUSES = [("treating", "Still treating"), ("mmi", "Maximum medical improvement"), ("released", "Released")]
LIEN_TYPES = ["medical", "health_plan", "medicare", "medicaid", "erisa", "workers_comp", "attorney", "other"]
LIEN_STATUSES = ["open", "negotiating", "resolved", "paid"]
DEFAULT_FEE_PCT = 33.33
DRAFT_NOTE = "Draft prepared for attorney review. This document is a template, not legal advice."


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v, default=0.0):
    try:
        return float(str(v or "").replace("%", "").strip())
    except ValueError:
        return default


def _case_for(matter):
    return PiCase.query.filter_by(matter_id=matter.id).first()


def _providers(matter):
    return MedicalProvider.query.filter_by(matter_id=matter.id).order_by(
        MedicalProvider.first_visit_on.asc().nulls_last(), MedicalProvider.id).all()


def _liens(matter):
    return Lien.query.filter_by(matter_id=matter.id).order_by(Lien.id).all()


def total_billed_cents(matter):
    return sum(int(p.total_billed_cents or 0) for p in _providers(matter))


def liens_payable_cents(matter):
    return sum(int(l.payable_cents or 0) for l in _liens(matter) if l.status != "paid")


def client_dob(contact):
    """Date of birth from a contact custom field named dob (any case, spaces or underscores allowed)."""
    if not contact:
        return ""
    for k, v in contact.custom_fields.items():
        key = str(k).strip().lower().replace("_", " ")
        if key in ("dob", "date of birth", "birth date", "birthdate"):
            return "" if v is None else str(v)
    return ""


def _fmt_date(d):
    return d.strftime("%B %-d, %Y") if d else ""


def _lines(s):
    return [l.strip() for l in str(s or "").splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# PDF plumbing
# ---------------------------------------------------------------------------
class PiPDF(DocPDF):
    """Firm letterhead from DocPDF plus a footer line saying the document is a draft for attorney review."""

    def footer(self):
        self.set_y(-18)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 4.5, _txt(DRAFT_NOTE), align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4.5, f"{_txt(self.doc_title)}   Page {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0, 0, 0)


def _txt(s):
    return (str(s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
            .replace("–", "-").replace("—", "-").replace("•", "-")
            .encode("latin-1", "replace").decode("latin-1"))


def _para(pdf, text, size=10.5, style="", gap=2.5):
    pdf.set_font("Helvetica", style, size)
    pdf.multi_cell(0, 5.2, _txt(text), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(gap)


def _line(pdf, text, size=10.5, style=""):
    pdf.set_font("Helvetica", style, size)
    pdf.cell(0, 5.2, _txt(text), new_x="LMARGIN", new_y="NEXT")


def _table(pdf, headers, rows, widths, aligns):
    pdf.set_font("Helvetica", "", 9.5)
    with pdf.table(col_widths=widths, text_align=aligns, line_height=5.5, borders_layout="HORIZONTAL_LINES") as t:
        r = t.row()
        for h in headers:
            r.cell(_txt(h))
        for row in rows:
            r = t.row()
            for c in row:
                r.cell(_txt(c))
    pdf.ln(3)


def _letter_head(pdf, to_lines, re_lines):
    _line(pdf, _fmt_date(date.today()))
    pdf.ln(3)
    for l in to_lines:
        _line(pdf, l)
    pdf.ln(3)
    for i, l in enumerate(re_lines):
        _line(pdf, ("Re: " if i == 0 else "      ") + l, style="B" if i == 0 else "")
    pdf.ln(3)


def _signature(pdf, matter):
    f = Firm.get()
    attorney = matter.responsible
    pdf.ln(2)
    _line(pdf, "Sincerely,")
    pdf.ln(6)
    _line(pdf, (attorney.name if attorney else f.name) or "")
    _line(pdf, f.name or "")
    contact = " | ".join(x for x in (f.phone, f.email) if x)
    if contact:
        _line(pdf, contact)


def _pdf_bytes(pdf):
    out = pdf.output()
    return bytes(out)


def save_pdf_document(matter, pdf, name, folder, user):
    """Write the PDF under UPLOAD_DIR/<matter_id>/ and add a Document row. Caller commits."""
    data = _pdf_bytes(pdf)
    rel_dir = str(matter.id)
    out_dir = os.path.join(current_app.config["UPLOAD_DIR"], rel_dir)
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{uuid.uuid4().hex}_{secure_filename(name) or 'document.pdf'}"
    with open(os.path.join(out_dir, fname), "wb") as fh:
        fh.write(data)
    doc = Document(matter_id=matter.id, name=name[:300], path=f"{rel_dir}/{fname}", size=len(data),
                   mime="application/pdf", uploaded_by_id=user.id if user else None, folder=folder,
                   extracted_text="")
    db.session.add(doc)
    db.session.flush()
    audit("generate", "document", doc.id, f"{name} for {matter.number}", user.id if user else None)
    audit("generate_document", "matter", matter.id, name, user.id if user else None)
    return doc


# ---------------------------------------------------------------------------
# letters
# ---------------------------------------------------------------------------
def _client_re_lines(matter, case):
    c = matter.client
    lines = [f"Patient: {c.display_name}"]
    dob = client_dob(c)
    if dob:
        lines.append(f"Date of birth: {dob}")
    if case and case.date_of_loss:
        lines.append(f"Date of loss: {_fmt_date(case.date_of_loss)}")
    lines.append(f"Our file: {matter.number}")
    return lines


def build_request_letter(matter, case, provider, what):
    """what = "records" or "bills". Returns a PiPDF."""
    f = Firm.get()
    title = "Request for medical records" if what == "records" else "Request for itemized billing"
    pdf = PiPDF(f, title=title)
    pdf.add_page()
    to_lines = [provider.name, "Attn: Records custodian" if what == "records" else "Attn: Billing department"]
    to_lines += _lines(provider.address)
    if provider.fax:
        to_lines.append(f"Fax: {provider.fax}")
    _letter_head(pdf, to_lines, _client_re_lines(matter, case))
    _line(pdf, "To the records custodian:" if what == "records" else "To the billing department:")
    pdf.ln(2)
    client = matter.client.display_name
    _para(pdf, f"This office represents {client} in connection with injuries sustained on "
               f"{_fmt_date(case.date_of_loss) if case and case.date_of_loss else 'the date of loss noted above'}. "
               f"A HIPAA-compliant authorization signed by the patient is enclosed with this request, and this "
               f"request is made under that authorization.")
    span = ""
    if provider.first_visit_on or provider.last_visit_on:
        span = (f" for the period {_fmt_date(provider.first_visit_on) or 'first visit'} through "
                f"{_fmt_date(provider.last_visit_on) or 'the present'}")
    if what == "records":
        _para(pdf, f"Please send a complete copy of the patient's medical records{span}, including intake forms, "
                   f"history and physical, progress and office notes, operative reports, diagnostic imaging reports, "
                   f"laboratory results, prescriptions, referrals, discharge summaries and correspondence.")
        _para(pdf, "If your office charges a fee for copies, please send an invoice with the records or call us "
                   "before processing if the fee will exceed $100. Certified copies are not required at this time.")
    else:
        _para(pdf, f"Please send an itemized statement of all charges for the patient's care{span}, showing each "
                   f"date of service, the CPT or procedure code, the amount charged, any payments or adjustments, "
                   f"the payer, and the balance outstanding. Please also state whether any lien or assignment has "
                   f"been asserted against the patient's recovery.")
    _para(pdf, f"Please send the material to {f.name}, {', '.join(_lines(f.address)) or 'the address above'}"
               f"{', or by email to ' + f.email if f.email else ''}. Call {f.phone or 'our office'} with any question.")
    _para(pdf, "Thank you for your help.")
    _signature(pdf, matter)
    pdf.ln(4)
    _line(pdf, "Enclosure: signed HIPAA authorization", size=9, style="I")
    return pdf


def build_reduction_letter(matter, case, lien, pct, proposed_cents):
    f = Firm.get()
    pdf = PiPDF(f, title="Lien reduction request")
    pdf.add_page()
    to_lines = [lien.holder] + ([f"Attn: {lien.contact}"] if lien.contact else []) + ["Subrogation / lien department"]
    re_lines = _client_re_lines(matter, case)
    re_lines.append(f"Lien type: {lien.type.replace('_', ' ')}")
    _letter_head(pdf, to_lines, re_lines)
    _line(pdf, "To whom it may concern:")
    pdf.ln(2)
    client = matter.client.display_name
    _para(pdf, f"This office represents {client} in the personal injury claim arising from the date of loss above. "
               f"You have asserted a lien or reimbursement claim of {cents_to_str(lien.original_cents)} against "
               f"any recovery.")
    gross = case.offer_cents if case and case.offer_cents else 0
    if gross:
        _para(pdf, f"The claim is resolving for a limited amount ({cents_to_str(gross)}) that does not fully "
                   f"compensate our client for the injuries and losses involved. Attorney's fees and case costs "
                   f"must also be paid from that sum.")
    else:
        _para(pdf, "The available recovery is limited and does not fully compensate our client for the injuries "
                   "and losses involved. Attorney's fees and case costs must also be paid from that sum.")
    _para(pdf, f"We ask that you reduce your claim by {pct:g} percent, to {cents_to_str(proposed_cents)}, in "
               f"recognition of the procurement costs our client bears and the limited recovery. Please confirm "
               f"the reduced figure in writing so we can finalize the disbursement. If you require a different "
               f"basis for reduction under the plan or statute that governs your claim, tell us what you need.")
    _para(pdf, f"Please reply to {f.name}{', ' + f.email if f.email else ''}{', ' + f.phone if f.phone else ''}.")
    _para(pdf, "Thank you for your prompt attention.")
    _signature(pdf, matter)
    return pdf


def build_demand_package(matter, case, providers, demand_cents):
    f = Firm.get()
    pdf = PiPDF(f, title="Demand package")
    pdf.add_page()
    to_lines = [case.insurer or "Claims department"]
    if case.adjuster_name:
        to_lines.append(f"Attn: {case.adjuster_name}")
    for x in (case.adjuster_email, case.adjuster_phone):
        if x:
            to_lines.append(x)
    re_lines = [f"Claimant: {matter.client.display_name}"]
    if case.claim_number:
        re_lines.append(f"Claim number: {case.claim_number}")
    if case.date_of_loss:
        re_lines.append(f"Date of loss: {_fmt_date(case.date_of_loss)}")
    re_lines.append("FOR SETTLEMENT PURPOSES ONLY")
    _letter_head(pdf, to_lines, re_lines)
    _line(pdf, "Dear " + (case.adjuster_name or "Claims representative") + ":")
    pdf.ln(2)
    _para(pdf, f"This office represents {matter.client.display_name} for injuries sustained on "
               f"{_fmt_date(case.date_of_loss) or 'the date of loss'}. This letter sets out the facts, the "
               f"injuries and treatment, the medical specials, and our client's demand.")
    _line(pdf, "Facts", size=12, style="B")
    pdf.ln(1)
    _para(pdf, case.incident_description or "(Incident description not yet entered.)")
    if case.liability_notes:
        _line(pdf, "Liability", size=12, style="B")
        pdf.ln(1)
        _para(pdf, case.liability_notes)
    _line(pdf, "Injuries and treatment", size=12, style="B")
    pdf.ln(1)
    _para(pdf, case.injuries or "(Injuries not yet entered.)")
    status = dict(TREATMENT_STATUSES).get(case.treatment_status, case.treatment_status or "")
    if status:
        _para(pdf, f"Treatment status: {status}.")
    _line(pdf, "Treatment chronology", size=12, style="B")
    pdf.ln(2)
    rows = [(p.name, p.specialty or "", _fmt_date(p.first_visit_on), _fmt_date(p.last_visit_on),
             "yes" if p.records_received_on else "requested" if p.records_requested_on else "no") for p in providers]
    if rows:
        _table(pdf, ("Provider", "Specialty", "First visit", "Last visit", "Records"), rows,
               (52, 36, 30, 30, 26), ("LEFT", "LEFT", "LEFT", "LEFT", "LEFT"))
    else:
        _para(pdf, "(No providers entered.)")
    _line(pdf, "Medical specials", size=12, style="B")
    pdf.ln(2)
    total = sum(int(p.total_billed_cents or 0) for p in providers)
    srows = [(p.name, pdf_money(p.total_billed_cents or 0)) for p in providers]
    srows.append(("Total medical specials", pdf_money(total)))
    _table(pdf, ("Provider", "Billed"), srows, (130, 44), ("LEFT", "RIGHT"))
    _line(pdf, "Demand", size=12, style="B")
    pdf.ln(1)
    limits = f" We understand the applicable policy limits to be {cents_to_str(case.policy_limits_cents)}." \
        if case.policy_limits_cents else ""
    _para(pdf, f"In light of the liability facts, the injuries described above and medical specials of "
               f"{cents_to_str(total)}, our client demands {cents_to_str(demand_cents)} in full settlement of all "
               f"claims arising from this loss.{limits} Please respond within 30 days of the date of this letter.")
    _para(pdf, "This letter is a settlement communication and is not admissible for any other purpose. It does not "
               "waive any claim, right or remedy.")
    _signature(pdf, matter)
    return pdf


# ---------------------------------------------------------------------------
# settlement worksheet math (mirrors the free Settlement Disbursement Sheet)
# ---------------------------------------------------------------------------
def compute_worksheet(matter, gross_cents, fee_pct, extra_costs=None, other_cents=0):
    """Return a dict of every line. extra_costs = [(description, cents)] typed in by hand.
    fee = gross x pct; costs = every expense on the matter (billable or not) plus extras; liens = payable of
    every lien not yet paid; net = gross minus all of it. Parts always sum to the gross by construction."""
    gross = int(gross_cents or 0)
    pct = float(fee_pct or 0)
    fee = int(round(gross * pct / 100.0))
    expenses = [{"id": e.id, "date": e.date.isoformat() if e.date else "", "description": e.description or "",
                 "cents": int(e.amount_cents or 0), "billable": bool(e.billable)}
                for e in Expense.query.filter_by(matter_id=matter.id).order_by(Expense.date, Expense.id).all()]
    extras = [{"description": d, "cents": int(c)} for d, c in (extra_costs or []) if int(c or 0) > 0]
    costs = sum(x["cents"] for x in expenses) + sum(x["cents"] for x in extras)
    liens = [{"id": l.id, "holder": l.holder, "type": l.type, "original": int(l.original_cents or 0),
              "cents": int(l.payable_cents or 0), "status": l.status}
             for l in _liens(matter) if l.status != "paid"]
    lien_total = sum(x["cents"] for x in liens)
    other = int(other_cents or 0)
    net = gross - fee - costs - lien_total - other
    return {"gross": gross, "fee_pct": pct, "fee": fee, "expenses": expenses, "extras": extras, "costs": costs,
            "liens": liens, "liens_total": lien_total, "other": other, "net": net,
            "balanced": fee + costs + lien_total + other + net == gross}


def default_fee_pct(matter):
    return float(matter.contingency_pct) if matter.contingency_pct and matter.contingency_pct > 0 else DEFAULT_FEE_PCT


def _detail(ws):
    try:
        return json.loads(ws.detail_json or "{}")
    except Exception:
        return {}


def build_worksheet_pdf(matter, ws):
    f = Firm.get()
    d = _detail(ws)
    pdf = PiPDF(f, title="Settlement disbursement worksheet")
    pdf.add_page()
    _line(pdf, "Settlement disbursement worksheet", size=14, style="B")
    pdf.ln(1)
    _line(pdf, f"{matter.label}  |  Client: {matter.client.display_name}  |  Status: {ws.status}", size=9.5)
    _line(pdf, f"Prepared {_fmt_date(ws.created_at.date() if ws.created_at else date.today())}"
               f"{'  |  Approved ' + _fmt_date(ws.approved_on) if ws.approved_on else ''}"
               f"{'  |  Disbursed ' + _fmt_date(ws.disbursed_on) if ws.disbursed_on else ''}", size=9.5)
    pdf.ln(4)
    rows = [("Gross settlement", "", pdf_money(ws.gross_cents)),
            (f"Attorney fee ({ws.fee_pct:g}% of gross)", "", pdf_money(-ws.fee_cents))]
    for e in d.get("expenses", []):
        rows.append((f"Cost: {e.get('description') or 'expense'}", e.get("date", ""), pdf_money(-e["cents"])))
    for e in d.get("extras", []):
        rows.append((f"Cost: {e.get('description') or 'other cost'}", "", pdf_money(-e["cents"])))
    rows.append(("Costs subtotal", "", pdf_money(-ws.costs_cents)))
    for l in d.get("liens", []):
        rows.append((f"Lien: {l['holder']} ({l['type'].replace('_', ' ')})", "", pdf_money(-l["cents"])))
    rows.append(("Liens subtotal", "", pdf_money(-ws.liens_cents)))
    if ws.other_deductions_cents:
        rows.append(("Other deductions", "", pdf_money(-ws.other_deductions_cents)))
    rows.append(("Net to client", "", pdf_money(ws.net_to_client_cents)))
    _table(pdf, ("Line", "Date", "Amount"), rows, (110, 30, 34), ("LEFT", "LEFT", "RIGHT"))
    parts = ws.fee_cents + ws.costs_cents + ws.liens_cents + ws.other_deductions_cents + ws.net_to_client_cents
    _para(pdf, f"Balance check: fee + costs + liens + other + net = {pdf_money(parts)}; gross = "
               f"{pdf_money(ws.gross_cents)}. {'Balanced.' if parts == ws.gross_cents else 'OUT OF BALANCE.'}",
          size=9.5, style="B")
    _para(pdf, "Client acknowledgement: I have reviewed this disbursement and approve the payments listed.",
          size=9.5)
    pdf.ln(6)
    _line(pdf, "______________________________        ______________", size=9.5)
    _line(pdf, "Client signature                                          Date", size=9)
    return pdf


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------
@bp.route("")
@login_required
def index():
    cases = PiCase.query.join(Matter, PiCase.matter_id == Matter.id).order_by(Matter.number).all()
    cols = {k: [] for k in STAGE_KEYS}
    for c in cases:
        cols.setdefault(c.stage if c.stage in cols else "intake", []).append({
            "case": c, "matter": c.matter, "billed": total_billed_cents(c.matter),
            "liens": liens_payable_cents(c.matter)})
    have = {c.matter_id for c in cases}
    open_matters = [m for m in Matter.query.filter(Matter.status != "closed").order_by(Matter.number).all()
                    if m.id not in have]
    return render_template("pi/index.html", stages=STAGES, cols=cols, open_matters=open_matters, count=len(cases))


@bp.route("/start", methods=["POST"])
@login_required
def start():
    m = db.session.get(Matter, _int(request.form.get("matter_id")) or 0) or abort(404)
    if not _case_for(m):
        _start_case(m)
        db.session.commit()
        flash(f"Started a personal injury case on {m.label}.", "ok")
    return redirect(url_for("pi.case", matter_id=m.id))


def _start_case(m):
    c = PiCase(matter_id=m.id, stage="intake", treatment_status="treating")
    db.session.add(c)
    db.session.flush()
    audit("pi_start", "matter", m.id, "personal injury case started", current_user().id)
    return c


# ---------------------------------------------------------------------------
# case page
# ---------------------------------------------------------------------------
def _load(matter_id):
    m = db.session.get(Matter, matter_id) or abort(404)
    c = _case_for(m) or abort(404)
    return m, c


@bp.route("/<int:matter_id>")
@login_required
def case(matter_id):
    m = db.session.get(Matter, matter_id) or abort(404)
    c = _case_for(m)
    if not c:
        # The matter page links here for every matter; the first visit starts the PI case.
        c = _start_case(m)
        db.session.commit()
    providers = _providers(m)
    liens = _liens(m)
    worksheets = SettlementWorksheet.query.filter_by(matter_id=m.id).order_by(
        SettlementWorksheet.is_current.desc(), SettlementWorksheet.id.desc()).all()
    current = next((w for w in worksheets if w.is_current), None)
    prior = [w for w in worksheets if not w.is_current]
    preview = compute_worksheet(m, current.gross_cents if current else (c.offer_cents or 0),
                                current.fee_pct if current else default_fee_pct(m))
    docs = Document.query.filter(Document.matter_id == m.id, Document.is_current == True,  # noqa: E712
                                 Document.folder.in_(["Medical records", "Liens", "Demand", "Settlement"])).order_by(
        Document.created_at.desc()).all()
    tasks = Task.query.filter_by(matter_id=m.id, done=False).order_by(Task.due_on.asc().nulls_last()).all()
    return render_template("pi/case.html", m=m, c=c, providers=providers, liens=liens, stages=STAGES,
                           incident_types=INCIDENT_TYPES, treatment_statuses=TREATMENT_STATUSES,
                           lien_types=LIEN_TYPES, lien_statuses=LIEN_STATUSES,
                           total_billed=sum(int(p.total_billed_cents or 0) for p in providers),
                           liens_payable=sum(int(l.payable_cents or 0) for l in liens if l.status != "paid"),
                           current=current, current_detail=_detail(current) if current else {}, prior=prior,
                           preview=preview, default_fee_pct=default_fee_pct(m), docs=docs, tasks=tasks,
                           trust_balance=m.client.trust_balance_cents(), dob=client_dob(m.client))


@bp.route("/<int:matter_id>/facts", methods=["POST"])
@login_required
def facts(matter_id):
    m, c = _load(matter_id)
    f = request.form
    c.date_of_loss = parse_date(f.get("date_of_loss"))
    c.incident_type = f.get("incident_type", "") if f.get("incident_type", "") in dict(INCIDENT_TYPES) else ""
    c.incident_description = f.get("incident_description", "").strip()
    c.injuries = f.get("injuries", "").strip()
    ts = f.get("treatment_status", "treating")
    c.treatment_status = ts if ts in dict(TREATMENT_STATUSES) else "treating"
    c.insurer = f.get("insurer", "").strip()
    c.claim_number = f.get("claim_number", "").strip()
    c.adjuster_name = f.get("adjuster_name", "").strip()
    c.adjuster_phone = f.get("adjuster_phone", "").strip()
    c.adjuster_email = f.get("adjuster_email", "").strip()
    c.policy_limits_cents = parse_money(f.get("policy_limits"))
    c.um_uim_limits_cents = parse_money(f.get("um_uim_limits"))
    c.liability_notes = f.get("liability_notes", "").strip()
    st = f.get("stage", c.stage)
    c.stage = st if st in STAGE_KEYS else c.stage
    audit("pi_facts", "matter", m.id, f"stage {c.stage}", current_user().id)
    db.session.commit()
    flash("Case facts saved.", "ok")
    return redirect(url_for("pi.case", matter_id=m.id) + "#facts")


# ---- providers ----
def _fill_provider(p, f):
    p.name = f.get("name", "").strip()
    p.specialty = f.get("specialty", "").strip()
    p.phone = f.get("phone", "").strip()
    p.fax = f.get("fax", "").strip()
    p.email = f.get("email", "").strip()
    p.address = f.get("address", "").strip()
    p.first_visit_on = parse_date(f.get("first_visit_on"))
    p.last_visit_on = parse_date(f.get("last_visit_on"))
    p.records_requested_on = parse_date(f.get("records_requested_on"))
    p.records_received_on = parse_date(f.get("records_received_on"))
    p.bills_requested_on = parse_date(f.get("bills_requested_on"))
    p.bills_received_on = parse_date(f.get("bills_received_on"))
    p.total_billed_cents = parse_money(f.get("total_billed"))
    p.notes = f.get("notes", "").strip()


@bp.route("/<int:matter_id>/providers/new", methods=["GET", "POST"])
@login_required
def provider_new(matter_id):
    m, c = _load(matter_id)
    p = MedicalProvider(matter_id=m.id, name="")
    if request.method == "POST":
        _fill_provider(p, request.form)
        if not p.name:
            flash("The provider needs a name.", "error")
            return render_template("pi/provider_form.html", m=m, p=p, is_new=True)
        db.session.add(p)
        db.session.flush()
        audit("pi_provider_add", "matter", m.id, p.name, current_user().id)
        db.session.commit()
        flash(f"Added {p.name}.", "ok")
        return redirect(url_for("pi.case", matter_id=m.id) + "#providers")
    return render_template("pi/provider_form.html", m=m, p=p, is_new=True)


@bp.route("/<int:matter_id>/providers/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def provider_edit(matter_id, pid):
    m, c = _load(matter_id)
    p = MedicalProvider.query.filter_by(id=pid, matter_id=m.id).first() or abort(404)
    if request.method == "POST":
        _fill_provider(p, request.form)
        if not p.name:
            flash("The provider needs a name.", "error")
            return render_template("pi/provider_form.html", m=m, p=p, is_new=False)
        db.session.commit()
        flash(f"Saved {p.name}.", "ok")
        return redirect(url_for("pi.case", matter_id=m.id) + "#providers")
    return render_template("pi/provider_form.html", m=m, p=p, is_new=False)


@bp.route("/<int:matter_id>/providers/<int:pid>/delete", methods=["POST"])
@login_required
def provider_delete(matter_id, pid):
    m, c = _load(matter_id)
    p = MedicalProvider.query.filter_by(id=pid, matter_id=m.id).first() or abort(404)
    audit("pi_provider_delete", "matter", m.id, p.name, current_user().id)
    db.session.delete(p)
    db.session.commit()
    flash(f"Removed {p.name}.", "ok")
    return redirect(url_for("pi.case", matter_id=m.id) + "#providers")


def _request_letter(matter_id, pid, what):
    m, c = _load(matter_id)
    p = MedicalProvider.query.filter_by(id=pid, matter_id=m.id).first() or abort(404)
    pdf = build_request_letter(m, c, p, what)
    label = "Records request" if what == "records" else "Bills request"
    name = f"{label} - {p.name} - {m.number}.pdf"
    doc = save_pdf_document(m, pdf, name, "Medical records", current_user())
    if what == "records":
        p.records_requested_on = date.today()
    else:
        p.bills_requested_on = date.today()
    if c.stage in ("intake", "treating"):
        c.stage = "records"
    audit("pi_request_" + what, "matter", m.id, p.name, current_user().id)
    db.session.commit()
    flash(f"{label} letter for {p.name} saved to Documents (Medical records). Attach the signed HIPAA "
          f"authorization before sending.", "ok")
    return redirect(url_for("documents.download", id=doc.id))


@bp.route("/<int:matter_id>/providers/<int:pid>/request-records", methods=["POST"])
@login_required
def provider_request_records(matter_id, pid):
    return _request_letter(matter_id, pid, "records")


@bp.route("/<int:matter_id>/providers/<int:pid>/request-bills", methods=["POST"])
@login_required
def provider_request_bills(matter_id, pid):
    return _request_letter(matter_id, pid, "bills")


# ---- liens ----
def _fill_lien(l, f):
    l.holder = f.get("holder", "").strip()
    t = f.get("type", "medical")
    l.type = t if t in LIEN_TYPES else "other"
    l.original_cents = parse_money(f.get("original"))
    reduced = (f.get("reduced") or "").strip()
    l.reduced_cents = parse_money(reduced) if reduced else None
    s = f.get("status", "open")
    l.status = s if s in LIEN_STATUSES else "open"
    l.contact = f.get("contact", "").strip()
    l.notes = f.get("notes", "").strip()


@bp.route("/<int:matter_id>/liens/new", methods=["GET", "POST"])
@login_required
def lien_new(matter_id):
    m, c = _load(matter_id)
    l = Lien(matter_id=m.id, holder="")
    if request.method == "POST":
        _fill_lien(l, request.form)
        if not l.holder:
            flash("The lien needs a holder.", "error")
            return render_template("pi/lien_form.html", m=m, l=l, is_new=True, lien_types=LIEN_TYPES,
                                   lien_statuses=LIEN_STATUSES)
        db.session.add(l)
        db.session.flush()
        audit("pi_lien_add", "matter", m.id, f"{l.holder} {cents_to_str(l.original_cents)}", current_user().id)
        db.session.commit()
        flash(f"Added lien from {l.holder}.", "ok")
        return redirect(url_for("pi.case", matter_id=m.id) + "#liens")
    return render_template("pi/lien_form.html", m=m, l=l, is_new=True, lien_types=LIEN_TYPES,
                           lien_statuses=LIEN_STATUSES)


@bp.route("/<int:matter_id>/liens/<int:lid>/edit", methods=["GET", "POST"])
@login_required
def lien_edit(matter_id, lid):
    m, c = _load(matter_id)
    l = Lien.query.filter_by(id=lid, matter_id=m.id).first() or abort(404)
    if request.method == "POST":
        _fill_lien(l, request.form)
        if not l.holder:
            flash("The lien needs a holder.", "error")
            return render_template("pi/lien_form.html", m=m, l=l, is_new=False, lien_types=LIEN_TYPES,
                                   lien_statuses=LIEN_STATUSES)
        db.session.commit()
        flash(f"Saved lien from {l.holder}.", "ok")
        return redirect(url_for("pi.case", matter_id=m.id) + "#liens")
    return render_template("pi/lien_form.html", m=m, l=l, is_new=False, lien_types=LIEN_TYPES,
                           lien_statuses=LIEN_STATUSES)


@bp.route("/<int:matter_id>/liens/<int:lid>/delete", methods=["POST"])
@login_required
def lien_delete(matter_id, lid):
    m, c = _load(matter_id)
    l = Lien.query.filter_by(id=lid, matter_id=m.id).first() or abort(404)
    audit("pi_lien_delete", "matter", m.id, l.holder, current_user().id)
    db.session.delete(l)
    db.session.commit()
    flash(f"Removed the lien from {l.holder}.", "ok")
    return redirect(url_for("pi.case", matter_id=m.id) + "#liens")


@bp.route("/<int:matter_id>/liens/<int:lid>/reduction-letter", methods=["POST"])
@login_required
def lien_reduction_letter(matter_id, lid):
    m, c = _load(matter_id)
    l = Lien.query.filter_by(id=lid, matter_id=m.id).first() or abort(404)
    pct = _float(request.form.get("pct"), -1)
    if not 0 < pct < 100:
        flash("Enter a reduction percentage between 0 and 100.", "error")
        return redirect(url_for("pi.case", matter_id=m.id) + "#liens")
    proposed = int(round(int(l.original_cents or 0) * (100.0 - pct) / 100.0))
    pdf = build_reduction_letter(m, c, l, pct, proposed)
    name = f"Lien reduction request - {l.holder} - {m.number}.pdf"
    doc = save_pdf_document(m, pdf, name, "Liens", current_user())
    if l.status == "open":
        l.status = "negotiating"
    audit("pi_lien_reduction_letter", "matter", m.id, f"{l.holder} {pct:g}% to {cents_to_str(proposed)}",
          current_user().id)
    db.session.commit()
    flash(f"Reduction request to {l.holder} ({pct:g}%, proposing {cents_to_str(proposed)}) saved to Documents "
          f"(Liens). The lien stays at its current figure until the holder confirms.", "ok")
    return redirect(url_for("documents.download", id=doc.id))


# ---- demand ----
@bp.route("/<int:matter_id>/demand", methods=["POST"])
@login_required
def demand(matter_id):
    m, c = _load(matter_id)
    c.demand_sent_on = parse_date(request.form.get("demand_sent_on"))
    c.demand_amount_cents = parse_money(request.form.get("demand_amount"))
    c.offer_cents = parse_money(request.form.get("offer"))
    audit("pi_demand", "matter", m.id,
          f"demand {cents_to_str(c.demand_amount_cents)} offer {cents_to_str(c.offer_cents)}", current_user().id)
    db.session.commit()
    flash("Demand and offer saved.", "ok")
    return redirect(url_for("pi.case", matter_id=m.id) + "#demand")


@bp.route("/<int:matter_id>/demand/package", methods=["POST"])
@login_required
def demand_package(matter_id):
    m, c = _load(matter_id)
    amount = parse_money(request.form.get("demand_amount")) or c.demand_amount_cents or 0
    if amount <= 0:
        flash("Enter the demand amount before building the package.", "error")
        return redirect(url_for("pi.case", matter_id=m.id) + "#demand")
    c.demand_amount_cents = amount
    providers = _providers(m)
    pdf = build_demand_package(m, c, providers, amount)
    name = f"Demand package - {m.number}.pdf"
    doc = save_pdf_document(m, pdf, name, "Demand", current_user())
    if request.form.get("mark_sent"):
        c.demand_sent_on = date.today()
        if c.stage in ("intake", "treating", "records"):
            c.stage = "demand"
    audit("pi_demand_package", "matter", m.id,
          f"{cents_to_str(amount)}{' marked sent' if request.form.get('mark_sent') else ''}", current_user().id)
    db.session.commit()
    flash(f"Demand package saved to Documents (Demand).{' Demand marked as sent today.' if c.demand_sent_on == date.today() and request.form.get('mark_sent') else ''}", "ok")
    return redirect(url_for("documents.download", id=doc.id))


# ---- settlement worksheet ----
@bp.route("/<int:matter_id>/worksheet", methods=["POST"])
@login_required
def worksheet_new(matter_id):
    m, c = _load(matter_id)
    gross = parse_money(request.form.get("gross"))
    if gross <= 0:
        flash("Enter the gross settlement amount.", "error")
        return redirect(url_for("pi.case", matter_id=m.id) + "#settlement")
    pct = _float(request.form.get("fee_pct"), default_fee_pct(m))
    if not 0 <= pct <= 100:
        flash("The fee percentage must be between 0 and 100.", "error")
        return redirect(url_for("pi.case", matter_id=m.id) + "#settlement")
    extras = []
    for desc, amt in zip(request.form.getlist("extra_desc"), request.form.getlist("extra_amount")):
        cents = parse_money(amt)
        if cents > 0:
            extras.append((desc.strip() or "Other cost", cents))
    other = parse_money(request.form.get("other_deductions"))
    d = compute_worksheet(m, gross, pct, extras, other)
    d["other_payee"] = request.form.get("other_payee", "").strip()
    for prev in SettlementWorksheet.query.filter_by(matter_id=m.id, is_current=True).all():
        prev.is_current = False
    ws = SettlementWorksheet(matter_id=m.id, gross_cents=d["gross"], fee_pct=pct, fee_cents=d["fee"],
                             costs_cents=d["costs"], liens_cents=d["liens_total"], other_deductions_cents=d["other"],
                             net_to_client_cents=d["net"], detail_json=json.dumps(d), status="draft",
                             is_current=True, created_by_id=current_user().id)
    db.session.add(ws)
    db.session.flush()
    audit("pi_worksheet", "matter", m.id,
          f"gross {cents_to_str(d['gross'])} net {cents_to_str(d['net'])}", current_user().id)
    db.session.commit()
    if d["net"] < 0:
        flash("Saved, but the net to client is negative. Reduce liens or costs before approving.", "error")
    else:
        flash("Settlement worksheet saved. Older worksheets are kept below.", "ok")
    return redirect(url_for("pi.case", matter_id=m.id) + "#settlement")


def _ws(matter_id, wid):
    m, c = _load(matter_id)
    ws = SettlementWorksheet.query.filter_by(id=wid, matter_id=m.id).first() or abort(404)
    return m, c, ws


@bp.route("/<int:matter_id>/worksheet/<int:wid>/approve", methods=["POST"])
@login_required
def worksheet_approve(matter_id, wid):
    m, c, ws = _ws(matter_id, wid)
    if not ws.is_current:
        flash("Only the current worksheet can be approved.", "error")
    elif ws.status != "draft":
        flash(f"This worksheet is already {ws.status}.", "error")
    elif ws.net_to_client_cents < 0:
        flash("The net to client is negative. Fix the worksheet before approving it.", "error")
    else:
        ws.status = "approved"
        ws.approved_on = date.today()
        audit("pi_worksheet_approve", "matter", m.id, f"worksheet {ws.id}", current_user().id)
        db.session.commit()
        flash("Worksheet approved.", "ok")
    return redirect(url_for("pi.case", matter_id=m.id) + "#settlement")


@bp.route("/<int:matter_id>/worksheet/<int:wid>/disburse", methods=["POST"])
@login_required
def worksheet_disburse(matter_id, wid):
    m, c, ws = _ws(matter_id, wid)
    back = redirect(url_for("pi.case", matter_id=m.id) + "#settlement")
    if not ws.is_current or ws.status != "approved":
        flash("Approve the current worksheet before marking it disbursed.", "error")
        return back
    d = _detail(ws)
    firm = Firm.get()
    client = m.client
    today = date.today()
    uid = current_user().id
    ref = f"WS-{ws.id}"
    rows = []
    if request.form.get("record_deposit"):
        rows.append(("deposit", ws.gross_cents, f"Settlement proceeds, {m.number}", c.insurer or "Settlement"))
    if ws.fee_cents:
        rows.append(("to_operating", -ws.fee_cents, f"Attorney fee ({ws.fee_pct:g}%), {m.number}", firm.name))
    if ws.costs_cents:
        rows.append(("to_operating", -ws.costs_cents, f"Case costs reimbursed, {m.number}", firm.name))
    for l in d.get("liens", []):
        if l.get("cents"):
            rows.append(("disbursement", -int(l["cents"]), f"Lien payoff ({l.get('type', '').replace('_', ' ')}), "
                                                           f"{m.number}", l.get("holder", "")))
    if ws.other_deductions_cents:
        rows.append(("disbursement", -ws.other_deductions_cents, f"Other deductions, {m.number}",
                     d.get("other_payee") or "Other"))
    if ws.net_to_client_cents:
        rows.append(("disbursement", -ws.net_to_client_cents, f"Net settlement to client, {m.number}",
                     client.display_name))
    # Validate the whole run against the trust rules before writing anything.
    running_c = client.trust_balance_cents()
    running_m = m.trust_balance_cents()
    for ttype, delta, desc, payee in rows:
        if delta < 0:
            if running_c + delta < 0:
                flash(f"Refused: {client.display_name} holds {cents_to_str(running_c)} in trust at that point and "
                      f"the {desc.lower()} of {cents_to_str(-delta)} would overdraw the client. Tick 'record the "
                      f"settlement deposit' or deposit the funds on the trust page first.", "error")
                return back
            if running_m + delta < 0:
                flash(f"Refused: {m.label} holds {cents_to_str(running_m)} in trust at that point and the "
                      f"{desc.lower()} of {cents_to_str(-delta)} would overdraw the matter.", "error")
                return back
        running_c += delta
        running_m += delta
    for ttype, delta, desc, payee in rows:
        t = TrustTransaction(client_id=client.id, matter_id=m.id, date=today, type=ttype, amount_cents=delta,
                             description=desc, payee=payee or "", reference=ref, cleared=False, created_by_id=uid)
        db.session.add(t)
        db.session.flush()
        audit("trust_" + ttype, "trust_transaction", t.id, f"{client.display_name} {cents_to_str(delta)} {desc}", uid)
    for l in d.get("liens", []):
        lien = db.session.get(Lien, l.get("id") or 0)
        if lien and lien.matter_id == m.id and l.get("cents"):
            lien.status = "paid"
    ws.status = "disbursed"
    ws.disbursed_on = today
    if c.stage not in ("settled", "closed"):
        c.stage = "settled"
    audit("pi_worksheet_disburse", "matter", m.id, f"worksheet {ws.id}, {len(rows)} trust rows", uid)
    db.session.commit()
    flash(f"Disbursement recorded: {len(rows)} trust ledger rows for {client.display_name}.", "ok")
    return back


@bp.route("/<int:matter_id>/worksheet/<int:wid>/pdf", methods=["POST"])
@login_required
def worksheet_pdf(matter_id, wid):
    m, c, ws = _ws(matter_id, wid)
    pdf = build_worksheet_pdf(m, ws)
    name = f"Settlement worksheet {ws.id} - {m.number}.pdf"
    doc = save_pdf_document(m, pdf, name, "Settlement", current_user())
    ws.pdf_path = doc.path
    db.session.commit()
    flash("Worksheet PDF saved to Documents (Settlement).", "ok")
    return redirect(url_for("documents.download", id=doc.id))


# ---- standard tasks ----
def standard_tasks(m, c, assignee_id=None):
    """Task specs for the matter as [(title, kind, due, notes)]. Needs a date of loss."""
    dol = c.date_of_loss
    if not dol:
        return []
    specs = []
    providers = _providers(m)
    if providers:
        for p in providers:
            specs.append((f"Request records: {p.name}", "task", dol + timedelta(days=30),
                          "Send the records request with the signed HIPAA authorization."))
            if p.records_requested_on:
                specs.append((f"Follow up on records request: {p.name}", "task",
                              p.records_requested_on + timedelta(days=30),
                              "Call the records custodian if nothing has arrived."))
    else:
        specs.append(("Request medical records from treating providers", "task", dol + timedelta(days=30),
                      "Add each provider to the PI case, then send a records request per provider."))
    try:
        sol = dol.replace(year=dol.year + 2)
    except ValueError:  # Feb 29
        sol = dol + timedelta(days=730)
    specs.append(("Statute of limitations check", "deadline", sol,
                  "Placeholder at two years from the date of loss. Confirm this state's limitations period for "
                  "this claim type (and any notice deadline for a government defendant) and correct the date."))
    if c.demand_sent_on:
        specs.append(("Demand follow-up", "task", c.demand_sent_on + timedelta(days=30),
                      "No response to the demand within 30 days: call the adjuster."))
    return specs


@bp.route("/<int:matter_id>/tasks/standard", methods=["POST"])
@login_required
def tasks_standard(matter_id):
    m, c = _load(matter_id)
    if not c.date_of_loss:
        flash("Enter the date of loss first; the standard tasks are dated from it.", "error")
        return redirect(url_for("pi.case", matter_id=m.id) + "#facts")
    existing = {t.title for t in Task.query.filter_by(matter_id=m.id).all()}
    assignee = m.responsible_user_id or current_user().id
    added = 0
    for title, kind, due, notes in standard_tasks(m, c):
        if title in existing:
            continue
        t = Task(matter_id=m.id, title=title, kind=kind, due_on=due, priority="high" if kind == "deadline" else "normal",
                 assignee_id=assignee, notes=notes)
        db.session.add(t)
        db.session.flush()
        audit("add_task", "matter", m.id, title, current_user().id)
        existing.add(title)
        added += 1
    db.session.commit()
    flash(f"Added {added} standard PI task(s)." if added else "Every standard PI task already exists on this matter.",
          "ok")
    return redirect(url_for("pi.case", matter_id=m.id) + "#tasks")
