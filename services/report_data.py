"""
Report builders. Each returns one plain dict that the HTML view, the CSV export
and the PDF renderer all consume, so the three formats can never disagree:

    {key, title, filename, session_name, term_name,
     meta: [(label, value)], summary: [(label, value)], columns: [...], rows: [[...]]}

Term separation: a term/student/class/daily report is filtered by the chosen
term id. Only build_session_report() spans terms, and it lists them separately.
"""

from datetime import date

from services import stats_service as stats
from services.stats_service import pct


def _pct(v):
    return "—" if v is None else f"{v}%"


def _name(student):
    return " ".join(p for p in (student["first_name"], student["middle_name"] if "middle_name" in student.keys() else None, student["surname"]) if p)


def _summary(tot):
    return [("Present", tot["present"]), ("Late", tot["late"]), ("Absent", tot["absent"]),
            ("Excused", tot["excused"]), ("Entries recorded", tot["total"]), ("Attendance %", _pct(tot["pct"]))]


def _slug(text):
    return "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")


def _filters(scope, **extra):
    f = dict(scope or {})
    f.update({k: v for k, v in extra.items() if v not in (None, "")})
    return f


def build_student_report(db, school_id, student, session, term, scope=None, date_from=None, date_to=None, status=None):
    f = _filters(scope, student_id=student["id"], session_id=session["id"] if session else None,
                 term_id=term["id"] if term else None, date_from=date_from, date_to=date_to, status=status)
    tot = stats.totals(db, school_id, f)
    days = stats.day_summary(db, school_id, f)
    rows = stats.records(db, school_id, f)
    summary = _summary(tot) + [("School days recorded", days["days_recorded"]), ("Days present", days["days_attended"]),
                               ("Days absent", days["days_absent"]), ("Days late (only)", days["days_late"])]
    sname = session["name"] if session else "All sessions"
    tname = term["name"] if term else "All terms"
    full = f"{student['first_name']} {student['surname']}"
    return {
        "key": "student", "title": "Student Attendance Report", "session_name": sname, "term_name": tname,
        "filename": f"student_{_slug(student['admission_number'])}_{_slug(sname)}_{_slug(tname)}",
        "meta": [("Student", full), ("Admission No.", student["admission_number"]),
                 ("Class", student["class_name"] if "class_name" in student.keys() and student["class_name"] else "—"),
                 ("Academic Session", sname), ("Term", tname), ("Report date", date.today().isoformat())],
        "summary": summary,
        "columns": ["Date", "Class", "Subject", "Term", "Status", "Method", "Recorded by"],
        "rows": [[r["session_date"], r["class_name"], r["subject_name"], r["term_name"], r["status"].title(),
                  r["verification_method"], r["recorded_by"] or ""] for r in rows],
    }


def build_class_report(db, school_id, class_row, session, term, scope=None, date_from=None, date_to=None):
    f = _filters(scope, class_id=class_row["id"], session_id=session["id"] if session else None,
                 term_id=term["id"] if term else None, date_from=date_from, date_to=date_to)
    tot = stats.totals(db, school_id, f)
    rows = stats.per_student(db, school_id, f)
    sname = session["name"] if session else "All sessions"
    tname = term["name"] if term else "All terms"
    return {
        "key": "class", "title": "Class Attendance Report", "session_name": sname, "term_name": tname,
        "filename": f"class_{_slug(class_row['name'])}_{_slug(sname)}_{_slug(tname)}",
        "meta": [("Class", class_row["name"]), ("Academic Session", sname), ("Term", tname),
                 ("Report date", date.today().isoformat())],
        "summary": _summary(tot),
        "columns": ["Admission No.", "Student", "Present", "Late", "Absent", "Excused", "Attendance %"],
        "rows": [[r["admission_number"], r["name"], r["present"], r["late"], r["absent"], r["excused"],
                  _pct(r["pct"])] for r in rows],
    }


def build_daily_report(db, school_id, day, class_row=None, scope=None):
    f = _filters(scope, date=day, class_id=class_row["id"] if class_row else None)
    tot = stats.totals(db, school_id, f)
    rows = stats.records(db, school_id, f)
    sessions = sorted({r["session_name"] for r in rows})
    terms = sorted({r["term_name"] for r in rows})
    sname = ", ".join(sessions) or "—"
    tname = ", ".join(terms) or "—"
    return {
        "key": "daily", "title": "Daily Attendance Report", "session_name": sname, "term_name": tname,
        "filename": f"daily_{day}" + (f"_{_slug(class_row['name'])}" if class_row else ""),
        "meta": [("Date", day), ("Class", class_row["name"] if class_row else "All classes"),
                 ("Academic Session", sname), ("Term", tname), ("Report date", date.today().isoformat())],
        "summary": _summary(tot),
        "columns": ["Class", "Admission No.", "Student", "Subject", "Status", "Method", "Recorded by"],
        "rows": [[r["class_name"], r["admission_number"], f"{r['first_name']} {r['surname']}", r["subject_name"],
                  r["status"].title(), r["verification_method"], r["recorded_by"] or ""] for r in rows],
    }


def build_term_report(db, school_id, session, term, class_row=None, scope=None):
    f = _filters(scope, session_id=session["id"], term_id=term["id"], class_id=class_row["id"] if class_row else None)
    tot = stats.totals(db, school_id, f)
    if class_row:
        columns = ["Admission No.", "Student", "Present", "Late", "Absent", "Excused", "Attendance %"]
        rows = [[r["admission_number"], r["name"], r["present"], r["late"], r["absent"], r["excused"], _pct(r["pct"])]
                for r in stats.per_student(db, school_id, f)]
    else:
        columns = ["Class", "Present", "Late", "Absent", "Excused", "Attendance %"]
        rows = [[r["class_name"], r["present"], r["late"], r["absent"], r["excused"], _pct(r["pct"])]
                for r in stats.per_class(db, school_id, f)]
    return {
        "key": "term", "title": "Term Attendance Report", "session_name": session["name"], "term_name": term["name"],
        "filename": f"term_{_slug(session['name'])}_{_slug(term['name'])}" + (f"_{_slug(class_row['name'])}" if class_row else ""),
        "meta": [("Academic Session", session["name"]), ("Term", term["name"]),
                 ("Term dates", f"{term['start_date']} to {term['end_date']}"),
                 ("Class", class_row["name"] if class_row else "All classes"),
                 ("Report date", date.today().isoformat())],
        "summary": _summary(tot), "columns": columns, "rows": rows,
    }


def build_session_report(db, school_id, session, class_row=None, scope=None):
    """Full academic-session report: each term listed separately plus a session total."""
    f = _filters(scope, session_id=session["id"], class_id=class_row["id"] if class_row else None)
    tot = stats.totals(db, school_id, f)
    term_rows = {r["term_id"]: r for r in stats.per_term(db, school_id, f)}
    terms = db.execute("SELECT * FROM academic_terms WHERE school_id = ? AND academic_session_id = ? ORDER BY start_date",
                       (school_id, session["id"])).fetchall()
    rows = []
    for t in terms:
        r = term_rows.get(t["id"])
        rows.append([t["name"], f"{t['start_date']} to {t['end_date']}",
                     r["present"] if r else 0, r["late"] if r else 0, r["absent"] if r else 0,
                     r["excused"] if r else 0, _pct(r["pct"]) if r else "—"])
    rows.append(["Whole session", f"{session['start_date']} to {session['end_date']}", tot["present"], tot["late"],
                 tot["absent"], tot["excused"], _pct(tot["pct"])])
    return {
        "key": "session", "title": "Academic Session Attendance Report", "session_name": session["name"],
        "term_name": "All terms",
        "filename": f"session_{_slug(session['name'])}" + (f"_{_slug(class_row['name'])}" if class_row else ""),
        "meta": [("Academic Session", session["name"]), ("Term", "All terms (listed separately)"),
                 ("Class", class_row["name"] if class_row else "All classes"),
                 ("Report date", date.today().isoformat())],
        "summary": _summary(tot),
        "columns": ["Term", "Dates", "Present", "Late", "Absent", "Excused", "Attendance %"], "rows": rows,
    }
