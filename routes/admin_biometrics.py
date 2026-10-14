"""Biometric enrollment. Thin orchestration only; OpenCV logic lives in services/.
Face data is stored per school (FACE_DATA_DIR/<school_id>/) and the recognition
model is trained only on that school's students, so schools never mix."""

import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from database.db import get_db
from services import face_detection
from services.face_recognition import FaceRecognitionService
from utils.audit import log_action
from utils.decorators import ADMIN_ROLES, role_required
from utils.tenancy import owned, school_id

bp = Blueprint("admin_biometrics", __name__, url_prefix="/admin/biometrics")


def school_recognizer(app, sid):
    return FaceRecognitionService(os.path.join(app.config["FACE_DATA_DIR"], str(sid)), app.config["FACE_RECOGNITION_THRESHOLD"])


def _active_ids(db, sid):
    return [r["student_id"] for r in db.execute(
        """SELECT bp.student_id FROM biometric_profiles bp JOIN students st ON st.id = bp.student_id
           WHERE bp.is_active = 1 AND st.school_id = ?""", (sid,)).fetchall()]


@bp.route("/")
@role_required(*ADMIN_ROLES)
def enrollment_list():
    rows = get_db().execute(
        """SELECT st.id, st.admission_number, st.first_name, st.surname, bp.sample_count, bp.is_active AS profile_active
           FROM students st LEFT JOIN biometric_profiles bp ON bp.student_id = st.id
           WHERE st.status = 'active' AND st.school_id = ? ORDER BY st.surname, st.first_name""", (school_id(),)).fetchall()
    return render_template("admin/face_enrollment_list.html", students=rows)


@bp.route("/<int:student_id>")
@role_required(*ADMIN_ROLES)
def enrollment_capture(student_id):
    student = owned(get_db(), "students", student_id)
    if student is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin_biometrics.enrollment_list"))
    recognizer = school_recognizer(current_app, school_id())
    return render_template("admin/face_enrollment_capture.html", student=student,
                           current_samples=recognizer.sample_count(student_id),
                           required_samples=current_app.config["FACE_SAMPLES_REQUIRED"])


@bp.route("/<int:student_id>/capture", methods=("POST",))
@role_required(*ADMIN_ROLES)
def enrollment_submit_sample(student_id):
    if owned(get_db(), "students", student_id, "id") is None:
        return {"ok": False, "error": "Student not found."}, 404
    file = request.files.get("frame")
    if file is None or file.filename == "":
        return {"ok": False, "error": "No frame uploaded."}, 400
    try:
        face_crop = face_detection.extract_single_face(face_detection.decode_image(file.read()))
    except face_detection.NoFaceDetected:
        return {"ok": False, "error": "No face detected. Please face the camera directly."}, 200
    except face_detection.MultipleFacesDetected:
        return {"ok": False, "error": "More than one face detected. Only one person should be in frame."}, 200
    except ValueError:
        return {"ok": False, "error": "Could not read the captured frame."}, 400
    recognizer = school_recognizer(current_app, school_id())
    recognizer.save_sample(student_id, face_crop, recognizer.sample_count(student_id))
    count = recognizer.sample_count(student_id)
    required = current_app.config["FACE_SAMPLES_REQUIRED"]
    return {"ok": True, "sample_count": count, "required": required, "complete": count >= required}


@bp.route("/<int:student_id>/finish", methods=("POST",))
@role_required(*ADMIN_ROLES)
def enrollment_finish(student_id):
    db = get_db()
    sid = school_id()
    if owned(db, "students", student_id, "id") is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin_biometrics.enrollment_list"))
    recognizer = school_recognizer(current_app, sid)
    sample_count = recognizer.sample_count(student_id)
    if sample_count == 0:
        flash("No face samples were captured — cannot enroll.", "error")
        return redirect(url_for("admin_biometrics.enrollment_capture", student_id=student_id))
    if db.execute("SELECT id FROM biometric_profiles WHERE student_id = ?", (student_id,)).fetchone():
        db.execute("UPDATE biometric_profiles SET sample_count = ?, is_active = 1, updated_at = datetime('now') WHERE student_id = ?",
                   (sample_count, student_id))
    else:
        db.execute("INSERT INTO biometric_profiles (student_id, sample_count, is_active) VALUES (?, ?, 1)", (student_id, sample_count))
    db.commit()
    try:
        n_faces, n_people = recognizer.train_all(_active_ids(db, sid))
        log_action("biometric.enroll", "student", student_id, new_value={"sample_count": sample_count})
        flash(f"Face enrollment complete. Model retrained on {n_faces} samples across {n_people} students.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_people.student_view", student_id=student_id))


@bp.route("/<int:student_id>/remove", methods=("POST",))
@role_required(*ADMIN_ROLES)
def enrollment_remove(student_id):
    db = get_db()
    sid = school_id()
    if owned(db, "students", student_id, "id") is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin_biometrics.enrollment_list"))
    recognizer = school_recognizer(current_app, sid)
    recognizer.delete_student_samples(student_id)
    db.execute("DELETE FROM biometric_profiles WHERE student_id = ?", (student_id,))
    db.commit()
    log_action("biometric.remove", "student", student_id)
    ids = _active_ids(db, sid)
    if ids:
        try:
            recognizer.train_all(ids)
        except ValueError:
            pass
    flash("Face profile removed.", "success")
    return redirect(url_for("admin_people.student_view", student_id=student_id))
