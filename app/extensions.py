import sqlite3

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    """Gunicorn runs multiple worker processes against the same SQLite file. Without this, one worker holding
    a write transaction for the length of an import batch or a bulk API loop makes any other worker's write
    fail outright with "database is locked" instead of waiting its turn: reproduced with a 500-row contact
    import running alongside ordinary time-entry writes, failing from the second row on. WAL lets writers and
    readers stop blocking each other, and a real busy_timeout makes a writer wait for a slow one rather than
    give up immediately."""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()
    # pysqlite's own implicit BEGIN/COMMIT bookkeeping does not understand SAVEPOINT (used by
    # db.session.begin_nested(), which every importer row goes through), so it can silently drop out of the
    # transaction it thinks it is in and re-open one mid-batch, right where a second process's write can win
    # the race and turn the retry the busy_timeout above is supposed to grant into an immediate "database is
    # locked". SQLAlchemy's own docs for pysqlite hand transaction control to us to prevent exactly this.
    dbapi_connection.isolation_level = None


@event.listens_for(Engine, "begin")
def _sqlite_begin(conn):
    if conn.engine.dialect.name != "sqlite":
        return
    conn.exec_driver_sql("BEGIN")
