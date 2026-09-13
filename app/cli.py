"""Coil CLI commands.

Run with: `docker compose exec coil python -m app.cli <command>`

Available commands:
- agenda, reminders, interest, emailin, sequences, monthly_invoicing, webhooks
- backup (new: creates dated backup of data/)
- voice_reminders, payment_plans, case_audit
"""
import os
import sqlite3
import sys
import tarfile
from datetime import date, datetime, timedelta
from pathlib import Path
from markupsafe import escape
from flask import current_app
from .config import DATA_DIR
from .extensions import db
from .models import (Firm, User, Task, Matter, Invoice, InvoiceEvent, Engagement, IntakeLead, AuditLog, audit, now)
from .helpers import cents_to_str
from .services.mail import send_email
from .blueprints.webhooks_out import run_webhooks


def _today_start():
    return datetime.combine(date.today(), datetime.min.time())


def _base():
    return current_app.config["BASE_URL"]


def _wrap(title, sections):
    """sections: list of (heading, [html list items]). Empty sections are skipped."""
    body = "".join(f"<h3 style='margin:16px 0 6px;font-size:15px'>{escape(h)}</h3><ul style='margin:0;padding-left:18px'>"
                   + "".join(f"<li>{item}</li>" for item in items) + "</ul>" for h, items in sections if items)
    if not body:
        body = "<p>Nothing on the list today.</p>"
    return (f"<div style='font-family:Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;color:#1c2430'>"
            f"<h2 style='font-size:18px'>{escape(title)}</h2>{body}</div>")


def _link(path, text):
    return f"<a href='{_base()}{path}'>{escape(text)}</a>"


# ---------------------------------------------------------------------------
# agenda
# ---------------------------------------------------------------------------
def build_agenda(user):
    today = date.today()
    soon = today + timedelta(days=14)
    tasks = Task.query.filter(Task.done == False, Task.due_on != None, Task.due_on <= today,
                              db.or_(Task.assignee_id == user.id, Task.assignee_id == None)).order_by(Task.due_on).all()
    sols = Matter.query.filter(Matter.status != "closed", Matter.sol_date != None, Matter.sol_date <= soon).order_by(
        Matter.sol_date).all()
    overdue = Invoice.query.filter(Invoice.status.in_(["sent", "viewed", "partial"]), Invoice.due_on != None,
                                   Invoice.due_on < today).order_by(Invoice.due_on).all()
    stale = Engagement.query.filter(Engagement.status.in_(["sent", "viewed"]), Engagement.sent_at != None,
                                    Engagement.sent_at <= now() - timedelta(days=2)).order_by(
        Engagement.sent_at).all()
    leads = IntakeLead.query.filter_by(status="new").order_by(IntakeLead.created_at.desc()).all()
    # Agent R: time capture suggestions waiting for this user (browser extension). Additive, never blocks the agenda.
    try:
        from .models import TimeSuggestion
        pending_suggestions = TimeSuggestion.query.filter_by(user_id=user.id, status="pending").count()
        suggestion_minutes = sum(int(s.minutes or 0) for s in
                                 TimeSuggestion.query.filter_by(user_id=user.id, status="pending").all())
    except Exception:
        pending_suggestions, suggestion_minutes = 0, 0

    def task_item(t):
        tag = "overdue" if t.due_on < today else "today"
        m = f" ({escape(t.matter.number)})" if t.matter else ""
        return f"[{tag}] {_link(f'/tasks/{t.id}', t.title)}{m}, due {t.due_on:%b %-d}"

    sections = [
        ("Tasks due today or overdue", [task_item(t) for t in tasks]),
        ("Limitations deadlines within 14 days", [
            f"{_link(f'/matters/{m.id}', m.label)}: {m.sol_date:%b %-d, %Y}" + (f" ({escape(m.sol_basis)})" if m.sol_basis else "")
            for m in sols]),
        ("Overdue invoices", [
            f"{_link(f'/invoices/{i.id}', i.number or 'invoice')} {escape(i.client.display_name if i.client else '')}, "
            f"{cents_to_str(i.balance_cents)} due {i.due_on:%b %-d}" for i in overdue]),
        ("Engagement letters unsigned for more than 2 days", [
            f"{_link(f'/engagements/{e.id}', e.contact.display_name)}: {escape(e.matter.name)}, sent {e.sent_at:%b %-d}, "
            f"viewed {e.view_count or 0}x" for e in stale]),
        ("New intake leads", [
            f"{_link(f'/intake/{l.id}', l.name)} ({escape(l.matter_type or 'no type')}), {l.created_at:%b %-d}" for l in leads]),
        ("Time capture suggestions", [
            f"{pending_suggestions} pending suggestion{'s' if pending_suggestions != 1 else ''} "
            f"({suggestion_minutes / 60:.1f} h captured): {_link('/time/suggestions', 'review and log them')}"
        ] if pending_suggestions else []),
    ]
    return sections


def run_agenda():
    """Email each active user their agenda once per day. Returns the number of emails sent."""
    firm = Firm.get()
    if not firm.daily_agenda_email:
        return 0
    start = _today_start()
    sent = 0
    for u in User.query.filter_by(is_active=True).all():
        already = AuditLog.query.filter(AuditLog.action == "agenda_sent", AuditLog.user_id == u.id,
                                        AuditLog.created_at >= start).first()
        if already or not u.email:
            continue
        sections = build_agenda(u)
        n = sum(len(items) for _, items in sections)
        title = f"Agenda for {date.today():%A, %B %-d}"
        html = _wrap(title, sections)
        send_email(u.email, f"{title}: {n} item{'s' if n != 1 else ''}", html,
                   text=f"Your agenda has {n} items. Open {_base()}/ to review.")
        audit("agenda_sent", "user", u.id, f"{n} items", u.id)
        db.session.commit()
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# reminders
# ---------------------------------------------------------------------------
def _already_reminded(entity, entity_id, today_iso):
    return AuditLog.query.filter_by(action="reminder_sent", entity=entity, entity_id=entity_id,
                                    detail=today_iso).first() is not None


def send_invoice_reminder(inv, days_past):
    firm = Firm.get()
    to = inv.sent_to or (inv.client.email if inv.client else "")
    url = f"{_base()}/p/{inv.public_token}"
    pixel = f"{_base()}/track/invoice/{inv.public_token}.gif"
    subject = f"Reminder: invoice {inv.number} from {firm.name} is {days_past} days past due"
    html = (f"<div style='font-family:Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1c2430'>"
            f"<p>Hello {escape(inv.client.first_name or inv.client.display_name if inv.client else '')},</p>"
            f"<p>Invoice {escape(inv.number or '')} for {cents_to_str(inv.balance_cents)} was due on "
            f"{inv.due_on:%B %-d, %Y}. You can view and pay it here:</p>"
            f"<p><a href='{url}' style='background:#1f5f8b;color:#fff;padding:10px 18px;border-radius:6px;"
            f"text-decoration:none;display:inline-block'>View invoice</a></p>"
            f"<p style='font-size:12px;color:#666'>Link: {url}</p>"
            f"<p style='font-size:13px;color:#666'>{escape(firm.name or '')}<br>{escape(firm.phone or '')}</p>"
            f"<img src='{pixel}' width='1' height='1' alt=''></div>")
    if to:
        send_email(to, subject, html, text=f"Invoice {inv.number} is past due. View and pay: {url}",
                   reply_to=firm.email or None)
    db.session.add(InvoiceEvent(invoice_id=inv.id, event="reminder", detail=f"{days_past} days past due, to {to or 'no email'}"))
    return to


def run_reminders():
    """Invoice reminders at 7 and 21 days past due, engagement reminders at 3 and 7 days after sending.

    Returns (invoice_reminders, engagement_reminders) sent this run.
    """
    from .blueprints.engagements import send_engagement_reminder
    today = date.today()
    today_iso = today.isoformat()
    inv_count = eng_count = 0

    for days in (7, 21):
        due = today - timedelta(days=days)
        for inv in Invoice.query.filter(Invoice.status.in_(["sent", "viewed", "partial"]), Invoice.due_on == due).all():
            if _already_reminded("invoice", inv.id, today_iso):
                continue
            send_invoice_reminder(inv, days)
            audit("reminder_sent", "invoice", inv.id, today_iso)
            db.session.commit()
            inv_count += 1

    # Engagement.sent_at is naive UTC, so the day window must be built from the UTC date,
    # not the local one, or reminders silently skip near midnight.
    utc_today = now().date()
    for days in (3, 7):
        day = utc_today - timedelta(days=days)
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        for e in Engagement.query.filter(Engagement.status.in_(["sent", "viewed"]), Engagement.sent_at >= start,
                                         Engagement.sent_at < end).all():
            if _already_reminded("engagement", e.id, today_iso):
                continue
            send_engagement_reminder(e, detail=f"{days} days unsigned")
            audit("reminder_sent", "engagement", e.id, today_iso)
            db.session.commit()
            eng_count += 1
    return inv_count, eng_count


# ---------------------------------------------------------------------------
# evergreen retainer top-ups (runs with `reminders`)
# ---------------------------------------------------------------------------
EVERGREEN_DAYS = 14


def run_evergreen():
    """Email a trust top-up request for every open matter under its evergreen minimum, at most once per
    matter every 14 days (AuditLog action="evergreen_sent"). Returns the number of requests sent."""
    from .blueprints.trust import evergreen_shortfalls, send_deposit_request
    since = now() - timedelta(days=EVERGREEN_DAYS)
    sent = 0
    for matter, balance, shortfall in evergreen_shortfalls():
        recent = AuditLog.query.filter(AuditLog.action == "evergreen_sent", AuditLog.entity == "matter",
                                       AuditLog.entity_id == matter.id, AuditLog.created_at >= since).first()
        if recent:
            continue
        client = matter.client
        if not client or not client.email:
            audit("evergreen_skipped", "matter", matter.id, "client has no email")
            db.session.commit()
            continue
        try:
            send_deposit_request(client, matter, shortfall)
        except ValueError as e:
            audit("evergreen_skipped", "matter", matter.id, str(e)[:300])
            db.session.commit()
            continue
        audit("evergreen_sent", "matter", matter.id,
              f"balance {cents_to_str(balance)} below minimum {cents_to_str(matter.trust_minimum_cents)}; "
              f"requested {cents_to_str(shortfall)} from {client.email}")
        db.session.commit()
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# interest on overdue invoices
# ---------------------------------------------------------------------------
def run_interest(today=None):
    """Add one interest line per overdue invoice per month. Returns (invoices_charged, total_cents)."""
    from .blueprints.invoices import apply_interest, OPEN_STATUSES
    firm = Firm.get()
    if not firm.interest_apr_bps:
        return 0, 0
    today = today or date.today()
    cutoff = today - timedelta(days=firm.interest_grace_days or 0)
    count = total = 0
    for inv in Invoice.query.filter(Invoice.status.in_(list(OPEN_STATUSES)), Invoice.due_on != None,
                                    Invoice.due_on < cutoff).order_by(Invoice.due_on).all():
        cents = apply_interest(inv, firm=firm, today=today)
        if cents:
            db.session.commit()
            count += 1
            total += cents
    return count, total


# ---------------------------------------------------------------------------
# email filing (IMAP -> matters)
# ---------------------------------------------------------------------------
def run_emailin():
    """Pull unseen mail from the IMAP_* mailbox and file it to matters. Idempotent on Message-ID.
    Returns dict(filed, unfiled, skipped). Does nothing when IMAP_HOST is blank."""
    from .blueprints.emailin import run_emailin as _run, imap_configured
    if not imap_configured():
        print("emailin: IMAP_HOST / IMAP_USER not set, nothing to do")
        return dict(filed=0, unfiled=0, skipped=0)
    return _run()


# ---------------------------------------------------------------------------
# follow-up sequences (Agent H)
# ---------------------------------------------------------------------------
def run_sequences(today=None):
    """Advance every active lead sequence whose next step date has arrived.

    Sends the step by email only when Firm.sequences_auto_send is on; otherwise it writes a draft Message that
    staff send from /intake/drafts. Idempotent per step (LeadSequence.next_step plus a unique Message key).
    Returns (sent, drafted).
    """
    from .models import LeadSequence
    from .blueprints.intake import process_lead_sequence
    today = today or date.today()
    auto = bool(Firm.get().sequences_auto_send)
    sent = drafted = 0
    for ls in LeadSequence.query.filter_by(status="active").order_by(LeadSequence.id).all():
        s, d = process_lead_sequence(ls, today, auto)
        sent += s
        drafted += d
    return sent, drafted


# ---------------------------------------------------------------------------
# monthly invoicing (Agent I; Clio Manage AI parity: "automates monthly invoicing")
# ---------------------------------------------------------------------------
def run_monthly_invoicing(today=None, force=False):
    """On Firm.monthly_billing_day (or any day with force=True), build one draft invoice per open matter that has
    auto_invoice_monthly on and something billable (unbilled time or expenses, or a flat-fee milestone due). When
    Firm.monthly_billing_send is on and the client has an email, the draft is sent right away. Idempotent per
    matter per month through AuditLog action="monthly_invoiced" detail=YYYY-MM. The owner gets one summary
    email listing what was built, sent and skipped.

    Returns dict(built=[Invoice], sent=[Invoice], skipped=[(matter, reason)], ran=bool, reason=str).
    """
    from .blueprints.invoices import build_for_matter, _send_invoice_email
    today = today or date.today()
    firm = Firm.get()
    out = {"built": [], "sent": [], "skipped": [], "ran": False, "reason": ""}
    day = firm.monthly_billing_day or 0
    if not day and not force:
        out["reason"] = "monthly billing day is 0 (off) in Settings, Invoice template"
        return out
    if day and today.day != day and not force:
        out["reason"] = f"today is day {today.day}, billing day is {day}"
        return out
    out["ran"] = True
    ym = today.strftime("%Y-%m")
    owner = (User.query.filter_by(role="owner", is_active=True).order_by(User.id).first()
             or User.query.filter_by(is_active=True).order_by(User.id).first())
    issued_on = today
    due_on = today + timedelta(days=firm.invoice_terms_days or 30)
    matters = Matter.query.filter(Matter.status != "closed", Matter.auto_invoice_monthly == True).order_by(  # noqa: E712
        Matter.number).all()
    for m in matters:
        done = AuditLog.query.filter_by(action="monthly_invoiced", entity="matter", entity_id=m.id, detail=ym).first()
        if done:
            out["skipped"].append((m, f"already invoiced for {ym}"))
            continue
        try:
            created = build_for_matter(m, owner, issued_on, due_on, today)
        except ValueError as e:
            out["skipped"].append((m, str(e)))
            continue
        if not created:
            out["skipped"].append((m, "nothing unbilled"))
            continue
        audit("monthly_invoiced", "matter", m.id, ym, owner.id if owner else None)
        db.session.commit()
        out["built"].extend(created)
        if firm.monthly_billing_send:
            for inv in created:
                if not (inv.client and inv.client.email):
                    out["skipped"].append((m, f"{inv.number} left as draft: client has no email"))
                    continue
                err = _send_invoice_email(inv)
                if err:
                    out["skipped"].append((m, f"{inv.number} not sent: {err}"))
                    continue
                db.session.add(InvoiceEvent(invoice_id=inv.id, event="sent", detail=f"to {inv.sent_to} (monthly run)"))
                audit("send", "invoice", inv.id, f"{inv.number} to {inv.sent_to} (monthly run)", owner.id if owner else None)
                db.session.commit()
                out["sent"].append(inv)
    _monthly_summary_email(firm, owner, out, today)
    return out


def _monthly_summary_email(firm, owner, out, today):
    to = (firm.email or (owner.email if owner else "") or "").strip()
    if not to:
        return
    built = out["built"]
    sent_ids = {i.id for i in out["sent"]}

    def inv_item(i):
        state = "sent to " + escape(i.sent_to) if i.id in sent_ids else "draft"
        return (f"{_link(f'/invoices/{i.id}', i.number)} {escape(i.matter.label if i.matter else '')}, "
                f"{escape(i.client.display_name if i.client else '')}, {cents_to_str(i.total_cents)} ({state})")

    sections = [
        ("Invoices built", [inv_item(i) for i in built]),
        ("Skipped", [f"{_link(f'/matters/{m.id}', m.label)}: {escape(reason)}" for m, reason in out["skipped"]]),
    ]
    title = f"Monthly invoicing for {today:%B %Y}"
    n_sent = len(out["sent"])
    summary = (f"{len(built)} invoice{'s' if len(built) != 1 else ''} built, {n_sent} sent, "
               f"{len(out['skipped'])} skipped")
    html = _wrap(title, sections).replace("<p>Nothing on the list today.</p>",
                                          "<p>No opted-in matter had anything to bill.</p>")
    html = html.replace(f"<h2 style='font-size:18px'>{escape(title)}</h2>",
                        f"<h2 style='font-size:18px'>{escape(title)}</h2><p>{summary}. Drafts wait under "
                        f"{_link('/invoices?status=draft', 'Invoices, Draft')}.</p>")
    send_email(to, f"{title}: {summary}", html, text=f"{summary}. Review at {_base()}/invoices?status=draft")


def run_case_audit(today=None):
    """Nightly case audit (Agent N). See app/blueprints/caseaudit.py. Idempotent: re-runs refresh last_seen_on,
    resolve what no longer holds, and email the summary at most once per day."""
    from .blueprints.caseaudit import run_case_audit as _run
    return _run(today=today)


# ---------------------------------------------------------------------------
# payment plans (Agent P; see app/blueprints/money.py)
# ---------------------------------------------------------------------------
def run_voice_reminders(today=None):
    """Outbound reminder calls for calendar events and court dates inside Firm.voice_reminder_days, one per
    event per client, idempotent through AuditLog action=voice_reminded. Logic lives in blueprints/voice.py."""
    from .blueprints.voice import run_voice_reminders as _run
    return _run(today=today)


def run_payment_plans(today=None):
    """Charge or remind every active payment plan due today or earlier. Once per plan per day through AuditLog
    plan_charged / plan_reminded. Returns dict(charged, reminded, failed, completed, skipped)."""
    from .blueprints.money import run_payment_plans as _run
    return _run(today=today)


def _sqlite_path():
    """Filesystem path of the SQLite database, or None if this instance is not on SQLite."""
    uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
    if not uri.startswith("sqlite:"):
        return None
    return Path(uri.split("sqlite:///", 1)[-1])


def backup():
    """Dated backup of the database, uploads and generated PDFs.

    The database is copied through SQLite's own online backup API rather than being read
    off disk by tar. tar on a live database can capture a torn file: pages written after
    tar passed them, plus a write-ahead log it never saw. That restores into a corrupt
    database, and you find out during the emergency. ops/backup.sh does the same thing
    from outside the container; this is the in-container equivalent for self-hosters.
    """
    data_dir = Path(DATA_DIR)
    # Inside the data directory on purpose: that is the path bind-mounted from the host, so
    # the archive survives `docker compose up --build`. A sibling directory lives only in the
    # container layer, where the next rebuild deletes every backup the firm has taken.
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = now().strftime("%Y%m%d-%H%M%S")
    backup_file = backup_dir / f"coil-backup-{stamp}.tar.gz"
    # Two backups in the same second would otherwise silently overwrite each other, and
    # the nightly cron and an update can land together.
    n = 1
    while backup_file.exists():
        backup_file = backup_dir / f"coil-backup-{stamp}-{n}.tar.gz"
        n += 1
    snapshot = data_dir / ".backup-snapshot.db"

    print(f"Creating backup: {backup_file.name}")
    db_path = _sqlite_path()
    try:
        if db_path and db_path.exists():
            src = sqlite3.connect(str(db_path))
            try:
                dst = sqlite3.connect(str(snapshot))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        else:
            snapshot = None
            print("  no SQLite database found; backing up files only")

        with tarfile.open(backup_file, "w:gz") as tar:
            if snapshot:
                tar.add(snapshot, arcname="data/practice.db")
            for name in ("uploads", "pdf"):
                d = data_dir / name
                if d.exists():
                    tar.add(d, arcname=f"data/{name}")
    finally:
        if snapshot and snapshot.exists():
            snapshot.unlink()

    # Prune oldest first. Without this a nightly cron grows without bound and the
    # disk fills, which takes Coil down for the same reason no backup would have.
    keep = int(os.environ.get("COIL_BACKUP_KEEP", "14"))
    # By mtime, not by name: the collision suffix makes "...-231629-1.tar.gz" sort before
    # "...-231629.tar.gz", so a lexical sort would delete the newest file, not the oldest.
    archives = sorted(backup_dir.glob("coil-backup-*.tar.gz"), key=lambda f: f.stat().st_mtime)
    for old_file in archives[:-keep] if keep > 0 else []:
        old_file.unlink()
        print(f"  removed old backup {old_file.name}")

    size_mb = backup_file.stat().st_size / (1024 * 1024)
    print(f"Backup complete: {backup_file.name} ({size_mb:.1f} MB)")
    print(f"Keeping the newest {keep} (set COIL_BACKUP_KEEP to change).")
    print(f"Backups are stored in: {backup_dir}")
    print("This folder is inside your mounted data directory, so copy it somewhere off this")
    print("machine as well. A backup on the same disk does not survive losing the disk.")
    print("Restore with: tar xzf <file> -C <install dir>   (stop Coil first)")
    return backup_file


# Every command that main() will run. An unknown command must NOT fall through to the
# reminders branch: a typo like `app.cli backupp` would then email every client with an
# open invoice instead of printing usage.
COMMANDS = ("agenda", "reminders", "interest", "emailin", "sequences", "webhooks",
            "monthly_invoicing", "backup", "case_audit", "payment_plans", "voice_reminders")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        if argv:
            print(f"unknown command: {argv[0]}")
        print("usage: python -m app.cli <command>")
        print("commands: " + ", ".join(COMMANDS))
        return 2

    cmd = argv[0]
    from . import create_app
    app = create_app()
    with app.app_context():
        if cmd == "backup":
            backup()
        elif cmd == "case_audit":
            r = run_case_audit()
            print(f"case_audit: {r['matters']} open matters ({r['pi_matters']} PI), {len(r['new'])} new, "
                  f"{r['seen']} still open, {r['resolved']} resolved, {r['ai']} AI flags"
                  f"{', summary emailed' if r['emailed'] else ''}")
        elif cmd == "monthly_invoicing":
            r = run_monthly_invoicing(force="--force" in argv[1:])
            if not r["ran"]:
                print(f"monthly_invoicing: skipped, {r['reason']} (use --force to run today)")
            else:
                print(f"monthly_invoicing: {len(r['built'])} built, {len(r['sent'])} sent, {len(r['skipped'])} skipped")
                for m, reason in r["skipped"]:
                    print(f"  skipped {m.number}: {reason}")
        elif cmd == "webhooks":
            r, ok, bad = run_webhooks()
            print(f"webhooks: {r} retried, {ok} delivered, {bad} still failing")
        elif cmd == "payment_plans":
            r = run_payment_plans()
            print(f"payment_plans: {r['charged']} charged, {r['reminded']} reminded, {r['failed']} failed, "
                  f"{r['completed']} completed, {r['skipped']} already handled today")
        elif cmd == "voice_reminders":
            r = run_voice_reminders()
            print(f"voice_reminders: {len(r['placed'])} call(s) placed, {r['failed']} failed, "
                  f"{r['skipped']} already reminded, {len(r['would'])} would be placed"
                  + (f" ({r['reason']})" if r["reason"] else ""))
            for to, text in r["would"]:
                print(f"  would call {to}: {text}")
        elif cmd == "agenda":
            n = run_agenda()
            print(f"agenda: {n} email(s) sent")
        elif cmd == "interest":
            n, cents = run_interest()
            print(f"interest: {n} invoice(s) charged, {cents_to_str(cents)} total")
        elif cmd == "emailin":
            c = run_emailin()
            print(f"emailin: {c['filed']} filed, {c['unfiled']} unfiled, {c['skipped']} already seen")
        elif cmd == "sequences":
            s, d = run_sequences()
            print(f"sequences: {s} sent, {d} drafted" + ("" if Firm.get().sequences_auto_send else
                                                          " (auto-send is off; drafts wait at /intake/drafts)"))
        else:
            i, e = run_reminders()
            ev = run_evergreen()
            print(f"reminders: {i} invoice, {e} engagement, {ev} evergreen top-up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
