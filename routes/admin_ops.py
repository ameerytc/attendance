"""Operational admin pages: offline-sync review, notification queue, manual backup.

Scope note: this is the review/trigger UI over real tables and a real file copy —
not automatic conflict resolution, a live SMS/email provider, or scheduled
off-server backups (see README). On PostgreSQL, use Railway's database backups
instead of the file backup below."""

import json
import os
import shutil
from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_from_directory, url_for

from database.db import get_db
from services.attendance_service import record_attendance
from utils.audit import log_action
from utils.decorators import ADMIN_ROLES, role_required
from utils.tenancy import owned, school_id

bp = Blueprint("admin_ops", __name__, url_prefix="/admin")


@bp.route("/sync-review")
@role_required(*ADMIN_ROLES)
def sync_review():
    rows = get_db().execute(
        """SELECT ev.*, d.name AS device_name FROM offline_sync_events ev LEFT JOIN devices d ON d.id = ev.device_id
           WHERE ev.school_id = ? ORDER BY ev.received_at DESC LIMIT 200""", (school_id(),)).fetchall()
    return render_template("admin/sync_review.html", rows=rows)


@bp.route("/sync-review/<int:event_id>/apply", methods=("POST",))
@role_required(*ADMIN_ROLES)
def sync_apply(event_id):
    """Apply one queued offline event through the same validation chain as any check-in
    (so it is still refused if the term is no longer active)."""
    db = get_db()
    event = owned(db, "offline_sync_events", event_id)
    if event is None or event["status"] != "pending":
        flash("Event not found or already resolved.", "error")
        return redirect(url_for("admin_ops.sync_review"))
    payload = json.loads(event["payload_json"] or "{}")
    result = record_attendance(db, event["session_id"], event["student_id"], "offline_sync", confidence=payload.get("confidence"),
                               device_id=event["device_id"], school_id=school_id())
    new_status = "synced" if result.ok else "conflict"
    db.execute("UPDATE offline_sync_events SET status = ?, resolution_note = ? WHERE id = ?", (new_status, result.message, event_id))
    db.commit()
    log_action("offline_sync.apply", "offline_sync_event", event_id, new_value={"status": new_status})
    flash(f"Event {new_status}: {result.message}", "success" if result.ok else "warning")
    return redirect(url_for("admin_ops.sync_review"))


@bp.route("/sync-review/<int:event_id>/reject", methods=("POST",))
@role_required(*ADMIN_ROLES)
def sync_reject(event_id):
    db = get_db()
    if owned(db, "offline_sync_events", event_id):
        db.execute("UPDATE offline_sync_events SET status = 'rejected', resolution_note = ? WHERE id = ? AND school_id = ?",
                   (request.form.get("reason", "").strip() or "Rejected by admin", event_id, school_id()))
        db.commit()
        log_action("offline_sync.reject", "offline_sync_event", event_id)
        flash("Event rejected.", "info")
    return redirect(url_for("admin_ops.sync_review"))


@bp.route("/notifications")
@role_required(*ADMIN_ROLES)
def notifications_queue():
    rows = get_db().execute("SELECT * FROM notifications WHERE school_id = ? ORDER BY created_at DESC LIMIT 200", (school_id(),)).fetchall()
    return render_template("admin/notifications_queue.html", rows=rows)


# The backup copies the WHOLE database (all schools), so only the platform owner may use it.
@bp.route("/backup", methods=("GET", "POST"))
@role_required("super_admin")
def backup():
    backup_dir = current_app.config["BACKUP_DIR"]
    os.makedirs(backup_dir, exist_ok=True)
    if request.method == "POST":
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(backup_dir, f"attendance-{stamp}.db")
        shutil.copy2(current_app.config["DATABASE_PATH"], dest)
        log_action("backup.manual", "database", None, new_value={"file": dest})
        flash(f"Backup created: attendance-{stamp}.db", "success")
        return redirect(url_for("admin_ops.backup"))
    return render_template("admin/backup.html", files=sorted(os.listdir(backup_dir), reverse=True))


@bp.route("/backup/<path:filename>")
@role_required("super_admin")
def backup_download(filename):
    return send_from_directory(current_app.config["BACKUP_DIR"], filename, as_attachment=True)
