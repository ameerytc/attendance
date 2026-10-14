"""
Academic sessions and terms.

Rules enforced here (and backed by DB constraints / partial unique indexes):
  * session name unique per school; start < end
  * exactly three terms per session, created together with the session
  * term dates: start < end, inside the session, chronological, no overlap
  * one active session per school, one active term per session
  * terms are activated by an administrator, never automatically
  * closing/archiving/activating never touches attendance rows
"""

import sqlite3
from datetime import date, timedelta

from utils.timeutil import parse_date

TERM_NAMES = ("First Term", "Second Term", "Third Term")


class AcademicError(ValueError):
    """User-facing validation error."""


def default_term_ranges(start: date, end: date):
    """Split a session into three consecutive, non-overlapping thirds."""
    total = (end - start).days + 1
    third = max(total // 3, 1)
    s1, e1 = start, start + timedelta(days=third - 1)
    s2, e2 = e1 + timedelta(days=1), e1 + timedelta(days=third)
    s3, e3 = e2 + timedelta(days=1), end
    return [(s1, e1), (s2, e2), (s3, e3)]


def _check_session_fields(name, start_raw, end_raw):
    name = (name or "").strip()
    if not name:
        raise AcademicError("Session name is required (e.g. 2026/2027).")
    start, end = parse_date(start_raw), parse_date(end_raw)
    if start is None or end is None:
        raise AcademicError("Valid start and end dates are required.")
    if start >= end:
        raise AcademicError("The session end date must be after its start date.")
    return name, start, end


def get_session(db, school_id, session_id):
    return db.execute("SELECT * FROM academic_sessions WHERE id = ? AND school_id = ?",
                      (session_id, school_id)).fetchone()


def get_term(db, school_id, term_id):
    return db.execute("SELECT * FROM academic_terms WHERE id = ? AND school_id = ?",
                      (term_id, school_id)).fetchone()


def terms_for_session(db, school_id, session_id):
    return db.execute(
        """SELECT * FROM academic_terms WHERE school_id = ? AND academic_session_id = ?
           ORDER BY start_date""", (school_id, session_id)).fetchall()


def create_session(db, school_id, name, start_raw, end_raw) -> int:
    name, start, end = _check_session_fields(name, start_raw, end_raw)
    dup = db.execute("SELECT 1 FROM academic_sessions WHERE school_id = ? AND lower(name) = lower(?)",
                     (school_id, name)).fetchone()
    if dup:
        raise AcademicError(f"A session named '{name}' already exists for this school.")
    try:
        cur = db.execute(
            "INSERT INTO academic_sessions (school_id, name, start_date, end_date) VALUES (?, ?, ?, ?)",
            (school_id, name, start.isoformat(), end.isoformat()))
        session_id = cur.lastrowid
        for term_name, (ts, te) in zip(TERM_NAMES, default_term_ranges(start, end)):
            db.execute(
                """INSERT INTO academic_terms (school_id, academic_session_id, name, start_date, end_date)
                   VALUES (?, ?, ?, ?, ?)""",
                (school_id, session_id, term_name, ts.isoformat(), te.isoformat()))
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise AcademicError("Could not create the session (duplicate or invalid data).") from exc
    return session_id


def update_session(db, school_id, session_id, name, start_raw, end_raw):
    row = get_session(db, school_id, session_id)
    if row is None:
        raise AcademicError("Session not found.")
    if row["status"] == "archived":
        raise AcademicError("Archived sessions cannot be edited.")
    name, start, end = _check_session_fields(name, start_raw, end_raw)
    dup = db.execute(
        "SELECT 1 FROM academic_sessions WHERE school_id = ? AND lower(name) = lower(?) AND id != ?",
        (school_id, name, session_id)).fetchone()
    if dup:
        raise AcademicError(f"A session named '{name}' already exists for this school.")
    for t in terms_for_session(db, school_id, session_id):
        if parse_date(t["start_date"]) < start or parse_date(t["end_date"]) > end:
            raise AcademicError(f"{t['name']} would fall outside the new session dates. "
                                "Adjust the term dates first.")
    db.execute("UPDATE academic_sessions SET name = ?, start_date = ?, end_date = ? WHERE id = ?",
               (name, start.isoformat(), end.isoformat(), session_id))
    db.commit()


def update_term(db, school_id, term_id, start_raw, end_raw):
    term = get_term(db, school_id, term_id)
    if term is None:
        raise AcademicError("Term not found.")
    session = get_session(db, school_id, term["academic_session_id"])
    if session["status"] in ("closed", "archived"):
        raise AcademicError("Terms of a closed or archived session cannot be edited.")
    start, end = parse_date(start_raw), parse_date(end_raw)
    if start is None or end is None:
        raise AcademicError("Valid start and end dates are required.")
    if start >= end:
        raise AcademicError("A term's end date must be after its start date.")
    if start < parse_date(session["start_date"]) or end > parse_date(session["end_date"]):
        raise AcademicError(f"Term dates must fall inside the session "
                            f"({session['start_date']} to {session['end_date']}).")
    order = {n: i for i, n in enumerate(TERM_NAMES)}
    for other in terms_for_session(db, school_id, session["id"]):
        if other["id"] == term["id"]:
            continue
        o_start, o_end = parse_date(other["start_date"]), parse_date(other["end_date"])
        if start <= o_end and end >= o_start:
            raise AcademicError(f"These dates overlap {other['name']} "
                                f"({other['start_date']} to {other['end_date']}).")
        if order[other["name"]] < order[term["name"]] and o_end >= start:
            raise AcademicError(f"{term['name']} must start after {other['name']} ends.")
        if order[other["name"]] > order[term["name"]] and o_start <= end:
            raise AcademicError(f"{term['name']} must end before {other['name']} starts.")
    db.execute("UPDATE academic_terms SET start_date = ?, end_date = ? WHERE id = ?",
               (start.isoformat(), end.isoformat(), term_id))
    db.commit()


def _complete_active_terms(db, session_id):
    db.execute("UPDATE academic_terms SET status = 'completed' "
               "WHERE academic_session_id = ? AND status = 'active'", (session_id,))


def activate_session(db, school_id, session_id):
    """Make one session active. The previously active session is *closed* (its
    data stays intact). Terms are never auto-activated."""
    row = get_session(db, school_id, session_id)
    if row is None:
        raise AcademicError("Session not found.")
    if row["status"] == "archived":
        raise AcademicError("Archived sessions cannot be re-activated.")
    if row["status"] == "active":
        return
    try:
        prev = db.execute("SELECT id FROM academic_sessions WHERE school_id = ? AND status = 'active'",
                          (school_id,)).fetchone()
        if prev:
            _complete_active_terms(db, prev["id"])
            db.execute("UPDATE academic_sessions SET status = 'closed' WHERE id = ?", (prev["id"],))
        db.execute("UPDATE academic_sessions SET status = 'active' WHERE id = ?", (session_id,))
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise AcademicError("Another session is already active for this school.") from exc


def close_session(db, school_id, session_id):
    row = get_session(db, school_id, session_id)
    if row is None:
        raise AcademicError("Session not found.")
    if row["status"] not in ("active", "upcoming"):
        raise AcademicError("Only active or upcoming sessions can be closed.")
    _complete_active_terms(db, session_id)
    db.execute("UPDATE academic_sessions SET status = 'closed' WHERE id = ?", (session_id,))
    db.commit()


def archive_session(db, school_id, session_id):
    row = get_session(db, school_id, session_id)
    if row is None:
        raise AcademicError("Session not found.")
    if row["status"] != "closed":
        raise AcademicError("Close the session before archiving it.")
    db.execute("UPDATE academic_sessions SET status = 'archived' WHERE id = ?", (session_id,))
    db.commit()


def activate_term(db, school_id, term_id):
    term = get_term(db, school_id, term_id)
    if term is None:
        raise AcademicError("Term not found.")
    session = get_session(db, school_id, term["academic_session_id"])
    if session["status"] != "active":
        raise AcademicError("Activate the academic session before activating one of its terms.")
    if term["status"] == "active":
        return
    try:
        _complete_active_terms(db, session["id"])
        db.execute("UPDATE academic_terms SET status = 'active' WHERE id = ?", (term_id,))
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise AcademicError("Another term is already active in this session.") from exc


def complete_term(db, school_id, term_id):
    term = get_term(db, school_id, term_id)
    if term is None or term["status"] != "active":
        raise AcademicError("Only an active term can be marked completed.")
    db.execute("UPDATE academic_terms SET status = 'completed' WHERE id = ?", (term_id,))
    db.commit()


def current_context(db, school_id):
    """(active session row, active term row) — either may be None."""
    session = db.execute("SELECT * FROM academic_sessions WHERE school_id = ? AND status = 'active'",
                         (school_id,)).fetchone()
    term = None
    if session:
        term = db.execute("SELECT * FROM academic_terms WHERE academic_session_id = ? AND status = 'active'",
                          (session["id"],)).fetchone()
    return session, term


def selected_context(db, school_id, session_arg=None, term_arg=None):
    """Resolve the session/term a page should show. Defaults to the active ones;
    explicit ids are validated against the school. term None => whole session."""
    sessions = db.execute("SELECT * FROM academic_sessions WHERE school_id = ? ORDER BY start_date DESC",
                          (school_id,)).fetchall()
    session = None
    try:
        if session_arg:
            session = get_session(db, school_id, int(session_arg))
    except (TypeError, ValueError):
        session = None
    active_session, active_term = current_context(db, school_id)
    if session is None:
        session = active_session or (sessions[0] if sessions else None)
    terms = terms_for_session(db, school_id, session["id"]) if session else []
    term = None
    try:
        if term_arg:
            cand = get_term(db, school_id, int(term_arg))
            if cand and session and cand["academic_session_id"] == session["id"]:
                term = cand
    except (TypeError, ValueError):
        term = None
    if term is None and not term_arg and active_term and session and active_term["academic_session_id"] == session["id"]:
        term = active_term
    return {"session": session, "term": term, "sessions": sessions, "terms": terms,
            "active_session": active_session, "active_term": active_term}


def open_for_attendance(db, term_id):
    """(ok, message). Attendance may only be recorded under an active term of an
    active session — never under an archived/closed/completed one."""
    row = db.execute(
        """SELECT t.name AS term_name, t.status AS term_status, s.name AS session_name,
                  s.status AS session_status
           FROM academic_terms t JOIN academic_sessions s ON s.id = t.academic_session_id
           WHERE t.id = ?""", (term_id,)).fetchone()
    if row is None:
        return False, "Term not found."
    if row["session_status"] != "active":
        return False, f"Session {row['session_name']} is {row['session_status']}; attendance cannot be recorded."
    if row["term_status"] != "active":
        return False, f"{row['term_name']} is {row['term_status']}, not active; attendance cannot be recorded."
    return True, ""
