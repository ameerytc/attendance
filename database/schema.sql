-- ============================================================
-- School Management & Attendance System — schema v2
-- Multi-school, session/term-aware attendance.
-- SQLite for now; kept portable so the later SQLAlchemy/PostgreSQL
-- step maps 1:1.
-- Tenant rule: every school-owned table carries school_id and every
-- query in the application filters on it.
-- ============================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schools (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS school_settings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id               INTEGER NOT NULL UNIQUE REFERENCES schools(id) ON DELETE CASCADE,
    school_name             TEXT NOT NULL,
    school_name_arabic      TEXT,
    motto                   TEXT,
    address                 TEXT,
    phone                   TEXT,
    email                   TEXT,
    website                 TEXT,
    contact_info            TEXT,
    logo_url                TEXT,
    timezone                TEXT NOT NULL DEFAULT 'Africa/Lagos',
    attendance_threshold_pct REAL NOT NULL DEFAULT 75.0,
    late_after_minutes      INTEGER NOT NULL DEFAULT 15,
    session_default_minutes INTEGER NOT NULL DEFAULT 60,
    qr_token_ttl_seconds    INTEGER NOT NULL DEFAULT 90,
    enable_face_verification INTEGER NOT NULL DEFAULT 1 CHECK (enable_face_verification IN (0,1)),
    enable_qr_verification  INTEGER NOT NULL DEFAULT 1 CHECK (enable_qr_verification IN (0,1)),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id       INTEGER REFERENCES schools(id) ON DELETE CASCADE,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN (
                        'super_admin', 'school_admin', 'teacher', 'parent',
                        'student', 'attendance_operator', 'auditor'
                    )),
    full_name       TEXT NOT NULL,
    username        TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    password_changed_at  TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    last_login_at   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (role = 'super_admin' OR school_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username ON users(username) WHERE username IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_school ON users(school_id);

CREATE TABLE IF NOT EXISTS academic_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id   INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'upcoming' CHECK (status IN ('upcoming', 'active', 'closed', 'archived')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (school_id, name),
    CHECK (start_date < end_date)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_session
    ON academic_sessions(school_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS academic_terms (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id           INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE CASCADE,
    name                TEXT NOT NULL CHECK (name IN ('First Term', 'Second Term', 'Third Term')),
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'upcoming' CHECK (status IN ('upcoming', 'active', 'completed')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (academic_session_id, name),
    CHECK (start_date < end_date)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_term
    ON academic_terms(academic_session_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_terms_school ON academic_terms(school_id);

CREATE TABLE IF NOT EXISTS sections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id   INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    UNIQUE (school_id, name)
);

CREATE TABLE IF NOT EXISTS departments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id   INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    section_id  INTEGER REFERENCES sections(id) ON DELETE SET NULL,
    name        TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    UNIQUE (school_id, section_id, name)
);

CREATE TABLE IF NOT EXISTS teachers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id       INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    staff_id        TEXT NOT NULL,
    first_name      TEXT,
    middle_name     TEXT,
    surname         TEXT,
    gender          TEXT CHECK (gender IN ('male', 'female')),
    address         TEXT,
    department_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    phone           TEXT,
    photo_path      TEXT,                          -- storage key of the passport photo
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (school_id, staff_id)
);
CREATE INDEX IF NOT EXISTS idx_teachers_school ON teachers(school_id);

CREATE TABLE IF NOT EXISTS classes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id           INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    section_id          INTEGER REFERENCES sections(id) ON DELETE SET NULL,
    department_id       INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE CASCADE,
    class_teacher_id    INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (school_id, name, academic_session_id)
);
CREATE INDEX IF NOT EXISTS idx_classes_school ON classes(school_id);

CREATE TABLE IF NOT EXISTS subjects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id   INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL CHECK (category IN ('QURANIC', 'ACADEMIC', 'ISLAMIC_STUDIES', 'LANGUAGE', 'OTHER')),
    description TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (school_id, code)
);

CREATE TABLE IF NOT EXISTS subject_assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id   INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    class_id    INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    teacher_id  INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    term_id     INTEGER NOT NULL REFERENCES academic_terms(id) ON DELETE CASCADE,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    UNIQUE (subject_id, class_id, teacher_id, term_id)
);
CREATE INDEX IF NOT EXISTS idx_subj_assign_school ON subject_assignments(school_id);
CREATE INDEX IF NOT EXISTS idx_subj_assign_class ON subject_assignments(class_id);
CREATE INDEX IF NOT EXISTS idx_subj_assign_teacher ON subject_assignments(teacher_id);

CREATE TABLE IF NOT EXISTS students (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id           INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    user_id             INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    admission_number    TEXT NOT NULL,
    first_name          TEXT NOT NULL,
    middle_name         TEXT,
    surname             TEXT NOT NULL,
    photo_path          TEXT,
    gender              TEXT CHECK (gender IN ('male', 'female')),
    date_of_birth       TEXT,
    phone               TEXT,
    address             TEXT,
    class_id            INTEGER REFERENCES classes(id) ON DELETE SET NULL,
    academic_session_id INTEGER REFERENCES academic_sessions(id) ON DELETE SET NULL,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                            'active', 'graduated', 'suspended', 'withdrawn',
                            'transferred', 'deferred', 'inactive')),
    quranic_level       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (school_id, admission_number)
);
CREATE INDEX IF NOT EXISTS idx_students_school ON students(school_id);
CREATE INDEX IF NOT EXISTS idx_students_class ON students(class_id);
CREATE INDEX IF NOT EXISTS idx_students_status ON students(status);

CREATE TABLE IF NOT EXISTS guardians (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id       INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    student_id      INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    full_name       TEXT NOT NULL,
    relationship    TEXT,
    phone           TEXT,
    email           TEXT,
    is_emergency_contact INTEGER NOT NULL DEFAULT 0 CHECK (is_emergency_contact IN (0,1)),
    notify_by_sms   INTEGER NOT NULL DEFAULT 0 CHECK (notify_by_sms IN (0,1)),
    notify_by_email INTEGER NOT NULL DEFAULT 0 CHECK (notify_by_email IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_guardians_student ON guardians(student_id);

CREATE TABLE IF NOT EXISTS parent_students (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id       INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    parent_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    student_id      INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    relationship    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (parent_user_id, student_id)
);
CREATE INDEX IF NOT EXISTS idx_parent_students_parent ON parent_students(parent_user_id);

CREATE TABLE IF NOT EXISTS class_enrollments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id      INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    subject_assignment_id INTEGER NOT NULL REFERENCES subject_assignments(id) ON DELETE CASCADE,
    enrolled_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (student_id, subject_assignment_id)
);

CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id       INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    device_code     TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    location        TEXT,
    assigned_class_id INTEGER REFERENCES classes(id) ON DELETE SET NULL,
    api_key_hash    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'offline' CHECK (status IN ('online', 'offline')),
    software_version TEXT,
    last_seen_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Attendance rows are pinned to school, class, academic session and term.
-- session/term/class FKs are RESTRICT so history can never be cascaded away.
CREATE TABLE IF NOT EXISTS attendance_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id           INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    session_code        TEXT NOT NULL UNIQUE,
    subject_assignment_id INTEGER NOT NULL REFERENCES subject_assignments(id) ON DELETE CASCADE,
    class_id            INTEGER NOT NULL REFERENCES classes(id),
    academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id),
    academic_term_id    INTEGER NOT NULL REFERENCES academic_terms(id),
    device_id           INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    session_date        TEXT NOT NULL,
    scheduled_start     TEXT NOT NULL,
    scheduled_end       TEXT,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'closed')),
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    paused_at           TEXT,
    closed_at           TEXT,
    started_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (subject_assignment_id, session_date)
);
CREATE INDEX IF NOT EXISTS idx_att_sessions_school_term ON attendance_sessions(school_id, academic_term_id);
CREATE INDEX IF NOT EXISTS idx_att_sessions_class ON attendance_sessions(class_id);
CREATE INDEX IF NOT EXISTS idx_att_sessions_date ON attendance_sessions(session_date);
CREATE INDEX IF NOT EXISTS idx_att_sessions_status ON attendance_sessions(status);

CREATE TABLE IF NOT EXISTS attendance_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id       INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    session_id      INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
    student_id      INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    class_id        INTEGER NOT NULL REFERENCES classes(id),
    academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id),
    academic_term_id    INTEGER NOT NULL REFERENCES academic_terms(id),
    status          TEXT NOT NULL CHECK (status IN ('present', 'late', 'absent', 'excused', 'pending_verification')),
    verification_method TEXT NOT NULL DEFAULT 'manual' CHECK (verification_method IN ('face', 'qr', 'rfid', 'manual', 'offline_sync')),
    confidence      REAL,
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    marked_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    quran_juz       TEXT,
    quran_surah     TEXT,
    quran_note      TEXT,
    UNIQUE (session_id, student_id)
);
CREATE INDEX IF NOT EXISTS idx_records_school_term ON attendance_records(school_id, academic_term_id);
CREATE INDEX IF NOT EXISTS idx_records_student_term ON attendance_records(student_id, academic_term_id);
CREATE INDEX IF NOT EXISTS idx_records_session ON attendance_records(session_id);
CREATE INDEX IF NOT EXISTS idx_records_class_term ON attendance_records(class_id, academic_term_id);

CREATE TABLE IF NOT EXISTS attendance_corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL REFERENCES attendance_records(id) ON DELETE CASCADE,
    previous_status TEXT NOT NULL,
    new_status      TEXT NOT NULL,
    reason          TEXT NOT NULL,
    corrected_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    corrected_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_corrections_record ON attendance_corrections(record_id);

CREATE TABLE IF NOT EXISTS verification_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES attendance_sessions(id) ON DELETE CASCADE,
    student_id      INTEGER REFERENCES students(id) ON DELETE SET NULL,
    method          TEXT NOT NULL CHECK (method IN ('face', 'qr', 'rfid', 'manual')),
    result          TEXT NOT NULL CHECK (result IN (
                        'success', 'rejected_duplicate', 'rejected_not_enrolled',
                        'rejected_expired_token', 'rejected_no_match', 'rejected_multi_face',
                        'rejected_term_closed', 'error')),
    confidence      REAL,
    device_id       INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_verif_events_session ON verification_events(session_id);

CREATE TABLE IF NOT EXISTS qr_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
    student_id      INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    token           TEXT NOT NULL UNIQUE,
    expires_at      TEXT NOT NULL,
    used_at         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qr_tokens_session ON qr_tokens(session_id);

CREATE TABLE IF NOT EXISTS biometric_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id      INTEGER NOT NULL UNIQUE REFERENCES students(id) ON DELETE CASCADE,
    sample_count    INTEGER NOT NULL DEFAULT 0,
    model_path      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    enrolled_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS offline_sync_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id       INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    event_uid       TEXT NOT NULL UNIQUE,
    device_id       INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    session_id      INTEGER REFERENCES attendance_sessions(id) ON DELETE SET NULL,
    student_id      INTEGER REFERENCES students(id) ON DELETE SET NULL,
    payload_json    TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    received_at     TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'synced', 'rejected', 'conflict')),
    resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_events_status ON offline_sync_events(status);

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id       INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    recipient_type  TEXT NOT NULL CHECK (recipient_type IN ('user', 'guardian')),
    recipient_id    INTEGER NOT NULL,
    channel         TEXT NOT NULL CHECK (channel IN ('sms', 'email', 'whatsapp', 'in_app')),
    category        TEXT NOT NULL,
    subject         TEXT,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'failed')),
    provider        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications(recipient_type, recipient_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id       INTEGER REFERENCES schools(id) ON DELETE CASCADE,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action          TEXT NOT NULL,
    resource_type   TEXT,
    resource_id     INTEGER,
    previous_value  TEXT,
    new_value       TEXT,
    reason          TEXT,
    ip_address      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_school ON audit_logs(school_id);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);

CREATE TABLE IF NOT EXISTS import_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id       INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    imported_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    filename        TEXT,
    total_rows      INTEGER NOT NULL DEFAULT 0,
    success_count   INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    invalid_class_count INTEGER NOT NULL DEFAULT 0,
    missing_field_count INTEGER NOT NULL DEFAULT 0,
    error_report_json TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
