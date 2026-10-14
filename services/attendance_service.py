"""
Attendance engine (term-aware).

Every attendance session and record is pinned to a school, class, academic
session and term. record_attendance() is still the single validation chain that
face / QR / manual / offline-sync check-ins funnel through; it now additionally
refuses to write under a term that is not active.
"""

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from services import academic_service
from services.settings_service import get_settings
from utils.audit import log_action
from utils.timeutil import school_now

VERIFICATION_METHODS = ("face", "qr", "rfid", "manual", "offline_sync")
ATTENDANCE_STATUSES = ("present", "late", "absent", "excused", "pending_verification")
MANUAL_STATUSES = ("present", "late", "absent", "excused")


@dataclass
class AttendanceResult:
    ok: bool
    code: str  # RECORDED / ALREADY_RECORDED / NOT_ENROLLED / SESSION_CLOSED / TERM_CLOSED / NOT_RECOGNIZED / ERROR
    message: str
    student_name: Optional[str] = None
    admission_number: Optional[str] = None
    confidence: Optional[float] = None
    status: Optional[str] = None


def generate_session_code(subject_code: str, class_name: str, when: datetime) -> str:
    clean_subject = "".join(ch for ch in subject_code.upper() if ch.isalnum())[:8]
    clean_class = "".join(ch for ch in class_name.upper() if ch.isalnum())[:8]
    return (f"ATT-{when.strftime('%Y%m%d')}-{clean_subject}-{clean_class}"
            f"-{when.strftime('%H%M')}-{secrets.token_hex(2).upper()}")


def compute_status(settings, session_row, check_in_time: datetime) -> str:
    late_after = (settings["late_after_minutes"] if settings else None) or 15
    start = datetime.fromisoformat(session_row["scheduled_start"])
    return "present" if (check_in_time - start).total_seconds() / 60.0 <= late_after else "late"


def start_or_reuse_session(db, school_id: int, user_id: int, teacher_id: int, assignment_id):
    """
    Open today's attendance session for one of the teacher's assignments.
    Returns (session_row | None, code, message); code is one of
    CREATED, REUSED, ALREADY_CLOSED, NOT_ASSIGNED, TERM_CLOSED.
    An existing session for the same assignment and school-local date is reused,
    never duplicated.
    """
    try:
        assignment_id = int(assignment_id)
    except (TypeError, ValueError):
        return None, "NOT_ASSIGNED", "That class/subject is not assigned to you."
    assignment = db.execute(
        """SELECT sa.*, sub.code AS subject_code, c.name AS class_name
           FROM subject_assignments sa
           JOIN subjects sub ON sub.id = sa.subject_id
           JOIN classes c ON c.id = sa.class_id
           WHERE sa.id = ? AND sa.teacher_id = ? AND sa.school_id = ? AND sa.is_active = 1""",
        (assignment_id, teacher_id, school_id)).fetchone()
    if assignment is None:
        return None, "NOT_ASSIGNED", "That class/subject is not assigned to you."

    ok, msg = academic_service.open_for_attendance(db, assignment["term_id"])
    if not ok:
        return None, "TERM_CLOSED", msg

    settings = get_settings(school_id)
    now = school_now(settings)
    today = now.strftime("%Y-%m-%d")
    existing = db.execute(
        "SELECT * FROM attendance_sessions WHERE subject_assignment_id = ? AND session_date = ?",
        (assignment_id, today)).fetchone()
    if existing:
        if existing["status"] == "closed":
            return existing, "ALREADY_CLOSED", "Attendance for this class/subject was already taken and closed today."
        return existing, "REUSED", "An attendance session is already open for this class/subject today."

    term = academic_service.get_term(db, school_id, assignment["term_id"])
    code = generate_session_code(assignment["subject_code"], assignment["class_name"], now)
    try:
        cur = db.execute(
            """INSERT INTO attendance_sessions
                   (school_id, session_code, subject_assignment_id, class_id, academic_session_id,
                    academic_term_id, session_date, scheduled_start, started_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (school_id, code, assignment_id, assignment["class_id"], term["academic_session_id"],
             term["id"], today, now.isoformat(timespec="seconds"), user_id))
        db.commit()
    except sqlite3.IntegrityError:  # lost a race with another request: reuse theirs
        db.rollback()
        existing = db.execute(
            "SELECT * FROM attendance_sessions WHERE subject_assignment_id = ? AND session_date = ?",
            (assignment_id, today)).fetchone()
        return existing, "REUSED", "An attendance session is already open for this class/subject today."
    row = db.execute("SELECT * FROM attendance_sessions WHERE id = ?", (cur.lastrowid,)).fetchone()
    log_action("attendance.session_started", "attendance_session", row["id"], school_id=school_id)
    return row, "CREATED", f"Attendance session {code} started."


def _log_event(db, session_id, student_id, method, result, confidence=None, device_id=None):
    method = method if method in ("face", "qr", "rfid", "manual") else "manual"
    db.execute(
        """INSERT INTO verification_events (session_id, student_id, method, result, confidence, device_id)
           VALUES (?, ?, ?, ?, ?, ?)""", (session_id, student_id, method, result, confidence, device_id))


def record_attendance(db, session_id: int, student_id: Optional[int], method: str,
                      confidence: Optional[float] = None, marked_by_user_id: Optional[int] = None,
                      device_id: Optional[int] = None, quran_fields: Optional[dict] = None,
                      status_override: Optional[str] = None,
                      school_id: Optional[int] = None) -> AttendanceResult:
    """The single validation chain. Never inserts unless every check passes."""
    if method not in VERIFICATION_METHODS:
        return AttendanceResult(False, "ERROR", "Unknown verification method.")

    if student_id is None:
        _log_event(db, session_id, None, method, "rejected_no_match", confidence, device_id)
        db.commit()
        return AttendanceResult(False, "NOT_RECOGNIZED", "No match found.", confidence=confidence)

    session = db.execute("SELECT * FROM attendance_sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None or (school_id is not None and session["school_id"] != school_id):
        return AttendanceResult(False, "ERROR", "Attendance session not found.")
    if session["status"] != "active":
        _log_event(db, session_id, student_id, method, "error", confidence, device_id)
        db.commit()
        return AttendanceResult(False, "SESSION_CLOSED", "This attendance session is not active.")

    open_ok, open_msg = academic_service.open_for_attendance(db, session["academic_term_id"])
    if not open_ok:
        _log_event(db, session_id, student_id, method, "rejected_term_closed", confidence, device_id)
        db.commit()
        return AttendanceResult(False, "TERM_CLOSED", open_msg)

    student = db.execute(
        """SELECT st.*, u.full_name, u.is_active FROM students st
           JOIN users u ON u.id = st.user_id WHERE st.id = ? AND st.school_id = ?""",
        (student_id, session["school_id"])).fetchone()
    if student is None or not student["is_active"] or student["status"] != "active":
        _log_event(db, session_id, student_id, method, "rejected_not_enrolled", confidence, device_id)
        db.commit()
        return AttendanceResult(False, "ERROR", "Student record not found or inactive.")

    if student["class_id"] != session["class_id"]:
        _log_event(db, session_id, student_id, method, "rejected_not_enrolled", confidence, device_id)
        db.commit()
        return AttendanceResult(False, "NOT_ENROLLED", "Student is not in this class.",
                                student_name=student["full_name"], admission_number=student["admission_number"])

    if db.execute("SELECT 1 FROM attendance_records WHERE session_id = ? AND student_id = ?",
                  (session_id, student_id)).fetchone():
        _log_event(db, session_id, student_id, method, "rejected_duplicate", confidence, device_id)
        db.commit()
        return AttendanceResult(False, "ALREADY_RECORDED", "Attendance already recorded for this session.",
                                student_name=student["full_name"], admission_number=student["admission_number"],
                                confidence=confidence)

    settings = get_settings(session["school_id"])
    if status_override is not None:
        if method != "manual" or status_override not in MANUAL_STATUSES:
            return AttendanceResult(False, "ERROR", "Invalid attendance status.")
        status = status_override
    else:
        status = compute_status(settings, session, school_now(settings))
    q = quran_fields or {}
    try:
        db.execute(
            """INSERT INTO attendance_records
                   (school_id, session_id, student_id, class_id, academic_session_id, academic_term_id,
                    status, verification_method, confidence, marked_by_user_id,
                    quran_juz, quran_surah, quran_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session["school_id"], session_id, student_id, session["class_id"],
             session["academic_session_id"], session["academic_term_id"], status, method,
             confidence, marked_by_user_id, q.get("juz"), q.get("surah"), q.get("note")))
    except sqlite3.IntegrityError:
        db.rollback()
        return AttendanceResult(False, "ALREADY_RECORDED", "Attendance already recorded for this session.",
                                student_name=student["full_name"], admission_number=student["admission_number"])
    _log_event(db, session_id, student_id, method, "success", confidence, device_id)
    db.commit()
    return AttendanceResult(True, "RECORDED", "Attendance recorded.", student_name=student["full_name"],
                            admission_number=student["admission_number"], confidence=confidence, status=status)


def correct_attendance(db, record_id: int, new_status: str, reason: str, corrected_by_user_id: int,
                       school_id: Optional[int] = None) -> bool:
    """Never a silent edit: previous/new status, who, when and why are kept."""
    if new_status not in ATTENDANCE_STATUSES or not reason or not reason.strip():
        return False
    record = db.execute(
        """SELECT ar.*, s.status AS session_status FROM attendance_records ar
           JOIN academic_sessions s ON s.id = ar.academic_session_id WHERE ar.id = ?""",
        (record_id,)).fetchone()
    if record is None or (school_id is not None and record["school_id"] != school_id):
        return False
    if record["session_status"] == "archived":
        return False
    db.execute("UPDATE attendance_records SET status = ? WHERE id = ?", (new_status, record_id))
    db.execute(
        """INSERT INTO attendance_corrections (record_id, previous_status, new_status, reason, corrected_by_user_id)
           VALUES (?, ?, ?, ?, ?)""", (record_id, record["status"], new_status, reason.strip(), corrected_by_user_id))
    db.commit()
    log_action("attendance.correction", "attendance_record", record_id,
               previous_value={"status": record["status"]}, new_value={"status": new_status},
               reason=reason.strip(), school_id=record["school_id"])
    return True


def close_session(db, session_id: int, closed_by_user_id: int, school_id: Optional[int] = None):
    """Close a session and record every enrolled-but-unmarked student as absent."""
    session = db.execute("SELECT * FROM attendance_sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None or session["status"] == "closed":
        return
    if school_id is not None and session["school_id"] != school_id:
        return
    # Only fill in absences while the term is still open; otherwise just close the
    # session so nothing new is written under a completed/archived term.
    open_ok, _ = academic_service.open_for_attendance(db, session["academic_term_id"])
    roster = db.execute("SELECT id FROM students WHERE class_id = ? AND school_id = ? AND status = 'active'",
                        (session["class_id"], session["school_id"])).fetchall() if open_ok else []
    marked = {r["student_id"] for r in db.execute(
        "SELECT student_id FROM attendance_records WHERE session_id = ?", (session_id,)).fetchall()}
    for student in roster:
        if student["id"] not in marked:
            db.execute(
                """INSERT INTO attendance_records
                       (school_id, session_id, student_id, class_id, academic_session_id, academic_term_id,
                        status, verification_method, marked_by_user_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'absent', 'manual', ?)""",
                (session["school_id"], session_id, student["id"], session["class_id"],
                 session["academic_session_id"], session["academic_term_id"], closed_by_user_id))
    db.execute("UPDATE attendance_sessions SET status = 'closed', closed_at = datetime('now') WHERE id = ?",
               (session_id,))
    db.commit()
    log_action("attendance.session_closed", "attendance_session", session_id, school_id=session["school_id"])
