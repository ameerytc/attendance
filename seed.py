"""
Development/testing seed. Creates ONE demo school with: a school admin, a platform
super admin, a teacher, three students, two parents (parent1 -> students 1+2,
parent2 -> student 3), an active 2026/2027 session with First Term active, one class,
one subject and a First-Term assignment.

DEV ONLY — never use these credentials or fake data in a real deployment.
    python seed.py
"""

from werkzeug.security import generate_password_hash

from app import create_app
from database.db import get_db, init_db
from services import academic_service, settings_service

DEV_PASSWORD = "DevPass123!"
SCHOOL = {"code": "demo", "name": "Demo Model Academy"}
ADMIN = {"email": "admin@dev.demo.local", "full_name": "Dev Admin"}
SUPER_ADMIN = {"email": "platform@dev.demo.local", "full_name": "Platform Admin"}
TEACHER = {"email": "teacher@dev.demo.local", "full_name": "Malam Musa Ibrahim", "staff_id": "DEV-STAFF-001"}
STUDENTS = [
    {"email": "student1@dev.demo.local", "first_name": "Ahmed", "surname": "Bello", "admission_number": "DEMO/2026/0001"},
    {"email": "student2@dev.demo.local", "first_name": "Fatima", "surname": "Usman", "admission_number": "DEMO/2026/0002"},
    {"email": "student3@dev.demo.local", "first_name": "Yusuf", "surname": "Garba", "admission_number": "DEMO/2026/0003"},
]
PARENT1 = {"email": "parent1@dev.demo.local", "full_name": "Hajiya Bello"}
PARENT2 = {"email": "parent2@dev.demo.local", "full_name": "Mallam Garba"}
CLASS_NAME = "JSS 2A"
SUBJECT = {"code": "MATH101", "name": "Mathematics", "category": "ACADEMIC"}
SESSION = {"name": "2026/2027", "start_date": "2026-09-01", "end_date": "2027-07-31"}


def run():
    app = create_app()
    with app.app_context():
        init_db()
        db = get_db()
        if db.execute("SELECT id FROM users WHERE email = ?", (ADMIN["email"],)).fetchone():
            print("Seed data already present — skipping.")
            return
        pw = generate_password_hash(DEV_PASSWORD)

        sid = db.execute("INSERT INTO schools (code, name) VALUES (?, ?)", (SCHOOL["code"], SCHOOL["name"])).lastrowid
        db.commit()
        settings_service.update_settings({"school_name": SCHOOL["name"], "motto": "Knowledge and character"}, sid)

        session_id = academic_service.create_session(db, sid, SESSION["name"], SESSION["start_date"], SESSION["end_date"])
        academic_service.activate_session(db, sid, session_id)
        terms = academic_service.terms_for_session(db, sid, session_id)
        academic_service.activate_term(db, sid, terms[0]["id"])

        db.execute("INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (NULL, ?, ?, 'super_admin', ?)",
                   (SUPER_ADMIN["email"], pw, SUPER_ADMIN["full_name"]))
        db.execute("INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (?, ?, ?, 'school_admin', ?)",
                   (sid, ADMIN["email"], pw, ADMIN["full_name"]))
        section_id = db.execute("INSERT INTO sections (school_id, name) VALUES (?, 'Secondary')", (sid,)).lastrowid

        t_user = db.execute("INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (?, ?, ?, 'teacher', ?)",
                            (sid, TEACHER["email"], pw, TEACHER["full_name"])).lastrowid
        teacher_id = db.execute("INSERT INTO teachers (school_id, user_id, staff_id) VALUES (?, ?, ?)", (sid, t_user, TEACHER["staff_id"])).lastrowid
        class_id = db.execute("INSERT INTO classes (school_id, name, section_id, academic_session_id, class_teacher_id) VALUES (?, ?, ?, ?, ?)",
                              (sid, CLASS_NAME, section_id, session_id, teacher_id)).lastrowid
        subject_id = db.execute("INSERT INTO subjects (school_id, code, name, category) VALUES (?, ?, ?, ?)",
                                (sid, SUBJECT["code"], SUBJECT["name"], SUBJECT["category"])).lastrowid
        assignment_id = db.execute("INSERT INTO subject_assignments (school_id, subject_id, class_id, teacher_id, term_id) VALUES (?, ?, ?, ?, ?)",
                                   (sid, subject_id, class_id, teacher_id, terms[0]["id"])).lastrowid

        student_ids = []
        for s in STUDENTS:
            u = db.execute("INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (?, ?, ?, 'student', ?)",
                           (sid, s["email"], pw, f"{s['first_name']} {s['surname']}")).lastrowid
            student_ids.append(db.execute(
                """INSERT INTO students (school_id, user_id, admission_number, first_name, surname, class_id, academic_session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""", (sid, u, s["admission_number"], s["first_name"], s["surname"], class_id, session_id)).lastrowid)

        for parent, kids in ((PARENT1, student_ids[:2]), (PARENT2, student_ids[2:])):
            p = db.execute("INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (?, ?, ?, 'parent', ?)",
                           (sid, parent["email"], pw, parent["full_name"])).lastrowid
            for k in kids:
                db.execute("INSERT INTO parent_students (school_id, parent_user_id, student_id, relationship) VALUES (?, ?, ?, 'Parent')", (sid, p, k))
        db.commit()
        print("Seeded demo school. All accounts use password:", DEV_PASSWORD)
        for label, acct in (("School admin", ADMIN), ("Super admin", SUPER_ADMIN), ("Teacher", TEACHER), ("Parent 1", PARENT1), ("Parent 2", PARENT2)):
            print(f"  {label}: {acct['email']}")
        for s in STUDENTS:
            print(f"  Student: {s['email']}")


if __name__ == "__main__":
    run()
