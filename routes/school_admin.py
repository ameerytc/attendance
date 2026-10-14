"""
School administration: dashboard, settings (school info, logo, academic rules,
account), academic sessions & terms, classes/subjects/assignments, and platform
school management for super administrators. Everything is school-scoped.
"""

import secrets
from datetime import datetime

from flask import (Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from database.db import get_db
from services import academic_service, image_service, settings_service, stats_service, storage_service
from services.academic_service import AcademicError
from utils.audit import log_action
from utils.decorators import ADMIN_ROLES, role_required
from utils.tenancy import owned, school_id
from utils.timeutil import school_today
from utils.validators import is_non_empty, is_valid_email, is_valid_password, validate_required_fields

bp = Blueprint("school_admin", __name__, url_prefix="/admin")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ================================================================ dashboard
@bp.route("/")
@role_required(*ADMIN_ROLES)
def dashboard():
    db = get_db()
    sid = school_id()
    settings = settings_service.get_settings(sid)
    ctx = academic_service.selected_context(db, sid, request.args.get("session_id"), request.args.get("term_id"))
    session_row, term_row = ctx["session"], ctx["term"]

    f = {}
    if session_row:
        f["session_id"] = session_row["id"]
    if term_row:
        f["term_id"] = term_row["id"]

    today = school_today(settings)
    today_stats = stats_service.today_distinct(db, sid, today, f)
    counts = {
        "students": db.execute("SELECT COUNT(*) c FROM students WHERE school_id = ? AND status = 'active'", (sid,)).fetchone()["c"],
        "teachers": db.execute("SELECT COUNT(*) c FROM teachers WHERE school_id = ? AND is_active = 1", (sid,)).fetchone()["c"],
        "classes": db.execute(
            "SELECT COUNT(*) c FROM classes WHERE school_id = ? AND is_active = 1" + (" AND academic_session_id = ?" if session_row else ""),
            (sid, session_row["id"]) if session_row else (sid,)).fetchone()["c"],
    }
    series = stats_service.daily_series(db, sid, f, limit=14)
    max_total = max([r["total"] for r in series] or [1]) or 1
    return render_template(
        "admin/dashboard.html", ctx=ctx, session_row=session_row, term_row=term_row, today=today,
        today_stats=today_stats, counts=counts, term_totals=stats_service.totals(db, sid, f),
        series=series, max_total=max_total, recent=stats_service.recent_activity(db, sid, f, 10),
        low_attendance=stats_service.low_attendance(db, sid, f, settings["attendance_threshold_pct"], limit=10),
        active_sessions=db.execute("SELECT COUNT(*) c FROM attendance_sessions WHERE school_id = ? AND status IN ('active','paused')",
                                   (sid,)).fetchone()["c"],
        settings=settings)


# ================================================================ settings
def _settings_context(db, sid):
    settings = settings_service.get_settings(sid)
    ctx = academic_service.selected_context(db, sid, request.args.get("session_id"), None)
    return dict(settings=settings, storage=storage_service.status(current_app.config), ctx=ctx,
                sessions=ctx["sessions"], terms=academic_service.terms_for_session(db, sid, ctx["session"]["id"]) if ctx["session"] else [])


@bp.route("/settings")
@role_required(*ADMIN_ROLES)
def settings_page():
    return render_template("admin/settings.html", **_settings_context(get_db(), school_id()))


@bp.route("/settings/school", methods=("POST",))
@role_required(*ADMIN_ROLES)
def settings_school():
    sid = school_id()
    form = request.form
    errors = validate_required_fields(form, ["school_name"])
    email = form.get("email", "").strip()
    website = form.get("website", "").strip()
    if email and not is_valid_email(email):
        errors.append("The school email address is not valid.")
    if website and not website.lower().startswith(("http://", "https://")):
        errors.append("The website must start with http:// or https://.")
    for field, limit in (("school_name", 150), ("motto", 200), ("address", 300), ("contact_info", 500), ("phone", 40)):
        if len(form.get(field, "")) > limit:
            errors.append(f"{field.replace('_', ' ').title()} is too long (max {limit} characters).")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("school_admin.settings_page") + "#school-info")
    before = dict(settings_service.get_settings(sid))
    fields = {"school_name": form["school_name"].strip(),
              "school_name_arabic": form.get("school_name_arabic", "").strip() or None,
              "motto": form.get("motto", "").strip() or None, "address": form.get("address", "").strip() or None,
              "phone": form.get("phone", "").strip() or None, "email": email or None, "website": website or None,
              "contact_info": form.get("contact_info", "").strip() or None}
    settings_service.update_settings(fields, sid)
    log_action("settings.school_info", "school_settings", sid, previous_value={k: before[k] for k in fields}, new_value=fields)
    flash("School information saved.", "success")
    return redirect(url_for("school_admin.settings_page") + "#school-info")


@bp.route("/settings/logo", methods=("POST",))
@role_required(*ADMIN_ROLES)
def settings_logo_upload():
    sid = school_id()
    cfg = current_app.config
    status = storage_service.status(cfg)
    if not status.configured:
        flash(status.message, "error")
        return redirect(url_for("school_admin.settings_page") + "#logo")
    upload = request.files.get("logo")
    if upload is None or not upload.filename:
        flash("Choose an image to upload first.", "error")
        return redirect(url_for("school_admin.settings_page") + "#logo")
    try:
        data, ext, content_type = image_service.process_logo(upload, cfg["MAX_LOGO_BYTES"])
    except image_service.ImageRejected as exc:
        flash(str(exc), "error")
        return redirect(url_for("school_admin.settings_page") + "#logo")
    key = image_service.new_logo_key(sid, ext)
    try:
        backend = storage_service.get_backend(cfg)
        backend.save(key, data, content_type)
    except (storage_service.StorageError, storage_service.StorageNotConfigured) as exc:
        flash(f"The logo could not be saved: {exc}", "error")
        return redirect(url_for("school_admin.settings_page") + "#logo")
    old = settings_service.get_settings(sid)["logo_url"]
    settings_service.update_settings({"logo_url": key}, sid)
    if old and old != key:
        try:
            backend.delete(old)
        except storage_service.StorageError:
            current_app.logger.warning("could not delete old logo %s", old)
    log_action("settings.logo_upload", "school_settings", sid, new_value={"logo": key})
    flash("Logo updated." + ("" if status.persistent else " (Development storage — not permanent.)"), "success")
    return redirect(url_for("school_admin.settings_page") + "#logo")


@bp.route("/settings/logo/remove", methods=("POST",))
@role_required(*ADMIN_ROLES)
def settings_logo_remove():
    sid = school_id()
    old = settings_service.get_settings(sid)["logo_url"]
    if old:
        try:
            storage_service.get_backend(current_app.config).delete(old)
        except (storage_service.StorageError, storage_service.StorageNotConfigured):
            current_app.logger.warning("could not delete logo %s", old)
        settings_service.update_settings({"logo_url": None}, sid)
        log_action("settings.logo_remove", "school_settings", sid)
        flash("Logo removed.", "info")
    return redirect(url_for("school_admin.settings_page") + "#logo")


@bp.route("/settings/academic", methods=("POST",))
@role_required(*ADMIN_ROLES)
def settings_academic():
    db = get_db()
    sid = school_id()
    form = request.form
    errors = []
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(form.get("timezone", "Africa/Lagos").strip())
    except Exception:  # noqa: BLE001
        errors.append("Unknown timezone (use a name such as Africa/Lagos).")
    try:
        threshold = float(form.get("attendance_threshold_pct", 75))
        late_after = int(form.get("late_after_minutes", 15))
        default_minutes = int(form.get("session_default_minutes", 60))
        qr_ttl = int(form.get("qr_token_ttl_seconds", 90))
        if not (0 <= threshold <= 100) or late_after < 0 or default_minutes < 1 or qr_ttl < 10:
            raise ValueError
    except ValueError:
        errors.append("Attendance rule values are out of range.")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("school_admin.settings_page") + "#academic")
    fields = {"timezone": form.get("timezone", "Africa/Lagos").strip(), "attendance_threshold_pct": threshold,
              "late_after_minutes": late_after, "session_default_minutes": default_minutes,
              "qr_token_ttl_seconds": qr_ttl,
              "enable_face_verification": 1 if form.get("enable_face_verification") else 0,
              "enable_qr_verification": 1 if form.get("enable_qr_verification") else 0}
    before = dict(settings_service.get_settings(sid))
    settings_service.update_settings(fields, sid)
    log_action("settings.attendance_rules", "school_settings", sid, previous_value={k: before[k] for k in fields}, new_value=fields)

    try:
        if form.get("current_session_id"):
            academic_service.activate_session(db, sid, _int(form["current_session_id"]))
        if form.get("current_term_id"):
            academic_service.activate_term(db, sid, _int(form["current_term_id"]))
    except AcademicError as exc:
        flash(str(exc), "error")
    else:
        flash("Academic settings saved.", "success")
    return redirect(url_for("school_admin.settings_page") + "#academic")


@bp.route("/settings/account", methods=("POST",))
@role_required(*ADMIN_ROLES)
def settings_account():
    db = get_db()
    form = request.form
    errors = validate_required_fields(form, ["full_name", "email"])
    if not is_valid_email(form.get("email", "")):
        errors.append("A valid email address is required.")
    if db.execute("SELECT 1 FROM users WHERE email = ? AND id != ?", (form.get("email", "").strip().lower(), g.user["id"])).fetchone():
        errors.append("That email is already in use by another account.")
    changing = bool(form.get("new_password"))
    if changing:
        if not check_password_hash(g.user["password_hash"], form.get("current_password", "")):
            errors.append("Current password is incorrect.")
        elif not is_valid_password(form.get("new_password", ""), 10):
            errors.append("New password must be at least 10 characters.")
        elif form.get("new_password") != form.get("confirm_password"):
            errors.append("New password and confirmation do not match.")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("school_admin.settings_page") + "#account")
    db.execute("UPDATE users SET full_name = ?, email = ?, updated_at = datetime('now') WHERE id = ?",
               (form["full_name"].strip(), form["email"].strip().lower(), g.user["id"]))
    if changing:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(form["new_password"]), g.user["id"]))
    db.commit()
    log_action("account.update", "user", g.user["id"], new_value={"password_changed": changing})
    flash("Account updated.", "success")
    return redirect(url_for("school_admin.settings_page") + "#account")


# ======================================================= academic sessions
@bp.route("/academic-sessions", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def academic_sessions():
    db = get_db()
    sid = school_id()
    if request.method == "POST":
        try:
            new_id = academic_service.create_session(db, sid, request.form.get("name"), request.form.get("start_date"),
                                                     request.form.get("end_date"))
            log_action("academic_session.create", "academic_session", new_id)
            flash("Academic session created with First, Second and Third Term. Review the term dates below.", "success")
        except AcademicError as exc:
            flash(str(exc), "error")
        return redirect(url_for("school_admin.academic_sessions"))
    sessions = db.execute("SELECT * FROM academic_sessions WHERE school_id = ? ORDER BY start_date DESC", (sid,)).fetchall()
    terms = {}
    for t in db.execute("SELECT * FROM academic_terms WHERE school_id = ? ORDER BY start_date", (sid,)).fetchall():
        terms.setdefault(t["academic_session_id"], []).append(t)
    return render_template("admin/academic_sessions.html", sessions=sessions, terms=terms)


@bp.route("/academic-sessions/<int:session_id>")
@role_required(*ADMIN_ROLES)
def academic_session_detail(session_id):
    db = get_db()
    sid = school_id()
    row = academic_service.get_session(db, sid, session_id)
    if row is None:
        flash("Session not found.", "error")
        return redirect(url_for("school_admin.academic_sessions"))
    f = {"session_id": session_id}
    per_term = {r["term_id"]: r for r in stats_service.per_term(db, sid, f)}
    return render_template("admin/academic_session_detail.html", session_row=row,
                           terms=academic_service.terms_for_session(db, sid, session_id), per_term=per_term,
                           totals=stats_service.totals(db, sid, f), per_class=stats_service.per_class(db, sid, f))


def _session_action(action, label):
    def view(session_id):
        try:
            action(get_db(), school_id(), session_id)
            log_action(f"academic_session.{label}", "academic_session", session_id)
            flash(f"Session {label}d.".replace("ee", "e"), "success")
        except AcademicError as exc:
            flash(str(exc), "error")
        return redirect(url_for("school_admin.academic_sessions"))
    view.__name__ = f"{label}_academic_session"
    return view


bp.add_url_rule("/academic-sessions/<int:session_id>/activate", "activate_academic_session",
                role_required(*ADMIN_ROLES)(_session_action(academic_service.activate_session, "activate")), methods=("POST",))
bp.add_url_rule("/academic-sessions/<int:session_id>/close", "close_academic_session",
                role_required(*ADMIN_ROLES)(_session_action(academic_service.close_session, "close")), methods=("POST",))
bp.add_url_rule("/academic-sessions/<int:session_id>/archive", "archive_academic_session",
                role_required(*ADMIN_ROLES)(_session_action(academic_service.archive_session, "archive")), methods=("POST",))


@bp.route("/academic-sessions/<int:session_id>/edit", methods=("POST",))
@role_required(*ADMIN_ROLES)
def edit_academic_session(session_id):
    try:
        academic_service.update_session(get_db(), school_id(), session_id, request.form.get("name"),
                                        request.form.get("start_date"), request.form.get("end_date"))
        log_action("academic_session.edit", "academic_session", session_id)
        flash("Session updated.", "success")
    except AcademicError as exc:
        flash(str(exc), "error")
    return redirect(url_for("school_admin.academic_sessions"))


@bp.route("/terms")
@role_required(*ADMIN_ROLES)
def terms():
    return redirect(url_for("school_admin.academic_sessions"))


@bp.route("/terms/<int:term_id>/edit", methods=("POST",))
@role_required(*ADMIN_ROLES)
def edit_term(term_id):
    try:
        academic_service.update_term(get_db(), school_id(), term_id, request.form.get("start_date"), request.form.get("end_date"))
        log_action("term.edit", "term", term_id)
        flash("Term dates updated.", "success")
    except AcademicError as exc:
        flash(str(exc), "error")
    return redirect(url_for("school_admin.academic_sessions"))


@bp.route("/terms/<int:term_id>/activate", methods=("POST",))
@role_required(*ADMIN_ROLES)
def activate_term(term_id):
    try:
        academic_service.activate_term(get_db(), school_id(), term_id)
        log_action("term.activate", "term", term_id)
        flash("Term activated. Previously recorded attendance is unchanged.", "success")
    except AcademicError as exc:
        flash(str(exc), "error")
    return redirect(request.form.get("next") or url_for("school_admin.academic_sessions"))


@bp.route("/terms/<int:term_id>/complete", methods=("POST",))
@role_required(*ADMIN_ROLES)
def complete_term(term_id):
    try:
        academic_service.complete_term(get_db(), school_id(), term_id)
        log_action("term.complete", "term", term_id)
        flash("Term marked completed.", "success")
    except AcademicError as exc:
        flash(str(exc), "error")
    return redirect(url_for("school_admin.academic_sessions"))


# ================================================== sections / departments
@bp.route("/sections", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def sections():
    db = get_db()
    sid = school_id()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not is_non_empty(name):
            flash("Section name is required.", "error")
        else:
            try:
                cur = db.execute("INSERT INTO sections (school_id, name, description) VALUES (?, ?, ?)",
                                 (sid, name, request.form.get("description", "").strip() or None))
                db.commit()
                log_action("section.create", "section", cur.lastrowid)
                flash("Section created.", "success")
            except Exception:  # noqa: BLE001
                db.rollback()
                flash("A section with that name already exists.", "error")
        return redirect(url_for("school_admin.sections"))
    return render_template("admin/sections.html", rows=db.execute("SELECT * FROM sections WHERE school_id = ? ORDER BY name", (sid,)).fetchall())


@bp.route("/departments", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def departments():
    db = get_db()
    sid = school_id()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        section_id = _int(request.form.get("section_id"))
        if not is_non_empty(name):
            flash("Department name is required.", "error")
        elif section_id and owned(db, "sections", section_id) is None:
            flash("Unknown section.", "error")
        else:
            try:
                cur = db.execute("INSERT INTO departments (school_id, section_id, name) VALUES (?, ?, ?)", (sid, section_id, name))
                db.commit()
                log_action("department.create", "department", cur.lastrowid)
                flash("Department created.", "success")
            except Exception:  # noqa: BLE001
                db.rollback()
                flash("That department already exists.", "error")
        return redirect(url_for("school_admin.departments"))
    rows = db.execute("""SELECT d.*, s.name AS section_name FROM departments d LEFT JOIN sections s ON s.id = d.section_id
                         WHERE d.school_id = ? ORDER BY d.name""", (sid,)).fetchall()
    return render_template("admin/departments.html", rows=rows,
                           sections=db.execute("SELECT * FROM sections WHERE school_id = ? AND is_active = 1 ORDER BY name", (sid,)).fetchall())


# ============================================================== classes
@bp.route("/classes", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def classes_list():
    db = get_db()
    sid = school_id()
    active_session, _ = academic_service.current_context(db, sid)
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["name"])
        session_id = active_session["id"] if active_session else _int(request.form.get("academic_session_id"))
        if not session_id or academic_service.get_session(db, sid, session_id) is None:
            errors.append("No academic session is active. Create and activate one first.")
        for table, field in (("sections", "section_id"), ("departments", "department_id"), ("teachers", "class_teacher_id")):
            value = _int(request.form.get(field))
            if request.form.get(field) and (value is None or owned(db, table, value) is None):
                errors.append(f"Invalid {field.replace('_id', '').replace('_', ' ')}.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            try:
                cur = db.execute(
                    """INSERT INTO classes (school_id, name, section_id, department_id, academic_session_id, class_teacher_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (sid, request.form["name"].strip(), _int(request.form.get("section_id")), _int(request.form.get("department_id")),
                     session_id, _int(request.form.get("class_teacher_id"))))
                db.commit()
                log_action("class.create", "class", cur.lastrowid)
                flash("Class created.", "success")
            except Exception:  # noqa: BLE001
                db.rollback()
                flash("A class with that name already exists in this session.", "error")
        return redirect(url_for("school_admin.classes_list"))
    rows = db.execute(
        """SELECT c.*, sec.name AS section_name, dep.name AS department_name, t.staff_id, u.full_name AS teacher_name,
                  s.name AS session_name,
                  (SELECT COUNT(*) FROM students st WHERE st.class_id = c.id AND st.status = 'active') AS student_count
           FROM classes c LEFT JOIN sections sec ON sec.id = c.section_id LEFT JOIN departments dep ON dep.id = c.department_id
           LEFT JOIN teachers t ON t.id = c.class_teacher_id LEFT JOIN users u ON u.id = t.user_id
           JOIN academic_sessions s ON s.id = c.academic_session_id
           WHERE c.school_id = ? ORDER BY s.start_date DESC, c.name""", (sid,)).fetchall()
    return render_template(
        "admin/classes.html", rows=rows, active_session=active_session,
        sections=db.execute("SELECT * FROM sections WHERE school_id = ? AND is_active = 1 ORDER BY name", (sid,)).fetchall(),
        departments=db.execute("SELECT * FROM departments WHERE school_id = ? AND is_active = 1 ORDER BY name", (sid,)).fetchall(),
        teachers=db.execute("""SELECT t.id, u.full_name FROM teachers t JOIN users u ON u.id = t.user_id
                               WHERE t.school_id = ? AND t.is_active = 1 ORDER BY u.full_name""", (sid,)).fetchall())


@bp.route("/classes/<int:class_id>/deactivate", methods=("POST",))
@role_required(*ADMIN_ROLES)
def deactivate_class(class_id):
    db = get_db()
    if owned(db, "classes", class_id):
        db.execute("UPDATE classes SET is_active = 0 WHERE id = ? AND school_id = ?", (class_id, school_id()))
        db.commit()
        log_action("class.deactivate", "class", class_id)
        flash("Class deactivated.", "info")
    return redirect(url_for("school_admin.classes_list"))


# ============================================================== subjects
@bp.route("/subjects", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def subjects_list():
    db = get_db()
    sid = school_id()
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["code", "name", "category"])
        if request.form.get("category") not in ("QURANIC", "ACADEMIC", "ISLAMIC_STUDIES", "LANGUAGE", "OTHER"):
            errors.append("Invalid subject category.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            try:
                cur = db.execute("INSERT INTO subjects (school_id, code, name, category, description) VALUES (?, ?, ?, ?, ?)",
                                 (sid, request.form["code"].strip().upper(), request.form["name"].strip(), request.form["category"],
                                  request.form.get("description", "").strip() or None))
                db.commit()
                log_action("subject.create", "subject", cur.lastrowid)
                flash("Subject created.", "success")
            except Exception:  # noqa: BLE001
                db.rollback()
                flash("A subject with that code already exists.", "error")
        return redirect(url_for("school_admin.subjects_list"))
    return render_template("admin/subjects.html", rows=db.execute(
        "SELECT * FROM subjects WHERE school_id = ? ORDER BY category, name", (sid,)).fetchall())


@bp.route("/subjects/<int:subject_id>/deactivate", methods=("POST",))
@role_required(*ADMIN_ROLES)
def deactivate_subject(subject_id):
    db = get_db()
    if owned(db, "subjects", subject_id):
        db.execute("UPDATE subjects SET is_active = 0 WHERE id = ? AND school_id = ?", (subject_id, school_id()))
        db.commit()
        log_action("subject.deactivate", "subject", subject_id)
        flash("Subject deactivated.", "info")
    return redirect(url_for("school_admin.subjects_list"))


@bp.route("/subject-assignments", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def subject_assignments():
    db = get_db()
    sid = school_id()
    _, active_term = academic_service.current_context(db, sid)
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["subject_id", "class_id", "teacher_id"])
        term_id = _int(request.form.get("term_id")) or (active_term["id"] if active_term else None)
        term = academic_service.get_term(db, sid, term_id) if term_id else None
        if term is None:
            errors.append("Choose a term (or activate one first).")
        elif term["status"] == "completed":
            errors.append("That term is completed; assign subjects to an active or upcoming term.")
        ids = {}
        for table, field in (("subjects", "subject_id"), ("classes", "class_id"), ("teachers", "teacher_id")):
            ids[field] = _int(request.form.get(field))
            if ids[field] is None or owned(db, table, ids[field]) is None:
                errors.append(f"Invalid {field.replace('_id', '')}.")
        if not errors:
            cls = owned(db, "classes", ids["class_id"])
            if term and cls["academic_session_id"] != term["academic_session_id"]:
                errors.append("That class belongs to a different academic session than the chosen term.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            try:
                cur = db.execute("INSERT INTO subject_assignments (school_id, subject_id, class_id, teacher_id, term_id) VALUES (?, ?, ?, ?, ?)",
                                 (sid, ids["subject_id"], ids["class_id"], ids["teacher_id"], term["id"]))
                db.commit()
                log_action("subject_assignment.create", "subject_assignment", cur.lastrowid)
                flash("Teacher assigned to class/subject.", "success")
            except Exception:  # noqa: BLE001
                db.rollback()
                flash("This subject is already assigned to that class/teacher/term.", "error")
        return redirect(url_for("school_admin.subject_assignments"))
    rows = db.execute(
        """SELECT sa.*, sub.code, sub.name AS subject_name, c.name AS class_name, u.full_name AS teacher_name,
                  t.name AS term_name, s.name AS session_name, t.status AS term_status
           FROM subject_assignments sa JOIN subjects sub ON sub.id = sa.subject_id JOIN classes c ON c.id = sa.class_id
           JOIN teachers te ON te.id = sa.teacher_id JOIN users u ON u.id = te.user_id
           JOIN academic_terms t ON t.id = sa.term_id JOIN academic_sessions s ON s.id = t.academic_session_id
           WHERE sa.school_id = ? ORDER BY s.start_date DESC, t.start_date, c.name, sub.name""", (sid,)).fetchall()
    return render_template(
        "admin/subject_assignments.html", rows=rows, active_term=active_term,
        subjects=db.execute("SELECT * FROM subjects WHERE school_id = ? AND is_active = 1 ORDER BY name", (sid,)).fetchall(),
        classes=db.execute("SELECT * FROM classes WHERE school_id = ? AND is_active = 1 ORDER BY name", (sid,)).fetchall(),
        teachers=db.execute("""SELECT t.id, u.full_name FROM teachers t JOIN users u ON u.id = t.user_id
                               WHERE t.school_id = ? AND t.is_active = 1 ORDER BY u.full_name""", (sid,)).fetchall(),
        terms=db.execute("""SELECT t.id, t.name, t.status, s.name AS session_name FROM academic_terms t
                            JOIN academic_sessions s ON s.id = t.academic_session_id
                            WHERE t.school_id = ? AND t.status != 'completed' AND s.status IN ('active','upcoming')
                            ORDER BY s.start_date DESC, t.start_date""", (sid,)).fetchall())


# ============================================================== audit log
@bp.route("/audit-log")
@role_required("super_admin", "school_admin", "auditor")
def audit_log():
    db = get_db()
    sid = school_id()
    page = max(_int(request.args.get("page")) or 1, 1)
    per_page = 50
    total = db.execute("SELECT COUNT(*) c FROM audit_logs WHERE school_id = ?", (sid,)).fetchone()["c"]
    rows = db.execute("""SELECT al.*, u.full_name, u.email FROM audit_logs al LEFT JOIN users u ON u.id = al.user_id
                         WHERE al.school_id = ? ORDER BY al.created_at DESC, al.id DESC LIMIT ? OFFSET ?""",
                      (sid, per_page, (page - 1) * per_page)).fetchall()
    return render_template("admin/audit_log.html", rows=rows, page=page, per_page=per_page, total=total)


# ================================================== platform: schools
@bp.route("/schools", methods=("GET", "POST"))
@role_required("super_admin")
def schools():
    db = get_db()
    if request.method == "POST":
        code = request.form.get("code", "").strip().lower()
        name = request.form.get("name", "").strip()
        admin_email = request.form.get("admin_email", "").strip().lower()
        admin_name = request.form.get("admin_name", "").strip()
        errors = []
        if not code or not code.replace("-", "").isalnum():
            errors.append("School code must be letters, numbers or dashes.")
        if not name:
            errors.append("School name is required.")
        if not is_valid_email(admin_email) or not admin_name:
            errors.append("The first administrator's name and a valid email are required.")
        if db.execute("SELECT 1 FROM schools WHERE code = ?", (code,)).fetchone():
            errors.append("That school code is already in use.")
        if db.execute("SELECT 1 FROM users WHERE email = ?", (admin_email,)).fetchone():
            errors.append("That administrator email is already registered.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            temp = secrets.token_urlsafe(12)
            cur = db.execute("INSERT INTO schools (code, name) VALUES (?, ?)", (code, name))
            school_new = cur.lastrowid
            db.execute("INSERT INTO school_settings (school_id, school_name) VALUES (?, ?)", (school_new, name))
            db.execute("INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (?, ?, ?, 'school_admin', ?)",
                       (school_new, admin_email, generate_password_hash(temp), admin_name))
            db.commit()
            log_action("school.create", "school", school_new, school_id=school_new)
            flash(f"School created. First administrator temporary password: {temp} (share securely; change after login).", "success")
        return redirect(url_for("school_admin.schools"))
    rows = db.execute("""SELECT s.*, (SELECT COUNT(*) FROM students st WHERE st.school_id = s.id) AS students,
                                (SELECT COUNT(*) FROM teachers t WHERE t.school_id = s.id) AS teachers
                         FROM schools s ORDER BY s.name""").fetchall()
    return render_template("admin/schools.html", rows=rows)


@bp.route("/schools/switch", methods=("POST",))
@role_required("super_admin")
def switch_school():
    target = _int(request.form.get("school_id"))
    if target and get_db().execute("SELECT 1 FROM schools WHERE id = ?", (target,)).fetchone():
        session["school_id"] = target
        flash("Switched school.", "info")
    return redirect(url_for("school_admin.dashboard"))
