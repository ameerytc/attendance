"""Teacher routes. A teacher only ever sees classes/subjects assigned to them (subject
assignments); every lookup also filters by school. Attendance is recorded under the
assignment's term and only while that term (and its session) is active."""

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database.db import get_db
from routes.admin_biometrics import school_recognizer
from services import academic_service, attendance_service, face_detection, qr_service, settings_service, stats_service
from services.attendance_service import MANUAL_STATUSES, close_session, record_attendance
from utils.access import teacher_class_ids, teacher_row
from utils.audit import log_action
from utils.decorators import TEACHER_ROLES, role_required
from utils.tenancy import school_id
from utils.timeutil import school_today
from utils.validators import is_valid_email, is_valid_password, validate_required_fields

bp = Blueprint("teacher", __name__, url_prefix="/teacher")


def _own_session(db, teacher_id, session_id):
    return db.execute(
        """SELECT sess.* FROM attendance_sessions sess JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           WHERE sess.id = ? AND sa.teacher_id = ? AND sess.school_id = ?""", (session_id, teacher_id, school_id())).fetchone()


def _assignments(db, teacher_id, only_open=False):
    rows = db.execute(
        """SELECT sa.*, sub.name AS subject_name, sub.code AS subject_code, c.name AS class_name,
                  t.name AS term_name, t.status AS term_status, s.name AS session_name, s.status AS session_status,
                  (SELECT COUNT(*) FROM students st WHERE st.class_id = sa.class_id AND st.status = 'active') AS enrolled_count
           FROM subject_assignments sa JOIN subjects sub ON sub.id = sa.subject_id JOIN classes c ON c.id = sa.class_id
           JOIN academic_terms t ON t.id = sa.term_id JOIN academic_sessions s ON s.id = t.academic_session_id
           WHERE sa.teacher_id = ? AND sa.school_id = ? AND sa.is_active = 1
           ORDER BY s.start_date DESC, t.start_date, c.name, sub.name""", (teacher_id, school_id())).fetchall()
    out = [dict(r, can_start=(r["term_status"] == "active" and r["session_status"] == "active")) for r in rows]
    return [r for r in out if r["can_start"]] if only_open else out


@bp.route("/dashboard")
@role_required(*TEACHER_ROLES)
def dashboard():
    db = get_db()
    teacher = teacher_row(db)
    if teacher is None:
        flash("No teacher profile is linked to this account.", "error")
        return render_template("teacher/dashboard.html", assignments=[], active_sessions=[], todays_count=0, teacher=None, total_students=0, class_names=[])
    sid = school_id()
    settings = settings_service.get_settings(sid)
    active_sessions = db.execute(
        """SELECT sess.*, sub.name AS subject_name, c.name AS class_name FROM attendance_sessions sess
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id JOIN subjects sub ON sub.id = sa.subject_id
           JOIN classes c ON c.id = sa.class_id
           WHERE sa.teacher_id = ? AND sess.school_id = ? AND sess.status IN ('active','paused') ORDER BY sess.started_at DESC""",
        (teacher["id"], sid)).fetchall()
    todays = db.execute(
        """SELECT COUNT(*) c FROM attendance_records ar JOIN attendance_sessions sess ON sess.id = ar.session_id
           JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
           WHERE sa.teacher_id = ? AND ar.school_id = ? AND sess.session_date = ?""",
        (teacher["id"], sid, school_today(settings))).fetchone()["c"]
    class_ids = teacher_class_ids(db)
    total_students = db.execute("SELECT COUNT(*) c FROM students WHERE school_id = ? AND status = 'active' AND class_id IN (%s)" % ",".join("?" * max(len(class_ids), 1)),
                                [sid] + (class_ids or [-1])).fetchone()["c"]
    class_names = [r["name"] for r in db.execute("SELECT name FROM classes WHERE school_id = ? AND id IN (%s) ORDER BY name" % ",".join("?" * max(len(class_ids), 1)),
                                                 [sid] + (class_ids or [-1])).fetchall()]
    return render_template("teacher/dashboard.html", assignments=_assignments(db, teacher["id"], only_open=True),
                           active_sessions=active_sessions, todays_count=todays, teacher=teacher, total_students=total_students,
                           class_names=class_names)


@bp.route("/classes")
@role_required(*TEACHER_ROLES)
def my_classes():
    db = get_db()
    teacher = teacher_row(db)
    assignments = _assignments(db, teacher["id"]) if teacher else []
    class_ids = teacher_class_ids(db)
    classes = []
    for cid in class_ids:
        c = db.execute("SELECT * FROM classes WHERE id = ? AND school_id = ?", (cid, school_id())).fetchone()
        if c:
            students = db.execute("SELECT id, admission_number, first_name, surname FROM students WHERE class_id = ? AND school_id = ? AND status = 'active' ORDER BY surname, first_name",
                                  (cid, school_id())).fetchall()
            classes.append({"row": c, "students": students, "subjects": [a for a in assignments if a["class_id"] == cid]})
    return render_template("teacher/classes.html", classes=classes)


@bp.route("/take-attendance")
@role_required(*TEACHER_ROLES)
def take_attendance():
    db = get_db()
    teacher = teacher_row(db)
    sid = school_id()
    ctx = academic_service.selected_context(db, sid, request.args.get("session_id"), request.args.get("term_id"))
    today = school_today(settings_service.get_settings(sid))
    items = []
    for a in (_assignments(db, teacher["id"]) if teacher else []):
        if ctx["term"] and a["term_id"] != ctx["term"]["id"]:
            continue
        existing = db.execute("SELECT id, status FROM attendance_sessions WHERE subject_assignment_id = ? AND session_date = ?", (a["id"], today)).fetchone()
        items.append(dict(a, existing=existing))
    return render_template("teacher/take_attendance.html", ctx=ctx, items=items, today=today)


@bp.route("/sessions/start", methods=("POST",))
@role_required(*TEACHER_ROLES)
def sessions_start():
    db = get_db()
    teacher = teacher_row(db)
    if teacher is None:
        flash("No teacher profile is linked to this account.", "error")
        return redirect(url_for("teacher.dashboard"))
    row, code, message = attendance_service.start_or_reuse_session(db, school_id(), g.user["id"], teacher["id"],
                                                                   request.form.get("subject_assignment_id"))
    if code in ("NOT_ASSIGNED", "TERM_CLOSED"):
        flash(message, "error")
        return redirect(url_for("teacher.take_attendance"))
    if code == "ALREADY_CLOSED":
        flash(message, "warning")
        return redirect(url_for("teacher.history"))
    flash(message, "success" if code == "CREATED" else "warning")
    return redirect(url_for("teacher.scanner", session_id=row["id"]))


def _toggle(session_id, new_status, clear_col, message, action):
    db = get_db()
    teacher = teacher_row(db)
    if teacher and _own_session(db, teacher["id"], session_id):
        db.execute(f"UPDATE attendance_sessions SET status = ?, {clear_col} WHERE id = ?", (new_status, session_id))
        db.commit()
        log_action(action, "attendance_session", session_id)
        flash(message, "info")
    return redirect(url_for("teacher.scanner", session_id=session_id))


@bp.route("/sessions/<int:session_id>/pause", methods=("POST",))
@role_required(*TEACHER_ROLES)
def sessions_pause(session_id):
    return _toggle(session_id, "paused", "paused_at = datetime('now')", "Session paused.", "attendance.session_paused")


@bp.route("/sessions/<int:session_id>/resume", methods=("POST",))
@role_required(*TEACHER_ROLES)
def sessions_resume(session_id):
    return _toggle(session_id, "active", "paused_at = NULL", "Session resumed.", "attendance.session_resumed")


@bp.route("/sessions/<int:session_id>/close", methods=("POST",))
@role_required(*TEACHER_ROLES)
def sessions_close(session_id):
    db = get_db()
    teacher = teacher_row(db)
    if teacher is None or _own_session(db, teacher["id"], session_id) is None:
        flash("Session not found.", "error")
        return redirect(url_for("teacher.dashboard"))
    close_session(db, session_id, g.user["id"], school_id=school_id())
    flash("Attendance session closed.", "success")
    return redirect(url_for("teacher.dashboard"))


@bp.route("/scanner/<int:session_id>")
@role_required(*TEACHER_ROLES)
def scanner(session_id):
    db = get_db()
    teacher = teacher_row(db)
    sid = school_id()
    session = _own_session(db, teacher["id"], session_id) if teacher else None
    if session is None:
        flash("Session not found.", "error")
        return redirect(url_for("teacher.dashboard"))
    assignment = db.execute("SELECT * FROM subject_assignments WHERE id = ?", (session["subject_assignment_id"],)).fetchone()
    subject = db.execute("SELECT * FROM subjects WHERE id = ?", (assignment["subject_id"],)).fetchone()
    class_row = db.execute("SELECT * FROM classes WHERE id = ?", (assignment["class_id"],)).fetchone()
    term = academic_service.get_term(db, sid, session["academic_term_id"])
    open_ok, open_msg = academic_service.open_for_attendance(db, session["academic_term_id"])
    stat = {"present": 0, "late": 0, "absent": 0, "excused": 0}
    for row in db.execute("SELECT status, COUNT(*) c FROM attendance_records WHERE session_id = ? GROUP BY status", (session_id,)).fetchall():
        if row["status"] in stat:
            stat[row["status"]] = row["c"]
    enrolled = db.execute("SELECT COUNT(*) c FROM students WHERE class_id = ? AND school_id = ? AND status = 'active'", (assignment["class_id"], sid)).fetchone()["c"]
    live = db.execute("""SELECT ar.*, st.first_name, st.surname, st.admission_number FROM attendance_records ar
                         JOIN students st ON st.id = ar.student_id WHERE ar.session_id = ? ORDER BY ar.recorded_at DESC LIMIT 30""", (session_id,)).fetchall()
    return render_template("teacher/scanner.html", session=session, subject=subject, class_row=class_row, term=term,
                           term_open=open_ok, term_message=open_msg, stat=stat, enrolled_count=enrolled, live_records=live,
                           settings=settings_service.get_settings(sid))


def _json_result(result):
    return jsonify({"state": result.code, "message": result.message, "student_name": result.student_name,
                    "admission_number": result.admission_number, "confidence": result.confidence, "status": result.status})


@bp.route("/scanner/<int:session_id>/recognize", methods=("POST",))
@role_required(*TEACHER_ROLES)
def scanner_recognize(session_id):
    db = get_db()
    teacher = teacher_row(db)
    session = _own_session(db, teacher["id"], session_id) if teacher else None
    if session is None:
        return jsonify({"state": "ERROR", "message": "Session not found."}), 404
    if session["status"] != "active":
        return jsonify({"state": "ERROR", "message": "This session is not active."}), 400
    file = request.files.get("frame")
    if file is None or file.filename == "":
        return jsonify({"state": "ERROR", "message": "No frame received."}), 400
    try:
        image = face_detection.decode_image(file.read())
    except ValueError:
        return jsonify({"state": "ERROR", "message": "Could not read the captured frame."}), 400
    try:
        face_crop = face_detection.extract_single_face(image)
    except face_detection.NoFaceDetected:
        return jsonify({"state": "WAITING", "message": "Waiting for a face..."})
    except face_detection.MultipleFacesDetected:
        return jsonify({"state": "ERROR", "message": "Multiple faces detected. One student at a time."})
    student_id, confidence = school_recognizer(current_app, school_id()).recognize(face_crop)
    return _json_result(record_attendance(db, session_id, student_id, "face", confidence=confidence,
                                          marked_by_user_id=g.user["id"], school_id=school_id()))


@bp.route("/scanner/<int:session_id>/qr", methods=("POST",))
@role_required(*TEACHER_ROLES)
def scanner_qr_redeem(session_id):
    db = get_db()
    teacher = teacher_row(db)
    if teacher is None or _own_session(db, teacher["id"], session_id) is None:
        return jsonify({"state": "ERROR", "message": "Session not found."}), 404
    redeemed_session, student_id, error = qr_service.redeem_token(db, request.form.get("token", "").strip(), school_id())
    messages = {"expired_token": ("ERROR", "This code has expired. Ask the student to refresh it."),
                "already_used": ("ALREADY_RECORDED", "This code was already used."), "invalid_token": ("ERROR", "Invalid code.")}
    if error:
        state, msg = messages[error]
        return jsonify({"state": state, "message": msg})
    if redeemed_session != session_id:
        return jsonify({"state": "ERROR", "message": "This code was issued for a different session."})
    return _json_result(record_attendance(db, session_id, student_id, "qr", marked_by_user_id=g.user["id"], school_id=school_id()))


@bp.route("/sessions/<int:session_id>/manual", methods=("GET", "POST"))
@role_required(*TEACHER_ROLES)
def manual_attendance(session_id):
    """Authorized manual attendance, e.g. when face recognition fails. Teacher picks
    the status; the record is tagged method=manual and carries the teacher's id."""
    db = get_db()
    teacher = teacher_row(db)
    session = _own_session(db, teacher["id"], session_id) if teacher else None
    if session is None:
        flash("Session not found.", "error")
        return redirect(url_for("teacher.dashboard"))
    if request.method == "POST":
        try:
            student_id = int(request.form.get("student_id"))
        except (TypeError, ValueError):
            flash("Choose a student.", "error")
            return redirect(url_for("teacher.manual_attendance", session_id=session_id))
        status = request.form.get("status", "present")
        if status not in MANUAL_STATUSES:
            flash("Invalid status.", "error")
        else:
            result = record_attendance(db, session_id, student_id, "manual", marked_by_user_id=g.user["id"],
                                       status_override=status, school_id=school_id())
            flash(result.message, "success" if result.ok else "error")
        return redirect(url_for("teacher.manual_attendance", session_id=session_id))
    roster = db.execute(
        """SELECT st.id, st.admission_number, st.first_name, st.surname,
                  (SELECT ar.status FROM attendance_records ar WHERE ar.session_id = ? AND ar.student_id = st.id) AS marked_status
           FROM students st WHERE st.class_id = ? AND st.school_id = ? AND st.status = 'active' ORDER BY st.surname, st.first_name""",
        (session_id, session["class_id"], school_id())).fetchall()
    return render_template("teacher/manual_attendance.html", session=session, roster=roster, statuses=MANUAL_STATUSES)


@bp.route("/history")
@role_required(*TEACHER_ROLES)
def history():
    """The teacher's attendance sessions, filterable by academic session, term, class and date."""
    db = get_db()
    teacher = teacher_row(db)
    sid = school_id()
    ctx = academic_service.selected_context(db, sid, request.args.get("session_id") or "", request.args.get("term_id") or "")
    clauses, params = ["sa.teacher_id = ?", "sess.school_id = ?"], [teacher["id"] if teacher else -1, sid]
    if request.args.get("session_id") and ctx["session"]:
        clauses.append("sess.academic_session_id = ?")
        params.append(ctx["session"]["id"])
    if request.args.get("term_id") and ctx["term"]:
        clauses.append("sess.academic_term_id = ?")
        params.append(ctx["term"]["id"])
    if request.args.get("date"):
        clauses.append("sess.session_date = ?")
        params.append(request.args["date"])
    if request.args.get("class_id", "").isdigit():
        clauses.append("sess.class_id = ?")
        params.append(int(request.args["class_id"]))
    rows = db.execute(
        f"""SELECT sess.*, sub.name AS subject_name, c.name AS class_name, t.name AS term_name, s.name AS session_name,
                   (SELECT COUNT(*) FROM attendance_records ar WHERE ar.session_id = sess.id AND ar.status IN ('present','late')) AS present_count,
                   (SELECT COUNT(*) FROM students st WHERE st.class_id = sa.class_id AND st.status = 'active') AS enrolled_count
            FROM attendance_sessions sess JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
            JOIN subjects sub ON sub.id = sa.subject_id JOIN classes c ON c.id = sa.class_id
            JOIN academic_terms t ON t.id = sess.academic_term_id JOIN academic_sessions s ON s.id = sess.academic_session_id
            WHERE {' AND '.join(clauses)} ORDER BY sess.session_date DESC, sess.started_at DESC LIMIT 200""", params).fetchall()
    classes = db.execute("SELECT id, name FROM classes WHERE school_id = ? AND id IN (%s)" % ",".join("?" * max(len(teacher_class_ids(db)), 1)),
                         [sid] + (teacher_class_ids(db) or [-1])).fetchall()
    return render_template("teacher/history.html", sessions=rows, ctx=ctx, classes=classes, args=request.args)


@bp.route("/reports")
@role_required(*TEACHER_ROLES)
def reports_home():
    db = get_db()
    sid = school_id()
    ids = teacher_class_ids(db)
    classes = db.execute("SELECT id, name FROM classes WHERE school_id = ? AND id IN (%s) ORDER BY name" % ",".join("?" * max(len(ids), 1)),
                         [sid] + (ids or [-1])).fetchall()
    return render_template("teacher/reports.html", classes=classes, ctx=academic_service.selected_context(db, sid, None, None))


@bp.route("/profile", methods=("GET", "POST"))
@role_required(*TEACHER_ROLES, "parent", "student")
def profile():
    db = get_db()
    teacher = teacher_row(db) if g.user["role"] in TEACHER_ROLES else None
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
            return render_template("teacher/profile.html", teacher=teacher)
        db.execute("UPDATE users SET full_name = ?, email = ?, updated_at = datetime('now') WHERE id = ?",
                   (form["full_name"].strip(), form["email"].strip().lower(), g.user["id"]))
        if changing:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(form["new_password"]), g.user["id"]))
        db.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("teacher.profile"))
    return render_template("teacher/profile.html", teacher=teacher)
