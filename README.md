# Gradely Backend API

Django REST Framework API for **Gradely**, an automated bubble-sheet grading and classroom management system.

It handles authentication and role-based access (teachers see only their own classes, admins see everything), classes and rosters, quizzes and answer keys, and statistics and reports. It is also the **only** client of the Gradely Scanner API: the frontend talks to Django, and Django talks to the scanner.

```
frontend (Next.js) ──► backend (this repo, Django) ──► scanner (FastAPI + OpenCV, Hugging Face Space)
                              │                              │
                              └──► Supabase Postgres         └──► Supabase Storage (graded sheet images)
```

---

## Core models

* **`User`**: custom user (email login) with a `role` (`ADMIN` / `FACULTY`) and admin approval.
* **`Subject`**: course (`code`, `name`, `grade_level`).
* **`Classroom`**: a teacher's section of a subject, with its student roster.
* **`Student`**: `name` plus a 6-digit `student_id` in the format **`YYNNNN`**: the year they were registered plus a sequence number (e.g. `260001`). Students shade this ID on the answer sheet.
* **`Quiz`**: belongs to a classroom. `total_score` is the **number of items** (1–100, one point each). `answer_key` is `{"1": "A", ...}`. It also caches mean/min/max/attendee statistics.
* **`QuizResult`**: one per student per quiz: score, per-item `student_answers` (`selected`, `key`, `correct`, `needs_review`), and `scanned_image_url` (the graded sheet image).

## Scanning workflow (`/gradely/quizzes/{id}/...`)

| Step | Endpoint | What it does |
|---|---|---|
| 1 | `PUT answer_key/` | Set the key: multipart `file` (CSV/Excel, columns *item, answer*) or JSON `{"answer_key": {...}}`. It must cover every item. Saved results are re-graded. |
| 2 | `GET answer_sheet/` | Printable PDF from the scanner, pre-filled with the quiz title, subject, teacher and grade & section. |
| 3 | `POST scan/` | Multipart `files` (photos). Django forwards them with the key to the scanner and matches each shaded ID to the class roster. **Nothing is saved yet.** |
| 4 | `POST save_results/` | `{"results": [{"student_id", "student_answers", "scanned_image_url"}]}` after the teacher's review. Scores are recomputed on the server from the stored key. |

Other quiz endpoints: `GET export_report/` (CSV with stats, item analysis and results).

`GET /gradely/scanner/status/` returns `{"ready", "image_storage"}`. The frontend polls it on the Scan tab because the free Hugging Face Space sleeps when idle and needs up to a minute to wake.

### Admin: faculty management (`/gradely/admin/faculty/`, admins only)

| Method | Path | What it does |
|---|---|---|
| `GET` | `admin/faculty/` | All faculty accounts with class/student/quiz counts (`?is_active=`, `?is_approved=` filters) |
| `POST` | `admin/faculty/` | Create an account (`email`, `first_name`, `last_name`, `password`); it starts approved |
| `GET` | `admin/faculty/{id}/` | One account plus their classes |
| `PATCH` | `admin/faculty/{id}/` | Edit name/email, approve, or deactivate (`is_active: false` blocks login, keeps data) |
| `DELETE` | `admin/faculty/{id}/` | Delete the account **and** their classes, quizzes and results (students stay) |
| `POST` | `admin/faculty/{id}/set_password/` | Set a new password |

Admin accounts never appear here, so admins can't delete each other (or themselves) from this page.

### Access rules
* Teachers see and edit only their own classes, quizzes and enrolled students. Admins see everything.
* Students are created by roster import and removed per class (`remove_student`), so `/students/` supports list, retrieve and update only.
* `/users/` returns only the requesting user, unless they're an admin.
* A quiz's number of items can't change once it has saved results.

Roster import: `POST /gradely/classrooms/{id}/upload-students/` with a CSV/Excel file that has a `name` column. New students get the next `YYNNNN` IDs.

---

## Local development

Requires Python 3.12.

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

### Environment variables (`.env` next to `manage.py`)

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | yes | Django secret key |
| `DATABASE_URI` | yes | Supabase Postgres connection string |
| `DEBUG` | no | default `True` |
| `DB_SSL_REQUIRE` | no | default `True`; set `False` for a local database |
| `SCANNER_API_URL` | no | default `https://azcreas-gradely-scanner-api.hf.space`; use `http://127.0.0.1:7860` for a local scanner |
| `SCANNER_API_KEY` | no | shared secret sent as `X-API-Key`; must match the scanner's `SCANNER_API_KEY` |
| `SCANNER_TIMEOUT` | no | seconds per scanner request, default `120` (a sleeping Hugging Face Space can take a while to wake up) |

### Tests

Tests run against a throwaway SQLite database and mock the scanner:

```bash
# PowerShell
$env:DATABASE_URL="sqlite:///db.sqlite3"; $env:DB_SSL_REQUIRE="False"; python manage.py test gradely
# bash
DATABASE_URL=sqlite:///db.sqlite3 DB_SSL_REQUIRE=False python manage.py test gradely
```

`DATABASE_URL` takes precedence over `DATABASE_URI`, so you can also use it to run the whole app locally on SQLite.
