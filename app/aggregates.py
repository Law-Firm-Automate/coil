"""Per-matter money figures for many matters at once.

Matter.trust_balance_cents(), outstanding_cents(), unbilled_time_cents() and
unbilled_expense_cents() are right for one matter on its own page. On a list of three
thousand they are twelve thousand queries, and the matters list took twenty-one seconds
while the matters export took twenty-six and was about to hit gunicorn's timeout. This
is the same four numbers in four queries, whatever the count.

The figures must agree with the per-matter methods to the cent, because the export sits
next to the screen and a firm will compare them. Three of the four are integer sums and
group cleanly in SQL. Unbilled time is not: the model rounds each entry's minutes x rate
/ 60 on its own, with Python's half-to-even, and only then adds them up. A SQL SUM over
the unrounded values, or SQLite's half-away-from-zero ROUND, differs from that by a cent
on some matters. So the rows come back raw and are rounded here the same way the model
does it. It is one query and thirty thousand cheap operations.
"""
from sqlalchemy import func, case
from .extensions import db
from .models import Matter, TrustTransaction, TimeEntry, Expense, Invoice

OPEN_INVOICE_STATES_EXCLUDED = ("draft", "void", "paid")


def matter_money(matter_ids=None):
    """Return (trust, outstanding, unbilled_time, unbilled_expense), each {matter_id: cents}.

    Missing keys mean zero. Pass matter_ids to restrict; None means every matter.
    """
    def _scope(q, col):
        return q.filter(col.in_(matter_ids)) if matter_ids is not None else q

    trust = dict(_scope(
        db.session.query(TrustTransaction.matter_id, func.coalesce(func.sum(TrustTransaction.amount_cents), 0))
        .filter(TrustTransaction.matter_id != None),  # noqa: E711
        TrustTransaction.matter_id).group_by(TrustTransaction.matter_id).all())

    # Matter.outstanding_cents(): balance of every invoice not draft, void or paid, where
    # balance is max(total - paid, 0). Integer arithmetic, so SQL and Python agree.
    bal = func.max(Invoice.total_cents - func.coalesce(Invoice.paid_cents, 0), 0)
    outstanding = dict(_scope(
        db.session.query(Invoice.matter_id, func.coalesce(func.sum(bal), 0))
        .filter(Invoice.status.notin_(OPEN_INVOICE_STATES_EXCLUDED)),
        Invoice.matter_id).group_by(Invoice.matter_id).all())

    unbilled_expense = dict(_scope(
        db.session.query(Expense.matter_id, func.coalesce(func.sum(Expense.amount_cents), 0))
        .filter(Expense.billable == True, Expense.invoice_id == None),  # noqa: E712,E711
        Expense.matter_id).group_by(Expense.matter_id).all())

    unbilled_time = {}
    rows = _scope(
        db.session.query(TimeEntry.matter_id, TimeEntry.minutes, TimeEntry.rate_cents)
        .filter(TimeEntry.billable == True, TimeEntry.invoice_id == None),  # noqa: E712,E711
        TimeEntry.matter_id).all()
    for mid, minutes, rate in rows:
        # Exactly TimeEntry.amount_cents, per row, then summed.
        unbilled_time[mid] = unbilled_time.get(mid, 0) + int(round((minutes or 0) * (rate or 0) / 60.0))

    return trust, outstanding, unbilled_time, unbilled_expense


class MatterMoney:
    """The four figures for one matter, looked up from the bulk dicts. Zero when absent."""
    __slots__ = ("trust", "outstanding", "unbilled_time", "unbilled_expense")

    def __init__(self, mid, bulk):
        t, o, ut, ue = bulk
        self.trust = int(t.get(mid, 0))
        self.outstanding = int(o.get(mid, 0))
        self.unbilled_time = int(ut.get(mid, 0))
        self.unbilled_expense = int(ue.get(mid, 0))

    @property
    def unbilled(self):
        return self.unbilled_time + self.unbilled_expense


def money_for(matters):
    """{matter_id: MatterMoney} for a list of Matter rows, in four queries."""
    ids = [m.id for m in matters]
    if not ids:
        return {}
    bulk = matter_money(ids)
    return {mid: MatterMoney(mid, bulk) for mid in ids}
