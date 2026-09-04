"""All persistence models. Money is stored as integer cents. Dates are naive UTC."""
from datetime import datetime, date
import json
import secrets
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func
from .extensions import db


def now():
    return datetime.utcnow()


def new_token(n=32):
    return secrets.token_urlsafe(n)


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    password_hash = db.Column(db.String(300), nullable=False)
    # owner | attorney | paralegal | billing | readonly  ("staff" is accepted as a legacy alias for attorney)
    role = db.Column(db.String(20), default="owner")
    hourly_rate_cents = db.Column(db.Integer, default=0)
    # What an hour of this person costs the firm (salary + overhead). Drives matter profitability.
    cost_rate_cents = db.Column(db.Integer, default=0)
    initials = db.Column(db.String(6), default="")
    is_active = db.Column(db.Boolean, default=True)
    office_id = db.Column(db.Integer, db.ForeignKey("offices.id"))
    # JSON list of dashboard card keys the user wants, in order. Empty = default set.
    dashboard_json = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=now)
    office = db.relationship("Office", foreign_keys=[office_id])

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Firm(db.Model):
    """Single row (id=1) holding firm settings."""
    __tablename__ = "firm"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), default="My Law Firm")
    address = db.Column(db.Text, default="")
    phone = db.Column(db.String(50), default="")
    email = db.Column(db.String(200), default="")
    website = db.Column(db.String(200), default="")
    timezone = db.Column(db.String(60), default="America/Chicago")
    default_rate_cents = db.Column(db.Integer, default=30000)
    invoice_terms_days = db.Column(db.Integer, default=30)
    invoice_prefix = db.Column(db.String(10), default="INV-")
    next_invoice_number = db.Column(db.Integer, default=1001)
    matter_prefix = db.Column(db.String(10), default="M-")
    next_matter_number = db.Column(db.Integer, default=1001)
    invoice_footer = db.Column(db.Text, default="Thank you for your business.")
    # Card surcharge. Stored in basis points (300 = 3.00%). Never applied to ACH/eCheck.
    surcharge_enabled = db.Column(db.Boolean, default=False)
    surcharge_bps = db.Column(db.Integer, default=300)
    trust_bank_name = db.Column(db.String(200), default="IOLTA Trust Account")
    operating_bank_name = db.Column(db.String(200), default="Operating Account")
    trust_account_last4 = db.Column(db.String(4), default="")
    daily_agenda_email = db.Column(db.Boolean, default=True)
    currency = db.Column(db.String(3), default="USD")  # default for new matters
    # Interest on overdue invoices: annual rate in basis points (1200 = 12%), applied monthly after the grace period.
    interest_apr_bps = db.Column(db.Integer, default=0)
    interest_grace_days = db.Column(db.Integer, default=30)
    # When on, invoices built by non-owners go to pending_approval and an owner/billing user must approve before send.
    require_invoice_approval = db.Column(db.Boolean, default=False)
    ledes_firm_id = db.Column(db.String(40), default="")  # LAW_FIRM_ID in LEDES 1998B, usually the firm's tax id
    # Client-facing language default: en | es
    default_language = db.Column(db.String(5), default="en")

    @staticmethod
    def get():
        f = db.session.get(Firm, 1)
        if not f:
            f = Firm(id=1)
            db.session.add(f)
            db.session.commit()
        return f


class Office(db.Model):
    """A firm location. Users and matters may belong to one; invoices print the matter's office address."""
    __tablename__ = "offices"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    address = db.Column(db.Text, default="")
    phone = db.Column(db.String(50), default="")
    email = db.Column(db.String(200), default="")
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)


class Contact(db.Model):
    __tablename__ = "contacts"
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), default="person")  # person | company
    first_name = db.Column(db.String(100), default="")
    last_name = db.Column(db.String(100), default="")
    company_name = db.Column(db.String(200), default="")
    email = db.Column(db.String(200), default="")
    phone = db.Column(db.String(50), default="")
    address = db.Column(db.Text, default="")
    notes = db.Column(db.Text, default="")
    tags = db.Column(db.String(300), default="")
    is_client = db.Column(db.Boolean, default=False)
    aliases = db.Column(db.Text, default="")  # newline-separated other names (maiden, DBA, etc.)
    created_at = db.Column(db.DateTime, default=now)
    custom_fields_json = db.Column(db.Text, default="{}")
    language = db.Column(db.String(5), default="")  # "" = firm default; en | es for client-facing pages and emails
    ledes_client_id = db.Column(db.String(40), default="")  # CLIENT_ID the carrier assigns

    matters = db.relationship("Matter", back_populates="client", foreign_keys="Matter.client_id")

    @property
    def custom_fields(self):
        try:
            return json.loads(self.custom_fields_json or "{}")
        except Exception:
            return {}

    @custom_fields.setter
    def custom_fields(self, d):
        self.custom_fields_json = json.dumps(d or {})

    @property
    def display_name(self):
        if self.kind == "company":
            return self.company_name or "(unnamed company)"
        n = f"{self.first_name} {self.last_name}".strip()
        return n or self.company_name or "(unnamed)"

    @property
    def sort_name(self):
        if self.kind == "company":
            return self.company_name
        return f"{self.last_name}, {self.first_name}".strip(", ")

    def trust_balance_cents(self):
        v = db.session.query(func.coalesce(func.sum(TrustTransaction.amount_cents), 0)).filter(
            TrustTransaction.client_id == self.id).scalar()
        return int(v or 0)


class Matter(db.Model):
    __tablename__ = "matters"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True)
    client_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=False)
    name = db.Column(db.String(300), nullable=False)
    practice_area = db.Column(db.String(100), default="")
    status = db.Column(db.String(20), default="open")  # pending | open | closed
    opened_on = db.Column(db.Date, default=date.today)
    closed_on = db.Column(db.Date)
    responsible_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    billing_type = db.Column(db.String(20), default="flat")  # flat | hourly | contingency | hybrid
    hourly_rate_cents = db.Column(db.Integer, default=0)  # 0 = use user/firm default
    flat_fee_cents = db.Column(db.Integer, default=0)
    contingency_pct = db.Column(db.Float, default=0.0)
    description = db.Column(db.Text, default="")
    custom_fields_json = db.Column(db.Text, default="{}")
    sol_date = db.Column(db.Date)  # statute of limitations deadline
    sol_basis = db.Column(db.String(200), default="")
    court = db.Column(db.String(200), default="")
    case_number = db.Column(db.String(100), default="")
    created_at = db.Column(db.DateTime, default=now)
    office_id = db.Column(db.Integer, db.ForeignKey("offices.id"))
    originating_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))  # who brought the work in
    currency = db.Column(db.String(3), default="")  # "" = firm default
    # Evergreen retainer: when the matter's trust balance falls below the minimum, ask the client to top up to the target.
    trust_minimum_cents = db.Column(db.Integer, default=0)
    trust_replenish_to_cents = db.Column(db.Integer, default=0)
    ledes_matter_id = db.Column(db.String(40), default="")  # CLIENT_MATTER_ID the carrier assigns
    template_id = db.Column(db.Integer, db.ForeignKey("matter_templates.id"))

    client = db.relationship("Contact", back_populates="matters", foreign_keys=[client_id])
    responsible = db.relationship("User", foreign_keys=[responsible_user_id])
    originator = db.relationship("User", foreign_keys=[originating_user_id])
    office = db.relationship("Office", foreign_keys=[office_id])
    template = db.relationship("MatterTemplate", foreign_keys=[template_id])
    payers = db.relationship("MatterPayer", back_populates="matter", cascade="all, delete-orphan")
    parties = db.relationship("MatterParty", back_populates="matter", cascade="all, delete-orphan")
    milestones = db.relationship("FlatFeeMilestone", back_populates="matter", cascade="all, delete-orphan",
                                 order_by="FlatFeeMilestone.sort")
    time_entries = db.relationship("TimeEntry", back_populates="matter")
    expenses = db.relationship("Expense", back_populates="matter")
    invoices = db.relationship("Invoice", back_populates="matter")
    tasks = db.relationship("Task", back_populates="matter")
    documents = db.relationship("Document", back_populates="matter")
    notes = db.relationship("Note", back_populates="matter", order_by="Note.created_at.desc()")

    @property
    def custom_fields(self):
        try:
            return json.loads(self.custom_fields_json or "{}")
        except Exception:
            return {}

    @custom_fields.setter
    def custom_fields(self, d):
        self.custom_fields_json = json.dumps(d or {})

    @property
    def label(self):
        return f"{self.number} {self.name}"

    @property
    def currency_code(self):
        return self.currency or Firm.get().currency or "USD"

    def effective_rate_cents(self, user=None):
        if self.hourly_rate_cents:
            return self.hourly_rate_cents
        if user and user.hourly_rate_cents:
            return user.hourly_rate_cents
        return Firm.get().default_rate_cents

    def unbilled_time_cents(self):
        return sum(t.amount_cents for t in self.time_entries if t.billable and t.invoice_id is None)

    def unbilled_expense_cents(self):
        return sum(e.amount_cents for e in self.expenses if e.billable and e.invoice_id is None)

    def trust_balance_cents(self):
        v = db.session.query(func.coalesce(func.sum(TrustTransaction.amount_cents), 0)).filter(
            TrustTransaction.matter_id == self.id).scalar()
        return int(v or 0)

    def outstanding_cents(self):
        return sum(i.balance_cents for i in self.invoices if i.status not in ("draft", "void", "paid"))


class MatterPayer(db.Model):
    """Split billing: who pays what share of a matter's invoices. Absent = the client pays 100%."""
    __tablename__ = "matter_payers"
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=False)
    percent = db.Column(db.Float, default=100.0)
    label = db.Column(db.String(120), default="")  # e.g. "Insurer", "Co-defendant"
    matter = db.relationship("Matter", back_populates="payers")
    contact = db.relationship("Contact")


class MatterTemplate(db.Model):
    """Practice-area template: billing defaults, milestone schedule, task workflow, custom fields."""
    __tablename__ = "matter_templates"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    practice_area = db.Column(db.String(100), default="")
    description = db.Column(db.Text, default="")
    billing_type = db.Column(db.String(20), default="flat")
    hourly_rate_cents = db.Column(db.Integer, default=0)
    flat_fee_cents = db.Column(db.Integer, default=0)
    contingency_pct = db.Column(db.Float, default=0.0)
    # [{"description": "...", "amount_cents": 0, "due_offset_days": null}]
    milestones_json = db.Column(db.Text, default="[]")
    # [{"title": "...", "kind": "task|deadline|court_date", "offset_days": 7, "priority": "normal", "assignee": "responsible|none"}]
    tasks_json = db.Column(db.Text, default="[]")
    # {"Field name": "default value"}
    custom_fields_json = db.Column(db.Text, default="{}")
    sol_years = db.Column(db.Float, default=0.0)  # 0 = do not set; otherwise sol_date = opened_on + years
    sol_basis = db.Column(db.String(200), default="")
    trust_minimum_cents = db.Column(db.Integer, default=0)
    trust_replenish_to_cents = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)

    def _load(self, attr, default):
        try:
            return json.loads(getattr(self, attr) or "") or default
        except Exception:
            return default

    @property
    def milestones(self):
        return self._load("milestones_json", [])

    @property
    def tasks(self):
        return self._load("tasks_json", [])

    @property
    def custom_fields(self):
        return self._load("custom_fields_json", {})


class MatterParty(db.Model):
    """Everyone connected to a matter other than the client: adverse parties, witnesses, co-counsel."""
    __tablename__ = "matter_parties"
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"))
    name = db.Column(db.String(300), nullable=False)
    role = db.Column(db.String(30), default="adverse")  # adverse | witness | co_counsel | opposing_counsel | other
    notes = db.Column(db.String(300), default="")
    matter = db.relationship("Matter", back_populates="parties")
    contact = db.relationship("Contact")


class FlatFeeMilestone(db.Model):
    __tablename__ = "flat_fee_milestones"
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"), nullable=False)
    description = db.Column(db.String(300), nullable=False)
    amount_cents = db.Column(db.Integer, default=0)
    due_on = db.Column(db.Date)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    sort = db.Column(db.Integer, default=0)
    matter = db.relationship("Matter", back_populates="milestones")
    invoice = db.relationship("Invoice", foreign_keys=[invoice_id])

    @property
    def invoiced(self):
        return self.invoice_id is not None


class ConflictCheck(db.Model):
    __tablename__ = "conflict_checks"
    id = db.Column(db.Integer, primary_key=True)
    run_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    query = db.Column(db.Text, nullable=False)  # newline-separated names searched
    results_json = db.Column(db.Text, default="[]")
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"))
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"))
    outcome = db.Column(db.String(20), default="clear")  # clear | conflict | waived | unresolved
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=now)
    run_by = db.relationship("User")
    matter = db.relationship("Matter")

    @property
    def results(self):
        try:
            return json.loads(self.results_json or "[]")
        except Exception:
            return []


class TimeEntry(db.Model):
    __tablename__ = "time_entries"
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    date = db.Column(db.Date, default=date.today, nullable=False)
    minutes = db.Column(db.Integer, default=0, nullable=False)
    description = db.Column(db.Text, default="")
    rate_cents = db.Column(db.Integer, default=0)
    billable = db.Column(db.Boolean, default=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    activity_code = db.Column(db.String(20), default="")  # UTBMS activity code, e.g. A104
    task_code = db.Column(db.String(20), default="")  # UTBMS task code, e.g. L110
    created_at = db.Column(db.DateTime, default=now)
    matter = db.relationship("Matter", back_populates="time_entries")
    user = db.relationship("User")
    invoice = db.relationship("Invoice", foreign_keys=[invoice_id], back_populates="time_entries")

    @property
    def hours(self):
        return round(self.minutes / 60.0, 2)

    @property
    def amount_cents(self):
        return int(round(self.minutes * self.rate_cents / 60.0))


class Timer(db.Model):
    """A running timer. One per user."""
    __tablename__ = "timers"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"))
    description = db.Column(db.Text, default="")
    started_at = db.Column(db.DateTime, default=now)
    accumulated_seconds = db.Column(db.Integer, default=0)
    paused = db.Column(db.Boolean, default=False)
    matter = db.relationship("Matter")

    def elapsed_seconds(self):
        base = self.accumulated_seconds or 0
        if not self.paused and self.started_at:
            base += int((now() - self.started_at).total_seconds())
        return base


class Expense(db.Model):
    __tablename__ = "expenses"
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    date = db.Column(db.Date, default=date.today)
    description = db.Column(db.Text, default="")
    category = db.Column(db.String(60), default="")
    amount_cents = db.Column(db.Integer, default=0)
    billable = db.Column(db.Boolean, default=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    receipt_path = db.Column(db.String(400), default="")
    expense_code = db.Column(db.String(20), default="")  # UTBMS expense code, e.g. E101
    created_at = db.Column(db.DateTime, default=now)
    matter = db.relationship("Matter", back_populates="expenses")
    invoice = db.relationship("Invoice", foreign_keys=[invoice_id], back_populates="expenses")


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=False)
    kind = db.Column(db.String(20), default="flat")  # flat | hourly | hybrid | contingency
    status = db.Column(db.String(20), default="draft")  # draft | sent | viewed | partial | paid | void
    issued_on = db.Column(db.Date, default=date.today)
    due_on = db.Column(db.Date)
    subtotal_cents = db.Column(db.Integer, default=0)
    tax_cents = db.Column(db.Integer, default=0)
    total_cents = db.Column(db.Integer, default=0)
    paid_cents = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text, default="")
    public_token = db.Column(db.String(80), unique=True, default=new_token)
    sent_at = db.Column(db.DateTime)
    sent_to = db.Column(db.String(200), default="")
    first_viewed_at = db.Column(db.DateTime)
    view_count = db.Column(db.Integer, default=0)
    pdf_path = db.Column(db.String(400), default="")
    created_at = db.Column(db.DateTime, default=now)
    # Approval workflow: none | pending | approved | rejected. Only matters when Firm.require_invoice_approval.
    approval_status = db.Column(db.String(20), default="none")
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    approval_note = db.Column(db.String(300), default="")
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    # Split billing: this invoice is payer_contact's share (split_pct) of the matter's charges. NULL = the client, 100%.
    payer_contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"))
    split_pct = db.Column(db.Float, default=100.0)
    split_group = db.Column(db.String(40), default="")  # same value on every invoice produced by one split build
    interest_cents = db.Column(db.Integer, default=0)  # total interest added so far (also present as lines)
    last_interest_on = db.Column(db.Date)
    currency = db.Column(db.String(3), default="USD")
    ledes_exported_at = db.Column(db.DateTime)

    matter = db.relationship("Matter", back_populates="invoices")
    client = db.relationship("Contact", foreign_keys=[client_id])
    payer = db.relationship("Contact", foreign_keys=[payer_contact_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    lines = db.relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan",
                            order_by="InvoiceLine.sort")
    time_entries = db.relationship("TimeEntry", back_populates="invoice", foreign_keys="TimeEntry.invoice_id")
    expenses = db.relationship("Expense", back_populates="invoice", foreign_keys="Expense.invoice_id")
    payments = db.relationship("Payment", back_populates="invoice")
    events = db.relationship("InvoiceEvent", back_populates="invoice", cascade="all, delete-orphan",
                             order_by="InvoiceEvent.created_at")

    @property
    def balance_cents(self):
        return max(0, (self.total_cents or 0) - (self.paid_cents or 0))

    def recalc(self):
        self.subtotal_cents = sum(l.amount_cents for l in self.lines)
        self.total_cents = self.subtotal_cents + (self.tax_cents or 0)
        self.paid_cents = sum(p.amount_cents for p in self.payments)
        if self.status in ("void", "draft"):
            return
        if self.paid_cents >= self.total_cents and self.total_cents > 0:
            self.status = "paid"
        elif self.paid_cents > 0:
            self.status = "partial"
        elif self.status == "paid":
            self.status = "sent"

    @property
    def is_overdue(self):
        return self.status in ("sent", "viewed", "partial") and self.due_on and self.due_on < date.today()


class InvoiceLine(db.Model):
    __tablename__ = "invoice_lines"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    kind = db.Column(db.String(20), default="flat")  # time | expense | flat | adjustment | discount | interest
    date = db.Column(db.Date)
    description = db.Column(db.Text, default="")
    quantity = db.Column(db.Float, default=1.0)  # hours for time lines, 1 otherwise
    unit_cents = db.Column(db.Integer, default=0)
    amount_cents = db.Column(db.Integer, default=0)
    time_entry_id = db.Column(db.Integer, db.ForeignKey("time_entries.id"))
    expense_id = db.Column(db.Integer, db.ForeignKey("expenses.id"))
    milestone_id = db.Column(db.Integer, db.ForeignKey("flat_fee_milestones.id"))
    sort = db.Column(db.Integer, default=0)
    invoice = db.relationship("Invoice", back_populates="lines")


class InvoiceEvent(db.Model):
    __tablename__ = "invoice_events"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    event = db.Column(db.String(30))  # sent | viewed | link_clicked | paid | reminder
    ip = db.Column(db.String(60), default="")
    ua = db.Column(db.String(300), default="")
    detail = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=now)
    invoice = db.relationship("Invoice", back_populates="events")


class Payment(db.Model):
    """Money received into operating (or applied from trust) against an invoice."""
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"))
    client_id = db.Column(db.Integer, db.ForeignKey("contacts.id"))
    amount_cents = db.Column(db.Integer, default=0)  # amount applied to the invoice (excludes surcharge)
    surcharge_cents = db.Column(db.Integer, default=0)
    stripe_fee_cents = db.Column(db.Integer, default=0)
    method = db.Column(db.String(20), default="card")  # card | ach | check | cash | wire | trust | other
    account = db.Column(db.String(20), default="operating")  # operating | trust
    stripe_payment_intent = db.Column(db.String(120), default="")
    stripe_checkout_session = db.Column(db.String(120), default="")
    reference = db.Column(db.String(120), default="")
    received_on = db.Column(db.Date, default=date.today)
    note = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=now)
    invoice = db.relationship("Invoice", back_populates="payments")
    client = db.relationship("Contact")
    matter = db.relationship("Matter")


class TrustTransaction(db.Model):
    """Client trust (IOLTA) ledger. amount_cents is signed: deposits positive, disbursements negative."""
    __tablename__ = "trust_transactions"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=False)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"))
    date = db.Column(db.Date, default=date.today, nullable=False)
    type = db.Column(db.String(30), nullable=False)  # deposit | disbursement | to_operating | refund | interest | bank_fee
    amount_cents = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(300), default="")
    payee = db.Column(db.String(200), default="")
    reference = db.Column(db.String(120), default="")  # check number, wire ref, Stripe id
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"))
    cleared = db.Column(db.Boolean, default=False)
    cleared_on = db.Column(db.Date)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=now)
    client = db.relationship("Contact")
    matter = db.relationship("Matter")
    invoice = db.relationship("Invoice")
    created_by = db.relationship("User")


class TrustReconciliation(db.Model):
    """Three-way reconciliation: bank statement = book balance = sum of client ledgers."""
    __tablename__ = "trust_reconciliations"
    id = db.Column(db.Integer, primary_key=True)
    period_end = db.Column(db.Date, nullable=False)
    bank_statement_cents = db.Column(db.Integer, default=0)
    book_balance_cents = db.Column(db.Integer, default=0)
    client_ledgers_cents = db.Column(db.Integer, default=0)
    outstanding_deposits_cents = db.Column(db.Integer, default=0)
    outstanding_disbursements_cents = db.Column(db.Integer, default=0)
    adjusted_bank_cents = db.Column(db.Integer, default=0)
    balanced = db.Column(db.Boolean, default=False)
    detail_json = db.Column(db.Text, default="{}")
    notes = db.Column(db.Text, default="")
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=now)
    created_by = db.relationship("User")


class Task(db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"))
    title = db.Column(db.String(300), nullable=False)
    kind = db.Column(db.String(20), default="task")  # task | deadline | court_date
    due_on = db.Column(db.Date)
    priority = db.Column(db.String(10), default="normal")  # low | normal | high
    assignee_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    done = db.Column(db.Boolean, default=False)
    done_at = db.Column(db.DateTime)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=now)
    matter = db.relationship("Matter", back_populates="tasks")
    assignee = db.relationship("User")

    @property
    def is_overdue(self):
        return (not self.done) and self.due_on and self.due_on < date.today()


class CalendarEvent(db.Model):
    __tablename__ = "calendar_events"
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"))
    title = db.Column(db.String(300), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime)
    all_day = db.Column(db.Boolean, default=False)
    location = db.Column(db.String(300), default="")
    notes = db.Column(db.Text, default="")
    uid = db.Column(db.String(80), default=new_token)
    created_at = db.Column(db.DateTime, default=now)
    # Whose calendar this sits on. NULL = firm-wide (everyone sees it).
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    # none | daily | weekly | biweekly | monthly | yearly, expanded on the fly in the month view and as RRULE in ICS.
    recurrence = db.Column(db.String(12), default="none")
    recurrence_until = db.Column(db.Date)
    matter = db.relationship("Matter")
    user = db.relationship("User")

    def occurrences(self, start, end):
        """Yield occurrence start datetimes within [start, end) for this event, expanding recurrence."""
        from dateutil.relativedelta import relativedelta
        if not self.starts_at:
            return
        step = {"daily": relativedelta(days=1), "weekly": relativedelta(weeks=1), "biweekly": relativedelta(weeks=2),
                "monthly": relativedelta(months=1), "yearly": relativedelta(years=1)}.get(self.recurrence or "none")
        if not step:
            if start <= self.starts_at < end:
                yield self.starts_at
            return
        until = datetime.combine(self.recurrence_until, datetime.max.time()) if self.recurrence_until else None
        cur, n = self.starts_at, 0
        while cur < end and n < 1000:
            if until and cur > until:
                return
            if cur >= start:
                yield cur
            cur = self.starts_at + step * (n + 1)
            n += 1


class Document(db.Model):
    __tablename__ = "documents"
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"), nullable=False)
    name = db.Column(db.String(300), nullable=False)
    path = db.Column(db.String(500), nullable=False)
    size = db.Column(db.Integer, default=0)
    mime = db.Column(db.String(120), default="")
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    shared_to_portal = db.Column(db.Boolean, default=False)
    uploaded_by_client = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)
    # Plain text pulled out of txt/md/csv/docx/pdf at upload so conflict checks can search inside files.
    extracted_text = db.Column(db.Text, default="")
    matter = db.relationship("Matter", back_populates="documents")
    uploaded_by = db.relationship("User")


class Note(db.Model):
    __tablename__ = "notes"
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"))
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now)
    matter = db.relationship("Matter", back_populates="notes")
    user = db.relationship("User")


class IntakeLead(db.Model):
    """Public intake form submission. Converts into Contact + Matter + Engagement in one step."""
    __tablename__ = "intake_leads"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), default="")
    phone = db.Column(db.String(50), default="")
    matter_type = db.Column(db.String(100), default="")
    description = db.Column(db.Text, default="")
    adverse_party = db.Column(db.String(300), default="")
    source = db.Column(db.String(100), default="web")
    status = db.Column(db.String(20), default="new")  # new | contacted | converted | declined
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"))
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"))
    conflict_check_id = db.Column(db.Integer, db.ForeignKey("conflict_checks.id"))
    created_at = db.Column(db.DateTime, default=now)
    contact = db.relationship("Contact")
    matter = db.relationship("Matter")
    conflict_check = db.relationship("ConflictCheck")


class LetterTemplate(db.Model):
    __tablename__ = "letter_templates"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    kind = db.Column(db.String(30), default="engagement")  # engagement | declination | general
    subject = db.Column(db.String(300), default="")
    body_html = db.Column(db.Text, default="")  # Jinja-style merge fields: {{ client_name }}, {{ fee_summary }} ...
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)


class Engagement(db.Model):
    """An engagement letter sent for click-to-sign, with open and signature tracking."""
    __tablename__ = "engagements"
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey("letter_templates.id"))
    subject = db.Column(db.String(300), default="")
    body_html = db.Column(db.Text, default="")  # fully rendered
    token = db.Column(db.String(80), unique=True, default=new_token)
    status = db.Column(db.String(20), default="draft")  # draft | sent | viewed | signed | declined | void
    sent_at = db.Column(db.DateTime)
    sent_to = db.Column(db.String(200), default="")
    first_viewed_at = db.Column(db.DateTime)
    view_count = db.Column(db.Integer, default=0)
    signed_at = db.Column(db.DateTime)
    signer_name = db.Column(db.String(200), default="")
    signer_email = db.Column(db.String(200), default="")
    signer_ip = db.Column(db.String(60), default="")
    signer_ua = db.Column(db.String(300), default="")
    document_hash = db.Column(db.String(80), default="")  # sha256 of body_html at send time
    signature_hash = db.Column(db.String(80), default="")  # sha256 over doc hash + signer + timestamp
    pdf_path = db.Column(db.String(400), default="")
    created_at = db.Column(db.DateTime, default=now)
    matter = db.relationship("Matter")
    contact = db.relationship("Contact")
    template = db.relationship("LetterTemplate")
    events = db.relationship("EngagementEvent", back_populates="engagement", cascade="all, delete-orphan",
                             order_by="EngagementEvent.created_at")


class EngagementEvent(db.Model):
    __tablename__ = "engagement_events"
    id = db.Column(db.Integer, primary_key=True)
    engagement_id = db.Column(db.Integer, db.ForeignKey("engagements.id"), nullable=False)
    event = db.Column(db.String(30))  # sent | viewed | link_clicked | signed | declined | reminder
    ip = db.Column(db.String(60), default="")
    ua = db.Column(db.String(300), default="")
    detail = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=now)
    engagement = db.relationship("Engagement", back_populates="events")


class PortalToken(db.Model):
    """Magic-link login for the client portal."""
    __tablename__ = "portal_tokens"
    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=False)
    token = db.Column(db.String(80), unique=True, default=new_token)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=now)
    contact = db.relationship("Contact")


class Message(db.Model):
    """SMS / email log with a contact (two-way texting via Twilio)."""
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"))
    matter_id = db.Column(db.Integer, db.ForeignKey("matters.id"))
    direction = db.Column(db.String(5), default="out")  # in | out
    channel = db.Column(db.String(10), default="sms")  # sms | email
    to_addr = db.Column(db.String(200), default="")
    from_addr = db.Column(db.String(200), default="")
    body = db.Column(db.Text, default="")
    provider_id = db.Column(db.String(120), default="")
    status = db.Column(db.String(30), default="queued")
    created_at = db.Column(db.DateTime, default=now)
    # channel "portal": secure messages typed in the client portal or replied to from the staff thread.
    read_at = db.Column(db.DateTime)  # when the other side opened it
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))  # staff author for outbound portal messages
    contact = db.relationship("Contact")
    matter = db.relationship("Matter")
    user = db.relationship("User")


class DocumentSignature(db.Model):
    """Click-to-sign on any uploaded document, with the same audit record as engagement letters."""
    __tablename__ = "document_signatures"
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=False)
    token = db.Column(db.String(80), unique=True, default=new_token)
    title = db.Column(db.String(300), default="")
    message = db.Column(db.Text, default="")  # note shown to the signer
    status = db.Column(db.String(20), default="draft")  # draft | sent | viewed | signed | declined | void
    sent_at = db.Column(db.DateTime)
    sent_to = db.Column(db.String(200), default="")
    first_viewed_at = db.Column(db.DateTime)
    view_count = db.Column(db.Integer, default=0)
    signed_at = db.Column(db.DateTime)
    signer_name = db.Column(db.String(200), default="")
    signer_email = db.Column(db.String(200), default="")
    signer_ip = db.Column(db.String(60), default="")
    signer_ua = db.Column(db.String(300), default="")
    document_hash = db.Column(db.String(80), default="")  # sha256 of the file bytes at send time
    signature_hash = db.Column(db.String(80), default="")
    certificate_pdf_path = db.Column(db.String(400), default="")
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=now)
    document = db.relationship("Document")
    contact = db.relationship("Contact")
    created_by = db.relationship("User")
    events = db.relationship("DocumentSignatureEvent", back_populates="signature", cascade="all, delete-orphan",
                             order_by="DocumentSignatureEvent.created_at")


class DocumentSignatureEvent(db.Model):
    __tablename__ = "document_signature_events"
    id = db.Column(db.Integer, primary_key=True)
    signature_id = db.Column(db.Integer, db.ForeignKey("document_signatures.id"), nullable=False)
    event = db.Column(db.String(30))  # sent | viewed | signed | declined | reminder
    ip = db.Column(db.String(60), default="")
    ua = db.Column(db.String(300), default="")
    detail = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=now)
    signature = db.relationship("DocumentSignature", back_populates="events")


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(60))
    entity = db.Column(db.String(60))
    entity_id = db.Column(db.Integer)
    detail = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=now)
    user = db.relationship("User")


def audit(action, entity, entity_id=None, detail="", user_id=None):
    db.session.add(AuditLog(user_id=user_id, action=action, entity=entity, entity_id=entity_id, detail=detail))
