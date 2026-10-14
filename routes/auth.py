"""Authentication: login/logout with lockout and audit trail."""

from datetime import datetime, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database.db import get_db
from utils.audit import log_action
from utils.decorators import login_required
from utils.validators import is_non_empty

bp = Blueprint("auth", __name__, url_prefix="/auth")

ROLE_DASHBOARD = {
    "super_admin": "school_admin.dashboard",
    "school_admin": "school_admin.dashboard",
    "teacher": "teacher.dashboard",
    "attendance_operator": "teacher.dashboard",
    "student": "student.dashboard",
    "parent": "parent.dashboard",
    "auditor": "school_admin.audit_log",
}

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.get("user") is not None:
        return redirect(url_for(ROLE_DASHBOARD[g.user["role"]]))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        error, user = None, None
        db = get_db()
        if not is_non_empty(email) or not is_non_empty(password):
            error = "Email and password are required."
        else:
            user = db.execute("SELECT * FROM users WHERE email = ? OR username = ?", (email, email)).fetchone()
            if user is not None and user["locked_until"] and datetime.fromisoformat(user["locked_until"]) > datetime.now():
                error = "This account is temporarily locked due to repeated failed logins. Try again later."
            if error is None:
                if user is None or not check_password_hash(user["password_hash"], password):
                    error = "Incorrect email or password."
                    if user is not None:
                        new_count = user["failed_login_count"] + 1
                        locked_until = None
                        if new_count >= MAX_FAILED_ATTEMPTS:
                            locked_until = (datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="seconds")
                            error = f"Too many failed attempts. Account locked for {LOCKOUT_MINUTES} minutes."
                        db.execute("UPDATE users SET failed_login_count = ?, locked_until = ? WHERE id = ?",
                                   (new_count, locked_until, user["id"]))
                        db.commit()
                    log_action("auth.login_failed", "user", user["id"] if user else None, reason=f"email={email}",
                               school_id=user["school_id"] if user else None)
                elif not user["is_active"]:
                    error = "This account has been deactivated. Contact an administrator."
                elif user["school_id"]:
                    school = db.execute("SELECT status FROM schools WHERE id = ?", (user["school_id"],)).fetchone()
                    if school is None or school["status"] != "active":
                        error = "This school account is not active. Contact the platform administrator."

        if error is None:
            db.execute("UPDATE users SET failed_login_count = 0, locked_until = NULL, last_login_at = datetime('now') WHERE id = ?",
                       (user["id"],))
            db.commit()
            session.clear()
            session["user_id"] = user["id"]
            g.user = user
            log_action("auth.login_success", "user", user["id"], school_id=user["school_id"])
            if user["must_change_password"]:
                flash("Welcome! Please choose your own password to continue.", "info")
                return redirect(url_for("auth.change_password"))
            flash(f"Welcome back, {user['full_name']}.", "success")
            return redirect(url_for(ROLE_DASHBOARD[user["role"]]))
        flash(error, "error")

    return render_template("auth/login.html")


@bp.route("/logout")
def logout():
    if g.get("user") is not None:
        log_action("auth.logout", "user", g.user["id"])
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/change-password", methods=("GET", "POST"))
@login_required
def change_password():
    """First-login flow (and voluntary changes): the temporary password is replaced and expires."""
    forced = bool(g.user["must_change_password"])
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        errors = []
        if not check_password_hash(g.user["password_hash"], current):
            errors.append("Your current (temporary) password is incorrect.")
        if len(new) < 10:
            errors.append("The new password must be at least 10 characters.")
        if not (any(c.islower() for c in new) and any(c.isupper() for c in new) and any(c.isdigit() for c in new)):
            errors.append("Use upper-case, lower-case and a number in the new password.")
        if check_password_hash(g.user["password_hash"], new):
            errors.append("Choose a password different from the temporary one.")
        if new != confirm:
            errors.append("The new password and confirmation do not match.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            db = get_db()
            db.execute("""UPDATE users SET password_hash = ?, must_change_password = 0, password_changed_at = datetime('now'),
                          updated_at = datetime('now') WHERE id = ?""", (generate_password_hash(new), g.user["id"]))
            db.commit()
            log_action("auth.password_changed", "user", g.user["id"], reason="first login" if forced else "voluntary")
            flash("Password updated. You're all set.", "success")
            return redirect(url_for(ROLE_DASHBOARD[g.user["role"]]))
    return render_template("auth/change_password.html", forced=forced)
