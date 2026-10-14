"""
Database connection and initialization helpers.

Uses Python's built-in sqlite3 module directly (no ORM), with
parameterized queries throughout the application to prevent SQL
injection. Connections are stored on Flask's `g` object per-request
and closed automatically at teardown.
"""

import sqlite3

from flask import current_app, g


def get_db():
    """Return a request-scoped SQLite connection with row factory set."""
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE_PATH"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added after the first v2 release. CREATE TABLE IF NOT EXISTS never alters an existing
# table, so init_db() adds any that are missing (additive only — nothing is dropped or rewritten).
ADDITIVE_COLUMNS = {
    "users": [("username", "TEXT"), ("must_change_password", "INTEGER NOT NULL DEFAULT 0"), ("password_changed_at", "TEXT")],
    "teachers": [("first_name", "TEXT"), ("middle_name", "TEXT"), ("surname", "TEXT"), ("gender", "TEXT"), ("address", "TEXT")],
}


def _apply_additive_columns(db):
    for table, columns in ADDITIVE_COLUMNS.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in columns:
            if existing and name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db():
    """Create all tables from schema.sql if they don't already exist, then add any new columns."""
    db = get_db()
    with open(current_app.config["SCHEMA_PATH"], "r", encoding="utf-8") as f:
        script = f.read()
    # run table creation first; indexes on new columns need the columns to exist
    _apply_additive_columns(db)
    db.executescript(script)
    db.commit()


def init_app(app):
    """Register database lifecycle hooks with the Flask app."""
    app.teardown_appcontext(close_db)
