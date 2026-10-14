"""Parent portal. One account sees every child linked to it in parent_students — and
nothing else. Read-only: there is no route here that writes attendance."""

from flask import Blueprint, render_template, request

from database.db import get_db
from services import academic_service, stats_service
from utils.access import parent_student_ids
from utils.decorators import role_required
from utils.tenancy import school_id

bp = Blueprint("parent", __name__, url_prefix="/parent")


def _children(db):
    ids = parent_student_ids(db)
    if not ids:
        return []
    return db.execute(
        f"""SELECT st.*, c.name AS class_name FROM students st LEFT JOIN classes c ON c.id = st.class_id
            WHERE st.school_id = ? AND st.id IN ({','.join('?' * len(ids))}) ORDER BY st.first_name""", [school_id()] + ids).fetchall()


def _filters(ctx):
    f = {}
    if ctx["session"]:
        f["session_id"] = ctx["session"]["id"]
    if ctx["term"]:
        f["term_id"] = ctx["term"]["id"]
    return f


@bp.route("/dashboard")
@role_required("parent")
def dashboard():
    db = get_db()
    sid = school_id()
    ctx = academic_service.selected_context(db, sid, request.args.get("session_id"), request.args.get("term_id"))
    cards = []
    for child in _children(db):
        f = dict(_filters(ctx), student_id=child["id"])
        cards.append({"student": child, "totals": stats_service.totals(db, sid, f),
                      "days": stats_service.day_summary(db, sid, f), "recent": stats_service.records(db, sid, f, limit=5)})
    return render_template("parent/dashboard.html", cards=cards, ctx=ctx)


@bp.route("/children")
@role_required("parent")
def children():
    return render_template("parent/children.html", children=_children(get_db()),
                           ctx=academic_service.selected_context(get_db(), school_id(), None, None))


@bp.route("/attendance")
@role_required("parent")
def attendance():
    db = get_db()
    sid = school_id()
    kids = _children(db)
    ctx = academic_service.selected_context(db, sid, request.args.get("session_id"), request.args.get("term_id"))
    f = dict(_filters(ctx), student_ids=[k["id"] for k in kids])
    chosen = request.args.get("student_id", "")
    if chosen.isdigit() and int(chosen) in [k["id"] for k in kids]:
        f["student_id"] = int(chosen)
    if request.args.get("status"):
        f["status"] = request.args["status"]
    return render_template("parent/attendance.html", kids=kids, ctx=ctx, rows=stats_service.records(db, sid, f, limit=300),
                           totals=stats_service.totals(db, sid, f), args=request.args)


@bp.route("/reports")
@role_required("parent")
def reports():
    db = get_db()
    return render_template("parent/reports.html", children=_children(db), ctx=academic_service.selected_context(db, school_id(), None, None))


@bp.route("/notifications")
@role_required("parent")
def notifications():
    from flask import g
    rows = get_db().execute("""SELECT * FROM notifications WHERE school_id = ? AND recipient_type = 'user' AND recipient_id = ?
                               ORDER BY created_at DESC LIMIT 100""", (school_id(), g.user["id"])).fetchall()
    return render_template("parent/notifications.html", rows=rows)


@bp.route("/profile", methods=("GET", "POST"))
@role_required("parent")
def profile():
    from routes.teacher import profile as shared_profile
    return shared_profile()
