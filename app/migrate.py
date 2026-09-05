"""Additive schema migration for SQLite.

`db.create_all()` creates missing tables but never alters existing ones, so an upgraded Coil
would crash on the first query that touches a new column. This compares every mapped column
against the live table and adds what is missing with the model's default. Columns are never
dropped or changed; that is a deliberate limit, so an older Coil can still open the same file.
Runs on every startup and is a no-op when nothing is missing.
"""
import logging
from sqlalchemy import inspect, text
from .extensions import db

log = logging.getLogger("migrate")


def _sql_default(col):
    d = col.default
    if d is None or d.is_callable if hasattr(d, "is_callable") else False:
        return None
    v = d.arg
    if callable(v):
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return None


def add_missing_columns():
    """Return the list of 'table.column' names added."""
    engine = db.engine
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    added = []
    with engine.begin() as conn:
        for table in db.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all handles brand-new tables
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                default = _sql_default(col)
                if default is not None:
                    ddl += f" DEFAULT {default}"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    if added:
        log.warning("schema: added %d missing column(s): %s", len(added), ", ".join(added))
    return added
