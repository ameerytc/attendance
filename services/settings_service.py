"""Per-school settings: identity, branding, attendance rules."""

from flask import g

from database.db import get_db

ALLOWED = {
    "school_name", "school_name_arabic", "motto", "address", "phone", "email", "website",
    "contact_info", "logo_url", "timezone", "attendance_threshold_pct", "late_after_minutes",
    "session_default_minutes", "qr_token_ttl_seconds", "enable_face_verification",
    "enable_qr_verification",
}


def get_settings(school_id: int = None):
    school_id = school_id if school_id is not None else g.get("school_id")
    if school_id is None:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM school_settings WHERE school_id = ?", (school_id,)).fetchone()
    if row is None:
        school = db.execute("SELECT name FROM schools WHERE id = ?", (school_id,)).fetchone()
        if school is None:
            return None
        db.execute("INSERT INTO school_settings (school_id, school_name) VALUES (?, ?)",
                   (school_id, school["name"]))
        db.commit()
        row = db.execute("SELECT * FROM school_settings WHERE school_id = ?", (school_id,)).fetchone()
    return row


def update_settings(fields: dict, school_id: int = None):
    school_id = school_id if school_id is not None else g.get("school_id")
    updates = {k: v for k, v in fields.items() if k in ALLOWED}
    get_settings(school_id)  # make sure the row exists
    if updates:
        db = get_db()
        sets = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE school_settings SET {sets}, updated_at = datetime('now') WHERE school_id = ?",
                   list(updates.values()) + [school_id])
        db.commit()
    return get_settings(school_id)
