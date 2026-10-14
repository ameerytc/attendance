"""Serves a school's logo from persistent storage (works for the login page, so it
is public by design: it is the school's public branding)."""

from flask import Blueprint, Response, abort, current_app

from database.db import get_db
from services import storage_service

bp = Blueprint("branding", __name__, url_prefix="/branding")
_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


@bp.route("/logo/<int:school_id>")
def logo(school_id):
    row = get_db().execute("SELECT logo_url FROM school_settings WHERE school_id = ?", (school_id,)).fetchone()
    if row is None or not row["logo_url"]:
        abort(404)
    try:
        data = storage_service.get_backend(current_app.config).read(row["logo_url"])
    except Exception:  # noqa: BLE001 - storage outage must not 500 every page
        current_app.logger.warning("logo read failed", exc_info=True)
        abort(404)
    if data is None:
        abort(404)
    ext = row["logo_url"].rsplit(".", 1)[-1].lower()
    return Response(data, mimetype=_TYPES.get(ext, "application/octet-stream"),
                    headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"})
