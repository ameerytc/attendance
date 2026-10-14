"""
QR attendance verification: per-session, per-student, random, short-lived,
single-use tokens, validated server-side before record_attendance() runs.
"""

import secrets
from datetime import datetime, timedelta

from services.settings_service import get_settings
from utils.timeutil import school_now


def issue_token(db, session_id: int, student_id: int, school_id: int) -> dict:
    settings = get_settings(school_id)
    ttl = settings["qr_token_ttl_seconds"] or 90
    token = secrets.token_urlsafe(24)
    expires_at = school_now(settings) + timedelta(seconds=ttl)
    db.execute("INSERT INTO qr_tokens (session_id, student_id, token, expires_at) VALUES (?, ?, ?, ?)",
               (session_id, student_id, token, expires_at.isoformat(timespec="seconds")))
    db.commit()
    return {"token": token, "expires_at": expires_at, "ttl_seconds": ttl}


def redeem_token(db, token: str, school_id: int):
    """(session_id, student_id, None) on success, else (None, None, error_code).
    A token for another school's session is treated as invalid."""
    row = db.execute(
        """SELECT q.*, s.school_id FROM qr_tokens q
           JOIN attendance_sessions s ON s.id = q.session_id WHERE q.token = ?""", (token,)).fetchone()
    if row is None or row["school_id"] != school_id:
        return None, None, "invalid_token"
    if row["used_at"] is not None:
        return None, None, "already_used"
    settings = get_settings(school_id)
    if datetime.fromisoformat(row["expires_at"]) < school_now(settings):
        return None, None, "expired_token"
    db.execute("UPDATE qr_tokens SET used_at = datetime('now') WHERE id = ?", (row["id"],))
    db.commit()
    return row["session_id"], row["student_id"], None
