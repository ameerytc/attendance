"""
Application factory.

    flask --app app run --debug
CLI:
    flask --app app init-db
    flask --app app create-school --code demo --name "Demo School"
    flask --app app create-admin --school demo --email you@school.org --name "Head Admin"
    flask --app app create-super-admin --email you@org.org --name "Platform Admin"
"""

import getpass
import os

import click
from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from config import get_config
from database import db as database
from routes.auth import ROLE_DASHBOARD
from utils.csrf import csrf_protect, get_csrf_token
from utils.tenancy import resolve_school_id


def create_app():
    app = Flask(__name__)
    app.config.from_object(get_config())

    for path in (app.config["FACE_DATA_DIR"], app.config["BACKUP_DIR"]):
        os.makedirs(path, exist_ok=True)

    database.init_app(app)

    @app.before_request
    def load_logged_in_user():
        db = database.get_db()
        user_id = session.get("user_id")
        g.user, g.school_id = None, None
        if user_id is not None:
            g.user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if g.user is None or not g.user["is_active"]:
                session.clear()
                g.user = None
        if g.user is not None:
            g.school_id = resolve_school_id(db, g.user)
            if g.user["role"] != "super_admin":
                school = db.execute("SELECT status FROM schools WHERE id = ?", (g.school_id,)).fetchone()
                if school is None or school["status"] != "active":
                    session.clear()
                    g.user = g.school_id = None

    @app.before_request
    def force_password_change():
        """Accounts holding a temporary password can do nothing but set a new one (or log out)."""
        user = g.get("user")
        if user is not None and user["must_change_password"] and request.endpoint not in (
                "auth.change_password", "auth.logout", "static", "branding.logo", None):
            return redirect(url_for("auth.change_password"))

    @app.before_request
    def enforce_csrf():
        if request.path.startswith("/api/"):
            return
        if not csrf_protect():
            abort(400, description="Invalid or missing CSRF token.")

    app.jinja_env.globals["csrf_token"] = get_csrf_token

    @app.context_processor
    def inject_branding():
        """School branding for every template. On the (anonymous) login page the
        school comes from ?school=<code>, else the first active school."""
        from services import academic_service, settings_service, storage_service
        try:
            db = database.get_db()
            sid = g.get("school_id")
            if sid is None:
                code = request.args.get("school") or request.form.get("school")
                row = None
                if code:
                    row = db.execute("SELECT id FROM schools WHERE code = ? AND status = 'active'", (code,)).fetchone()
                if row is None:
                    row = db.execute("SELECT id FROM schools WHERE status = 'active' ORDER BY id LIMIT 1").fetchone()
                sid = row["id"] if row else None
            settings = settings_service.get_settings(sid) if sid else None
            logo_url = None
            if settings is not None and settings["logo_url"]:
                logo_url = url_for("branding.logo", school_id=sid, v=settings["updated_at"])
            ctx = {"school_settings": settings, "school_logo_url": logo_url, "branding_school_id": sid}
            if g.get("user") is not None and g.get("school_id"):
                s_row, t_row = academic_service.current_context(db, g.school_id)
                ctx.update(active_session=s_row, active_term=t_row)
                if g.user["role"] == "super_admin":
                    ctx["all_schools"] = db.execute("SELECT id, name FROM schools ORDER BY name").fetchall()
            return ctx
        except Exception:
            return {}

    from routes import (admin_biometrics, admin_devices, admin_ops, admin_people, api, auth, branding, media,
                        parent, reports, school_admin, student, teacher)

    for module in (auth, branding, media, school_admin, admin_people, admin_devices, admin_biometrics, admin_ops,
                   teacher, student, parent, reports, api):
        app.register_blueprint(module.bp)

    @app.route("/")
    def index():
        if g.user is None:
            return redirect(url_for("auth.login"))
        return redirect(url_for(ROLE_DASHBOARD[g.user["role"]]))

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("errors/400.html", description=getattr(e, "description", None)), 400

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(e):
        return render_template("errors/400.html", description="The uploaded file is too large."), 413

    @app.errorhandler(429)
    def too_many_requests(e):
        return render_template("errors/429.html"), 429

    @app.errorhandler(500)
    def server_error(e):
        return render_template("errors/500.html"), 500

    # ---------------------------------------------------------------- CLI
    @app.cli.command("init-db")
    def init_db_command():
        """Create tables from schema.sql (idempotent; never drops anything)."""
        with app.app_context():
            database.init_db()
        click.echo("Database initialized.")

    @app.cli.command("create-school")
    @click.option("--code", required=True)
    @click.option("--name", required=True)
    def create_school(code, name):
        from services import settings_service
        with app.app_context():
            database.init_db()
            db = database.get_db()
            cur = db.execute("INSERT INTO schools (code, name) VALUES (?, ?)", (code.strip().lower(), name.strip()))
            db.commit()
            settings_service.get_settings(cur.lastrowid)
            click.echo(f"School created (id {cur.lastrowid}, code '{code}').")

    def _prompt_password():
        pw = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password (min 12 chars): ")
        if len(pw) < 12:
            raise click.ClickException("Password must be at least 12 characters.")
        return pw

    @app.cli.command("create-admin")
    @click.option("--school", "school_code", required=True)
    @click.option("--email", required=True)
    @click.option("--name", "full_name", required=True)
    def create_admin(school_code, email, full_name):
        """Create the first administrator for a school (password from prompt or ADMIN_PASSWORD)."""
        with app.app_context():
            db = database.get_db()
            school = db.execute("SELECT id FROM schools WHERE code = ?", (school_code.lower(),)).fetchone()
            if school is None:
                raise click.ClickException("Unknown school code. Run create-school first.")
            db.execute("INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (?, ?, ?, 'school_admin', ?)",
                       (school["id"], email.strip().lower(), generate_password_hash(_prompt_password()), full_name))
            db.commit()
            click.echo("Administrator created.")

    @app.cli.command("create-super-admin")
    @click.option("--email", required=True)
    @click.option("--name", "full_name", required=True)
    def create_super_admin(email, full_name):
        with app.app_context():
            db = database.get_db()
            db.execute("INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (NULL, ?, ?, 'super_admin', ?)",
                       (email.strip().lower(), generate_password_hash(_prompt_password()), full_name))
            db.commit()
            click.echo("Super administrator created.")

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(debug=application.config.get("DEBUG", True))
