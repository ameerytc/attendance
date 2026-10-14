"""
Migrate a LEGACY (single-school, pre-term-aware) SQLite database into the new schema.

    python -m scripts.migrate_legacy_db --legacy old.db --target new.db \
        --school-code aia --school-name "My School" [--dry-run] [--confirm-unmapped-term-id N]

Safety rules (nothing here deletes or edits the legacy file; it is opened read-only):
  * Attendance is mapped to a term ONLY through its subject assignment's own term
    (attendance_session -> subject_assignment.term_id -> term -> session). It is never
    silently assigned to "the current term".
  * Attendance sessions whose assignment has no valid term are listed; the migration
    STOPS unless an administrator passes --confirm-unmapped-term-id to map them explicitly.
  * Several attendance sessions for the same assignment on the same day (now disallowed)
    stop the migration with a list so they can be merged by hand first.
  * Legacy terms must be named First/Second/Third Term.
  * Legacy role `super_admin` (the old school-level admin) becomes `school_admin`; create a
    platform super admin afterwards with `flask create-super-admin`.
  * Face images are not moved: copy storage/face_data/* to storage/face_data/<school_id>/.
  * The old logo file is not copied: re-upload it in Settings (persistent storage).
Runs in ONE transaction; any problem rolls everything back.
"""

import argparse
import os
import sqlite3
import sys
from datetime import date

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VALID_TERMS = ("First Term", "Second Term", "Third Term")


class MigrationError(RuntimeError):
    pass


def _cols(conn, schema, table):
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()]


def _copy(conn, table, school_id, where="", skip=()):
    """Copy common columns old.<table> -> main.<table>, adding school_id where the new table has one."""
    old, new = _cols(conn, "old", table), _cols(conn, "main", table)
    if not old:
        return 0
    common = [c for c in new if c in old and c not in skip and c != "school_id"]
    cols = ", ".join(common)
    if "school_id" in new:
        conn.execute(f"INSERT INTO main.{table} (school_id, {cols}) SELECT ?, {cols} FROM old.{table} {where}", (school_id,))
    else:
        conn.execute(f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM old.{table} {where}")
    return conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]


def migrate(legacy_path, target_path, school_code, school_name, confirm_unmapped_term_id=None, dry_run=False):
    if not os.path.exists(legacy_path):
        raise MigrationError(f"Legacy database not found: {legacy_path}")
    if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
        raise MigrationError("Target database already exists and is not empty; refusing to overwrite it.")
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        with open(os.path.join(ROOT, "database", "schema.sql"), encoding="utf-8") as fh:
            conn.executescript(fh.read())
        conn.execute("ATTACH DATABASE ? AS old", (os.path.abspath(legacy_path),))
        report = {"copied": {}, "warnings": [], "unmapped_sessions": 0}
        today = date.today().isoformat()

        # ---- pre-flight checks -------------------------------------------------
        bad_terms = [r["name"] for r in conn.execute("SELECT name FROM old.terms WHERE name NOT IN (?, ?, ?)", VALID_TERMS)]
        if bad_terms:
            raise MigrationError(f"Legacy terms must be named First/Second/Third Term. Rename these first: {sorted(set(bad_terms))}")
        dupes = conn.execute(
            """SELECT subject_assignment_id, session_date, COUNT(*) n FROM old.attendance_sessions
               GROUP BY subject_assignment_id, session_date HAVING n > 1""").fetchall()
        if dupes:
            raise MigrationError("Several attendance sessions share the same class/subject and date (now one per day). "
                                 "Merge them first: " + ", ".join(f"assignment {d[0]} on {d[1]} x{d[2]}" for d in dupes[:10]))
        unmapped = conn.execute(
            """SELECT s.id FROM old.attendance_sessions s
               LEFT JOIN old.subject_assignments sa ON sa.id = s.subject_assignment_id
               LEFT JOIN old.terms t ON t.id = sa.term_id WHERE t.id IS NULL""").fetchall()
        report["unmapped_sessions"] = len(unmapped)
        if unmapped and confirm_unmapped_term_id is None:
            raise MigrationError(f"{len(unmapped)} attendance session(s) have no valid term via their assignment "
                                 f"(ids {[u[0] for u in unmapped][:10]}). Re-run with --confirm-unmapped-term-id <term id> "
                                 "ONLY if you have confirmed which term they belong to.")
        if unmapped and not conn.execute("SELECT 1 FROM old.terms WHERE id = ?", (confirm_unmapped_term_id,)).fetchone():
            raise MigrationError("--confirm-unmapped-term-id does not match a legacy term.")

        # ---- school + settings ---------------------------------------------------
        conn.execute("INSERT INTO schools (id, code, name) VALUES (1, ?, ?)", (school_code.lower(), school_name))
        old_set = conn.execute("SELECT * FROM old.system_settings WHERE id = 1").fetchone()
        s = dict(old_set) if old_set else {}
        conn.execute(
            """INSERT INTO school_settings (school_id, school_name, school_name_arabic, motto, address, phone, email, timezone,
                   attendance_threshold_pct, late_after_minutes, session_default_minutes, qr_token_ttl_seconds,
                   enable_face_verification, enable_qr_verification)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (s.get("school_name") or school_name, s.get("school_name_arabic"), s.get("tagline"), s.get("address"), s.get("phone"),
             s.get("email"), s.get("timezone") or "Africa/Lagos", s.get("attendance_threshold_pct", 75.0),
             s.get("late_after_minutes", 15), s.get("session_default_minutes", 60), s.get("qr_token_ttl_seconds", 90),
             s.get("enable_face_verification", 1), s.get("enable_qr_verification", 1)))
        if s.get("logo_path"):
            report["warnings"].append("School logo was not migrated; upload it again in Settings so it is stored persistently.")

        # ---- users -----------------------------------------------------------------
        conn.execute(
            """INSERT INTO users (id, school_id, email, password_hash, role, full_name, is_active, failed_login_count,
                                  locked_until, last_login_at, created_at, updated_at)
               SELECT id, 1, email, password_hash, CASE role WHEN 'super_admin' THEN 'school_admin' ELSE role END,
                      full_name, is_active, failed_login_count, locked_until, last_login_at, created_at, updated_at FROM old.users""")

        # ---- sessions & terms (ids preserved) -----------------------------------
        conn.execute(
            """INSERT INTO academic_sessions (id, school_id, name, start_date, end_date, status, created_at)
               SELECT id, 1, name, start_date, end_date,
                      CASE WHEN is_active = 1 THEN 'active' WHEN end_date < ? THEN 'closed' ELSE 'upcoming' END, created_at
               FROM old.academic_sessions""", (today,))
        if conn.execute("SELECT COUNT(*) FROM old.academic_sessions WHERE is_active = 1").fetchone()[0] > 1:
            raise MigrationError("More than one legacy session is marked active; fix that in the legacy database first.")
        conn.execute(
            """INSERT INTO academic_terms (id, school_id, academic_session_id, name, start_date, end_date, status)
               SELECT id, 1, academic_session_id, name, start_date, end_date,
                      CASE WHEN is_active = 1 THEN 'active' WHEN end_date < ? THEN 'completed' ELSE 'upcoming' END FROM old.terms""", (today,))
        for sess in conn.execute("SELECT id, name FROM main.academic_sessions").fetchall():
            n = conn.execute("SELECT COUNT(*) FROM main.academic_terms WHERE academic_session_id = ?", (sess["id"],)).fetchone()[0]
            if n != 3:
                report["warnings"].append(f"Session {sess['name']} has {n} term(s), not 3. Add the missing terms in Academic Sessions.")

        # ---- plain copies ------------------------------------------------------------
        for table in ("sections", "departments", "teachers", "classes", "subjects", "subject_assignments", "students",
                      "guardians", "class_enrollments", "devices"):
            _copy(conn, table, 1)
        conn.execute("UPDATE main.students SET academic_session_id = (SELECT academic_session_id FROM main.classes c WHERE c.id = students.class_id) "
                     "WHERE academic_session_id IS NULL AND class_id IS NOT NULL")

        # ---- attendance: term via the assignment's own term ---------------------------
        override = confirm_unmapped_term_id
        conn.execute(
            """INSERT INTO attendance_sessions (id, school_id, session_code, subject_assignment_id, class_id, academic_session_id,
                   academic_term_id, device_id, session_date, scheduled_start, scheduled_end, status, started_at, paused_at,
                   closed_at, started_by_user_id)
               SELECT s.id, 1, s.session_code, s.subject_assignment_id, sa.class_id, t.academic_session_id, t.id, s.device_id,
                      s.session_date, s.scheduled_start, s.scheduled_end, s.status, s.started_at, s.paused_at, s.closed_at,
                      s.started_by_user_id
               FROM old.attendance_sessions s
               JOIN old.subject_assignments sa ON sa.id = s.subject_assignment_id
               JOIN old.terms t ON t.id = COALESCE(sa.term_id, ?)""", (override,))
        if unmapped:
            conn.execute(
                """INSERT INTO attendance_sessions (id, school_id, session_code, subject_assignment_id, class_id, academic_session_id,
                       academic_term_id, device_id, session_date, scheduled_start, scheduled_end, status, started_at, paused_at,
                       closed_at, started_by_user_id)
                   SELECT s.id, 1, s.session_code, s.subject_assignment_id, sa.class_id, t.academic_session_id, t.id, s.device_id,
                          s.session_date, s.scheduled_start, s.scheduled_end, s.status, s.started_at, s.paused_at, s.closed_at,
                          s.started_by_user_id
                   FROM old.attendance_sessions s
                   JOIN old.subject_assignments sa ON sa.id = s.subject_assignment_id
                   JOIN old.terms t ON t.id = ?
                   WHERE s.id NOT IN (SELECT id FROM main.attendance_sessions)""", (override,))
            report["warnings"].append(f"{len(unmapped)} attendance session(s) were mapped to term {override} on administrator confirmation.")
        conn.execute(
            """INSERT INTO attendance_records (id, school_id, session_id, student_id, class_id, academic_session_id, academic_term_id,
                   status, verification_method, confidence, recorded_at, marked_by_user_id, quran_juz, quran_surah, quran_note)
               SELECT r.id, 1, r.session_id, r.student_id, ns.class_id, ns.academic_session_id, ns.academic_term_id, r.status,
                      r.verification_method, r.confidence, r.recorded_at, r.marked_by_user_id, r.quran_juz, r.quran_surah, r.quran_note
               FROM old.attendance_records r JOIN main.attendance_sessions ns ON ns.id = r.session_id""")
        lost = conn.execute("SELECT COUNT(*) FROM old.attendance_records").fetchone()[0] - conn.execute("SELECT COUNT(*) FROM main.attendance_records").fetchone()[0]
        if lost:
            raise MigrationError(f"{lost} attendance record(s) could not be mapped to a session; aborting (nothing was written).")

        for table in ("attendance_corrections", "verification_events", "qr_tokens", "biometric_profiles", "offline_sync_events",
                      "notifications", "audit_logs", "import_batches"):
            _copy(conn, table, 1)

        conn.execute("PRAGMA foreign_keys = ON")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise MigrationError(f"Foreign key check failed for {len(problems)} row(s): {[tuple(p) for p in problems[:5]]}")
        for table in ("users", "students", "teachers", "classes", "academic_sessions", "academic_terms", "attendance_sessions", "attendance_records"):
            report["copied"][table] = conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
        if dry_run:
            conn.rollback()
            report["dry_run"] = True
        else:
            conn.commit()
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if dry_run and os.path.exists(target_path):
            os.remove(target_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--legacy", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--school-code", required=True)
    ap.add_argument("--school-name", required=True)
    ap.add_argument("--confirm-unmapped-term-id", type=int)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        rep = migrate(a.legacy, a.target, a.school_code, a.school_name, a.confirm_unmapped_term_id, a.dry_run)
    except MigrationError as exc:
        print("MIGRATION STOPPED:", exc, file=sys.stderr)
        sys.exit(1)
    print("Dry run only — nothing written." if rep.get("dry_run") else "Migration complete.")
    for k, v in rep["copied"].items():
        print(f"  {k}: {v}")
    for w in rep["warnings"]:
        print("  WARNING:", w)


if __name__ == "__main__":
    main()
