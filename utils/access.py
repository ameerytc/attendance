"""
Who may see which student / class. Used by reports, history and parent pages.

  admin roles   every student/class in the current school
  teacher       classes they teach (subject assignments) or are class teacher of
  parent        only children linked in parent_students
  student       only themselves
All lookups also filter by school_id, so cross-school ids are simply not found.
"""

from flask import g


def teacher_row(db):
    return db.execute("SELECT * FROM teachers WHERE user_id = ? AND school_id = ?",
                      (g.user["id"], g.school_id)).fetchone()


def teacher_class_ids(db):
    t = teacher_row(db)
    if t is None:
        return []
    rows = db.execute(
        """SELECT class_id AS id FROM subject_assignments WHERE teacher_id = ? AND school_id = ?
           UNION SELECT id FROM classes WHERE class_teacher_id = ? AND school_id = ?""",
        (t["id"], g.school_id, t["id"], g.school_id)).fetchall()
    return [r["id"] for r in rows]


def parent_student_ids(db):
    return [r["student_id"] for r in db.execute(
        "SELECT student_id FROM parent_students WHERE parent_user_id = ? AND school_id = ?",
        (g.user["id"], g.school_id)).fetchall()]


def own_student_id(db):
    r = db.execute("SELECT id FROM students WHERE user_id = ? AND school_id = ?",
                   (g.user["id"], g.school_id)).fetchone()
    return r["id"] if r else None


def scope_filters(db):
    """Stats filters that confine a query to what the current user may see."""
    role = g.user["role"]
    if role in ("super_admin", "school_admin", "auditor"):
        return {}
    if role in ("teacher", "attendance_operator"):
        return {"class_ids": teacher_class_ids(db)}
    if role == "parent":
        return {"student_ids": parent_student_ids(db)}
    if role == "student":
        sid = own_student_id(db)
        return {"student_ids": [sid] if sid else []}
    return {"student_ids": []}


def student_for_user(db, student_id):
    """Student row (with class name) if the current user may view it, else None."""
    try:
        student_id = int(student_id)
    except (TypeError, ValueError):
        return None
    row = db.execute(
        """SELECT st.*, c.name AS class_name FROM students st
           LEFT JOIN classes c ON c.id = st.class_id
           WHERE st.id = ? AND st.school_id = ?""", (student_id, g.school_id)).fetchone()
    if row is None:
        return None
    role = g.user["role"]
    if role in ("super_admin", "school_admin", "auditor"):
        return row
    if role == "parent":
        return row if student_id in parent_student_ids(db) else None
    if role == "student":
        return row if student_id == own_student_id(db) else None
    if role in ("teacher", "attendance_operator"):
        return row if row["class_id"] in teacher_class_ids(db) else None
    return None


def class_for_user(db, class_id):
    try:
        class_id = int(class_id)
    except (TypeError, ValueError):
        return None
    row = db.execute("SELECT * FROM classes WHERE id = ? AND school_id = ?", (class_id, g.school_id)).fetchone()
    if row is None:
        return None
    role = g.user["role"]
    if role in ("super_admin", "school_admin", "auditor"):
        return row
    if role in ("teacher", "attendance_operator"):
        return row if class_id in teacher_class_ids(db) else None
    return None
