"""Student routes: own attendance (filterable by session/term), QR self check-in token,
account settings. Students never mark attendance directly; record_attendance() runs the
full chain even for a QR token they generated."""

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database.db import get_db
from services import academic_service, qr_service, settings_service, stats_service
from utils.decorators import role_required
from utils.tenancy import school_id
from utils.validators import is_valid_email, is_valid_password, validate_required_fields

bp = Blueprint("student", __name__, url_prefix="/student")


def _student_row(db):
    return db.execute("""SELECT st.*, c.name AS class_name FROM students st LEFT JOIN classes c ON c.id = st.class_id
                         WHERE st.user_id = ? AND st.school_id = ?""", (g.user["id"], school_id())).fetchone()


@bp.route("/dashboard")
@role_required("student")
def dashboard():
    db = get_db()
    sid = school_id()
    student = _student_row(db)
    if student is None:
        flash("No student profile is linked to this account.", "error")
        return render_template("student/dashboard.html", student=None)
    ctx = academic_service.selected_context(db, sid, request.args.get("session_id"), request.args.get("term_id"))
    f = {"student_id": student["id"]}
    if ctx["session"]:
        f["session_id"] = ctx["session"]["id"]
    if ctx["term"]:
        f["term_id"] = ctx["term"]["id"]
    settings = settings_service.get_settings(sid)
    overall = stats_service.totals(db, sid, f)
    warn = overall["pct"] is not None and overall["pct"] < settings["attendance_threshold_pct"]
    return render_template("student/dashboard.html", student=student, ctx=ctx, overall=overall,
                           per_subject=stats_service.per_subject(db, sid, f), recent=stats_service.records(db, sid, f, limit=15),
                           low_attendance_warning=warn, settings=settings)


@bp.route("/qr")
@role_required("student")
def qr_checkin():
    db = get_db()
    student = _student_row(db)
    sessions = db.execute(
        """SELECT sess.*, sub.name AS subject_name FROM attendance_sessions sess
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id JOIN subjects sub ON sub.id = sa.subject_id
           WHERE sess.class_id = ? AND sess.school_id = ? AND sess.status = 'active' ORDER BY sess.started_at DESC""",
        (student["class_id"], school_id())).fetchall() if student and student["class_id"] else []
    return render_template("student/qr_checkin.html", active_sessions=sessions)


@bp.route("/qr/<int:session_id>/token", methods=("POST",))
@role_required("student")
def qr_request_token(session_id):
    db = get_db()
    student = _student_row(db)
    session = db.execute("SELECT * FROM attendance_sessions WHERE id = ? AND class_id = ? AND school_id = ? AND status = 'active'",
                         (session_id, student["class_id"] if student else -1, school_id())).fetchone()
    if session is None:
        return jsonify({"error": "Session not found or not active for your class."}), 404
    ok, msg = academic_service.open_for_attendance(db, session["academic_term_id"])
    if not ok:
        return jsonify({"error": msg}), 409
    issued = qr_service.issue_token(db, session_id, student["id"], school_id())
    return jsonify({"token": issued["token"], "expires_at": issued["expires_at"].isoformat(timespec="seconds"),
                    "ttl_seconds": issued["ttl_seconds"]})


@bp.route("/account", methods=("GET", "POST"))
@role_required("student")
def account_settings():
    db = get_db()
    if request.method == "POST":
        form = request.form
        errors = validate_required_fields(form, ["full_name", "email"])
        if not is_valid_email(form.get("email", "")):
            errors.append("A valid email address is required.")
        changing = bool(form.get("new_password"))
        if changing:
            if not check_password_hash(g.user["password_hash"], form.get("current_password", "")):
                errors.append("Current password is incorrect.")
            elif not is_valid_password(form.get("new_password", "")):
                errors.append("New password must be at least 8 characters.")
            elif form.get("new_password") != form.get("confirm_password"):
                errors.append("New password and confirmation do not match.")
        if db.execute("SELECT id FROM users WHERE email = ? AND id != ?", (form.get("email", "").strip().lower(), g.user["id"])).fetchone():
            errors.append("That email is already in use by another account.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("student/account.html")
        db.execute("UPDATE users SET full_name = ?, email = ?, updated_at = datetime('now') WHERE id = ?",
                   (form["full_name"].strip(), form["email"].strip().lower(), g.user["id"]))
        if changing:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(form["new_password"]), g.user["id"]))
        db.commit()
        flash("Account updated.", "success")
        return redirect(url_for("student.account_settings"))
    return render_template("student/account.html")
