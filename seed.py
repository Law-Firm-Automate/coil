"""Create the owner account and demo data so every screen has something on it. Safe to re-run."""
from datetime import date, timedelta, datetime
from app import create_app
from app.extensions import db
from app.models import *

app = create_app()
with app.app_context():
    if User.query.count() == 0:
        u = User(email="owner@example.com", name="Demo Owner", role="owner", hourly_rate_cents=35000, initials="DO")
        u.set_password("password123")
        db.session.add(u)
        f = Firm.get()
        f.name = "Demo Law PLLC"; f.address = "100 Main St, Suite 200\nAustin, TX 78701"; f.phone = "(512) 555-0100"
        f.email = "billing@demolaw.test"; f.surcharge_enabled = True; f.surcharge_bps = 300; f.next_matter_number = 1003
        db.session.commit()
    u = User.query.first()
    if Contact.query.count() == 0:
        c1 = Contact(first_name="Maria", last_name="Alvarez", email="maria@example.com", phone="+15125550111", is_client=True,
                     address="12 Oak Lane, Austin, TX 78702")
        c2 = Contact(kind="company", company_name="Bluebonnet Logistics LLC", email="ap@bluebonnet.test", is_client=True)
        c3 = Contact(first_name="Derek", last_name="Holloway", email="derek@example.com", is_client=False)
        db.session.add_all([c1, c2, c3]); db.session.commit()
        m1 = Matter(number="M-1001", client_id=c1.id, name="Alvarez Estate Plan", practice_area="Estate Planning",
                    billing_type="flat", flat_fee_cents=250000, responsible_user_id=u.id, opened_on=date.today()-timedelta(days=20))
        m2 = Matter(number="M-1002", client_id=c2.id, name="Bluebonnet v. Holloway (contract)", practice_area="Litigation",
                    billing_type="hourly", hourly_rate_cents=35000, responsible_user_id=u.id, sol_date=date.today()+timedelta(days=60),
                    sol_basis="4-year breach of written contract, TX CPRC 16.051", opened_on=date.today()-timedelta(days=45))
        db.session.add_all([m1, m2]); db.session.commit()
        db.session.add_all([
            MatterParty(matter_id=m2.id, contact_id=c3.id, name="Derek Holloway", role="adverse"),
            FlatFeeMilestone(matter_id=m1.id, description="Retainer on signing", amount_cents=125000, sort=0),
            FlatFeeMilestone(matter_id=m1.id, description="Balance on document execution", amount_cents=125000, sort=1),
            TimeEntry(matter_id=m2.id, user_id=u.id, date=date.today()-timedelta(days=3), minutes=90, rate_cents=35000, description="Draft demand letter; review contract exhibits"),
            TimeEntry(matter_id=m2.id, user_id=u.id, date=date.today()-timedelta(days=1), minutes=30, rate_cents=35000, description="Call with client re: settlement posture"),
            Expense(matter_id=m2.id, user_id=u.id, description="Certified mail", amount_cents=1245, category="Postage"),
            TrustTransaction(client_id=c2.id, matter_id=m2.id, type="deposit", amount_cents=500000, description="Initial retainer deposit", reference="Check 1042", cleared=True, created_by_id=u.id),
            Task(matter_id=m2.id, title="Serve demand letter", due_on=date.today()+timedelta(days=2), assignee_id=u.id, kind="task"),
            Task(matter_id=m1.id, title="Signing appointment", due_on=date.today()+timedelta(days=7), assignee_id=u.id, kind="task"),
            CalendarEvent(matter_id=m1.id, title="Alvarez signing", starts_at=datetime.utcnow()+timedelta(days=7, hours=2), ends_at=datetime.utcnow()+timedelta(days=7, hours=3), location="Office"),
            IntakeLead(name="Priya Natarajan", email="priya@example.com", phone="+15125550199", matter_type="Business formation", description="Wants to form an LLC for a consulting business.", source="web"),
            LetterTemplate(name="Standard engagement letter", kind="engagement", is_default=True, subject="Engagement letter: {{ matter_name }}",
                body_html="""<p>{{ date }}</p><p>{{ client_name }}<br>{{ client_address }}</p>
<p>Re: {{ matter_name }}</p>
<p>Dear {{ client_first_name }},</p>
<p>Thank you for choosing {{ firm_name }}. This letter confirms the terms on which we will represent you in the matter described above.</p>
<h3>Scope</h3><p>{{ scope }}</p>
<h3>Fees</h3><p>{{ fee_summary }}</p>
<p>Any funds paid in advance will be held in our client trust account and applied to invoices as earned. Unearned funds are refundable.</p>
<h3>Your agreement</h3><p>By signing below you confirm that you have read this letter, understand it, and agree to its terms. You may withdraw at any time, and we may withdraw as permitted by the rules of professional conduct.</p>
<p>Sincerely,<br>{{ attorney_name }}<br>{{ firm_name }}</p>"""),
        ])
        db.session.commit()
    print("seed ok: owner@example.com / password123")
