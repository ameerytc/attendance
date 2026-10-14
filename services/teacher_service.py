"""
Teacher registration: validation, unique ID / login / temporary-password generation, creation.

* Passwords are generated with `secrets`, hashed with Werkzeug, and the plaintext is returned to the
  caller ONCE (for the credentials download) — it is never stored.
* Every row is validated first (missing fields, bad email/gender, duplicate email / staff number /
  username both inside the file and against the school's existing accounts, unknown classes).
* Each valid row is created inside its own SAVEPOINT, so one failure never discards accounts that
  were already created.
"""

import csv
import io
import re
import secrets
import string

from werkzeug.security import generate_password_hash

from services import academic_service

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CSV_COLUMNS = ["first_name", "middle_name", "surname", "gender", "phone", "email", "staff_id", "address",
               "assigned_classes", "subject_code", "account_status"]
REQUIRED = ("first_name", "surname")
_UPPER, _LOWER, _DIGITS = "ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789"  # no look-alikes
MIN_PASSWORD = 10


def generate_password(length: int = 12) -> str:
    """Random password with upper, lower and digit characters (never starts with a symbol)."""
    while True:
        pw = "".join(secrets.choice(_UPPER + _LOWER + _DIGITS) for _ in range(length))
        if any(c in _UPPER for c in pw) and any(c in _LOWER for c in pw) and any(c in _DIGITS for c in pw):
            return pw


def csv_cell(value):
    """Neutralise spreadsheet formula injection in exported text."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def template_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    w.writerow(["Amina", "", "Bello", "female", "08012345678", "amina.bello@example.org", "", "Damaturu", "JSS 1A;JSS 1B", "MATH101", "active"])
    return buf.getvalue()


def parse_csv(text: str):
    """(rows, problems). Header names are normalised; unknown or missing required columns are reported."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["The file is empty or has no header row."]
    headers = [(h or "").strip().lower().replace(" ", "_") for h in reader.fieldnames]
    problems = []
    missing = [c for c in REQUIRED if c not in headers]
    if missing:
        problems.append("Missing required column(s): " + ", ".join(missing))
    unknown = [h for h in headers if h and h not in CSV_COLUMNS]
    if unknown:
        problems.append("Unknown column(s) ignored: " + ", ".join(unknown))
    rows = []
    for raw in reader:
        row = {(k or "").strip().lower().replace(" ", "_"): (v or "").strip() for k, v in raw.items() if k}
        rows.append({c: row.get(c, "") for c in CSV_COLUMNS})
    return rows, problems


def _class_lookup(db, school_id):
    session, _ = academic_service.current_context(db, school_id)
    sql, params = "SELECT id, name FROM classes WHERE school_id = ? AND is_active = 1", [school_id]
    if session:
        sql += " AND academic_session_id = ?"
        params.append(session["id"])
    return {r["name"].strip().lower(): r for r in db.execute(sql, params).fetchall()}


def split_classes(value):
    return [p.strip() for p in re.split(r"[;,|]", value or "") if p.strip()]


def validate_rows(db, school_id, rows, supplied_passwords=False):
    """Return a list of {row_no, data, errors, kind}; kind is ok | duplicate | invalid."""
    classes = _class_lookup(db, school_id)
    subjects = {r["code"].lower() for r in db.execute("SELECT code FROM subjects WHERE school_id = ? AND is_active = 1", (school_id,)).fetchall()}
    db_emails = {r["email"].lower() for r in db.execute("SELECT email FROM users").fetchall()}
    db_staff = {r["staff_id"].lower() for r in db.execute("SELECT staff_id FROM teachers WHERE school_id = ?", (school_id,)).fetchall()}
    seen_email, seen_staff, out = {}, {}, []
    for i, data in enumerate(rows, start=2):  # row 1 is the CSV header
        data = dict(data)
        errors, dup = [], False
        for field in REQUIRED:
            if not (data.get(field) or "").strip():
                errors.append(f"{field.replace('_', ' ').title()} is required")
        email = (data.get("email") or "").strip().lower()
        if email:
            if not EMAIL_RE.match(email):
                errors.append("Email address is not valid")
            elif email in db_emails:
                errors.append("Email already registered"); dup = True
            elif email in seen_email:
                errors.append(f"Duplicate email (same as row {seen_email[email]})"); dup = True
            else:
                seen_email[email] = i
        staff = (data.get("staff_id") or "").strip()
        if staff:
            if staff.lower() in db_staff:
                errors.append("Staff number already exists"); dup = True
            elif staff.lower() in seen_staff:
                errors.append(f"Duplicate staff number (same as row {seen_staff[staff.lower()]})"); dup = True
            else:
                seen_staff[staff.lower()] = i
        gender = (data.get("gender") or "").strip().lower()
        if gender and gender not in ("male", "female"):
            errors.append("Gender must be male or female")
        status = (data.get("account_status") or "active").strip().lower() or "active"
        if status not in ("active", "inactive"):
            errors.append("Account status must be active or inactive")
        for name in split_classes(data.get("assigned_classes")):
            if name.lower() not in classes:
                errors.append(f"Unknown class '{name}'")
        code = (data.get("subject_code") or "").strip()
        if code and code.lower() not in subjects:
            errors.append(f"Unknown subject code '{code}'")
        pw = data.get("temp_password") or ""
        if pw:
            if len(pw) < MIN_PASSWORD:
                errors.append(f"Temporary password must be at least {MIN_PASSWORD} characters")
            elif pw[0] in "=+-@":
                errors.append("Temporary password must not start with = + - or @")
        data.update(email=email, gender=gender or None, account_status=status)
        out.append({"row_no": i, "data": data, "errors": errors, "kind": "ok" if not errors else ("duplicate" if dup else "invalid")})
    return out


def _next_staff_id(db, school_id, school_code):
    prefix = f"T-{school_code.upper()}-"
    best = 0
    for r in db.execute("SELECT staff_id FROM teachers WHERE school_id = ? AND staff_id LIKE ?", (school_id, prefix + "%")).fetchall():
        tail = r["staff_id"][len(prefix):]
        if tail.isdigit():
            best = max(best, int(tail))
    while True:
        best += 1
        candidate = f"{prefix}{best:04d}"
        if not db.execute("SELECT 1 FROM teachers WHERE school_id = ? AND lower(staff_id) = lower(?)", (school_id, candidate)).fetchone():
            return candidate


def _username(db, first, surname):
    base = re.sub(r"[^a-z0-9]", "", (first or "").lower()) + "." + re.sub(r"[^a-z0-9]", "", (surname or "").lower())
    candidate, n = base, 1
    while db.execute("SELECT 1 FROM users WHERE username = ? OR email LIKE ?", (candidate, candidate + "@%")).fetchone():
        n += 1
        candidate = f"{base}{n}"
    return candidate


def create_teacher(db, school_id, school_code, data):
    """Create one teacher inside the caller's SAVEPOINT. Returns a result dict (with plaintext password)."""
    first, mid, sur = data["first_name"].strip(), (data.get("middle_name") or "").strip(), data["surname"].strip()
    username = _username(db, first, sur)
    email = data.get("email") or f"{username}@teachers.{school_code.lower()}.local"
    staff_id = (data.get("staff_id") or "").strip() or _next_staff_id(db, school_id, school_code)
    password = data.get("temp_password") or generate_password()
    full_name = " ".join(p for p in (first, mid, sur) if p)
    cur = db.execute(
        """INSERT INTO users (school_id, email, username, password_hash, role, full_name, is_active, must_change_password)
           VALUES (?, ?, ?, ?, 'teacher', ?, ?, 1)""",
        (school_id, email, username, generate_password_hash(password), full_name, 1 if data.get("account_status", "active") == "active" else 0))
    user_id = cur.lastrowid
    cur = db.execute(
        """INSERT INTO teachers (school_id, user_id, staff_id, first_name, middle_name, surname, gender, phone, address, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (school_id, user_id, staff_id, first, mid or None, sur, data.get("gender"),
         (data.get("phone") or "").strip() or None, (data.get("address") or "").strip() or None))
    teacher_id = cur.lastrowid
    classes = _class_lookup(db, school_id)
    assigned, notes = [], []
    for name in split_classes(data.get("assigned_classes")):
        cls = classes[name.lower()]
        row = db.execute("SELECT class_teacher_id FROM classes WHERE id = ?", (cls["id"],)).fetchone()
        if row["class_teacher_id"] is None:
            db.execute("UPDATE classes SET class_teacher_id = ? WHERE id = ? AND school_id = ?", (teacher_id, cls["id"], school_id))
            assigned.append(cls["name"])
        else:
            notes.append(f"{cls['name']} already has a class teacher (not changed)")
    code = (data.get("subject_code") or "").strip()
    if code:
        _, term = academic_service.current_context(db, school_id)
        subj = db.execute("SELECT id FROM subjects WHERE school_id = ? AND lower(code) = lower(?)", (school_id, code)).fetchone()
        if term is None:
            notes.append("No active term, so no subject assignment was created")
        else:
            for name in split_classes(data.get("assigned_classes")):
                db.execute("INSERT OR IGNORE INTO subject_assignments (school_id, subject_id, class_id, teacher_id, term_id) VALUES (?, ?, ?, ?, ?)",
                           (school_id, subj["id"], classes[name.lower()]["id"], teacher_id, term["id"]))
    return {"teacher_id": teacher_id, "user_id": user_id, "staff_id": staff_id, "first_name": first, "middle_name": mid,
            "surname": sur, "email": email, "username": username, "password": password, "assigned_classes": "; ".join(assigned),
            "status": "Active" if data.get("account_status", "active") == "active" else "Inactive", "notes": "; ".join(notes)}


def create_teachers(db, school_id, school_code, validated):
    """Create every valid row; failures are recorded, never raised. Returns (created, failed)."""
    created, failed = [], []
    for item in validated:
        if item["errors"]:
            failed.append({"row_no": item["row_no"], "kind": item["kind"], "errors": item["errors"], "data": item["data"]})
            continue
        db.execute("SAVEPOINT teacher_row")
        try:
            res = create_teacher(db, school_id, school_code, item["data"])
        except Exception as exc:  # noqa: BLE001 - isolate the row, keep the rest
            db.execute("ROLLBACK TO teacher_row")
            db.execute("RELEASE teacher_row")
            failed.append({"row_no": item["row_no"], "kind": "invalid", "errors": [f"Could not create account: {exc}"], "data": item["data"]})
        else:
            db.execute("RELEASE teacher_row")
            res["row_no"] = item["row_no"]
            created.append(res)
    db.commit()
    return created, failed


# ---------------------------------------------------------------- CSV exports
def credentials_csv(results) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Teacher ID", "First Name", "Middle Name", "Surname", "Email", "Username", "Temporary Password", "Assigned Classes", "Account Status"])
    for r in results:
        w.writerow([csv_cell(r["staff_id"]), csv_cell(r["first_name"]), csv_cell(r["middle_name"]), csv_cell(r["surname"]), csv_cell(r["email"]),
                    csv_cell(r["username"]), r["password"], csv_cell(r["assigned_classes"]), r["status"]])
    return buf.getvalue()


def error_report_csv(failed) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["row", "issue_type", "problems"] + CSV_COLUMNS)
    for f in failed:
        w.writerow([f["row_no"], f["kind"], " | ".join(f["errors"])] + [csv_cell(f["data"].get(c, "")) for c in CSV_COLUMNS])
    return buf.getvalue()


def teacher_list_rows(db, school_id, q="", status=""):
    clauses, params = ["t.school_id = ?"], [school_id]
    if q:
        clauses.append("(u.full_name LIKE ? OR t.staff_id LIKE ? OR u.email LIKE ? OR u.username LIKE ? OR t.phone LIKE ?)")
        params += [f"%{q}%"] * 5
    if status == "active":
        clauses.append("u.is_active = 1")
    elif status == "inactive":
        clauses.append("u.is_active = 0")
    rows = db.execute(
        f"""SELECT t.*, u.full_name, u.email, u.username, u.is_active, u.must_change_password,
                   (SELECT GROUP_CONCAT(name, '; ') FROM (SELECT DISTINCT c.name FROM classes c WHERE c.class_teacher_id = t.id AND c.school_id = t.school_id
                        UNION SELECT c.name FROM subject_assignments sa JOIN classes c ON c.id = sa.class_id WHERE sa.teacher_id = t.id ORDER BY 1)) AS class_names
            FROM teachers t JOIN users u ON u.id = t.user_id WHERE {' AND '.join(clauses)} ORDER BY u.full_name""", params).fetchall()
    return rows


def teacher_list_csv(rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Teacher ID", "First Name", "Middle Name", "Surname", "Gender", "Email", "Username", "Phone", "Address", "Assigned Classes", "Account Status"])
    for r in rows:
        w.writerow([csv_cell(r["staff_id"]), csv_cell(r["first_name"] or r["full_name"]), csv_cell(r["middle_name"]), csv_cell(r["surname"]),
                    r["gender"] or "", csv_cell(r["email"]), csv_cell(r["username"]), csv_cell(r["phone"]), csv_cell(r["address"]),
                    csv_cell(r["class_names"]), "Active" if r["is_active"] else "Inactive"])
    return buf.getvalue()
