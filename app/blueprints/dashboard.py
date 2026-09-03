from datetime import date, timedelta
from flask import Blueprint, render_template
from sqlalchemy import func
from ..extensions import db
from ..models import Matter, Invoice, Task, TimeEntry, IntakeLead, Engagement, TrustTransaction, Timer
from ..helpers import login_required, current_user

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    u = current_user()
    today = date.today()
    week_ago = today - timedelta(days=7)
    open_matters = Matter.query.filter_by(status="open").count()
    ar = db.session.query(func.coalesce(func.sum(Invoice.total_cents - Invoice.paid_cents), 0)).filter(
        Invoice.status.in_(["sent", "viewed", "partial"])).scalar() or 0
    overdue = Invoice.query.filter(Invoice.status.in_(["sent", "viewed", "partial"]), Invoice.due_on < today).all()
    wip = db.session.query(func.coalesce(func.sum(TimeEntry.minutes * TimeEntry.rate_cents / 60), 0)).filter(
        TimeEntry.billable == True, TimeEntry.invoice_id == None).scalar() or 0
    trust_total = db.session.query(func.coalesce(func.sum(TrustTransaction.amount_cents), 0)).scalar() or 0
    week_minutes = db.session.query(func.coalesce(func.sum(TimeEntry.minutes), 0)).filter(
        TimeEntry.date >= week_ago, TimeEntry.user_id == u.id).scalar() or 0
    tasks = Task.query.filter(Task.done == False).order_by(Task.due_on.asc().nulls_last()).limit(12).all()
    deadlines = Matter.query.filter(Matter.status != "closed", Matter.sol_date != None,
                                    Matter.sol_date <= today + timedelta(days=90)).order_by(Matter.sol_date).all()
    leads = IntakeLead.query.filter_by(status="new").order_by(IntakeLead.created_at.desc()).limit(8).all()
    engagements = Engagement.query.filter(Engagement.status.in_(["sent", "viewed"])).order_by(
        Engagement.sent_at.desc()).limit(8).all()
    recent_matters = Matter.query.order_by(Matter.created_at.desc()).limit(8).all()
    timer = Timer.query.filter_by(user_id=u.id).first()
    return render_template("dashboard.html", open_matters=open_matters, ar=int(ar), overdue=overdue, wip=int(wip),
                           trust_total=int(trust_total), week_minutes=int(week_minutes), tasks=tasks,
                           deadlines=deadlines, leads=leads, engagements=engagements, recent_matters=recent_matters,
                           timer=timer, today=today)
