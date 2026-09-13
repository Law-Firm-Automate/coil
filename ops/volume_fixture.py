"""Load a firm with enough records to find what breaks at scale.

Everything the QA passes ran against a firm with a dozen matters. The API and the time
list both broke at a few hundred rows; nobody knows what breaks at ten thousand. This
puts thousands of clients, matters, time entries, invoices and trust rows into a firm so
the exports, reports, ledger, conflict check and list pages can be watched under load.

Every record is unmistakably synthetic: names are "Vol NNNN Client", matter numbers are
V-000001 upward, descriptions carry the tag, emails go to example.test. Remove it all
with --clear. Never run this on a firm that holds anything real.

    python ops/volume_fixture.py                 # 3,000 matters and everything hanging off them
    python ops/volume_fixture.py --matters 500   # smaller
    python ops/volume_fixture.py --clear

Inside the container:  docker compose exec -T web python ops/volume_fixture.py
"""
import argparse
import os
import random
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (Contact, Matter, TimeEntry, Invoice, InvoiceLine, TrustTransaction,  # noqa: E402
                        User, Firm)

TAG = "[vol]"
SURNAMES = ["Marsh", "Okafor", "Lindqvist", "Delacroix", "Nakamura", "Brennan", "Castellano", "Whitfield",
            "Abernathy", "Okonkwo", "Petrova", "Haddad", "Sørensen", "Nguyen", "Fitzgerald", "Iyer",
            "Lee", "Brown", "Long", "Young"]          # the last four are common words, on purpose
FIRSTS = ["Ada", "Bruno", "Celia", "Dmitri", "Esme", "Farid", "Greta", "Hugo", "Ines", "Jonah",
          "Kira", "Luis", "Maeve", "Nils", "Orla", "Pavel", "Quinn", "Rosa", "Soren", "Tamsin"]
AREAS = ["Personal Injury", "Business", "Family", "Estate Planning", "Litigation", "Real Estate"]
WORK = ["Reviewed correspondence", "Drafted motion", "Client call", "Research on limitations",
        "Prepared discovery responses", "Court appearance", "Settlement negotiation", "Document review"]


def _clear():
    n_t = TimeEntry.query.filter(TimeEntry.description.like(f"{TAG}%")).delete(synchronize_session=False)
    n_tr = TrustTransaction.query.filter(TrustTransaction.description.like(f"{TAG}%")).delete(synchronize_session=False)
    vol_matters = Matter.query.filter(Matter.number.like("V-%")).all()
    ids = [m.id for m in vol_matters]
    n_l = n_i = 0
    if ids:
        inv_ids = [i.id for i in Invoice.query.filter(Invoice.matter_id.in_(ids)).all()]
        if inv_ids:
            n_l = InvoiceLine.query.filter(InvoiceLine.invoice_id.in_(inv_ids)).delete(synchronize_session=False)
            n_i = Invoice.query.filter(Invoice.id.in_(inv_ids)).delete(synchronize_session=False)
        n_m = Matter.query.filter(Matter.id.in_(ids)).delete(synchronize_session=False)
    else:
        n_m = 0
    n_c = Contact.query.filter(Contact.email.like("vol-%@example.test")).delete(synchronize_session=False)
    db.session.commit()
    print(f"removed {n_c} contacts, {n_m} matters, {n_t} time entries, {n_i} invoices ({n_l} lines), {n_tr} trust rows")


def build(n_matters):
    if Matter.query.filter(Matter.number.like("V-%")).count():
        print("volume fixture already loaded. Run with --clear first.")
        return
    rng = random.Random(20260912)      # same fixture every time, so timings are comparable
    users = User.query.filter_by(is_active=True).all() or [User.query.first()]
    firm = Firm.get()
    today = date.today()
    t0 = time.perf_counter()

    # Clients: one per three matters, so some clients carry several and conflicts have
    # something to find. Names deliberately collide with each other and with common words.
    n_clients = max(1, n_matters // 3)
    contacts = []
    for i in range(n_clients):
        contacts.append(Contact(first_name=rng.choice(FIRSTS), last_name=rng.choice(SURNAMES),
                                email=f"vol-{i:05d}@example.test", is_client=True,
                                phone=f"+1512555{i % 10000:04d}",
                                notes=f"{TAG} Vol {i:05d} Client. Synthetic record for load testing."))
    db.session.add_all(contacts)
    db.session.flush()
    print(f"  {len(contacts)} clients in {time.perf_counter() - t0:.1f}s")

    matters = []
    for i in range(n_matters):
        c = contacts[i % n_clients]
        opened = today - timedelta(days=rng.randint(10, 900))
        matters.append(Matter(number=f"V-{i + 1:06d}", client_id=c.id,
                              name=f"{c.last_name} matter {i + 1} ({rng.choice(['v. Vol Corp', 'estate', 'contract', 'lease'])})",
                              practice_area=rng.choice(AREAS), billing_type="hourly",
                              hourly_rate_cents=rng.choice([25000, 30000, 35000, 42500]),
                              responsible_user_id=rng.choice(users).id, opened_on=opened,
                              status="closed" if rng.random() < 0.3 else "open",
                              description=f"{TAG} Synthetic matter {i + 1} for load testing."))
    db.session.add_all(matters)
    db.session.flush()
    print(f"  {len(matters)} matters in {time.perf_counter() - t0:.1f}s")

    # Ten time entries per matter, about a third already invoiced.
    entries = []
    for m in matters:
        for k in range(10):
            d = m.opened_on + timedelta(days=rng.randint(0, 120))
            entries.append(TimeEntry(matter_id=m.id, user_id=m.responsible_user_id, date=d,
                                     minutes=rng.choice([6, 12, 18, 30, 48, 60, 90, 120]),
                                     rate_cents=m.hourly_rate_cents, billable=rng.random() < 0.9,
                                     description=f"{TAG} {rng.choice(WORK)}"))
    db.session.bulk_save_objects(entries)
    db.session.flush()
    print(f"  {len(entries)} time entries in {time.perf_counter() - t0:.1f}s")

    # One invoice per matter for the first third of its time, in a spread of states.
    invoices = 0
    for idx, m in enumerate(matters):
        mine = TimeEntry.query.filter_by(matter_id=m.id, billable=True).order_by(TimeEntry.date).limit(3).all()
        if not mine:
            continue
        total = sum(t.minutes * t.rate_cents // 60 for t in mine)
        status = rng.choice(["draft", "sent", "sent", "partial", "paid", "paid"])
        issued = max(t.date for t in mine) + timedelta(days=3)
        inv = Invoice(number=f"INV-V{idx + 1:06d}", matter_id=m.id, client_id=m.client_id, kind="hourly",
                      status=status, issued_on=issued, due_on=issued + timedelta(days=30),
                      subtotal_cents=total, total_cents=total,
                      paid_cents=total if status == "paid" else (total // 2 if status == "partial" else 0))
        for t in mine:
            inv.lines.append(InvoiceLine(kind="time", date=t.date, description=t.description,
                                         quantity=t.minutes / 60.0, unit_cents=t.rate_cents,
                                         amount_cents=t.minutes * t.rate_cents // 60, time_entry_id=t.id))
        db.session.add(inv)
        db.session.flush()
        for t in mine:
            t.invoice_id = inv.id
        invoices += 1
        if invoices % 500 == 0:
            db.session.flush()
    print(f"  {invoices} invoices in {time.perf_counter() - t0:.1f}s")

    # Trust: a retainer on two matters in three, a few disbursements against it, all dated
    # in the past so nothing here trips the post-dated or reconciled-period guards.
    trust = []
    for m in matters:
        if rng.random() < 0.66:
            dep = rng.choice([100000, 250000, 500000])
            when = m.opened_on
            trust.append(TrustTransaction(client_id=m.client_id, matter_id=m.id, date=when, type="deposit",
                                          amount_cents=dep, description=f"{TAG} Retainer", cleared=True))
            spent = 0
            for _ in range(rng.randint(0, 2)):
                amt = min(dep - spent, rng.choice([25000, 50000, 75000]))
                if amt <= 0:
                    break
                spent += amt
                trust.append(TrustTransaction(client_id=m.client_id, matter_id=m.id,
                                              date=when + timedelta(days=rng.randint(5, 60)),
                                              type="to_operating", amount_cents=-amt,
                                              description=f"{TAG} Fees earned", cleared=rng.random() < 0.8))
    db.session.bulk_save_objects(trust)
    db.session.commit()
    print(f"  {len(trust)} trust rows in {time.perf_counter() - t0:.1f}s")
    print(f"done in {time.perf_counter() - t0:.1f}s. Everything is tagged {TAG}; remove with --clear")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--matters", type=int, default=3000)
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()
    app = create_app()
    with app.app_context():
        if args.clear:
            _clear()
        else:
            build(args.matters)
