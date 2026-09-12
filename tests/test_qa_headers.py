"""X-Coil-Flash: flashed messages repeated in a response header, for automated QA.

A browser-driving tester reported "no message shown" three times on refusals that were
on the page, because it read the DOM before the flash region rendered after a redirect.
With COIL_QA_HEADERS=1 the message rides on the response that flashed it, redirect or
not, so an HTTP client sees it without scraping anything. Off by default: flashes name
clients and amounts, and proxies log headers.

Run: .venv/bin/python -m pytest tests/test_qa_headers.py -q
"""
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.helpers import login  # noqa: E402


def _build(tag, qa_headers):
    """One app per setting, each on its own database, built once per module."""
    db_path = os.path.join(ROOT, "data", f"test_qa_headers_{tag}.db")
    upload = os.path.join(ROOT, "data", "uploads", f"test_qa_headers_{tag}")
    pdf = os.path.join(ROOT, "data", "pdf", f"test_qa_headers_{tag}")
    if os.path.exists(db_path):
        os.remove(db_path)
    shutil.rmtree(upload, ignore_errors=True)
    shutil.rmtree(pdf, ignore_errors=True)
    uri = f"sqlite:///{db_path}"
    env = dict(os.environ, DATABASE_URL=uri, STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="", SMTP_HOST="")
    out = subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], env=env, cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    from app import create_app
    return create_app({"SQLALCHEMY_DATABASE_URI": uri, "UPLOAD_DIR": upload, "PDF_DIR": pdf,
                       "TESTING": True, "STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": "", "SMTP_HOST": "",
                       "COIL_QA_HEADERS": qa_headers})


@pytest.fixture(scope="module")
def app_on():
    app = _build("on", qa_headers=True)

    # A route that flashes a non-Latin name, registered before any request is handled.
    from flask import flash

    @app.route("/_qa_flash_greek")
    def _greek():
        flash("Θεοδώρα still holds $1,100.00 in trust.", "error")
        return "ok"

    return app


@pytest.fixture(scope="module")
def app_off():
    return _build("off", qa_headers=False)


def _bad_login(app):
    """A wrong password flashes an error and redirects: the exact shape that was misread."""
    c = app.test_client()
    r = c.get("/login")
    import re
    tok = re.search(rb'name="_csrf" value="([^"]+)"', r.data).group(1).decode()
    return c.post("/login", data={"email": "owner@example.com", "password": "wrong", "_csrf": tok})


def test_flash_rides_on_the_response_that_flashed_it(app_on):
    r = _bad_login(app_on)
    assert "X-Coil-Flash" in r.headers, "the refusal did not reach the header"
    assert r.headers["X-Coil-Flash-Encoding"] == "text"
    assert "error:" in r.headers["X-Coil-Flash"]


def test_off_by_default_because_flashes_name_clients(app_off):
    r = _bad_login(app_off)
    assert "X-Coil-Flash" not in r.headers


def test_non_ascii_message_survives_the_wire(app_on):
    """Headers are latin-1; a client called Θεοδώρα in a flash must not crash the response."""
    c = app_on.test_client()
    login(c)
    r = c.get("/_qa_flash_greek")
    assert r.status_code == 200
    assert r.headers["X-Coil-Flash-Encoding"] == "utf8-hex"
    decoded = bytes.fromhex(r.headers["X-Coil-Flash"]).decode("utf-8")
    assert "Θεοδώρα" in decoded and "$1,100.00" in decoded
