"""Outgoing webhooks.

Events are collected by SQLAlchemy mapper listeners during a flush (matter.created, invoice.sent, ...),
parked on the session, and delivered right after the commit succeeds so a rolled-back change never fires a
webhook. Delivery rows are written through a short-lived Session bound to the engine, which keeps them out of
the caller's transaction and makes `deliver_event` safe to call from anywhere, including after_commit hooks.

Each POST carries the JSON body, `X-Coil-Event`, `X-Coil-Delivery` and `X-Coil-Signature: sha256=<hmac>` where
the HMAC is computed over the raw body with the webhook's secret. `python -m app.cli webhooks` retries failed
deliveries with backoff, up to MAX_ATTEMPTS. There are no routes here; the CRUD pages live in settings.py.
"""
import hashlib
import hmac
import json
import logging
from datetime import date, datetime, timedelta

import requests
from flask import Blueprint
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session, object_session

from ..extensions import db
from ..models import (Webhook, WebhookDelivery, Matter, Invoice, Payment, Engagement, DocumentSignature,
                      IntakeLead, Task, now)

bp = Blueprint("webhooks_out", __name__)
log = logging.getLogger(__name__)

EVENTS = [
    ("matter.created", "A matter is opened"),
    ("matter.closed", "A matter is closed"),
    ("invoice.sent", "An invoice is emailed to the client"),
    ("invoice.paid", "An invoice reaches paid in full"),
    ("payment.received", "A payment is recorded (operating or trust)"),
    ("engagement.signed", "An engagement letter is signed"),
    ("document_signature.signed", "A document signature request is signed"),
    ("intake_lead.created", "A new intake lead arrives"),
    ("task.completed", "A task is marked done"),
]
EVENT_NAMES = [e for e, _ in EVENTS]
MAX_ATTEMPTS = 5
TIMEOUT_SECONDS = 5
# Minutes to wait after attempt n (1-based) before trying again.
BACKOFF_MINUTES = (1, 5, 30, 120, 720)

_ready_cache = {}


# ---------------------------------------------------------------- helpers
def sign(secret, body):
    """'sha256=<hex hmac>' over the raw body bytes."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    return "sha256=" + hmac.new((secret or "").encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(secret, body, header):
    return hmac.compare_digest(sign(secret, body), header or "")


def hook_events(hook):
    return [e.strip() for e in (hook.events or "").split(",") if e.strip()]


def _j(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _tables_ready(engine):
    key = str(engine.url)
    if key not in _ready_cache:
        insp = sa_inspect(engine)
        _ready_cache[key] = insp.has_table("webhooks") and insp.has_table("webhook_deliveries")
    return _ready_cache[key]


def build_body(delivery):
    return delivery.payload_json.encode("utf-8")


def attempt_delivery(delivery, hook):
    """One HTTP attempt. Mutates the delivery (attempts, status, response_code, last_error, last_at).
    Returns True on a 2xx."""
    body = build_body(delivery)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Coil-Webhooks/1.0",
        "X-Coil-Event": delivery.event,
        "X-Coil-Delivery": str(delivery.id),
        "X-Coil-Signature": sign(hook.secret, body),
    }
    delivery.attempts = (delivery.attempts or 0) + 1
    delivery.last_at = now()
    try:
        r = requests.post(hook.url, data=body, headers=headers, timeout=TIMEOUT_SECONDS)
        delivery.response_code = getattr(r, "status_code", None)
        if delivery.response_code is not None and 200 <= delivery.response_code < 300:
            delivery.status = "ok"
            delivery.last_error = ""
            return True
        delivery.status = "failed"
        delivery.last_error = f"HTTP {delivery.response_code}"[:300]
    except Exception as e:  # network errors, timeouts, bad URLs
        delivery.response_code = None
        delivery.status = "failed"
        delivery.last_error = (str(e) or e.__class__.__name__)[:300]
    return False


def deliver_event(name, payload):
    """Create one WebhookDelivery per active webhook subscribed to `name` and try each once.
    Returns the delivery ids. Uses its own Session so it never touches the caller's transaction."""
    engine = db.engine
    if not _tables_ready(engine):
        return []
    envelope = {"event": name, "created_at": now().isoformat(), "data": {k: _j(v) for k, v in (payload or {}).items()}}
    body = json.dumps(envelope, sort_keys=True)
    ids = []
    with Session(engine) as s:
        hooks = [h for h in s.query(Webhook).filter(Webhook.is_active == True).all()  # noqa: E712
                 if name in hook_events(h)]
        if not hooks:
            return []
        deliveries = []
        for h in hooks:
            d = WebhookDelivery(webhook_id=h.id, event=name, payload_json=body, status="pending", attempts=0)
            s.add(d)
            deliveries.append((d, h))
        s.commit()
        for d, h in deliveries:
            attempt_delivery(d, h)
            ids.append(d.id)
        s.commit()
    return ids


def due_for_retry(d, at=None):
    at = at or now()
    if d.status != "failed" or (d.attempts or 0) >= MAX_ATTEMPTS:
        return False
    if not d.last_at:
        return True
    wait = BACKOFF_MINUTES[min(d.attempts, len(BACKOFF_MINUTES)) - 1]
    return d.last_at + timedelta(minutes=wait) <= at


def run_webhooks(force=False, at=None):
    """Retry failed deliveries whose backoff has elapsed (all failed ones under the cap when force).
    Uses db.session; meant for the CLI and the settings page. Returns (retried, ok, still_failed)."""
    q = WebhookDelivery.query.filter(WebhookDelivery.status.in_(["failed", "pending"]),
                                     WebhookDelivery.attempts < MAX_ATTEMPTS).order_by(WebhookDelivery.id)
    retried = ok = failed = 0
    for d in q.all():
        if not force and d.status == "failed" and not due_for_retry(d, at):
            continue
        hook = d.webhook
        if not hook or not hook.is_active:
            continue
        retried += 1
        if attempt_delivery(d, hook):
            ok += 1
        else:
            failed += 1
        db.session.commit()
    return retried, ok, failed


# ---------------------------------------------------------------- event collection
def _queue(target, name, payload):
    sess = object_session(target)
    if sess is None:
        return
    sess.info.setdefault("coil_events", []).append((name, payload))


def _changed(target, attr):
    """(changed, old, new) for a column attribute in the current flush."""
    h = sa_inspect(target).attrs[attr].history
    if not h.has_changes():
        return False, None, None
    old = h.deleted[0] if h.deleted else None
    new = h.added[0] if h.added else None
    return True, old, new


def _matter_payload(m):
    return {"id": m.id, "number": m.number, "name": m.name, "status": m.status, "practice_area": m.practice_area,
            "client_id": m.client_id, "opened_on": m.opened_on, "closed_on": m.closed_on,
            "responsible_user_id": m.responsible_user_id}


def _invoice_payload(i):
    return {"id": i.id, "number": i.number, "status": i.status, "matter_id": i.matter_id, "client_id": i.client_id,
            "issued_on": i.issued_on, "due_on": i.due_on, "total_cents": i.total_cents, "paid_cents": i.paid_cents,
            "balance_cents": max(0, (i.total_cents or 0) - (i.paid_cents or 0)), "currency": i.currency,
            "sent_at": i.sent_at}


@event.listens_for(Session, "after_commit")
def _send_queued(session):
    events = session.info.pop("coil_events", None)
    if not events:
        return
    for name, payload in events:
        try:
            deliver_event(name, payload)
        except Exception:
            log.exception("webhook delivery for %s failed", name)


@event.listens_for(Session, "after_rollback")
def _drop_queued(session):
    session.info.pop("coil_events", None)


@event.listens_for(Matter, "after_insert")
def _matter_created(mapper, connection, m):
    _queue(m, "matter.created", _matter_payload(m))


@event.listens_for(Matter, "after_update")
def _matter_updated(mapper, connection, m):
    changed, old, new = _changed(m, "status")
    if changed and new == "closed" and old != "closed":
        _queue(m, "matter.closed", _matter_payload(m))


@event.listens_for(Invoice, "after_update")
def _invoice_updated(mapper, connection, i):
    changed, old, new = _changed(i, "sent_at")
    if changed and new:
        _queue(i, "invoice.sent", _invoice_payload(i))
    changed, old, new = _changed(i, "status")
    if changed and new == "paid" and old != "paid":
        _queue(i, "invoice.paid", _invoice_payload(i))


@event.listens_for(Payment, "after_insert")
def _payment_received(mapper, connection, p):
    _queue(p, "payment.received", {
        "id": p.id, "invoice_id": p.invoice_id, "matter_id": p.matter_id, "client_id": p.client_id,
        "amount_cents": p.amount_cents, "surcharge_cents": p.surcharge_cents, "stripe_fee_cents": p.stripe_fee_cents,
        "method": p.method, "account": p.account, "reference": p.reference, "received_on": p.received_on})


@event.listens_for(Engagement, "after_update")
def _engagement_updated(mapper, connection, e):
    changed, old, new = _changed(e, "status")
    if changed and new == "signed" and old != "signed":
        _queue(e, "engagement.signed", {"id": e.id, "matter_id": e.matter_id, "contact_id": e.contact_id,
                                        "status": e.status, "signed_at": e.signed_at})


@event.listens_for(DocumentSignature, "after_update")
def _signature_updated(mapper, connection, s):
    changed, old, new = _changed(s, "status")
    if changed and new == "signed" and old != "signed":
        _queue(s, "document_signature.signed", {"id": s.id, "document_id": s.document_id, "contact_id": s.contact_id,
                                                "status": s.status, "signed_at": s.signed_at})


@event.listens_for(IntakeLead, "after_insert")
def _lead_created(mapper, connection, l):
    _queue(l, "intake_lead.created", {"id": l.id, "name": l.name, "email": l.email, "phone": l.phone,
                                      "matter_type": l.matter_type, "source": l.source, "status": l.status})


@event.listens_for(Task, "after_update")
def _task_updated(mapper, connection, t):
    changed, old, new = _changed(t, "done")
    if changed and new and not old:
        _queue(t, "task.completed", {"id": t.id, "title": t.title, "matter_id": t.matter_id, "kind": t.kind,
                                     "due_on": t.due_on, "done_at": t.done_at, "assignee_id": t.assignee_id})
