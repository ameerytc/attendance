"""Route protection. Authorization is always enforced on the server; hiding a
link in a template is never the only control."""

from functools import wraps

from flask import abort, flash, g, redirect, url_for

ADMIN_ROLES = ("super_admin", "school_admin")
TEACHER_ROLES = ("teacher", "attendance_operator")
STAFF_ROLES = ADMIN_ROLES + TEACHER_ROLES + ("auditor",)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = g.get("user")
            if user is None:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("auth.login"))
            if user["role"] not in roles:
                abort(403)
            if g.get("school_id") is None:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator
