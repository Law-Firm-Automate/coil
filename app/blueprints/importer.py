"""Switch to Coil: import contacts, matters, time and expenses, bills, trust, tasks, calendar, notes and documents
from Clio, MyCase, PracticePanther or any CSV.

Flow per file: POST upload -> parse + auto-map + dry run -> preview page with mapping selects -> POST commit with
the (possibly edited) mapping -> rows applied in one transaction per 500, ExternalRef rows written so later files
link up and a re-import updates instead of duplicating -> ImportJob with counts and errors -> job page.

Nothing is ever deleted by an import. Trust rows that would take a client below zero are refused and listed.
"""
import csv
import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from datetime import datetime, date, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, Response, session
from sqlalchemy import func

from ..extensions import db
from ..models import (Contact, Matter, TimeEntry, Expense, Invoice, InvoiceLine, Payment, TrustTransaction, Task,
                      CalendarEvent, Document, Note, User, Firm, ImportJob, ExternalRef, audit)
from ..helpers import login_required, owner_required, current_user, cents_to_str
from . import _importmap as M

bp = Blueprint("importer", __name__, url_prefix="/import")

MAX_CSV_BYTES = 20 * 1024 * 1024
MAX_ZIP_BYTES = 200 * 1024 * 1024
BATCH = 500
SAMPLE = 20


# ---------------------------------------------------------------- storage for the two-step flow
def _dir():
    d = os.path.join(current_app.config["UPLOAD_DIR"], "imports")
    os.makedirs(d, exist_ok=True)
    return d


def _token_path(token, ext="json"):
    token = re.sub(r"[^a-f0-9]", "", token or "")
    if not token:
        abort(404)
    return os.path.join(_dir(), f"{token}.{ext}")


def _load(token):
    p = _token_path(token)
    if not os.path.isfile(p):
        return None
    with open(p) as fh:
        return json.load(fh)


def _save(token, data):
    with open(_token_path(token), "w") as fh:
        json.dump(data, fh)


def _parse_csv(raw):
    text = raw.decode("utf-8-sig", "ignore")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h.strip() for h in (reader.fieldnames or []) if h is not None]
    rows = []
    for i, r in enumerate(reader, start=2):
        clean = {}
        for k, v in r.items():
            if k is None:
                continue
            clean[k.strip()] = (v or "").strip() if isinstance(v, str) else ""
        if not any(clean.values()):
            continue
        clean["_row"] = i
        rows.append(clean)
    return headers, rows


def _refine_mapping(entity, source, headers, rows, mapping):
    """If an auto-mapped column is empty in every row, fall back to the next alias hit (Clio's Custom Number is
    often blank while Display Number is filled)."""
    sample = rows[:200]
    for field, _label, _req, multi in M.field_defs(entity):
        src = mapping.get(field) or ""
        if not src or multi:
            continue
        if any((r.get(src) or "") for r in sample):
            continue
        used = {h for v in mapping.values() for h in (v or "").split("|") if h}
        for a in M.aliases_for(source, entity, field):
            for h in headers:
                if h not in used and M.norm_header(h) == a and any((r.get(h) or "") for r in sample):
                    mapping[field] = h
                    used.add(h)
                    break
            else:
                continue
            break
    return mapping


# ---------------------------------------------------------------- resolution context
class Ctx:
    def __init__(self, source, entity, user, mapping, options, dry):
        self.source, self.entity, self.user, self.mapping, self.options, self.dry = source, entity, user, mapping, options, dry
        self.pending = {}          # (entity, external_id) -> True in dry mode
        self.seen = set()          # in-file dedupe keys in dry mode
        self.balances = {}         # ("c", client_id) / ("m", matter_id) -> running trust balance
        self.warnings_users = set()

    def ref_get(self, entity, ext):
        if not ext:
            return None
        if (entity, ext) in self.pending:
            return self.pending[(entity, ext)]
        r = ExternalRef.query.filter_by(source=self.source, entity=entity, external_id=str(ext)[:120]).first()
        return r.coil_id if r else None

    def ref_set(self, entity, ext, coil_id):
        if not ext:
            return
        ext = str(ext)[:120]
        if self.dry:
            self.pending[(entity, ext)] = coil_id or -1
            return
        r = ExternalRef.query.filter_by(source=self.source, entity=entity, external_id=ext).first()
        if r:
            r.coil_id = coil_id
        else:
            db.session.add(ExternalRef(source=self.source, entity=entity, external_id=ext, coil_id=coil_id))

    def get(self, model, coil_id):
        if not coil_id or coil_id < 0:
            return None
        return db.session.get(model, coil_id)


def _hash_key(*parts):
    return "h:" + hashlib.sha1("|".join(str(p or "").strip().lower() for p in parts).encode()).hexdigest()[:24]


def find_contact_by_name(name):
    n = M.clean_name(name).lower()
    if not n:
        return None
    c = Contact.query.filter(func.lower(Contact.company_name) == n).first()
    if c:
        return c
    first, last = M.split_name(name)
    if first or last:
        c = Contact.query.filter(func.lower(Contact.first_name) == first.lower(),
                                 func.lower(Contact.last_name) == last.lower()).first()
        if c:
            return c
    return Contact.query.filter(func.lower(func.trim(Contact.first_name + " " + Contact.last_name)) == n).first()


def find_contact_by_email(email):
    e = (email or "").strip().lower()
    if not e:
        return None
    return Contact.query.filter(func.lower(Contact.email) == e).first()


def resolve_client(ctx, ext_id="", name="", email=""):
    """Contact by the old system's id (ExternalRef), then email, then exact name. Returns (contact, how)."""
    cid = ctx.ref_get("contact", ext_id)
    c = ctx.get(Contact, cid) if cid else None
    if c:
        return c, "id"
    c = find_contact_by_email(email)
    if c:
        return c, "email"
    c = find_contact_by_name(name)
    if c:
        return c, "name"
    return None, ""


def find_matter_by_number(number):
    n = (number or "").strip()
    if not n:
        return None
    m = Matter.query.filter(func.lower(Matter.number) == n.lower()).first()
    if m:
        return m
    head, _, tail = n.partition("-")
    if tail:
        m = Matter.query.filter(func.lower(Matter.number) == head.strip().lower()).first()
        if m:
            return m
        m = Matter.query.filter(func.lower(Matter.name) == tail.strip().lower()).first()
        if m:
            return m
    return None


def find_matter_by_name(name):
    n = M.clean_name(name).lower()
    if not n:
        return None
    m = Matter.query.filter(func.lower(Matter.name) == n).first()
    if m:
        return m
    # "00012-Alvarez Estate" or "M-1001 Alvarez Estate" style labels
    parts = re.split(r"[\s:-]+", n, 1)
    if len(parts) == 2:
        m = Matter.query.filter(func.lower(Matter.name) == parts[1].strip()).first()
        if m:
            return m
        m = Matter.query.filter(func.lower(Matter.number) == parts[0].strip()).first()
        if m:
            return m
    return None


def resolve_matter(ctx, ext_id="", number="", name=""):
    mid = ctx.ref_get("matter", ext_id)
    m = ctx.get(Matter, mid) if mid else None
    if m:
        return m, "id"
    m = find_matter_by_number(number)
    if m:
        return m, "number"
    m = find_matter_by_name(name)
    if m:
        return m, "name"
    m = find_matter_by_name(number) if number else None
    if m:
        return m, "number"
    return None, ""


def resolve_user(text):
    t = M.clean_name(text)
    if not t:
        return None
    tl = t.lower()
    u = User.query.filter(func.lower(User.email) == tl).first()
    if u:
        return u
    u = User.query.filter(func.lower(User.name) == tl).first()
    if u:
        return u
    if "," in t:
        last, _, first = t.partition(",")
        u = User.query.filter(func.lower(User.name) == f"{first.strip()} {last.strip()}".lower()).first()
        if u:
            return u
    if len(t) <= 4:
        u = User.query.filter(func.lower(User.initials) == tl).first()
        if u:
            return u
    return None


def _custom_fields_from(raw, ctx):
    """Unmapped columns as {header: value} when the user ticked the option."""
    if not ctx.options.get("keep_unmapped"):
        return {}
    used = {h for v in ctx.mapping.values() for h in (v or "").split("|") if h}
    return {k: v for k, v in raw.items() if k not in used and not k.startswith("_") and (v or "").strip()}


# ---------------------------------------------------------------- per-entity prepare (validate + resolve) and apply
def prep_contacts(ctx, v, raw):
    msgs = []
    first, last, company = v["first_name"], v["last_name"], v["company_name"]
    if v["name"] and not (first or last):
        if M.looks_like_company(v["name"]) and not company:
            company = M.clean_name(v["name"])
        else:
            first, last = M.split_name(v["name"])
    kind_txt = (v["kind"] or "").lower()
    if kind_txt.startswith("comp") or kind_txt in ("organization", "organisation", "business", "firm"):
        kind = "company"
        if not company and (first or last):
            company = M.clean_name(f"{first} {last}")
            first = last = ""
    elif kind_txt.startswith("person") or kind_txt in ("individual", "people"):
        kind = "person"
    else:
        kind = "company" if (company and not (first or last)) else "person"
    if not (first or last or company):
        return "error", ["No name or company on this row."], None
    email = v["email"].lower()
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        msgs.append(f"Email '{email}' does not look valid, kept as text.")
    aliases = "\n".join(a.strip() for a in re.split(r"[;|\n]", v["aliases"]) if a.strip())
    tags = ", ".join(t.strip() for t in re.split(r"[;|,]", v["tags"]) if t.strip())
    rec = {"kind": kind, "first_name": first[:100], "last_name": last[:100], "company_name": company[:200],
           "email": email[:200], "phone": v["phone"][:50], "address": v["address"], "tags": tags[:300],
           "aliases": aliases, "notes": v["notes"], "external_id": v["external_id"],
           "is_client": M.parse_bool(v["is_client"]) if v["is_client"] else None,
           "custom": _custom_fields_from(raw, ctx)}
    display = company if kind == "company" else f"{first} {last}".strip()
    # dedupe: external id, then email, then exact name
    existing = ctx.get(Contact, ctx.ref_get("contact", rec["external_id"])) if rec["external_id"] else None
    how = "id" if existing else ""
    if not existing and email:
        existing = find_contact_by_email(email)
        how = "email" if existing else ""
    if not existing:
        existing = find_contact_by_name(display)
        how = "name" if existing else ""
    key = email or display.lower()
    in_file = key in ctx.seen
    ctx.seen.add(key)
    if ctx.dry and rec["external_id"]:
        ctx.ref_set("contact", rec["external_id"], -1)
    if existing or (ctx.dry and in_file):
        rec["existing_id"] = existing.id if existing else None
        if ctx.options.get("duplicates") == "skip":
            return "skip", [f"Matches existing contact by {how or 'row in this file'}, skipped."], rec
        return "update", [f"Updates {existing.display_name if existing else display} (matched by {how or 'this file'})."], rec
    return "create", msgs, rec


def apply_contacts(ctx, rec):
    c = db.session.get(Contact, rec["existing_id"]) if rec.get("existing_id") else None
    if c is None and rec.get("external_id"):
        c = ctx.get(Contact, ctx.ref_get("contact", rec["external_id"]))
    if c is None and rec["email"]:
        c = find_contact_by_email(rec["email"])
    if c is None:
        c = find_contact_by_name(rec["company_name"] if rec["kind"] == "company" else f"{rec['first_name']} {rec['last_name']}")
    created = c is None
    if created:
        c = Contact()
        db.session.add(c)
    c.kind = rec["kind"]
    for k in ("first_name", "last_name", "company_name", "email", "phone", "address", "tags", "notes"):
        if rec[k] or created:
            setattr(c, k, rec[k] or (getattr(c, k) or ""))
    if rec["aliases"]:
        have = [a for a in (c.aliases or "").split("\n") if a]
        for a in rec["aliases"].split("\n"):
            if a and a not in have:
                have.append(a)
        c.aliases = "\n".join(have)
    if rec["is_client"] is True or ctx.options.get("mark_clients"):
        c.is_client = True
    if rec["custom"]:
        cf = c.custom_fields
        cf.update(rec["custom"])
        c.custom_fields = cf
    db.session.flush()
    if rec["external_id"]:
        ctx.ref_set("contact", rec["external_id"], c.id)
    return c.id, created


def prep_matters(ctx, v, raw):
    msgs = []
    name = M.clean_name(v["name"]) or M.clean_name(v["description"])
    if not name:
        return "error", ["Matter has no name."], None
    client, how = resolve_client(ctx, v["client_external_id"], v["client_name"], v["client_email"])
    if not client:
        who = v["client_name"] or v["client_email"] or v["client_external_id"] or "(blank)"
        return "error", [f"Client '{who}' not found. Import contacts first, or check the client column."], None
    resp = resolve_user(v["responsible"])
    if v["responsible"] and not resp:
        msgs.append(f"Responsible attorney '{v['responsible']}' is not a Coil user; assigned to you.")
    opened = M.parse_any_date(v["opened_on"])
    if v["opened_on"] and not opened:
        msgs.append(f"Could not read opened date '{v['opened_on']}', used today.")
    closed = M.parse_any_date(v["closed_on"])
    status = M.matter_status(v["status"])
    if closed and not v["status"]:
        status = "closed"
    if status == "closed" and not closed:
        closed = opened or date.today()
    rate = M.money_or_none(v["hourly_rate"]) or 0
    flat = M.money_or_none(v["flat_fee"]) or 0
    bt = (v["billing_type"] or "").lower()
    if "conting" in bt:
        billing = "contingency"
    elif "hybrid" in bt:
        billing = "hybrid"
    elif "hour" in bt or "time" in bt or rate:
        billing = "hourly"
    elif "flat" in bt or "fixed" in bt or flat:
        billing = "flat"
    elif bt in ("no", "false", "non-billable", "nonbillable"):
        billing = "flat"
    else:
        billing = "hourly" if ctx.source in ("clio", "practicepanther") else "flat"
    number = (v["number"] or "").strip()[:30]
    if not number:
        # Clio: Custom Number is blank unless the firm numbers by hand; Display Number always has a value.
        for h, val in raw.items():
            if M.norm_header(h) in ("display number", "matter number", "number") and (val or "").strip():
                number = val.strip()[:30]
                break
    existing = ctx.get(Matter, ctx.ref_get("matter", v["external_id"])) if v["external_id"] else None
    if not existing and number:
        existing = Matter.query.filter(func.lower(Matter.number) == number.lower()).first()
    if not existing:
        cand = Matter.query.filter(func.lower(Matter.name) == name.lower(), Matter.client_id == client.id).first()
        existing = cand
    if ctx.dry and v["external_id"]:
        ctx.ref_set("matter", v["external_id"], -1)
    key = number.lower() if number else f"{client.id}:{name.lower()}"
    in_file = key in ctx.seen
    ctx.seen.add(key)
    if number and not existing and Matter.query.filter(func.lower(Matter.number) == number.lower()).first():
        msgs.append(f"Matter number {number} is already taken; the next Coil number will be assigned.")
        number = ""
    rec = {"external_id": v["external_id"], "number": number, "name": name[:300], "description": v["description"]
           if v["description"] != name else "", "client_id": client.id, "status": status, "practice_area": v["practice_area"][:100],
           "responsible_user_id": resp.id if resp else ctx.user.id, "opened_on": (opened or date.today()).isoformat(),
           "closed_on": closed.isoformat() if closed else None, "billing_type": billing, "hourly_rate_cents": rate,
           "flat_fee_cents": flat, "case_number": v["case_number"][:100], "court": v["court"][:200],
           "sol_date": (M.parse_any_date(v["sol_date"]) or date.min).isoformat() if M.parse_any_date(v["sol_date"]) else None,
           "custom": _custom_fields_from(raw, ctx), "existing_id": existing.id if existing else None,
           "client_how": how}
    msgs.insert(0, f"Client: {client.display_name} (by {how}).")
    if existing or (ctx.dry and in_file):
        if ctx.options.get("duplicates") == "skip":
            return "skip", msgs + ["Matches an existing matter, skipped."], rec
        return "update", msgs + [f"Updates {existing.label if existing else name}."], rec
    return "create", msgs, rec


def apply_matters(ctx, rec):
    from .matters import assign_number
    m = db.session.get(Matter, rec["existing_id"]) if rec.get("existing_id") else None
    if m is None and rec["external_id"]:
        m = ctx.get(Matter, ctx.ref_get("matter", rec["external_id"]))
    if m is None and rec["number"]:
        m = Matter.query.filter(func.lower(Matter.number) == rec["number"].lower()).first()
    if m is None:
        m = Matter.query.filter(func.lower(Matter.name) == rec["name"].lower(), Matter.client_id == rec["client_id"]).first()
    created = m is None
    if created:
        m = Matter(client_id=rec["client_id"], name=rec["name"])
        db.session.add(m)
        if rec["number"] and not Matter.query.filter(func.lower(Matter.number) == rec["number"].lower()).first():
            m.number = rec["number"]
        else:
            assign_number(m)
    m.name = rec["name"]
    m.client_id = rec["client_id"]
    m.status = rec["status"]
    m.opened_on = date.fromisoformat(rec["opened_on"])
    m.closed_on = date.fromisoformat(rec["closed_on"]) if rec["closed_on"] else None
    m.responsible_user_id = rec["responsible_user_id"]
    for k in ("practice_area", "description", "case_number", "court"):
        if rec[k] or created:
            setattr(m, k, rec[k] or (getattr(m, k) or ""))
    if created or rec["hourly_rate_cents"]:
        m.hourly_rate_cents = rec["hourly_rate_cents"]
    if created or rec["flat_fee_cents"]:
        m.flat_fee_cents = rec["flat_fee_cents"]
    if created:
        m.billing_type = rec["billing_type"]
    if rec["sol_date"]:
        m.sol_date = date.fromisoformat(rec["sol_date"])
    if rec["custom"]:
        cf = m.custom_fields
        cf.update(rec["custom"])
        m.custom_fields = cf
    client = db.session.get(Contact, rec["client_id"])
    if client and not client.is_client:
        client.is_client = True
    db.session.flush()
    if rec["external_id"]:
        ctx.ref_set("matter", rec["external_id"], m.id)
    return m.id, created


def _billed_flag(text):
    t = (text or "").strip().lower()
    return t in M.TRUE_WORDS or t in ("paid", "unpaid", "invoiced", "billed", "on bill", "partially paid", "partial")


def _billable_flag(text, header):
    t = (text or "").strip().lower()
    if not t:
        return True
    invert = M.norm_header(header or "").startswith("non")
    if t in M.TRUE_WORDS or t in ("billable", "billed"):
        val = True
    elif t in M.FALSE_WORDS:
        val = False
    else:
        cents = M.money_or_none(t)
        val = True if cents is None else cents > 0
        invert = False if cents is not None else invert
    return (not val) if invert else val


def prep_activities(ctx, v, raw):
    msgs = []
    when = M.parse_any_date(v["date"])
    if not when:
        return "error", [f"Could not read date '{v['date']}'."], None
    matter, how = resolve_matter(ctx, v["matter_external_id"], v["matter_number"], v["matter_name"])
    if not matter:
        who = v["matter_number"] or v["matter_name"] or v["matter_external_id"] or "(blank)"
        return "error", [f"Matter '{who}' not found. Import matters first."], None
    kind = M.activity_type(v["type"], v["category"], quantity_present=bool(v["quantity"]))
    user = resolve_user(v["user"])
    if not user:
        user = ctx.user
        if v["user"] and v["user"] not in ctx.warnings_users:
            ctx.warnings_users.add(v["user"])
        if v["user"]:
            msgs.append(f"User '{v['user']}' is not a Coil user; recorded under you.")
    billed = _billed_flag(v["billed"])
    billable = _billable_flag(v["billable"], ctx.mapping.get("billable", ""))
    desc = v["description"] or ""
    cat = v["category"] or ""
    if cat and desc and cat.lower() not in desc.lower():
        desc = f"{cat}: {desc}"
    elif cat and not desc:
        desc = cat
    if billed:
        desc = "[billed in previous system] " + desc
        billable = False
    ext = v["external_id"] or _hash_key(kind, when.isoformat(), matter.id, v["user"], v["quantity"], v["total"], desc)
    rec = {"kind": kind, "date": when.isoformat(), "matter_id": matter.id, "user_id": user.id, "description": desc,
           "billable": billable, "billed": billed, "external_id": ext, "matter_label": matter.label}
    total = M.money_or_none(v["total"])
    rate = M.money_or_none(v["rate"])
    if kind == "time":
        minutes = M.parse_hours_to_minutes(v["quantity"])
        if minutes is None:
            if total is not None and rate:
                minutes = int(round(total / rate * 60))
            else:
                return "error", [f"Could not read hours '{v['quantity']}'."], None
        if not rate:
            if total is not None and minutes:
                rate = int(round(total * 60 / minutes))
            else:
                rate = matter.effective_rate_cents(user)
        rec.update({"minutes": minutes, "rate_cents": rate, "task_code": v["task_code"][:20],
                    "activity_code": v["activity_code"][:20]})
        entity = "time"
    else:
        qty = None
        try:
            qty = float(v["quantity"]) if v["quantity"] else None
        except ValueError:
            qty = None
        amount = total if total is not None else int(round((rate or 0) * (qty or 1)))
        rec.update({"amount_cents": amount, "category": cat[:60], "expense_code": v["expense_code"][:20]})
        entity = "expense"
    rec["entity"] = entity
    existing = ctx.ref_get(entity, ext)
    if ctx.dry:
        ctx.ref_set(entity, ext, -1)
    msgs.insert(0, f"{'Time' if kind == 'time' else 'Expense'} on {matter.label} (by {how}).")
    if existing:
        if ctx.options.get("duplicates") == "skip":
            return "skip", msgs + ["Already imported, skipped."], rec
        return "update", msgs + ["Already imported, updated."], rec
    return "create", msgs, rec


def apply_activities(ctx, rec):
    entity = rec["entity"]
    model = TimeEntry if entity == "time" else Expense
    obj = ctx.get(model, ctx.ref_get(entity, rec["external_id"]))
    created = obj is None
    if created:
        obj = model(matter_id=rec["matter_id"])
        db.session.add(obj)
    obj.matter_id = rec["matter_id"]
    obj.user_id = rec["user_id"]
    obj.date = date.fromisoformat(rec["date"])
    obj.description = rec["description"]
    if obj.invoice_id is None:
        obj.billable = rec["billable"]
    if entity == "time":
        obj.minutes = rec["minutes"]
        obj.rate_cents = rec["rate_cents"]
        obj.task_code = rec["task_code"]
        obj.activity_code = rec["activity_code"]
    else:
        obj.amount_cents = rec["amount_cents"]
        obj.category = rec["category"]
        obj.expense_code = rec["expense_code"]
    db.session.flush()
    ctx.ref_set(entity, rec["external_id"], obj.id)
    return obj.id, created


def prep_bills(ctx, v, raw):
    msgs = []
    number = (v["number"] or "").strip()
    ext = v["external_id"] or (number and f"n:{number}") or ""
    if not ext:
        return "error", ["Bill has neither an id nor a number."], None
    matter, mhow = resolve_matter(ctx, v["matter_external_id"], v["matter_number"], v["matter_name"])
    client, chow = resolve_client(ctx, v["client_external_id"], v["client_name"], "")
    if not client and matter:
        client, chow = matter.client, "matter"
    if not matter and client:
        open_matters = [m for m in client.matters]
        if len(open_matters) == 1:
            matter, mhow = open_matters[0], "client's only matter"
    if not matter:
        who = v["matter_number"] or v["matter_name"] or v["matter_external_id"] or "(blank)"
        return "error", [f"Matter '{who}' not found; every invoice needs a matter."], None
    if not client:
        return "error", ["Client not found."], None
    total = M.money_or_none(v["total"])
    paid = M.money_or_none(v["paid"])
    balance = M.money_or_none(v["balance"])
    if total is None and balance is not None:
        total = balance + (paid or 0)
    if total is None:
        return "error", ["No total on this row."], None
    if paid is None:
        paid = max(0, total - balance) if balance is not None else 0
    issued = M.parse_any_date(v["issued_on"]) or date.today()
    due = M.parse_any_date(v["due_on"])
    paid_on = M.parse_any_date(v["paid_on"])
    status = M.invoice_status(v["status"], total, paid)
    existing = ctx.ref_get("invoice", ext)
    if not existing and number:
        inv = Invoice.query.filter(func.lower(Invoice.number) == number.lower()).first()
        if inv and ExternalRef.query.filter_by(source=ctx.source, entity="invoice", coil_id=inv.id).first():
            existing = inv.id
        elif inv:
            msgs.append(f"Invoice number {number} already exists in Coil; the next Coil number will be used.")
            number = ""
    if ctx.dry:
        ctx.ref_set("invoice", ext, -1)
    rec = {"external_id": ext, "number": number[:30], "matter_id": matter.id, "client_id": client.id,
           "issued_on": issued.isoformat(), "due_on": due.isoformat() if due else None, "total": total, "paid": paid,
           "status": status, "paid_on": (paid_on or issued).isoformat(), "source_number": v["number"] or v["external_id"]}
    msgs.insert(0, f"{matter.label} / {client.display_name}; total {cents_to_str(total)}, paid {cents_to_str(paid)}, "
                   f"status {status}.")
    if existing:
        if ctx.options.get("duplicates") == "skip":
            return "skip", msgs + ["Already imported, skipped."], rec
        return "update", msgs + ["Already imported, updated."], rec
    return "create", msgs, rec


def apply_bills(ctx, rec):
    from .invoices import _next_number
    firm = Firm.get()
    inv = ctx.get(Invoice, ctx.ref_get("invoice", rec["external_id"]))
    created = inv is None
    if created:
        inv = Invoice(matter_id=rec["matter_id"], client_id=rec["client_id"], kind="flat", created_by_id=ctx.user.id,
                      currency=firm.currency or "USD")
        db.session.add(inv)
        if rec["number"] and not Invoice.query.filter(func.lower(Invoice.number) == rec["number"].lower()).first():
            inv.number = rec["number"]
        else:
            inv.number = _next_number(firm)
    inv.matter_id, inv.client_id = rec["matter_id"], rec["client_id"]
    inv.issued_on = date.fromisoformat(rec["issued_on"])
    inv.due_on = date.fromisoformat(rec["due_on"]) if rec["due_on"] else inv.issued_on + timedelta(days=firm.invoice_terms_days or 30)
    inv.notes = f"Imported from {M.SOURCE_LABELS.get(ctx.source, ctx.source)}."
    if created:
        inv.sent_at = datetime.utcnow() if rec["status"] not in ("draft", "void") else None
    db.session.flush()
    desc = f"Imported balance from {M.SOURCE_LABELS.get(ctx.source, ctx.source)} bill {rec['source_number']}"
    line = next((l for l in inv.lines if (l.description or "").startswith("Imported balance from")), None)
    if line is None:
        line = InvoiceLine(invoice_id=inv.id, kind="flat", sort=0)
        db.session.add(line)
    line.date = inv.issued_on
    line.description = desc
    line.quantity = 1.0
    line.unit_cents = rec["total"]
    line.amount_cents = rec["total"]
    pay = ctx.get(Payment, ctx.ref_get("payment", rec["external_id"]))
    if rec["paid"] > 0:
        if pay is None:
            pay = Payment(invoice_id=inv.id, matter_id=inv.matter_id, client_id=inv.client_id, method="other",
                          account="operating")
            db.session.add(pay)
        pay.amount_cents = rec["paid"]
        pay.received_on = date.fromisoformat(rec["paid_on"])
        pay.note = f"Imported from {M.SOURCE_LABELS.get(ctx.source, ctx.source)}"[:300]
        pay.reference = f"{ctx.source} bill {rec['source_number']}"[:120]
        db.session.flush()
        ctx.ref_set("payment", rec["external_id"], pay.id)
    elif pay is not None:
        pay.amount_cents = 0
    inv.status = rec["status"] if rec["status"] in ("draft", "void") else "sent"
    db.session.flush()
    db.session.refresh(inv)
    inv.recalc()
    db.session.flush()
    ctx.ref_set("invoice", rec["external_id"], inv.id)
    return inv.id, created


def _bal(ctx, key, fetch):
    if key not in ctx.balances:
        ctx.balances[key] = fetch()
    return ctx.balances[key]


def prep_trust(ctx, v, raw):
    msgs = []
    when = M.parse_any_date(v["date"])
    if not when:
        return "error", [f"Could not read date '{v['date']}'."], None
    matter, mhow = resolve_matter(ctx, v["matter_external_id"], v["matter_number"], v["matter_name"])
    client, chow = resolve_client(ctx, v["client_external_id"], v["client_name"], "")
    if not client and matter:
        client, chow = matter.client, "matter"
    if not client:
        who = v["client_name"] or v["client_external_id"] or v["matter_number"] or v["matter_name"] or "(blank)"
        return "error", [f"Client '{who}' not found."], None
    if matter and matter.client_id != client.id:
        return "error", [f"Matter {matter.label} does not belong to {client.display_name}."], None
    amount = M.money_or_none(v["amount"])
    fin, fout = M.money_or_none(v["funds_in"]), M.money_or_none(v["funds_out"])
    signed = None
    if amount is not None and amount != 0:
        signed = amount
    elif fin:
        signed = abs(fin)
    elif fout:
        signed = -abs(fout)
    ttype = M.trust_type(v["type"], v["description"] if not v["type"] else "", signed)
    if signed is None:
        return "error", ["No amount on this row."], None
    if ttype in M.NEGATIVE_TRUST_TYPES:
        signed = -abs(signed)
    elif ttype in ("deposit", "interest"):
        signed = abs(signed)
    ext = v["external_id"] or _hash_key(when.isoformat(), client.id, signed, v["description"], v["reference"])
    existing_id = ctx.ref_get("trust", ext)
    existing = ctx.get(TrustTransaction, existing_id) if existing_id and existing_id > 0 else None
    delta = signed - (existing.amount_cents if existing else 0)
    cbal = _bal(ctx, ("c", client.id), client.trust_balance_cents)
    if cbal + delta < 0:
        return "error", [f"Refused: {client.display_name} would go to {cents_to_str(cbal + delta)} in trust on "
                         f"{when.isoformat()} (balance {cents_to_str(cbal)} before this row). Add the opening "
                         f"balance first, then re-upload the failed rows."], None
    if matter:
        mbal = _bal(ctx, ("m", matter.id), matter.trust_balance_cents)
        if mbal + delta < 0:
            return "error", [f"Refused: {matter.label} would go to {cents_to_str(mbal + delta)} in trust on "
                             f"{when.isoformat()}. Add the matter's opening balance first."], None
        ctx.balances[("m", matter.id)] = mbal + delta
    ctx.balances[("c", client.id)] = cbal + delta
    if ctx.dry:
        ctx.ref_set("trust", ext, -1)
    rec = {"external_id": ext, "client_id": client.id, "matter_id": matter.id if matter else None,
           "date": when.isoformat(), "type": ttype, "amount_cents": signed, "description": v["description"][:300],
           "payee": v["payee"][:200], "reference": v["reference"][:120],
           "cleared": M.parse_bool(v["cleared"]) if v["cleared"] else False,
           "cleared_on": (M.parse_any_date(v["cleared"]) or when).isoformat() if v["cleared"] and M.parse_bool(v["cleared"], True) else None}
    msgs.insert(0, f"{client.display_name}{' / ' + matter.label if matter else ''}: {ttype} {cents_to_str(signed)}, "
                   f"balance after {cents_to_str(ctx.balances[('c', client.id)])}.")
    if existing_id:
        if ctx.options.get("duplicates") == "skip":
            return "skip", msgs + ["Already imported, skipped."], rec
        return "update", msgs + ["Already imported, updated."], rec
    return "create", msgs, rec


def apply_trust(ctx, rec):
    t = ctx.get(TrustTransaction, ctx.ref_get("trust", rec["external_id"]))
    created = t is None
    if created:
        t = TrustTransaction(client_id=rec["client_id"], type=rec["type"], amount_cents=rec["amount_cents"],
                             date=date.fromisoformat(rec["date"]), created_by_id=ctx.user.id)
        db.session.add(t)
    t.client_id, t.matter_id = rec["client_id"], rec["matter_id"]
    t.date = date.fromisoformat(rec["date"])
    t.type, t.amount_cents = rec["type"], rec["amount_cents"]
    t.description, t.payee, t.reference = rec["description"], rec["payee"], rec["reference"]
    t.cleared = rec["cleared"]
    t.cleared_on = date.fromisoformat(rec["cleared_on"]) if rec["cleared_on"] else None
    db.session.flush()
    ctx.ref_set("trust", rec["external_id"], t.id)
    return t.id, created


def prep_tasks(ctx, v, raw):
    msgs = []
    title = M.clean_name(v["title"])
    if not title:
        return "error", ["Task has no title."], None
    matter = None
    if v["matter_external_id"] or v["matter_number"] or v["matter_name"]:
        matter, how = resolve_matter(ctx, v["matter_external_id"], v["matter_number"], v["matter_name"])
        if not matter:
            msgs.append(f"Matter '{v['matter_number'] or v['matter_name'] or v['matter_external_id']}' not found; task kept without a matter.")
    due = M.parse_any_date(v["due_on"])
    assignee = resolve_user(v["assignee"])
    if v["assignee"] and not assignee:
        msgs.append(f"Assignee '{v['assignee']}' is not a Coil user; left unassigned.")
    done_txt = (v["done"] or "").strip().lower()
    done = M.parse_bool(done_txt) or bool(M.parse_any_date(v["done"]))
    pr = (v["priority"] or "").lower()
    priority = "high" if pr in ("high", "urgent", "1", "critical") else "low" if pr in ("low", "3", "minor") else "normal"
    kind = "deadline" if re.search(r"\b(deadline|due|statute|limitation)\b", title.lower()) else "task"
    if re.search(r"\b(hearing|trial|court|arraignment|docket)\b", title.lower()):
        kind = "court_date"
    ext = v["external_id"] or _hash_key(title, matter.id if matter else "", due.isoformat() if due else "")
    existing = ctx.ref_get("task", ext)
    if ctx.dry:
        ctx.ref_set("task", ext, -1)
    rec = {"external_id": ext, "title": title[:300], "matter_id": matter.id if matter else None,
           "due_on": due.isoformat() if due else None, "assignee_id": assignee.id if assignee else None,
           "done": done, "priority": priority, "kind": kind, "notes": v["notes"]}
    if existing:
        if ctx.options.get("duplicates") == "skip":
            return "skip", msgs + ["Already imported, skipped."], rec
        return "update", msgs + ["Already imported, updated."], rec
    return "create", msgs, rec


def apply_tasks(ctx, rec):
    t = ctx.get(Task, ctx.ref_get("task", rec["external_id"]))
    created = t is None
    if created:
        t = Task(title=rec["title"])
        db.session.add(t)
    t.title, t.matter_id, t.kind, t.priority, t.notes = rec["title"], rec["matter_id"], rec["kind"], rec["priority"], rec["notes"]
    t.due_on = date.fromisoformat(rec["due_on"]) if rec["due_on"] else None
    t.assignee_id = rec["assignee_id"]
    if rec["done"] and not t.done:
        t.done, t.done_at = True, datetime.utcnow()
    elif not rec["done"]:
        t.done, t.done_at = False, None
    db.session.flush()
    ctx.ref_set("task", rec["external_id"], t.id)
    return t.id, created


def prep_calendar(ctx, v, raw):
    msgs = []
    title = M.clean_name(v["title"])
    if not title:
        return "error", ["Event has no title."], None
    start = M.parse_any_datetime(v["starts_at"])
    if not start:
        return "error", [f"Could not read start '{v['starts_at']}'."], None
    end = M.parse_any_datetime(v["ends_at"])
    all_day = M.parse_bool(v["all_day"]) if v["all_day"] else (M.parse_any_date(v["starts_at"]) is not None
                                                                and not re.search(r"\d:\d", v["starts_at"]))
    if end and end < start:
        end = None
    if not end and not all_day:
        end = start + timedelta(hours=1)
    matter = None
    if v["matter_external_id"] or v["matter_number"] or v["matter_name"]:
        matter, how = resolve_matter(ctx, v["matter_external_id"], v["matter_number"], v["matter_name"])
        if not matter:
            msgs.append(f"Matter '{v['matter_number'] or v['matter_name'] or v['matter_external_id']}' not found; event kept without a matter.")
    ext = v["external_id"] or _hash_key(title, start.isoformat())
    existing = ctx.ref_get("event", ext)
    if ctx.dry:
        ctx.ref_set("event", ext, -1)
    rec = {"external_id": ext, "title": title[:300], "starts_at": start.isoformat(), "ends_at": end.isoformat() if end else None,
           "all_day": bool(all_day), "matter_id": matter.id if matter else None, "location": v["location"][:300],
           "notes": v["description"]}
    if existing:
        if ctx.options.get("duplicates") == "skip":
            return "skip", msgs + ["Already imported, skipped."], rec
        return "update", msgs + ["Already imported, updated."], rec
    return "create", msgs, rec


def apply_calendar(ctx, rec):
    e = ctx.get(CalendarEvent, ctx.ref_get("event", rec["external_id"]))
    created = e is None
    if created:
        e = CalendarEvent(title=rec["title"], starts_at=datetime.fromisoformat(rec["starts_at"]))
        db.session.add(e)
    e.title, e.matter_id, e.location, e.notes, e.all_day = rec["title"], rec["matter_id"], rec["location"], rec["notes"], rec["all_day"]
    e.starts_at = datetime.fromisoformat(rec["starts_at"])
    e.ends_at = datetime.fromisoformat(rec["ends_at"]) if rec["ends_at"] else None
    db.session.flush()
    ctx.ref_set("event", rec["external_id"], e.id)
    return e.id, created


def prep_notes(ctx, v, raw):
    msgs = []
    body = (v["body"] or "").strip()
    if not body:
        return "error", ["Note is empty."], None
    matter, how = resolve_matter(ctx, v["matter_external_id"], v["matter_number"], v["matter_name"])
    if not matter:
        who = v["matter_number"] or v["matter_name"] or v["matter_external_id"] or "(blank)"
        return "error", [f"Matter '{who}' not found."], None
    when = M.parse_any_datetime(v["date"])
    author = resolve_user(v["author"])
    if v["author"] and not author:
        msgs.append(f"Author '{v['author']}' is not a Coil user; recorded under you.")
    if v["subject"] and v["subject"].strip().lower() not in body.lower():
        body = f"{v['subject'].strip()}\n\n{body}"
    ext = v["external_id"] or _hash_key(matter.id, when.isoformat() if when else "", body[:200])
    existing = ctx.ref_get("note", ext)
    if ctx.dry:
        ctx.ref_set("note", ext, -1)
    rec = {"external_id": ext, "matter_id": matter.id, "body": body, "user_id": author.id if author else ctx.user.id,
           "created_at": when.isoformat() if when else None}
    msgs.insert(0, f"Note on {matter.label}.")
    if existing:
        if ctx.options.get("duplicates") == "skip":
            return "skip", msgs + ["Already imported, skipped."], rec
        return "update", msgs + ["Already imported, updated."], rec
    return "create", msgs, rec


def apply_notes(ctx, rec):
    n = ctx.get(Note, ctx.ref_get("note", rec["external_id"]))
    created = n is None
    if created:
        n = Note(matter_id=rec["matter_id"], body=rec["body"])
        db.session.add(n)
    n.matter_id, n.body, n.user_id = rec["matter_id"], rec["body"], rec["user_id"]
    if rec["created_at"]:
        n.created_at = datetime.fromisoformat(rec["created_at"])
    db.session.flush()
    ctx.ref_set("note", rec["external_id"], n.id)
    return n.id, created


PREP = {"contacts": prep_contacts, "matters": prep_matters, "activities": prep_activities, "bills": prep_bills,
        "trust": prep_trust, "tasks": prep_tasks, "calendar": prep_calendar, "notes": prep_notes}
APPLY = {"contacts": apply_contacts, "matters": apply_matters, "activities": apply_activities, "bills": apply_bills,
         "trust": apply_trust, "tasks": apply_tasks, "calendar": apply_calendar, "notes": apply_notes}


# ---------------------------------------------------------------- run (dry or real)
def _ordered_rows(entity, rows, mapping):
    if entity != "trust":
        return rows
    def key(r):
        d = M.parse_any_date(M.row_values(mapping, r).get("date"))
        return (d or date.max, r.get("_row", 0))
    return sorted(rows, key=key)


def run_import(data, mapping, options, user, dry):
    """Returns {"counts": {...}, "rows": [sample], "errors": [{"row", "message", "data"}], "id_map": {...}}."""
    entity, source = data["entity"], data["source"]
    ctx = Ctx(source, entity, user, mapping, options, dry)
    counts = {"rows": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0}
    sample, errors, id_map = [], [], {}
    prep, apply = PREP[entity], APPLY[entity]
    rows = _ordered_rows(entity, data["rows"], mapping)
    since_commit = 0
    for raw in rows:
        counts["rows"] += 1
        v = M.row_values(mapping, raw)
        try:
            action, msgs, rec = prep(ctx, v, raw)
        except Exception as e:  # noqa: BLE001 - a bad row must not kill the file
            action, msgs, rec = "error", [f"Could not read this row: {e}"], None
        if action in ("create", "update") and not dry:
            try:
                with db.session.begin_nested():
                    coil_id, created = apply(ctx, rec)
                if rec.get("external_id"):
                    id_map[str(rec["external_id"])[:120]] = coil_id
                action = "create" if created else "update"
                since_commit += 1
            except Exception as e:  # noqa: BLE001 - the savepoint rolled back, the rest of the file goes on
                action, msgs = "error", msgs + [f"Failed to save: {e}"]
        if action == "error":
            counts["errors"] += 1
            errors.append({"row": raw.get("_row"), "message": " ".join(msgs), "data": {k: val for k, val in raw.items() if k != "_row"}})
        else:
            counts["created" if action == "create" else "updated" if action == "update" else "skipped"] += 1
        if len(sample) < SAMPLE:
            sample.append({"row": raw.get("_row"), "action": action, "messages": msgs, "values": v})
        if not dry and since_commit >= BATCH:
            db.session.commit()
            since_commit = 0
    if not dry:
        db.session.commit()
    return {"counts": counts, "rows": sample, "errors": errors, "id_map": id_map}


# ---------------------------------------------------------------- documents ZIP
def _match_folder(folder):
    f = M.clean_name(folder)
    if not f:
        return None
    m = find_matter_by_number(f) or find_matter_by_name(f)
    if m:
        return m
    fl = f.lower()
    cands = [x for x in Matter.query.all() if x.number and (x.number.lower() in fl or fl in (x.name or "").lower()
                                                            or (x.name or "").lower() in fl)]
    return cands[0] if len(cands) == 1 else None


def _zip_plan(path):
    from .documents import BLOCKED_EXT
    folders, skipped = {}, []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p]
            if not parts or any(p.startswith("__MACOSX") or p.startswith(".") for p in parts):
                continue
            if len(parts) < 2:
                skipped.append({"file": name, "reason": "Not inside a matter folder."})
                continue
            ext = parts[-1].rsplit(".", 1)[-1].lower() if "." in parts[-1] else ""
            if ext in BLOCKED_EXT:
                skipped.append({"file": name, "reason": f".{ext} files are not allowed."})
                continue
            if info.file_size > 25 * 1024 * 1024:
                skipped.append({"file": name, "reason": "Over 25 MB."})
                continue
            if info.file_size == 0:
                skipped.append({"file": name, "reason": "Empty file."})
                continue
            top = parts[0]
            folders.setdefault(top, {"folder": top, "files": [], "matter_id": None, "matter_label": ""})
            folders[top]["files"].append({"path": name, "name": parts[-1], "sub": "/".join(parts[1:-1]), "size": info.file_size})
    for top, f in folders.items():
        m = _match_folder(top)
        if m:
            f["matter_id"], f["matter_label"] = m.id, m.label
    return list(folders.values()), skipped


def _apply_zip(data, user):
    from .documents import store_bytes
    counts = {"rows": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0}
    errors, id_map = [], {}
    overrides = data.get("folder_matters", {})
    with zipfile.ZipFile(_token_path(data["token"], "zip")) as z:
        n = 0
        for f in data["folders"]:
            mid = overrides.get(f["folder"]) or f["matter_id"]
            for entry in f["files"]:
                n += 1
                counts["rows"] += 1
                if not mid:
                    counts["errors"] += 1
                    errors.append({"row": n, "message": f"Folder '{f['folder']}' does not match a matter.", "data": {"file": entry["path"]}})
                    continue
                ext = entry["path"][:120]
                if ExternalRef.query.filter_by(source=data["source"], entity="document", external_id=ext).first():
                    counts["skipped"] += 1
                    continue
                try:
                    with db.session.begin_nested():
                        doc, err = store_bytes(int(mid), entry["name"], z.read(entry["path"]), user_id=user.id,
                                               folder="Imported" + ("/" + entry["sub"] if entry["sub"] else ""))
                        if err:
                            raise ValueError(err)
                        db.session.flush()
                        db.session.add(ExternalRef(source=data["source"], entity="document", external_id=ext, coil_id=doc.id))
                    counts["created"] += 1
                    id_map[ext] = doc.id
                except Exception as e:  # noqa: BLE001
                    counts["errors"] += 1
                    errors.append({"row": n, "message": f"{entry['path']}: {e}", "data": {"file": entry["path"]}})
            if n % BATCH == 0:
                db.session.commit()
        for s in data.get("skipped", []):
            counts["rows"] += 1
            counts["skipped"] += 1
    db.session.commit()
    return {"counts": counts, "errors": errors, "id_map": id_map}


# ---------------------------------------------------------------- routes
def _source_from_form():
    s = (request.form.get("source") or session.get("import_source") or "generic").lower()
    if s not in M.SOURCE_LABELS:
        s = "generic"
    session["import_source"] = s
    return s


@bp.route("", strict_slashes=False)
@bp.route("/")
@owner_required
def index():
    jobs = ImportJob.query.order_by(ImportJob.created_at.desc()).limit(50).all()
    clients = Contact.query.filter_by(is_client=True).all()
    clients.sort(key=lambda c: c.sort_name.lower())
    matters = Matter.query.order_by(Matter.number).all()
    return render_template("importer/index.html", sources=M.SOURCES, entities=M.ENTITIES, jobs=jobs,
                           source=session.get("import_source", "clio"), clients=clients, matters=matters,
                           entity_labels=M.ENTITY_LABELS, source_labels=M.SOURCE_LABELS)


@bp.route("/guide")
@login_required
def guide():
    return render_template("importer/guide.html", sources=M.SOURCES)


@bp.route("/<entity>/upload", methods=["POST"])
@owner_required
def upload(entity):
    if entity not in M.ENTITY_LABELS:
        abort(404)
    source = _source_from_form()
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a file to upload.", "error")
        return redirect(url_for("importer.index"))
    token = uuid.uuid4().hex
    if entity == "documents":
        raw = f.read(MAX_ZIP_BYTES + 1)
        if len(raw) > MAX_ZIP_BYTES:
            flash("That ZIP is over 200 MB. Split it into smaller ZIPs.", "error")
            return redirect(url_for("importer.index"))
        with open(_token_path(token, "zip"), "wb") as fh:
            fh.write(raw)
        try:
            folders, skipped = _zip_plan(_token_path(token, "zip"))
        except zipfile.BadZipFile:
            os.remove(_token_path(token, "zip"))
            flash("That file is not a ZIP archive.", "error")
            return redirect(url_for("importer.index"))
        if not folders:
            os.remove(_token_path(token, "zip"))
            flash("No files found inside matter folders. The ZIP needs one top-level folder per matter.", "error")
            return redirect(url_for("importer.index"))
        _save(token, {"token": token, "entity": entity, "source": source, "filename": f.filename, "folders": folders,
                      "skipped": skipped, "folder_matters": {}})
        return redirect(url_for("importer.preview", token=token))
    raw = f.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        flash("That CSV is over 20 MB. Split it into smaller files.", "error")
        return redirect(url_for("importer.index"))
    headers, rows = _parse_csv(raw)
    if not headers or not rows:
        flash("Could not read any rows from that file. It needs a header row and at least one data row.", "error")
        return redirect(url_for("importer.index"))
    mapping = _refine_mapping(entity, source, headers, rows, M.auto_map(source, entity, headers))
    _save(token, {"token": token, "entity": entity, "source": source, "filename": f.filename, "headers": headers,
                  "rows": rows, "mapping": mapping,
                  "options": {"duplicates": "update", "keep_unmapped": False, "mark_clients": entity == "contacts"}})
    return redirect(url_for("importer.preview", token=token))


def _mapping_from_form(entity, headers):
    mapping = {}
    for field, _label, _req, _multi in M.field_defs(entity):
        val = request.form.get(f"map_{field}", "")
        if val and all(h in headers for h in val.split("|")):
            mapping[field] = val
        else:
            mapping[field] = ""
    return mapping


def _options_from_form(entity):
    return {"duplicates": "skip" if request.form.get("duplicates") == "skip" else "update",
            "keep_unmapped": bool(request.form.get("keep_unmapped")),
            "mark_clients": bool(request.form.get("mark_clients"))}


@bp.route("/preview/<token>", methods=["GET", "POST"])
@owner_required
def preview(token):
    data = _load(token)
    if not data:
        flash("That upload has expired. Upload the file again.", "error")
        return redirect(url_for("importer.index"))
    entity = data["entity"]
    if entity == "documents":
        if request.method == "POST":
            overrides = {}
            for f in data["folders"]:
                v = request.form.get("folder_" + hashlib.md5(f["folder"].encode()).hexdigest())
                if v and v.isdigit():
                    overrides[f["folder"]] = int(v)
            data["folder_matters"] = overrides
            _save(token, data)
            if request.form.get("do") == "commit":
                return _commit(data)
        matters = Matter.query.order_by(Matter.number).all()
        return render_template("importer/preview_zip.html", data=data, matters=matters, token=token,
                               source_label=M.SOURCE_LABELS.get(data["source"], data["source"]),
                               folder_key=lambda s: hashlib.md5(s.encode()).hexdigest())
    if request.method == "POST":
        # A commit posted without the mapping selects (a bare "import" call) keeps what the preview stored.
        if any(k.startswith("map_") for k in request.form):
            data["mapping"] = _mapping_from_form(entity, data["headers"])
        if "duplicates" in request.form:
            data["options"] = _options_from_form(entity)
        _save(token, data)
        if request.form.get("do") == "commit":
            return _commit(data)
    result = run_import(data, data["mapping"], data["options"], current_user(), dry=True)
    fields = M.field_defs(entity)
    missing_required = [label for f, label, req, _ in fields if req and not data["mapping"].get(f)]
    unmapped = M.unmapped_headers(data["headers"], data["mapping"])
    return render_template("importer/preview.html", data=data, token=token, result=result, fields=fields,
                           missing_required=missing_required, unmapped=unmapped,
                           entity_label=M.ENTITY_LABELS[entity], source_label=M.SOURCE_LABELS.get(data["source"], data["source"]),
                           first_row=(data["rows"][0] if data["rows"] else {}))


def _commit(data):
    user = current_user()
    entity = data["entity"]
    if entity == "documents":
        result = _apply_zip(data, user)
        mapping_json = {"headers": ["file"], "folders": {f["folder"]: (data.get("folder_matters", {}).get(f["folder"]) or f["matter_id"]) for f in data["folders"]}}
    else:
        fields = M.field_defs(entity)
        missing = [label for f, label, req, _ in fields if req and not data["mapping"].get(f)]
        if missing:
            flash("Map these required fields first: " + ", ".join(missing), "error")
            return redirect(url_for("importer.preview", token=data["token"]))
        result = run_import(data, data["mapping"], data["options"], user, dry=False)
        mapping_json = {"headers": data["headers"], "mapping": data["mapping"], "options": data["options"]}
    job = ImportJob(source=data["source"], entity=entity, filename=data["filename"][:300],
                    mapping_json=json.dumps(mapping_json), rows=result["counts"]["rows"], created=result["counts"]["created"],
                    updated=result["counts"]["updated"], skipped=result["counts"]["skipped"],
                    errors_json=json.dumps(result["errors"]), status="committed", id_map_json=json.dumps(result["id_map"]),
                    created_by_id=user.id)
    db.session.add(job)
    db.session.flush()
    audit("import", entity, job.id, f"{M.SOURCE_LABELS.get(data['source'], data['source'])} {data['filename']}: "
                                    f"{job.created} created, {job.updated} updated, {job.skipped} skipped, "
                                    f"{len(result['errors'])} errors", user.id)
    db.session.commit()
    for ext in ("json", "zip"):
        p = _token_path(data["token"], ext)
        if os.path.isfile(p):
            os.remove(p)
    flash(f"{M.ENTITY_LABELS[entity]}: {job.created} created, {job.updated} updated, {job.skipped} skipped, "
          f"{len(result['errors'])} rows with problems.", "ok" if not result["errors"] else "error")
    return redirect(url_for("importer.job", job_id=job.id))


@bp.route("/jobs/<int:job_id>")
@owner_required
def job(job_id):
    j = db.session.get(ImportJob, job_id) or abort(404)
    try:
        meta = json.loads(j.mapping_json or "{}")
    except Exception:
        meta = {}
    return render_template("importer/job.html", job=j, meta=meta, entity_label=M.ENTITY_LABELS.get(j.entity, j.entity),
                           source_label=M.SOURCE_LABELS.get(j.source, j.source))


@bp.route("/jobs/<int:job_id>/failed.csv")
@owner_required
def failed_csv(job_id):
    j = db.session.get(ImportJob, job_id) or abort(404)
    try:
        meta = json.loads(j.mapping_json or "{}")
    except Exception:
        meta = {}
    headers = meta.get("headers") or []
    errs = j.errors
    if not headers:
        seen = []
        for e in errs:
            for k in (e.get("data") or {}):
                if k not in seen:
                    seen.append(k)
        headers = seen
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(headers + ["Import error"])
    for e in errs:
        d = e.get("data") or {}
        w.writerow([d.get(h, "") for h in headers] + [e.get("message", "")])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=import-{j.id}-failed-rows.csv"})


@bp.route("/trust/opening", methods=["POST"])
@owner_required
def trust_opening():
    from ..helpers import parse_money, parse_date
    client = db.session.get(Contact, int(request.form.get("client_id") or 0))
    matter_id = request.form.get("matter_id") or ""
    matter = db.session.get(Matter, int(matter_id)) if matter_id.isdigit() else None
    amount = parse_money(request.form.get("amount"))
    when = M.parse_any_date(request.form.get("date")) or parse_date(request.form.get("date")) or date.today()
    if not client or amount <= 0:
        flash("Pick a client and enter a positive opening balance.", "error")
        return redirect(url_for("importer.index"))
    if matter and matter.client_id != client.id:
        flash("That matter does not belong to the selected client.", "error")
        return redirect(url_for("importer.index"))
    t = TrustTransaction(client_id=client.id, matter_id=matter.id if matter else None, date=when, type="deposit",
                         amount_cents=amount, description="Opening balance carried over from previous system",
                         reference="opening", cleared=True, cleared_on=when, created_by_id=current_user().id)
    db.session.add(t)
    db.session.flush()
    audit("trust_deposit", "trust_transaction", t.id, f"Opening balance {client.display_name} {cents_to_str(amount)}",
          current_user().id)
    db.session.commit()
    flash(f"Opening trust balance of {cents_to_str(amount)} recorded for {client.display_name}.", "ok")
    return redirect(url_for("importer.index"))
