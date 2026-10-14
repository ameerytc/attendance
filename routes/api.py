"""
/api/v1 — endpoints a physical attendance device talks to, authenticated by its own
API key (not a user session). A device belongs to one school and can only submit
events for that school's sessions/students. Events are deduplicated by event_uid and
queued as 'pending' for admin review (no automatic conflict resolution).
"""

import json
import uuid

from flask import Blueprint, jsonify, request
from werkzeug.security import check_password_hash

from database.db import get_db

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _authenticate_device(db):
    api_key = request.headers.get("X-Device-Key", "")
    if not api_key:
        return None
    for device in db.execute("SELECT * FROM devices").fetchall():
        if check_password_hash(device["api_key_hash"], api_key):
            return device
    return None


@bp.route("/devices/heartbeat", methods=("POST",))
def device_heartbeat():
    db = get_db()
    device = _authenticate_device(db)
    if device is None:
        return jsonify({"error": "Invalid or missing device key."}), 401
    version = (request.get_json(silent=True) or {}).get("software_version")
    db.execute("UPDATE devices SET status = 'online', last_seen_at = datetime('now'), "
               "software_version = COALESCE(?, software_version) WHERE id = ?", (version, device["id"]))
    db.commit()
    return jsonify({"ok": True, "device": device["device_code"]})


@bp.route("/attendance/offline-sync", methods=("POST",))
def offline_sync():
    """Body: {"events": [{"event_uid", "session_id", "student_id", "occurred_at", "payload"}]}"""
    db = get_db()
    device = _authenticate_device(db)
    if device is None:
        return jsonify({"error": "Invalid or missing device key."}), 401
    sid = device["school_id"]
    events = (request.get_json(silent=True) or {}).get("events", [])
    accepted = duplicates = rejected = 0
    for event in events:
        event_uid = event.get("event_uid") or str(uuid.uuid4())
        if db.execute("SELECT 1 FROM offline_sync_events WHERE event_uid = ?", (event_uid,)).fetchone():
            duplicates += 1
            continue
        session_ok = db.execute("SELECT 1 FROM attendance_sessions WHERE id = ? AND school_id = ?", (event.get("session_id"), sid)).fetchone()
        student_ok = db.execute("SELECT 1 FROM students WHERE id = ? AND school_id = ?", (event.get("student_id"), sid)).fetchone()
        if not session_ok or not student_ok:
            rejected += 1
            continue
        db.execute("""INSERT INTO offline_sync_events (school_id, event_uid, device_id, session_id, student_id, payload_json, occurred_at)
                      VALUES (?, ?, ?, ?, ?, ?, ?)""",
                   (sid, event_uid, device["id"], event["session_id"], event["student_id"],
                    json.dumps(event.get("payload", {})), event.get("occurred_at") or ""))
        accepted += 1
    db.commit()
    return jsonify({"accepted": accepted, "duplicates": duplicates, "rejected": rejected, "total": len(events)})
