"""Authenticated person photos (teacher / student passports). Never public: every request is
checked against the same visibility rules as the underlying records."""

from flask import Blueprint, Response, abort, current_app

from database.db import get_db
from services import storage_service
from utils.access import student_for_user
from utils.decorators import login_required
from utils.tenancy import school_id

bp = Blueprint("media", __name__, url_prefix="/media")
_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def _send(key):
    try:
        data = storage_service.get_backend(current_app.config).read(key)
    except Exception:  # noqa: BLE001
        abort(404)
    if data is None:
        abort(404)
    return Response(data, mimetype=_TYPES.get(key.rsplit(".", 1)[-1].lower(), "application/octet-stream"),
                    headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@bp.route("/teacher/<int:teacher_id>")
@login_required
def teacher_photo(teacher_id):
    from flask import g
    row = get_db().execute("SELECT photo_path, user_id FROM teachers WHERE id = ? AND school_id = ?", (teacher_id, school_id())).fetchone()
    if row is None or not row["photo_path"]:
        abort(404)
    role = g.user["role"]
    if role in ("super_admin", "school_admin", "auditor") or g.user["id"] == row["user_id"]:
        return _send(row["photo_path"])
    abort(403)


@bp.route("/student/<int:student_id>")
@login_required
def student_photo(student_id):
    student = student_for_user(get_db(), student_id)      # same visibility as the student record itself
    if student is None or not student["photo_path"]:
        abort(404)
    return _send(student["photo_path"])
