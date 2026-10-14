"""
Attendance statistics. ONE place defines the filters and the percentage rule so
dashboards, history pages, reports and exports always agree.

Percentage rule (documented in README):
    pct = (present + late) / (present + late + absent) * 100
over *recorded* attendance entries. Excused and pending-verification entries are
excluded; days/sessions with no attendance session are never counted as absences.

Filters (all optional, always combined with the mandatory school_id):
    session_id, term_id, class_id, class_ids, student_id, student_ids,
    date, date_from, date_to, status
"""

BASE_FROM = """
    FROM attendance_records ar
    JOIN attendance_sessions sess ON sess.id = ar.session_id
    JOIN students st ON st.id = ar.student_id
    JOIN classes c ON c.id = ar.class_id
    JOIN subject_assignments sa ON sa.id = sess.subject_assignment_id
    JOIN subjects sub ON sub.id = sa.subject_id
    JOIN academic_terms t ON t.id = ar.academic_term_id
    JOIN academic_sessions s ON s.id = ar.academic_session_id
"""

_COUNTS = """
    SUM(CASE WHEN ar.status = 'present' THEN 1 ELSE 0 END) AS present,
    SUM(CASE WHEN ar.status = 'late' THEN 1 ELSE 0 END) AS late,
    SUM(CASE WHEN ar.status = 'absent' THEN 1 ELSE 0 END) AS absent,
    SUM(CASE WHEN ar.status = 'excused' THEN 1 ELSE 0 END) AS excused,
    COUNT(*) AS total
"""


def pct(present, late, absent):
    counted = (present or 0) + (late or 0) + (absent or 0)
    if counted == 0:
        return None
    return round(100.0 * ((present or 0) + (late or 0)) / counted, 1)


def _where(school_id, f):
    clauses, params = ["ar.school_id = ?"], [school_id]
    f = f or {}
    simple = (("session_id", "ar.academic_session_id = ?"), ("term_id", "ar.academic_term_id = ?"),
              ("class_id", "ar.class_id = ?"), ("student_id", "ar.student_id = ?"),
              ("date", "sess.session_date = ?"), ("date_from", "sess.session_date >= ?"),
              ("date_to", "sess.session_date <= ?"), ("status", "ar.status = ?"))
    for key, sql in simple:
        if f.get(key) not in (None, ""):
            clauses.append(sql)
            params.append(f[key])
    for key, col in (("class_ids", "ar.class_id"), ("student_ids", "ar.student_id")):
        if f.get(key) is not None:
            ids = list(f[key])
            if not ids:
                clauses.append("1 = 0")
            else:
                clauses.append(f"{col} IN ({','.join('?' * len(ids))})")
                params.extend(ids)
    return " WHERE " + " AND ".join(clauses), params


def _rows(db, sql, params):
    return [dict(r) for r in db.execute(sql, params).fetchall()]


def count(db, school_id, f=None) -> int:
    where, params = _where(school_id, f)
    return db.execute(f"SELECT COUNT(*) c {BASE_FROM} {where}", params).fetchone()["c"]


def records(db, school_id, f=None, limit=None, offset=0):
    where, params = _where(school_id, f)
    sql = f"""SELECT ar.id, ar.status, ar.verification_method, ar.recorded_at, ar.student_id,
                     sess.session_date, st.admission_number, st.first_name, st.surname,
                     c.name AS class_name, sub.name AS subject_name,
                     t.name AS term_name, s.name AS session_name,
                     (SELECT u.full_name FROM users u WHERE u.id = ar.marked_by_user_id) AS recorded_by
              {BASE_FROM} {where}
              ORDER BY sess.session_date DESC, ar.recorded_at DESC, ar.id DESC"""
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
    return _rows(db, sql, params)


def totals(db, school_id, f=None) -> dict:
    where, params = _where(school_id, f)
    row = db.execute(f"SELECT {_COUNTS} {BASE_FROM} {where}", params).fetchone()
    out = {k: (row[k] or 0) for k in ("present", "late", "absent", "excused", "total")}
    out["pct"] = pct(out["present"], out["late"], out["absent"])
    return out


def _grouped(db, school_id, f, select_cols, group_by, order_by):
    where, params = _where(school_id, f)
    rows = _rows(db, f"SELECT {select_cols}, {_COUNTS} {BASE_FROM} {where} GROUP BY {group_by} ORDER BY {order_by}", params)
    for r in rows:
        r["pct"] = pct(r["present"], r["late"], r["absent"])
    return rows


def per_student(db, school_id, f=None):
    return _grouped(db, school_id, f,
                    "st.id AS student_id, st.admission_number, st.first_name || ' ' || st.surname AS name, "
                    "c.name AS class_name",
                    "st.id, c.name", "name")


def per_class(db, school_id, f=None):
    return _grouped(db, school_id, f, "c.id AS class_id, c.name AS class_name", "c.id", "c.name")


def per_term(db, school_id, f=None):
    return _grouped(db, school_id, f, "t.id AS term_id, t.name AS term_name, t.start_date",
                    "t.id", "t.start_date")


def per_subject(db, school_id, f=None):
    return _grouped(db, school_id, f, "sub.id AS subject_id, sub.name AS subject_name, sub.category",
                    "sub.id", "sub.name")


def daily_series(db, school_id, f=None, limit=14):
    """Last `limit` recorded dates (ascending) with per-status counts."""
    rows = _grouped(db, school_id, f, "sess.session_date AS day", "sess.session_date", "sess.session_date DESC")
    return list(reversed(rows[:limit]))


def day_summary(db, school_id, f=None) -> dict:
    """Day-level view for one student/filter: distinct recorded dates and how many of
    them the student attended at least once / was absent for / was late for."""
    where, params = _where(school_id, f)
    rows = db.execute(f"SELECT sess.session_date AS d, ar.status AS s {BASE_FROM} {where}", params).fetchall()
    days = {}
    for r in rows:
        days.setdefault(r["d"], set()).add(r["s"])
    present = sum(1 for v in days.values() if "present" in v)
    late = sum(1 for v in days.values() if "late" in v and "present" not in v)
    attended = sum(1 for v in days.values() if v & {"present", "late"})
    absent = sum(1 for v in days.values() if "absent" in v and not (v & {"present", "late"}))
    return {"days_recorded": len(days), "days_present": present, "days_late": late,
            "days_attended": attended, "days_absent": absent}


def today_distinct(db, school_id, today, f=None) -> dict:
    """Distinct students present/late/absent on one school-local date."""
    f = dict(f or {})
    f["date"] = today
    where, params = _where(school_id, f)
    rows = db.execute(f"SELECT ar.student_id AS sid, ar.status AS s {BASE_FROM} {where}", params).fetchall()
    by_student = {}
    for r in rows:
        by_student.setdefault(r["sid"], set()).add(r["s"])
    attended = {sid for sid, v in by_student.items() if v & {"present", "late"}}
    late_only = {sid for sid in attended if "present" not in by_student[sid]}
    absent = {sid for sid, v in by_student.items() if sid not in attended and "absent" in v}
    marked = len(attended) + len(absent)
    return {"present": len(attended), "late": len(late_only), "absent": len(absent),
            "pct": round(100.0 * len(attended) / marked, 1) if marked else None, "records": len(rows)}


def recent_activity(db, school_id, f=None, limit=10):
    return records(db, school_id, f, limit=limit)


def low_attendance(db, school_id, f, threshold, limit=None):
    rows = [r for r in per_student(db, school_id, f) if r["pct"] is not None and r["pct"] < threshold]
    rows.sort(key=lambda r: r["pct"])
    return rows[:limit] if limit else rows
