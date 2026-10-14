"""Teachers, students, parents/guardians and bulk import — all school-scoped.
Students are never hard-deleted: their status changes, since attendance history
references them."""

import secrets
from uuid import uuid4

from datetime import date, datetime

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from database.db import get_db
from services import (academic_service, credential_store, image_service, import_service, report_service, settings_service,
                      storage_service, teacher_service)
from utils.audit import log_action
from utils.decorators import ADMIN_ROLES, role_required
from utils.tenancy import owned, school_id
from utils.validators import is_valid_email, validate_required_fields

bp = Blueprint("admin_people", __name__, url_prefix="/admin")
STUDENT_STATUSES = ("active", "graduated", "suspended", "withdrawn", "transferred", "deferred", "inactive")


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _school_code(db, sid):
    return db.execute("SELECT code FROM schools WHERE id = ?", (sid,)).fetchone()["code"]


# -------------------------------------------------------------- teachers
def _store_photo(db, kind, row_id, upload):
    """Validate + store a passport photo; returns (ok, message). Replaces and cleans up the old file."""
    sid = school_id()
    status = storage_service.status(current_app.config)
    if not status.configured:
        return False, status.message
    try:
        data, ext, ctype = image_service.process_logo(upload, current_app.config["MAX_LOGO_BYTES"], max_side=800)
    except image_service.ImageRejected as exc:
        return False, str(exc)
    key = image_service.new_media_key(sid, kind, ext)
    try:
        backend = storage_service.get_backend(current_app.config)
        backend.save(key, data, ctype)
    except (storage_service.StorageError, storage_service.StorageNotConfigured) as exc:
        return False, f"Photo could not be saved: {exc}"
    table = "teachers" if kind == "teachers" else "students"
    old = db.execute(f"SELECT photo_path FROM {table} WHERE id = ? AND school_id = ?", (row_id, sid)).fetchone()
    db.execute(f"UPDATE {table} SET photo_path = ? WHERE id = ? AND school_id = ?", (key, row_id, sid))
    db.commit()
    if old and old["photo_path"] and old["photo_path"] != key:
        try:
            backend.delete(old["photo_path"])
        except storage_service.StorageError:
            current_app.logger.warning("could not delete old photo %s", old["photo_path"])
    return True, "Photo saved."


def _class_choices(db, sid):
    session, _ = academic_service.current_context(db, sid)
    sql, params = "SELECT id, name, class_teacher_id FROM classes WHERE school_id = ? AND is_active = 1", [sid]
    if session:
        sql += " AND academic_session_id = ?"
        params.append(session["id"])
    return db.execute(sql + " ORDER BY name", params).fetchall()


def _finish_batch(db, kind, created, failed, submitted):
    token = credential_store.put(school_id(), g.user["id"], {
        "kind": kind, "submitted": submitted, "results": created, "failed": failed,
        "created_at": datetime.now().isoformat(timespec="seconds")})
    log_action("teacher.bulk_create" if submitted > 1 else "teacher.create", "teacher", None,
               new_value={"submitted": submitted, "created": len(created), "failed": len(failed)})
    return redirect(url_for("admin_people.teacher_summary", token=token))


@bp.route("/teachers")
@role_required(*ADMIN_ROLES)
def teachers_list():
    db = get_db()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    rows = teacher_service.teacher_list_rows(db, school_id(), q, status)
    return render_template("admin/teachers_list.html", rows=rows, q=q, status=status)


@bp.route("/teachers/new", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def teacher_new():
    db = get_db()
    sid = school_id()
    form_data = {}
    if request.method == "POST":
        f = request.form
        row = {c: f.get(c, "").strip() for c in teacher_service.CSV_COLUMNS}
        row["assigned_classes"] = ";".join(f.getlist("class_names"))
        row["account_status"] = f.get("account_status", "active")
        row["temp_password"] = f.get("temp_password", "")
        form_data = dict(row)
        validated = teacher_service.validate_rows(db, sid, [row])
        photo = request.files.get("photo")
        photo_error = None
        if photo is not None and photo.filename:
            try:   # validate BEFORE creating anything so a bad photo does not leave a half-made account
                image_service.process_logo(photo, current_app.config["MAX_LOGO_BYTES"], max_side=800)
                photo.stream.seek(0)
            except image_service.ImageRejected as exc:
                photo_error = str(exc)
            if not storage_service.status(current_app.config).configured:
                photo_error = storage_service.status(current_app.config).message
        if validated[0]["errors"] or photo_error:
            for e in validated[0]["errors"] + ([photo_error] if photo_error else []):
                flash(e, "error")
        else:
            created, failed = teacher_service.create_teachers(db, sid, _school_code(db, sid), validated)
            if created and photo is not None and photo.filename:
                ok, msg = _store_photo(db, "teachers", created[0]["teacher_id"], photo)
                if not ok:
                    flash("Account created, but the photo was not saved: " + msg, "warning")
            return _finish_batch(db, "single", created, failed, 1)
    return render_template("admin/teacher_form.html", teacher=None, form=form_data, classes=_class_choices(db, sid),
                           subjects=db.execute("SELECT code, name FROM subjects WHERE school_id = ? AND is_active = 1 ORDER BY name", (sid,)).fetchall(),
                           storage=storage_service.status(current_app.config))


@bp.route("/teachers/bulk", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def teachers_bulk():
    """Method A: several rows typed into a form. Method B: CSV upload with a validate-first preview."""
    db = get_db()
    sid = school_id()
    preview = None
    if request.method == "POST":
        mode = request.form.get("mode")
        if mode == "form":
            f = request.form
            names = f.getlist("first_name")
            rows = []
            for i in range(len(names)):
                row = {c: (f.getlist(c)[i].strip() if c in f and i < len(f.getlist(c)) else "") for c in teacher_service.CSV_COLUMNS}
                if any(row.values()):
                    rows.append(row)
            if not rows:
                flash("Add at least one teacher.", "error")
            else:
                validated = teacher_service.validate_rows(db, sid, rows)
                created, failed = teacher_service.create_teachers(db, sid, _school_code(db, sid), validated)
                return _finish_batch(db, "bulk-form", created, failed, len(rows))
        elif mode == "csv":
            file = request.files.get("csv_file")
            if not file or not file.filename:
                flash("Choose a CSV file first.", "error")
            else:
                text = file.read().decode("utf-8-sig", errors="replace")
                rows, problems = teacher_service.parse_csv(text)
                if any(p.startswith("Missing required") for p in problems) or not rows:
                    for p in problems or ["The file has no data rows."]:
                        flash(p, "error")
                else:
                    for p in problems:
                        flash(p, "warning")
                    validated = teacher_service.validate_rows(db, sid, rows)
                    token = credential_store.put(sid, g.user["id"], {"kind": "csv-preview", "validated": validated, "filename": secure_filename(file.filename)})
                    preview = {"token": token, "validated": validated, "ok": sum(1 for v in validated if not v["errors"]),
                               "bad": sum(1 for v in validated if v["errors"])}
        elif mode == "confirm":
            payload = credential_store.get(request.form.get("token", ""), sid, g.user["id"])
            if not payload or payload.get("kind") != "csv-preview":
                flash("That preview has expired. Upload the file again.", "error")
            else:
                created, failed = teacher_service.create_teachers(db, sid, _school_code(db, sid), payload["validated"])
                return _finish_batch(db, "csv", created, failed, len(payload["validated"]))
    return render_template("admin/teachers_bulk.html", preview=preview, classes=_class_choices(db, sid), storage=storage_service.status(current_app.config))


@bp.route("/teachers/summary/<token>")
@role_required(*ADMIN_ROLES)
def teacher_summary(token):
    payload = credential_store.get(token, school_id(), g.user["id"])
    if payload is None or "results" not in payload:
        flash("That summary has expired. Your accounts were saved — see the teacher list. Use Reset password if someone needs new login details.", "info")
        return redirect(url_for("admin_people.teachers_list"))
    failed = payload["failed"]
    return render_template("admin/teacher_summary.html", token=token, p=payload, created=payload["results"], failed=failed,
                           duplicates=sum(1 for f in failed if f["kind"] == "duplicate"),
                           corrections=sum(1 for f in failed if f["kind"] != "duplicate"))


@bp.route("/teachers/summary/<token>/credentials.csv")
@role_required(*ADMIN_ROLES)
def teacher_credentials_csv(token):
    payload = credential_store.get(token, school_id(), g.user["id"])
    if payload is None or payload.get("secrets_cleared") or not payload.get("results"):
        flash("The credentials file is no longer available (it is only kept for a few minutes). Use Reset password to issue new login details.", "error")
        return redirect(url_for("admin_people.teachers_list"))
    log_action("teacher.credentials_download", "teacher", None, new_value={"count": len(payload["results"])})
    return Response(teacher_service.credentials_csv(payload["results"]), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=teacher_credentials.csv", "Cache-Control": "no-store"})


@bp.route("/teachers/summary/<token>/forget-passwords", methods=("POST",))
@role_required(*ADMIN_ROLES)
def teacher_forget_passwords(token):
    credential_store.drop_secrets(token, school_id(), g.user["id"])
    flash("Temporary passwords were removed from the server. Download is no longer possible.", "info")
    return redirect(url_for("admin_people.teacher_summary", token=token))


@bp.route("/teachers/summary/<token>/errors.csv")
@role_required(*ADMIN_ROLES)
def teacher_error_report(token):
    payload = credential_store.get(token, school_id(), g.user["id"])
    if payload is None:
        abort(404)
    failed = payload.get("failed") or [{"row_no": v["row_no"], "kind": v["kind"], "errors": v["errors"], "data": v["data"]}
                                      for v in payload.get("validated", []) if v["errors"]]
    return Response(teacher_service.error_report_csv(failed), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=teacher_import_errors.csv"})


@bp.route("/teachers/export.csv")
@role_required(*ADMIN_ROLES)
def teachers_export_csv():
    """Ordinary teacher list — never contains passwords."""
    rows = teacher_service.teacher_list_rows(get_db(), school_id(), request.args.get("q", "").strip(), request.args.get("status", ""))
    return Response(teacher_service.teacher_list_csv(rows), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=teacher_list.csv"})


@bp.route("/teachers/export.pdf")
@role_required(*ADMIN_ROLES)
def teachers_export_pdf():
    db = get_db()
    settings = settings_service.get_settings(school_id())
    rows = teacher_service.teacher_list_rows(db, school_id(), request.args.get("q", "").strip(), request.args.get("status", ""))
    report = {"title": "Teacher List", "meta": [("School", settings["school_name"]), ("Report date", date.today().isoformat())],
              "summary": [("Teachers", len(rows)), ("Active", sum(1 for r in rows if r["is_active"]))],
              "columns": ["Teacher ID", "Name", "Phone", "Email", "Classes", "Status"],
              "rows": [[r["staff_id"], r["full_name"], r["phone"] or "", r["email"], r["class_names"] or "", "Active" if r["is_active"] else "Inactive"] for r in rows]}
    pdf = report_service.render_pdf(settings, report_service.load_logo_bytes(current_app.config, settings), report)
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": "attachment; filename=teacher_list.pdf"})


@bp.route("/teachers/import/template.csv")
@role_required(*ADMIN_ROLES)
def teachers_import_template():
    return Response(teacher_service.template_csv(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=teacher_import_template.csv"})


@bp.route("/teachers/<int:teacher_id>")
@role_required(*ADMIN_ROLES)
def teacher_view(teacher_id):
    db = get_db()
    row = next((r for r in teacher_service.teacher_list_rows(db, school_id()) if r["id"] == teacher_id), None)
    if row is None:
        flash("Teacher not found.", "error")
        return redirect(url_for("admin_people.teachers_list"))
    assignments = db.execute(
        """SELECT sa.id, c.name AS class_name, sub.name AS subject_name, t.name AS term_name FROM subject_assignments sa
           JOIN classes c ON c.id = sa.class_id JOIN subjects sub ON sub.id = sa.subject_id JOIN academic_terms t ON t.id = sa.term_id
           WHERE sa.teacher_id = ? AND sa.school_id = ? ORDER BY t.start_date DESC, c.name""", (teacher_id, school_id())).fetchall()
    return render_template("admin/teacher_view.html", t=row, assignments=assignments)


@bp.route("/teachers/<int:teacher_id>/edit", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def teacher_edit(teacher_id):
    db = get_db()
    sid = school_id()
    t = owned(db, "teachers", teacher_id)
    if t is None:
        flash("Teacher not found.", "error")
        return redirect(url_for("admin_people.teachers_list"))
    user = db.execute("SELECT * FROM users WHERE id = ?", (t["user_id"],)).fetchone()
    if request.method == "POST":
        f = request.form
        errors = validate_required_fields(f, ["first_name", "surname"])
        email = f.get("email", "").strip().lower()
        if not is_valid_email(email):
            errors.append("A valid email is required.")
        if db.execute("SELECT 1 FROM users WHERE email = ? AND id != ?", (email, user["id"])).fetchone():
            errors.append("That email belongs to another account.")
        gender = f.get("gender") or None
        if gender not in (None, "male", "female"):
            errors.append("Invalid gender.")
        photo = request.files.get("photo")
        if photo is not None and photo.filename:
            try:
                image_service.process_logo(photo, current_app.config["MAX_LOGO_BYTES"], max_side=800)
                photo.stream.seek(0)
            except image_service.ImageRejected as exc:
                errors.append(str(exc))
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            full = " ".join(p for p in (f["first_name"].strip(), f.get("middle_name", "").strip(), f["surname"].strip()) if p)
            active = 1 if f.get("account_status", "active") == "active" else 0
            db.execute("UPDATE users SET full_name = ?, email = ?, is_active = ?, updated_at = datetime('now') WHERE id = ?", (full, email, active, user["id"]))
            db.execute("UPDATE teachers SET first_name = ?, middle_name = ?, surname = ?, gender = ?, phone = ?, address = ? WHERE id = ? AND school_id = ?",
                       (f["first_name"].strip(), f.get("middle_name", "").strip() or None, f["surname"].strip(), gender,
                        f.get("phone", "").strip() or None, f.get("address", "").strip() or None, teacher_id, sid))
            db.commit()
            if photo is not None and photo.filename:
                ok, msg = _store_photo(db, "teachers", teacher_id, photo)
                if not ok:
                    flash(msg, "warning")
            log_action("teacher.edit", "teacher", teacher_id)
            flash("Teacher updated.", "success")
            return redirect(url_for("admin_people.teacher_view", teacher_id=teacher_id))
    form = {"first_name": t["first_name"] or user["full_name"], "middle_name": t["middle_name"], "surname": t["surname"], "gender": t["gender"],
            "phone": t["phone"], "address": t["address"], "email": user["email"], "staff_id": t["staff_id"],
            "account_status": "active" if user["is_active"] else "inactive"}
    return render_template("admin/teacher_form.html", teacher=t, form=form, classes=[], subjects=[], storage=storage_service.status(current_app.config))


@bp.route("/teachers/<int:teacher_id>/reset-password", methods=("POST",))
@role_required(*ADMIN_ROLES)
def teacher_reset_password(teacher_id):
    """Issue a new temporary password (shown once via the credentials download); forces a change at next login."""
    db = get_db()
    row = db.execute("""SELECT t.*, u.email, u.username, u.is_active FROM teachers t JOIN users u ON u.id = t.user_id
                        WHERE t.id = ? AND t.school_id = ?""", (teacher_id, school_id())).fetchone()
    if row is None:
        flash("Teacher not found.", "error")
        return redirect(url_for("admin_people.teachers_list"))
    password = teacher_service.generate_password()
    db.execute("UPDATE users SET password_hash = ?, must_change_password = 1, failed_login_count = 0, locked_until = NULL WHERE id = ?",
               (generate_password_hash(password), row["user_id"]))
    db.commit()
    log_action("teacher.password_reset", "teacher", teacher_id)
    result = {"teacher_id": teacher_id, "staff_id": row["staff_id"], "first_name": row["first_name"] or "", "middle_name": row["middle_name"] or "",
              "surname": row["surname"] or "", "email": row["email"], "username": row["username"] or "", "password": password,
              "assigned_classes": "", "status": "Active" if row["is_active"] else "Inactive", "notes": "", "row_no": 1}
    return _finish_batch(db, "reset", [result], [], 1)


@bp.route("/teachers/<int:teacher_id>/toggle-active", methods=("POST",))
@role_required(*ADMIN_ROLES)
def toggle_teacher_active(teacher_id):
    db = get_db()
    row = db.execute("SELECT t.*, u.is_active AS user_active FROM teachers t JOIN users u ON u.id = t.user_id WHERE t.id = ? AND t.school_id = ?",
                     (teacher_id, school_id())).fetchone()
    if row:
        new_state = 0 if row["user_active"] else 1
        db.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_state, row["user_id"]))
        db.commit()
        log_action("teacher.toggle_active", "teacher", teacher_id, new_value={"is_active": new_state})
        flash("Teacher account " + ("activated." if new_state else "deactivated."), "success")
    return redirect(request.form.get("next") or url_for("admin_people.teachers_list"))


# -------------------------------------------------------------- students
@bp.route("/students", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def students_list():
    db = get_db()
    sid = school_id()
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["first_name", "surname", "admission_number", "class_id"])
        cls = owned(db, "classes", request.form.get("class_id"))
        if cls is None:
            errors.append("Choose a valid class.")
        photo = request.files.get("photo")
        has_photo = photo is not None and photo.filename
        if has_photo:
            try:   # validate first so a bad photo never leaves a half-created student
                image_service.process_logo(photo, current_app.config["MAX_LOGO_BYTES"], max_side=800)
                photo.stream.seek(0)
            except image_service.ImageRejected as exc:
                errors.append(str(exc))
            if not storage_service.status(current_app.config).configured:
                errors.append(storage_service.status(current_app.config).message)
        guardian_email = request.form.get("guardian_email", "").strip()
        if guardian_email and not is_valid_email(guardian_email):
            errors.append("The parent/guardian email is not valid.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            temp = secrets.token_urlsafe(9)
            slug = "".join(ch for ch in request.form["admission_number"].lower() if ch.isalnum()) or uuid4().hex[:6]
            email = f"{slug}@students.{_school_code(db, sid)}.local"
            try:
                cur = db.execute("INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (?, ?, ?, 'student', ?)",
                                 (sid, email, generate_password_hash(temp),
                                  f"{request.form['first_name'].strip()} {request.form['surname'].strip()}"))
                cur = db.execute(
                    """INSERT INTO students (school_id, user_id, admission_number, first_name, middle_name, surname, gender,
                           date_of_birth, phone, address, class_id, academic_session_id, quranic_level)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sid, cur.lastrowid, request.form["admission_number"].strip(), request.form["first_name"].strip(),
                     request.form.get("middle_name", "").strip() or None, request.form["surname"].strip(),
                     request.form.get("gender") if request.form.get("gender") in ("male", "female") else None,
                     request.form.get("date_of_birth") or None, request.form.get("phone", "").strip() or None,
                     request.form.get("address", "").strip() or None, cls["id"], cls["academic_session_id"],
                     request.form.get("quranic_level", "").strip() or None))
                student_new = cur.lastrowid
                if request.form.get("guardian_name", "").strip():
                    db.execute("""INSERT INTO guardians (school_id, student_id, full_name, relationship, phone, email, is_emergency_contact)
                                  VALUES (?, ?, ?, ?, ?, ?, 1)""",
                               (sid, student_new, request.form["guardian_name"].strip(), request.form.get("guardian_relationship", "").strip() or None,
                                request.form.get("guardian_phone", "").strip() or None, guardian_email or None))
                db.commit()
                log_action("student.create", "student", student_new)
                if has_photo:
                    ok, msg = _store_photo(db, "students", student_new, photo)
                    if not ok:
                        flash("Student created, but the photo was not saved: " + msg, "warning")
                flash(f"Student created. Temporary password: {temp}", "success")
            except Exception:  # noqa: BLE001
                db.rollback()
                flash("A student with that admission number already exists.", "error")
        return redirect(url_for("admin_people.students_list"))

    q = request.args.get("q", "").strip()
    class_filter = request.args.get("class_id", "")
    status_filter = request.args.get("status", "")
    page = max(_int(request.args.get("page")) or 1, 1)
    per_page = 50
    clauses, params = ["st.school_id = ?"], [sid]
    if q:
        clauses.append("(st.first_name LIKE ? OR st.surname LIKE ? OR st.admission_number LIKE ?)")
        params += [f"%{q}%"] * 3
    if class_filter:
        clauses.append("st.class_id = ?")
        params.append(class_filter)
    if status_filter:
        clauses.append("st.status = ?")
        params.append(status_filter)
    where = "WHERE " + " AND ".join(clauses)
    total = db.execute(f"SELECT COUNT(*) c FROM students st {where}", params).fetchone()["c"]
    rows = db.execute(f"""SELECT st.*, u.email, u.is_active, c.name AS class_name FROM students st
                          JOIN users u ON u.id = st.user_id LEFT JOIN classes c ON c.id = st.class_id
                          {where} ORDER BY st.surname, st.first_name LIMIT ? OFFSET ?""", params + [per_page, (page - 1) * per_page]).fetchall()
    classes = db.execute("SELECT * FROM classes WHERE school_id = ? AND is_active = 1 ORDER BY name", (sid,)).fetchall()
    return render_template("admin/students_list.html", rows=rows, classes=classes, q=q, class_filter=class_filter,
                           status_filter=status_filter, statuses=STUDENT_STATUSES, page=page, per_page=per_page, total=total)


@bp.route("/students/<int:student_id>")
@role_required(*ADMIN_ROLES)
def student_view(student_id):
    db = get_db()
    sid = school_id()
    student = db.execute("""SELECT st.*, u.email, u.is_active, c.name AS class_name FROM students st
                            JOIN users u ON u.id = st.user_id LEFT JOIN classes c ON c.id = st.class_id
                            WHERE st.id = ? AND st.school_id = ?""", (student_id, sid)).fetchone()
    if student is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin_people.students_list"))
    guardians = db.execute("SELECT * FROM guardians WHERE student_id = ? AND school_id = ?", (student_id, sid)).fetchall()
    biometric = db.execute("SELECT * FROM biometric_profiles WHERE student_id = ?", (student_id,)).fetchone()
    parents = db.execute("""SELECT u.id, u.full_name, u.email, ps.relationship FROM parent_students ps
                            JOIN users u ON u.id = ps.parent_user_id WHERE ps.student_id = ? AND ps.school_id = ?""",
                         (student_id, sid)).fetchall()
    return render_template("admin/student_view.html", student=student, guardians=guardians, biometric=biometric, parents=parents)


@bp.route("/students/<int:student_id>/status", methods=("POST",))
@role_required(*ADMIN_ROLES)
def update_student_status(student_id):
    db = get_db()
    before = owned(db, "students", student_id, "status")
    new_status = request.form.get("status")
    if before is None:
        flash("Student not found.", "error")
    elif new_status not in STUDENT_STATUSES:
        flash("Invalid status.", "error")
    else:
        db.execute("UPDATE students SET status = ?, updated_at = datetime('now') WHERE id = ? AND school_id = ?",
                   (new_status, student_id, school_id()))
        db.commit()
        log_action("student.status_change", "student", student_id, previous_value={"status": before["status"]},
                   new_value={"status": new_status})
        flash("Student status updated.", "success")
    return redirect(url_for("admin_people.student_view", student_id=student_id))


@bp.route("/students/<int:student_id>/photo", methods=("POST",))
@role_required(*ADMIN_ROLES)
def update_student_photo(student_id):
    db = get_db()
    photo = request.files.get("photo")
    if owned(db, "students", student_id, "id") is None:
        flash("Student not found.", "error")
    elif photo is None or not photo.filename:
        flash("Choose a photo first.", "error")
    else:
        ok, msg = _store_photo(db, "students", student_id, photo)
        flash(msg, "success" if ok else "error")
        if ok:
            log_action("student.photo", "student", student_id)
    return redirect(url_for("admin_people.student_view", student_id=student_id))


@bp.route("/students/<int:student_id>/guardians", methods=("POST",))
@role_required(*ADMIN_ROLES)
def add_guardian(student_id):
    db = get_db()
    errors = validate_required_fields(request.form, ["full_name"])
    if owned(db, "students", student_id) is None:
        errors.append("Student not found.")
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        db.execute("""INSERT INTO guardians (school_id, student_id, full_name, relationship, phone, email,
                          is_emergency_contact, notify_by_sms, notify_by_email) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (school_id(), student_id, request.form["full_name"].strip(), request.form.get("relationship", "").strip() or None,
                    request.form.get("phone", "").strip() or None, request.form.get("email", "").strip() or None,
                    1 if request.form.get("is_emergency_contact") else 0, 1 if request.form.get("notify_by_sms") else 0,
                    1 if request.form.get("notify_by_email") else 0))
        db.commit()
        log_action("guardian.create", "student", student_id)
        flash("Guardian added.", "success")
    return redirect(url_for("admin_people.student_view", student_id=student_id))


# --------------------------------------------------------------- parents
@bp.route("/parents", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def parents_list():
    db = get_db()
    sid = school_id()
    if request.method == "POST":
        errors = validate_required_fields(request.form, ["full_name", "email"])
        if not is_valid_email(request.form.get("email", "")):
            errors.append("A valid email is required.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            temp = secrets.token_urlsafe(9)
            try:
                cur = db.execute("INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (?, ?, ?, 'parent', ?)",
                                 (sid, request.form["email"].strip().lower(), generate_password_hash(temp), request.form["full_name"].strip()))
                db.commit()
                log_action("parent.create", "user", cur.lastrowid)
                flash(f"Parent account created. Temporary password: {temp} (share securely). Now link their children.", "success")
            except Exception:  # noqa: BLE001
                db.rollback()
                flash("That email is already registered.", "error")
        return redirect(url_for("admin_people.parents_list"))
    q = request.args.get("q", "").strip()
    where, params = "WHERE u.school_id = ? AND u.role = 'parent'", [sid]
    if q:
        where += " AND (u.full_name LIKE ? OR u.email LIKE ?)"
        params += [f"%{q}%"] * 2
    parents = db.execute(f"SELECT u.id, u.full_name, u.email, u.is_active FROM users u {where} ORDER BY u.full_name", params).fetchall()
    children = {}
    for r in db.execute("""SELECT ps.parent_user_id, st.id, st.first_name, st.surname, st.admission_number, c.name AS class_name
                           FROM parent_students ps JOIN students st ON st.id = ps.student_id LEFT JOIN classes c ON c.id = st.class_id
                           WHERE ps.school_id = ? ORDER BY st.first_name""", (sid,)).fetchall():
        children.setdefault(r["parent_user_id"], []).append(r)
    students = db.execute("SELECT id, first_name, surname, admission_number FROM students WHERE school_id = ? AND status = 'active' ORDER BY surname, first_name", (sid,)).fetchall()
    return render_template("admin/parents.html", parents=parents, children=children, students=students, q=q)


@bp.route("/parents/<int:parent_id>/link", methods=("POST",))
@role_required(*ADMIN_ROLES)
def link_child(parent_id):
    db = get_db()
    sid = school_id()
    parent = db.execute("SELECT id FROM users WHERE id = ? AND school_id = ? AND role = 'parent'", (parent_id, sid)).fetchone()
    student = owned(db, "students", request.form.get("student_id"))
    if parent is None or student is None:
        flash("Parent or student not found in this school.", "error")
    else:
        try:
            db.execute("INSERT INTO parent_students (school_id, parent_user_id, student_id, relationship) VALUES (?, ?, ?, ?)",
                       (sid, parent_id, student["id"], request.form.get("relationship", "").strip() or None))
            db.commit()
            log_action("parent.link_child", "student", student["id"], new_value={"parent_user_id": parent_id})
            flash("Child linked to parent account.", "success")
        except Exception:  # noqa: BLE001
            db.rollback()
            flash("That child is already linked to this parent.", "error")
    return redirect(url_for("admin_people.parents_list"))


@bp.route("/parents/<int:parent_id>/unlink/<int:student_id>", methods=("POST",))
@role_required(*ADMIN_ROLES)
def unlink_child(parent_id, student_id):
    db = get_db()
    db.execute("DELETE FROM parent_students WHERE parent_user_id = ? AND student_id = ? AND school_id = ?", (parent_id, student_id, school_id()))
    db.commit()
    log_action("parent.unlink_child", "student", student_id, previous_value={"parent_user_id": parent_id})
    flash("Child unlinked.", "info")
    return redirect(url_for("admin_people.parents_list"))


# ----------------------------------------------------------- bulk import
@bp.route("/students/import", methods=("GET", "POST"))
@role_required(*ADMIN_ROLES)
def students_import():
    db = get_db()
    sid = school_id()
    active_session, _ = academic_service.current_context(db, sid)
    summary = None
    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or not file.filename:
            flash("Choose a CSV file first.", "error")
        elif active_session is None:
            flash("No academic session is active. Activate one under Academic Sessions first.", "error")
        else:
            dry_run = bool(request.form.get("preview_only"))
            summary = import_service.preview_and_import(
                db, sid, _school_code(db, sid), file.read().decode("utf-8-sig", errors="replace"), g.user["id"],
                secure_filename(file.filename), active_session["id"], dry_run=dry_run)
            summary["report_token"] = credential_store.put(sid, g.user["id"], {"kind": "student-import", "summary": summary})
            if not dry_run:
                log_action("student.bulk_import", "import_batch", None, new_value={"total": summary["total_rows"], "success": summary["success_count"]})
                flash(f"Import complete: {summary['success_count']} succeeded, {summary['duplicate_count']} duplicates, "
                      f"{summary['invalid_class_count']} invalid class, {summary['missing_field_count']} missing fields.",
                      "success" if summary["success_count"] else "error")
    return render_template("admin/students_import.html", summary=summary)


@bp.route("/students/import/report/<token>.csv")
@role_required(*ADMIN_ROLES)
def students_import_report(token):
    """Downloadable result report for a student import (row errors; never contains passwords)."""
    payload = credential_store.get(token, school_id(), g.user["id"])
    if payload is None or payload.get("kind") != "student-import":
        abort(404)
    summ = payload["summary"]
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Total rows", summ["total_rows"], "Created", summ["success_count"], "Duplicates", summ["duplicate_count"],
                "Invalid class", summ["invalid_class_count"], "Missing fields", summ["missing_field_count"]])
    w.writerow(["row", "type", "detail"])
    for e in summ["errors"]:
        w.writerow([e["row"], e["type"], teacher_service.csv_cell(e["detail"])])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=student_import_report.csv"})


@bp.route("/students/import/template.csv")
@role_required(*ADMIN_ROLES)
def students_import_template():
    return Response(import_service.csv_template(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=student_import_template.csv"})
