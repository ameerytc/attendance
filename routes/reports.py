"""
Reports & history. One generic pipeline: a builder in services/report_data.py returns a
report dict; this module renders it as HTML (with filters), CSV or PDF. Authorization:
  admin   -> everything in the school
  teacher -> only students/classes they teach
  parent  -> only their linked children
  student -> only themselves
"""

from datetime import date

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from database.db import get_db
from services import academic_service, report_data, report_service, settings_service, stats_service
from utils.access import class_for_user, scope_filters, student_for_user
from utils.decorators import ADMIN_ROLES, login_required, role_required
from utils.tenancy import school_id

bp = Blueprint("reports", __name__, url_prefix="/reports")
ADMINISH = ADMIN_ROLES + ("auditor",)


def _ctx():
    return academic_service.selected_context(get_db(), school_id(), request.args.get("session_id"), request.args.get("term_id"))


def _deliver(report, ctx, template_extra=None, show=None):
    """Render html / csv / pdf for a built report."""
    settings = settings_service.get_settings(school_id())
    fmt = request.args.get("format", "html")
    if fmt == "csv":
        return Response(report_service.export_csv(report, settings["school_name"]), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={report['filename']}.csv"})
    if fmt == "pdf":
        pdf = report_service.render_pdf(settings, report_service.load_logo_bytes(current_app.config, settings), report)
        return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f"attachment; filename={report['filename']}.pdf"})
    db = get_db()
    classes = []
    if show and show.get("class"):
        ids = scope_filters(db).get("class_ids")
        sql, params = "SELECT id, name FROM classes WHERE school_id = ?", [school_id()]
        if ids is not None:
            sql += " AND id IN (%s)" % ",".join("?" * max(len(ids), 1))
            params += ids or [-1]
        classes = db.execute(sql + " ORDER BY name", params).fetchall()
    base = {k: v for k, v in request.args.items() if k != "format"}
    urls = {fmt_name: url_for(request.endpoint, **{**(request.view_args or {}), **base, "format": fmt_name}) for fmt_name in ("csv", "pdf")}
    return render_template("reports/report.html", report=report, ctx=ctx, show=show or {}, classes=classes, args=request.args,
                           csv_url=urls["csv"], pdf_url=urls["pdf"], **(template_extra or {}))


@bp.route("/")
@login_required
def index():
    if g.user["role"] == "parent":
        return redirect(url_for("parent.reports"))
    if g.user["role"] == "student":
        return redirect(url_for("reports.student_report", student_id=_own_student()))
    db = get_db()
    ids = scope_filters(db).get("class_ids")
    sql, params = "SELECT id, name FROM classes WHERE school_id = ?", [school_id()]
    if ids is not None:
        sql += " AND id IN (%s)" % ",".join("?" * max(len(ids), 1))
        params += ids or [-1]
    return render_template("reports/index.html", ctx=_ctx(), classes=db.execute(sql + " ORDER BY name", params).fetchall(),
                           today=date.today().isoformat(), is_admin=g.user["role"] in ADMINISH)


def _own_student():
    from utils.access import own_student_id
    sid = own_student_id(get_db())
    if sid is None:
        abort(404)
    return sid


@bp.route("/student/<int:student_id>")
@login_required
def student_report(student_id):
    """Student attendance history + individual report (filters: session, term, dates, status)."""
    db = get_db()
    student = student_for_user(db, student_id)
    if student is None:
        abort(404)
    ctx = _ctx()
    report = report_data.build_student_report(db, school_id(), student, ctx["session"], ctx["term"],
                                              date_from=request.args.get("date_from"), date_to=request.args.get("date_to"),
                                              status=request.args.get("status"))
    return _deliver(report, ctx, {"student": student}, show={"student_history": True, "range": True, "status": True})


@bp.route("/class/<int:class_id>")
@role_required(*ADMINISH, "teacher", "attendance_operator")
def class_report(class_id):
    db = get_db()
    cls = class_for_user(db, class_id)
    if cls is None:
        abort(404)
    ctx = _ctx()
    report = report_data.build_class_report(db, school_id(), cls, ctx["session"], ctx["term"],
                                            date_from=request.args.get("date_from"), date_to=request.args.get("date_to"))
    return _deliver(report, ctx, {"class_row": cls}, show={"range": True})


@bp.route("/daily")
@role_required(*ADMINISH, "teacher", "attendance_operator")
def daily_report():
    db = get_db()
    day = request.args.get("date") or date.today().isoformat()
    cls = None
    if request.args.get("class_id"):
        cls = class_for_user(db, request.args["class_id"])
        if cls is None:
            abort(404)
    scope = scope_filters(db)
    if "class_ids" in scope and cls is None and not scope["class_ids"]:
        scope = {"class_ids": []}
    report = report_data.build_daily_report(db, school_id(), day, cls, scope=scope)
    return _deliver(report, _ctx(), show={"date": True, "class": True, "no_session": True})


def _need_session_term(db, need_term):
    ctx = _ctx()
    if ctx["session"] is None or (need_term and ctx["term"] is None):
        flash("Choose an academic session" + (" and a term" if need_term else "") + " first.", "error")
        return None
    return ctx


@bp.route("/term")
@role_required(*ADMINISH, "teacher", "attendance_operator")
def term_report():
    db = get_db()
    ctx = _need_session_term(db, True)
    if ctx is None:
        return redirect(url_for("reports.index"))
    cls = class_for_user(db, request.args.get("class_id")) if request.args.get("class_id") else None
    scope = scope_filters(db)
    if cls is None and "class_ids" in scope:
        abort(403)  # teachers must choose one of their own classes
    if request.args.get("class_id") and cls is None:
        abort(404)
    report = report_data.build_term_report(db, school_id(), ctx["session"], ctx["term"], cls, scope=scope if cls is None else None)
    return _deliver(report, ctx, show={"class": True})


@bp.route("/session")
@role_required(*ADMINISH, "teacher", "attendance_operator")
def session_report():
    db = get_db()
    ctx = _need_session_term(db, False)
    if ctx is None:
        return redirect(url_for("reports.index"))
    cls = class_for_user(db, request.args.get("class_id")) if request.args.get("class_id") else None
    scope = scope_filters(db)
    if cls is None and "class_ids" in scope:
        abort(403)
    if request.args.get("class_id") and cls is None:
        abort(404)
    report = report_data.build_session_report(db, school_id(), ctx["session"], cls, scope=None)
    return _deliver(report, ctx, show={"class": True, "no_term": True})


@bp.route("/records")
@role_required(*ADMINISH)
def records_search():
    """Searchable attendance record list with CSV export (all filters honoured)."""
    db = get_db()
    sid = school_id()
    ctx = _ctx()
    f = {}
    if ctx["session"]:
        f["session_id"] = ctx["session"]["id"]
    if ctx["term"]:
        f["term_id"] = ctx["term"]["id"]
    for key in ("class_id", "date_from", "date_to", "status"):
        if request.args.get(key):
            f[key] = request.args[key]
    rows = stats_service.records(db, sid, f, limit=None if request.args.get("format") == "csv" else 500)
    q = request.args.get("q", "").strip().lower()
    if q:
        rows = [r for r in rows if q in f"{r['first_name']} {r['surname']} {r['admission_number']}".lower()]
    if request.args.get("format") == "csv":
        settings = settings_service.get_settings(sid)
        report = {"session_name": ctx["session"]["name"] if ctx["session"] else "All", "term_name": ctx["term"]["name"] if ctx["term"] else "All terms",
                  "columns": ["Date", "Admission No.", "Student", "Class", "Subject", "Term", "Status", "Method"],
                  "rows": [[r["session_date"], r["admission_number"], f"{r['first_name']} {r['surname']}", r["class_name"], r["subject_name"],
                            r["term_name"], r["status"], r["verification_method"]] for r in rows]}
        return Response(report_service.export_csv(report, settings["school_name"]), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=attendance_records.csv"})
    classes = db.execute("SELECT id, name FROM classes WHERE school_id = ? ORDER BY name", (sid,)).fetchall()
    return render_template("reports/records.html", rows=rows, ctx=ctx, classes=classes, args=request.args,
                           export_query={k: v for k, v in request.args.items() if k != "format"})


@bp.route("/sessions")
@role_required(*ADMINISH)
def sessions_list():
    db = get_db()
    ctx = _ctx()
    clauses, params = ["sess.school_id = ?"], [school_id()]
    if ctx["session"]:
        clauses.append("sess.academic_session_id = ?")
        params.append(ctx["session"]["id"])
    if ctx["term"]:
        clauses.append("sess.academic_term_id = ?")
        params.append(ctx["term"]["id"])
    rows = db.execute(
        f"""SELECT sess.*, sub.name AS subject_name, c.name AS class_name, u.full_name AS teacher_name, t.name AS term_name,
                   (SELECT COUNT(*) FROM attendance_records ar WHERE ar.session_id = sess.id AND ar.status IN ('present','late')) AS present_count
            FROM attendance_sessions sess JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
            JOIN subjects sub ON sub.id = sa.subject_id JOIN classes c ON c.id = sess.class_id
            JOIN teachers te ON te.id = sa.teacher_id JOIN users u ON u.id = te.user_id JOIN academic_terms t ON t.id = sess.academic_term_id
            WHERE {' AND '.join(clauses)} ORDER BY sess.session_date DESC, sess.started_at DESC LIMIT 300""", params).fetchall()
    return render_template("reports/sessions_list.html", rows=rows, ctx=ctx, args=request.args)


@bp.route("/low-attendance")
@role_required(*ADMINISH)
def low_attendance_report():
    db = get_db()
    sid = school_id()
    settings = settings_service.get_settings(sid)
    ctx = _ctx()
    f = {}
    if ctx["session"]:
        f["session_id"] = ctx["session"]["id"]
    if ctx["term"]:
        f["term_id"] = ctx["term"]["id"]
    return render_template("reports/low_attendance.html", rows=stats_service.low_attendance(db, sid, f, settings["attendance_threshold_pct"]),
                           ctx=ctx, threshold=settings["attendance_threshold_pct"], args=request.args)
