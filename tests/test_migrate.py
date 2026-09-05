"""Startup migration: missing columns come back, including ones with no default, and re-runs are no-ops."""
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "data", "test_migrate.db")


def test_missing_columns_are_added_at_startup():
    if os.path.exists(DB):
        os.remove(DB)
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{DB}"}
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True, cwd=ROOT, env=env)
    con = sqlite3.connect(DB)
    for stmt in ("ALTER TABLE pi_cases DROP COLUMN case_score",  # no default
                 "ALTER TABLE pi_cases DROP COLUMN overview_at",  # DateTime, no default
                 "ALTER TABLE firm DROP COLUMN invoice_accent",  # string default
                 "ALTER TABLE matters DROP COLUMN auto_invoice_monthly"):  # boolean default
        con.execute(stmt)
    con.commit(); con.close()
    os.environ["DATABASE_URL"] = env["DATABASE_URL"]
    from app import create_app
    from app.migrate import add_missing_columns
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": env["DATABASE_URL"]})
    con = sqlite3.connect(DB)
    cols = {r[1] for r in con.execute("PRAGMA table_info(pi_cases)")}
    assert {"case_score", "overview_at"} <= cols
    assert "invoice_accent" in {r[1] for r in con.execute("PRAGMA table_info(firm)")}
    assert con.execute("select invoice_accent from firm").fetchone()[0] == "#1f5f8b"
    assert con.execute("select auto_invoice_monthly from matters limit 1").fetchone()[0] == 0
    con.close()
    with app.app_context():
        assert add_missing_columns() == []  # second run is a no-op
