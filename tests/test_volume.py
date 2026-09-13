"""What breaks at volume, and the guard that it stays fixed.

On a firm with 3,013 matters and 31,288 time entries, the matters list took 21 seconds,
the matters export 26 seconds on 13,067 queries, and the time export 23 seconds on
38,313 queries. gunicorn's timeout is 30. The cause in every case was a query per row.

Two things are held here. First, the query count: each of these pages and exports must
stay flat as the data grows, so the count is asserted against a ceiling that does not
depend on row count. Second, exactness: the bulk figures the list and export now use must
agree with the per-matter model methods to the cent, because a firm will compare the
export against the screen, and unbilled time rounds per entry before summing.

Own SQLite file, seeded with the real volume fixture at a small size.
Run: .venv/bin/python -m pytest tests/test_volume.py -q
"""
import os
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import event

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_PATH = os.path.join(ROOT, "data", "test_volume.db")
DB_URI = f"sqlite:///{DB_PATH}"
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads", "test_volume")
PDF_DIR = os.path.join(ROOT, "data", "pdf", "test_volume")

from tests.helpers import login  # noqa: E402

N_MATTERS = 250          # enough that a per-row query is unmistakable in the count


@pytest.fixture(scope="module")
def app():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    shutil.rmtree(PDF_DIR, ignore_errors=True)
    env = dict(os.environ, DATABASE_URL=DB_URI, STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="", SMTP_HOST="")
    out = subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], env=env, cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    out = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "volume_fixture.py"), "--matters", str(N_MATTERS)],
                         env=env, cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    from app import create_app
    return create_app({"SQLALCHEMY_DATABASE_URI": DB_URI, "UPLOAD_DIR": UPLOAD_DIR, "PDF_DIR": PDF_DIR,
                       "TESTING": True, "STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": "", "SMTP_HOST": ""})


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    login(c)
    return c


@pytest.fixture
def count_queries(app):
    """Yields a dict whose 'n' is the number of SQL statements executed inside the test."""
    from app.extensions import db
    n = {"n": 0}

    def _bump(*a, **k):
        n["n"] += 1
    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", _bump)
    try:
        yield n
    finally:
        event.remove(engine, "before_cursor_execute", _bump)


# --- the count must not grow with the rows -------------------------------------------------

@pytest.mark.parametrize("path,ceiling", [
    ("/matters", 25),
    ("/exports/matters.csv", 25),
    ("/exports/time.csv", 25),
    ("/exports/trust.csv", 25),
    ("/reports/wip", 25),
    ("/invoices", 25),
])
def test_page_uses_a_fixed_number_of_queries(client, count_queries, path, ceiling):
    r = client.get(path)
    assert r.status_code == 200, path
    assert count_queries["n"] <= ceiling, \
        f"{path} ran {count_queries['n']} queries on {N_MATTERS} matters: a query per row is back"


def test_volume_fixture_is_actually_loaded(app):
    from app.extensions import db  # noqa: F401
    from app.models import Matter, TimeEntry
    with app.app_context():
        assert Matter.query.filter(Matter.number.like("V-%")).count() == N_MATTERS
        assert TimeEntry.query.count() >= N_MATTERS * 10


# --- the bulk figures agree with the per-matter methods, to the cent ------------------------

def test_bulk_money_matches_the_model_methods_exactly(app):
    from app.models import Matter
    from app.aggregates import money_for
    with app.app_context():
        matters = Matter.query.all()
        money = money_for(matters)
        mismatches = []
        for m in matters:
            mm = money[m.id]
            expected = (m.trust_balance_cents(), m.outstanding_cents(), m.unbilled_time_cents(),
                        m.unbilled_expense_cents())
            got = (mm.trust, mm.outstanding, mm.unbilled_time, mm.unbilled_expense)
            if expected != got:
                mismatches.append((m.number, expected, got))
        assert not mismatches, f"{len(mismatches)} matters disagree, first: {mismatches[:3]}"


def test_matters_export_figures_equal_the_matter_page_figures(app, client):
    """The export sits next to the screen. A cent of difference is a support ticket."""
    from app.models import Matter
    body = client.get("/exports/matters.csv").data.decode()
    lines = body.splitlines()
    header = lines[0].split(",")
    i_num, i_trust, i_out, i_unb = (header.index(h) for h in ("Number", "TrustBalance", "Outstanding", "UnbilledTime"))
    import csv
    import io as _io
    rows = {r[i_num]: r for r in csv.reader(_io.StringIO(body)) if r and r[i_num] != "Number"}
    with app.app_context():
        for m in Matter.query.filter(Matter.number.like("V-%")).limit(40).all():
            r = rows[m.number]
            assert r[i_trust] == f"{m.trust_balance_cents() / 100:.2f}", m.number
            assert r[i_out] == f"{m.outstanding_cents() / 100:.2f}", m.number
            assert r[i_unb] == f"{m.unbilled_time_cents() / 100:.2f}", m.number


def test_matters_list_paginates_and_shows_the_same_figures(app, client):
    from app.models import Matter
    r = client.get("/matters")
    assert r.status_code == 200
    body = r.data.decode()
    assert "page 1 of" in body
    # Each row carries two links, number and name, so count rows rather than hrefs.
    assert body.count("<tr>") - 1 <= 100, "more than one page of rows rendered"
    with app.app_context():
        # One matter that is on page 1 must show the exact figure the model computes.
        m = Matter.query.order_by(Matter.status, Matter.created_at.desc()).first()
        unbilled = (m.unbilled_time_cents() + m.unbilled_expense_cents()) / 100
        assert f"{unbilled:,.2f}" in body
