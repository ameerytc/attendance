"""
Multi-school isolation.

g.school_id is resolved once per request (app.py). Every query that touches
school-owned data must filter on it; `owned()` fetches a single row only if
it belongs to the current school, otherwise the caller gets None (-> 404).
"""

from flask import abort, g, session

OWNED_TABLES = {
    "students", "teachers", "classes", "subjects", "subject_assignments", "sections",
    "departments", "academic_sessions", "academic_terms", "attendance_sessions",
    "attendance_records", "devices", "guardians", "offline_sync_events",
}


def resolve_school_id(db, user):
    if user is None:
        return None
    if user["school_id"]:
        return user["school_id"]
    # super_admin: picks a school via the switcher; defaults to the first active one
    chosen = session.get("school_id")
    if chosen and db.execute("SELECT 1 FROM schools WHERE id = ?", (chosen,)).fetchone():
        return chosen
    row = db.execute("SELECT id FROM schools WHERE status = 'active' ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def school_id() -> int:
    sid = g.get("school_id")
    if sid is None:
        abort(403)
    return sid


def owned(db, table: str, row_id, columns: str = "*"):
    """Row from `table` with this id AND the current school_id, else None."""
    if table not in OWNED_TABLES:
        raise ValueError(f"{table} is not a tenant table")
    try:
        row_id = int(row_id)
    except (TypeError, ValueError):
        return None
    return db.execute(f"SELECT {columns} FROM {table} WHERE id = ? AND school_id = ?",
                      (row_id, school_id())).fetchone()


def owned_or_404(db, table: str, row_id, columns: str = "*"):
    row = owned(db, table, row_id, columns)
    if row is None:
        abort(404)
    return row
