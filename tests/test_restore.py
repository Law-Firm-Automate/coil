"""ops/restore.sh, which had never been run before 2026-09-11 and did not work.

The backup writes every member with a leading data/ (app/cli.py). restore.sh extracted
into $TARGET/data, so the database landed at $TARGET/data/data/practice.db. Coil then
found nothing where it looks, created an empty database, and the firm concluded the
backup was worthless. On the one day this script is ever run.

Run: .venv/bin/python -m pytest tests/test_restore.py -q
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "ops", "restore.sh")


def _archive(tmp, with_db=True):
    """A backup shaped exactly the way app.cli backup shapes one."""
    staging = os.path.join(tmp, "staging", "data")
    os.makedirs(os.path.join(staging, "uploads", "2"), exist_ok=True)
    if with_db:
        db = os.path.join(staging, "practice.db")
        con = sqlite3.connect(db)
        con.execute("create table matters (id integer primary key, name text)")
        con.execute("insert into matters (name) values ('Marchetti v. Nordvale')")
        con.commit()
        con.close()
    with open(os.path.join(staging, "uploads", "2", "note.txt"), "w") as f:
        f.write("an uploaded file")
    path = os.path.join(tmp, "coil-backup-test.tar.gz")
    with tarfile.open(path, "w:gz") as tar:
        if with_db:
            tar.add(os.path.join(staging, "practice.db"), arcname="data/practice.db")
        tar.add(os.path.join(staging, "uploads"), arcname="data/uploads")
    return path


def _run(archive, target):
    return subprocess.run(["bash", SCRIPT, archive, target], capture_output=True, text=True)


@pytest.fixture
def tmp():
    d = tempfile.mkdtemp(prefix="coil-restore-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_database_lands_where_the_app_looks_for_it(tmp):
    target = os.path.join(tmp, "install")
    out = _run(_archive(tmp), target)
    assert out.returncode == 0, out.stderr

    assert os.path.isfile(os.path.join(target, "data", "practice.db")), \
        "the app opens data/practice.db and would have found nothing"
    assert not os.path.exists(os.path.join(target, "data", "data")), \
        "the archive's own data/ prefix was applied twice"


def test_the_restored_database_is_the_firms_data(tmp):
    target = os.path.join(tmp, "install")
    assert _run(_archive(tmp), target).returncode == 0
    con = sqlite3.connect(os.path.join(target, "data", "practice.db"))
    assert con.execute("select name from matters").fetchone()[0] == "Marchetti v. Nordvale"
    con.close()


def test_uploads_come_back_too(tmp):
    target = os.path.join(tmp, "install")
    assert _run(_archive(tmp), target).returncode == 0
    assert os.path.isfile(os.path.join(target, "data", "uploads", "2", "note.txt"))


def test_an_archive_with_no_database_fails_loudly(tmp):
    """A restore that half worked and said nothing is worse than one that failed."""
    target = os.path.join(tmp, "install")
    out = _run(_archive(tmp, with_db=False), target)
    assert out.returncode != 0, "reported success with no database restored"
    assert "FAILED" in out.stderr


def test_it_refuses_to_overwrite_an_existing_firm(tmp):
    target = os.path.join(tmp, "install")
    os.makedirs(os.path.join(target, "data"))
    with open(os.path.join(target, "data", "practice.db"), "w") as f:
        f.write("someone else's live database")
    out = _run(_archive(tmp), target)
    assert out.returncode != 0
    assert "refusing" in out.stderr.lower()
    with open(os.path.join(target, "data", "practice.db")) as f:
        assert f.read() == "someone else's live database", "overwrote a live database"


def test_it_says_env_is_not_in_the_backup(tmp):
    """Losing SECRET_KEY voids every session, magic link and signed token."""
    target = os.path.join(tmp, "install")
    out = _run(_archive(tmp), target)
    assert "SECRET_KEY" in out.stdout
