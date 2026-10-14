# School Management & Attendance System

Flask + SQLite (raw `sqlite3`) + vanilla HTML/CSS/JS, OpenCV face recognition, multi-school, term-aware attendance.

> **Status of the database move.** This release delivers all features on the existing SQLite stack and is fully tested.
> The SQLAlchemy / Flask-Migrate / PostgreSQL conversion is a **separate, not-yet-done step**. `init-db` is idempotent
> (`CREATE TABLE IF NOT EXISTS`) and never drops data. Until the PostgreSQL step lands, run on Railway with the SQLite file
> on a **mounted Volume** (see below) as a single instance.

## What changed in this release
* **Multi-school**: `schools` table; `school_id` on every school-owned table and on every query. Users belong to one school;
  `super_admin` (platform) has none and manages schools / switches school in the top bar.
* **Academic sessions + 3 terms each** (First/Second/Third) with statuses, admin-controlled activation, overlap/range validation,
  one active session per school and one active term per session (also enforced by partial unique indexes).
* **Attendance is pinned to school, class, academic session and term** (FKs + indexes). Attendance sessions are unique per
  assignment per school-local day (reused, never duplicated); records are unique per session+student. Changing the active term never
  touches records. Recording is refused (UI and API) unless the term *and* session are active.
* **Settings** (school info, persistent logo upload, attendance rules, current session/term, account), **Academic Sessions** page.
* **Dashboards**: admin (session/term selector, term tabs, counts, today's present/absent/%, chart, recent activity), teacher
  (My Classes, Take Attendance, History with session/term/date/class filters, Reports), **parent** portal (new role), student.
* **Reports** (HTML / PDF with logo / CSV): student, class, daily, term, full-session. A term report contains only that term.
* Percentage rule: `(present + late) / (present + late + absent)` over recorded entries; excused excluded; days without an attendance
  session are never absences. Student history also shows distinct school days recorded / present / absent / late.

## Teacher management (new)
* **Add teacher** form: names, gender, phone, email, staff number, address, class(es), optional subject, account status, optional temporary password,
  **passport photo with live preview** (validated with Pillow, re-encoded, stored via the persistent storage layer, served only to authorised users).
* **Bulk add**: several rows in a form, or **CSV upload with validate-first preview** (missing/unknown columns, bad email/gender/class, duplicate emails and
  staff numbers inside the file and against the school). Valid rows are created even if others fail (per-row savepoints); duplicates never create accounts.
* **Auto-generated** teacher ID (`T-<SCHOOLCODE>-0001`), username/login email and a random 12-character temporary password. Only hashes are stored.
* **Summary page** (submitted / created / failed / duplicates / need corrections) with: Download Teacher Credentials (CSV), Download Teacher List (CSV),
  Download error report, View All Teachers, Continue to Dashboard. Closing it loses nothing — accounts are already saved.
* **Credentials CSV** (exact columns from the brief): kept only in a short-lived in-memory store (15 min, bound to the admin + school, `no-store`), never in the
  database and never in ordinary exports. Admins can drop the passwords early or use **Reset password** later. *The store is per process — deploy one gunicorn
  worker (as configured) or swap in Redis for several.*
* **First login**: accounts with a temporary password can only reach *Create your password* (min 10 chars, mixed case + number, must differ from the temporary
  one); then the temporary password stops working. Login works with email or username.
* Students: passport photo upload, parent/guardian details on the form, and a downloadable bulk-import report.
* Spreadsheet-formula injection is neutralised in exported text cells.

## iOS-style teacher & parent screens (new)
Teacher and parent users get a mobile-app shell: large collapsing titles, translucent blurred bars, bottom tab bar, inset grouped cards, pill buttons, animated
attendance rings, spring page/list/toast animations, safe-area support, dark mode, and `prefers-reduced-motion` support (`static/css/ios.css`). Admin keeps the
desktop sidebar layout. Verified by rendering in a 390×844 mobile viewport (screenshots in the delivery), not on a physical iPhone.

## Run locally
```
pip install -r requirements.txt
cp .env.example .env            # edit; for local dev set FLASK_ENV=development and remove the S3 block
python seed.py                  # demo school + accounts (password DevPass123!) — DEV ONLY
flask --app app run --debug
python -m unittest discover -s tests -v     # 55+ tests
```

## Persistent logo storage
Logos are stored via `services/storage_service.py`, selected by env vars:
* `STORAGE_BACKEND=s3` + `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` (+ `S3_ENDPOINT_URL`, `S3_REGION` for R2/B2/Railway Bucket).
* or `STORAGE_BACKEND=local` + `LOCAL_STORAGE_DIR` pointing at a **mounted Volume**.
If neither is configured in production, uploads are **disabled** with setup instructions on the Settings page (development falls
back to a local folder and says it is not persistent). Files are verified with Pillow, re-encoded, stored under a server-generated key
(`schools/<id>/logo/<uuid>.<ext>`) and served through `/branding/logo/<school_id>`. *The S3 path uses boto3 and is untested against a
real bucket in this repo's CI; the local-volume path and the storage abstraction are tested.*

## Deploy on Railway
1. New project → deploy this repo. Add a **Volume** mounted at `/data`.
2. Variables: `FLASK_ENV=production`, `SECRET_KEY=<long random>`, `DATABASE_PATH=/data/attendance.db`, `FACE_DATA_DIR=/data/face_data`,
   `BACKUP_DIR=/data/backups`, plus the storage variables above (S3 recommended; or `STORAGE_BACKEND=local`, `LOCAL_STORAGE_DIR=/data/branding`).
3. The start command (`railway.json`/`Procfile`) runs `flask init-db` (create-if-missing only) then gunicorn (1 worker; SQLite).
4. Create the first accounts (Railway shell / `railway run`):
```
flask --app app create-super-admin --email you@org.org --name "Platform Admin"      # prompts for password (≥12 chars)
flask --app app create-school --code myschool --name "My School"
flask --app app create-admin --school myschool --email head@school.org --name "Head Admin"
```
   or log in as the super admin → **Schools** → *Add a school* (creates the school and its first admin with a temporary password).
5. Log in → Settings → upload logo, then Academic Sessions → create session → activate session → activate First Term.
Nothing in deployment drops or rewrites data. Do **not** run more than one instance while on SQLite.

## Migrating an existing (legacy) database
```
python -m scripts.migrate_legacy_db --legacy old.db --target new.db --school-code myschool --school-name "My School" --dry-run
python -m scripts.migrate_legacy_db --legacy old.db --target new.db --school-code myschool --school-name "My School"
```
Attendance is mapped to a term **only through its subject assignment's own term**; sessions without a valid term stop the migration until an
admin passes `--confirm-unmapped-term-id`. See the script's docstring for all rules (legacy `super_admin` → `school_admin`, face data folder,
logo re-upload).

## Security notes
Server-side RBAC on every route (`role_required`), school scoping via `g.school_id` + `owned()`, CSRF on all forms, password hashing, login
lockout, upload validation, secrets only from env (production refuses the default `SECRET_KEY`). `/api/v1` device endpoints authenticate by
device key and only accept events for that device's school.

## Known limitations
No SQLAlchemy/PostgreSQL yet; offline-sync conflicts and notification delivery (SMS/email) remain manual/queued as before; PDF reports omit the
Arabic school name (the built-in PDF fonts cannot shape Arabic).
