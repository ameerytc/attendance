"""Bulk student import (school-scoped). Each row is validated before insert and
inserted inside a SAVEPOINT so one bad row never poisons the batch."""

import csv
import io
import json
import secrets

from werkzeug.security import generate_password_hash

REQUIRED_FIELDS = ["admission_number", "first_name", "surname", "class_name"]
TEMPLATE_HEADERS = ["admission_number", "first_name", "middle_name", "surname", "gender",
                    "date_of_birth", "phone", "address", "class_name"]


def csv_template() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow(["AIA/2026/0001", "Amina", "Bello", "Yusuf", "female",
                     "2013-04-12", "08012345678", "Damaturu", "JSS 1A"])
    return buf.getvalue()


def _dev_email(school_code: str, admission_number: str) -> str:
    slug = "".join(ch for ch in admission_number.lower() if ch.isalnum())
    return f"{slug}@students.{school_code.lower()}.local"


def preview_and_import(db, school_id: int, school_code: str, csv_text: str, imported_by_user_id: int,
                       filename: str, academic_session_id: int, dry_run: bool = False) -> dict:
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    class_lookup = {r["name"].strip().lower(): r["id"] for r in db.execute(
        "SELECT id, name FROM classes WHERE school_id = ? AND academic_session_id = ?",
        (school_id, academic_session_id)).fetchall()}
    existing = {r["admission_number"] for r in db.execute(
        "SELECT admission_number FROM students WHERE school_id = ?", (school_id,)).fetchall()}

    seen, errors, to_insert = set(), [], []
    for i, row in enumerate(rows, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        missing = [f for f in REQUIRED_FIELDS if not row.get(f)]
        if missing:
            errors.append({"row": i, "type": "missing_field", "detail": f"Missing: {', '.join(missing)}"})
            continue
        adm = row["admission_number"]
        if adm in existing or adm in seen:
            errors.append({"row": i, "type": "duplicate", "detail": f"Duplicate admission number {adm}"})
            continue
        class_id = class_lookup.get(row["class_name"].lower())
        if class_id is None:
            errors.append({"row": i, "type": "invalid_class", "detail": f"Unknown class '{row['class_name']}'"})
            continue
        gender = row.get("gender", "").lower() or None
        if gender not in (None, "male", "female"):
            errors.append({"row": i, "type": "error", "detail": f"Invalid gender '{row.get('gender')}'"})
            continue
        seen.add(adm)
        to_insert.append({**row, "class_id": class_id, "gender": gender})

    summary = {"total_rows": len(rows), "success_count": 0, "errors": errors, "dev_password": None,
               "duplicate_count": sum(1 for e in errors if e["type"] == "duplicate"),
               "invalid_class_count": sum(1 for e in errors if e["type"] == "invalid_class"),
               "missing_field_count": sum(1 for e in errors if e["type"] == "missing_field")}
    if dry_run:
        summary["success_count"] = len(to_insert)
        return summary

    password = secrets.token_urlsafe(9)
    pw_hash = generate_password_hash(password)
    inserted = 0
    for rec in to_insert:
        db.execute("SAVEPOINT import_row")
        try:
            cur = db.execute(
                "INSERT INTO users (school_id, email, password_hash, role, full_name) VALUES (?, ?, ?, 'student', ?)",
                (school_id, _dev_email(school_code, rec["admission_number"]), pw_hash,
                 f"{rec['first_name']} {rec['surname']}"))
            db.execute(
                """INSERT INTO students (school_id, user_id, admission_number, first_name, middle_name, surname,
                                         gender, date_of_birth, phone, address, class_id, academic_session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (school_id, cur.lastrowid, rec["admission_number"], rec["first_name"],
                 rec.get("middle_name") or None, rec["surname"], rec["gender"],
                 rec.get("date_of_birth") or None, rec.get("phone") or None, rec.get("address") or None,
                 rec["class_id"], academic_session_id))
        except Exception as exc:  # noqa: BLE001 - row isolation
            db.execute("ROLLBACK TO import_row")
            db.execute("RELEASE import_row")
            errors.append({"row": "?", "type": "error", "detail": str(exc)})
        else:
            db.execute("RELEASE import_row")
            inserted += 1
    summary["success_count"] = inserted
    summary["dev_password"] = password if inserted else None
    db.execute(
        """INSERT INTO import_batches (school_id, imported_by_user_id, filename, total_rows, success_count,
                duplicate_count, invalid_class_count, missing_field_count, error_report_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (school_id, imported_by_user_id, filename, summary["total_rows"], inserted, summary["duplicate_count"],
         summary["invalid_class_count"], summary["missing_field_count"], json.dumps(errors)))
    db.commit()
    return summary
