"""
Run locally against SQLite (no Supabase needed):
    DATABASE_URL=sqlite:///db.sqlite3 DB_SSL_REQUIRE=False python manage.py test gradely
"""
import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from . import answer_keys, scanner_client
from .models import Classroom, Quiz, QuizResult, Student, Subject, User
from .student_ids import allocate_student_ids


class StudentIdTests(TestCase):
    def test_sequence_continues_per_year(self):
        Student.objects.create(name="A", student_id="260007")
        Student.objects.create(name="B", student_id="250099")
        self.assertEqual(allocate_student_ids(2, year=2026), ["260008", "260009"])
        self.assertEqual(allocate_student_ids(1, year=2027), ["270001"])

    def test_year_full(self):
        Student.objects.create(name="A", student_id="269999")
        with self.assertRaises(ValueError):
            allocate_student_ids(1, year=2026)


class AnswerKeyTests(TestCase):
    def test_normalize_requires_every_item(self):
        self.assertEqual(answer_keys.normalize({"1": "a", 2: "B"}, 2), {"1": "A", "2": "B"})
        with self.assertRaisesRegex(answer_keys.AnswerKeyError, "missing item"):
            answer_keys.normalize({"1": "A", "3": "B"}, 3)
        with self.assertRaisesRegex(answer_keys.AnswerKeyError, "includes item"):
            answer_keys.normalize({"1": "A", "2": "B"}, 1)
        with self.assertRaisesRegex(answer_keys.AnswerKeyError, "must be one of"):
            answer_keys.normalize({"1": "E"}, 1)

    def test_parse_csv_with_header(self):
        f = SimpleUploadedFile("key.csv", b"Item,Answer\n1,a\n2,C\n3,d\n")
        self.assertEqual(answer_keys.parse_file(f, 3), {"1": "A", "2": "C", "3": "D"})


class ScanWorkflowTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="t@x.com", email="t@x.com", password="pw", first_name="Ana", last_name="Santos",
            role=User.Role.FACULTY, is_approved=True)
        subject = Subject.objects.create(name="Science", code="SCI6", grade_level=6)
        self.classroom = Classroom.objects.create(
            teacher=self.teacher, subject=subject, section_name="Rizal", school_year="2026-2027")
        self.alice = Student.objects.create(name="Alice", student_id="260001")
        self.ben = Student.objects.create(name="Ben", student_id="260002")
        self.outsider = Student.objects.create(name="Other", student_id="260003")
        self.classroom.students.add(self.alice, self.ben)
        self.quiz = Quiz.objects.create(classroom=self.classroom, title="Quiz 1", total_score=3)
        self.client = APIClient()
        self.client.force_authenticate(self.teacher)
        self.url = f"/gradely/quizzes/{self.quiz.id}"

    def set_key(self):
        return self.client.put(f"{self.url}/answer_key/", {"answer_key": {"1": "A", "2": "B", "3": "C"}}, format="json")

    def test_answer_key_upload_and_validation(self):
        res = self.client.put(f"{self.url}/answer_key/", {"file": SimpleUploadedFile("k.csv", b"1,A\n2,B\n")},
                              format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertIn("missing item(s) 3", res.data["error"])

        res = self.set_key()
        self.assertEqual(res.status_code, 200)
        detail = self.client.get(f"{self.url}/").data
        self.assertTrue(detail["has_answer_key"])
        self.assertEqual(detail["answer_key"], {"1": "A", "2": "B", "3": "C"})

    def test_scan_requires_key(self):
        res = self.client.post(f"{self.url}/scan/", {"files": [SimpleUploadedFile("a.jpg", b"x")]}, format="multipart")
        self.assertEqual(res.status_code, 400)

    @mock.patch.object(scanner_client, "scan_sheets")
    def test_scan_matches_roster(self, scan_sheets):
        self.set_key()
        scan_sheets.return_value = {"total_files": 1, "sheets_read": 2, "failed_count": 1, "results": [
            {"student_id": "260001", "score": 2, "student_answers": {}, "warnings": []},
            {"student_id": "260003", "score": 1, "student_answers": {}, "warnings": []},
            {"filename": "a.jpg", "error": "right sheet: Only 2 of 4 corner markers found"},
        ]}
        res = self.client.post(f"{self.url}/scan/", {"files": [SimpleUploadedFile("a.jpg", b"x")]}, format="multipart")
        self.assertEqual(res.status_code, 200)
        args = scan_sheets.call_args[0]
        self.assertEqual(args[1], {"1": "A", "2": "B", "3": "C"})
        first, second, _ = res.data["results"]
        self.assertEqual(first["student"]["name"], "Alice")
        self.assertIsNone(second["student"])            # not in this class
        self.assertIn("not in this class", second["warnings"][0])

    @mock.patch.object(scanner_client, "scan_sheets", side_effect=scanner_client.ScannerError("down"))
    def test_scan_scanner_down(self, _):
        self.set_key()
        res = self.client.post(f"{self.url}/scan/", {"files": [SimpleUploadedFile("a.jpg", b"x")]}, format="multipart")
        self.assertEqual(res.status_code, 502)

    def test_save_results_grades_server_side_and_regrades_on_key_change(self):
        self.set_key()
        payload = {"results": [
            # client claims a score of 3, but only items 1 and 2 match the key
            {"student_id": "260001", "score": 3, "scanned_image_url": "https://img/1.jpg",
             "student_answers": {"1": {"selected": "A"}, "2": {"selected": "B"}, "3": {"selected": "D"}}},
            {"student_id": "260003", "student_answers": {}},   # not enrolled
        ]}
        res = self.client.post(f"{self.url}/save_results/", payload, format="json")
        self.assertEqual(res.data["saved"], 1)
        self.assertIn("not enrolled", res.data["errors"][0])

        result = QuizResult.objects.get(quiz=self.quiz, student=self.alice)
        self.assertEqual(result.score_obtained, 2)
        self.assertFalse(result.student_answers["3"]["correct"])

        detail = self.client.get(f"{self.url}/").data
        row = next(r for r in detail["results"] if r["student_id"] == "260001")
        self.assertEqual(row["scanned_image_url"], "https://img/1.jpg")
        self.assertEqual(detail["item_analysis"][0]["percentage"], 100.0)

        # Teacher fixes item 3 in the key -> saved result is re-scored
        self.client.put(f"{self.url}/answer_key/", {"answer_key": {"1": "A", "2": "B", "3": "D"}}, format="json")
        result.refresh_from_db()
        self.quiz.refresh_from_db()
        self.assertEqual(result.score_obtained, 3)
        self.assertEqual(self.quiz.max_score, 3)

    @mock.patch.object(scanner_client, "generate_answer_sheet", return_value=b"%PDF-fake")
    def test_answer_sheet_prefilled(self, generate):
        res = self.client.get(f"{self.url}/answer_sheet/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        generate.assert_called_once_with(
            num_items=3, title="Quiz 1", subject="Science", teacher="Ana Santos", grade_section="Grade 6 - Rizal")

    def test_upload_students_assigns_yynnnn_ids(self):
        f = SimpleUploadedFile("roster.csv", b"Name\nAlice\ncarla cruz\nDan\n")
        res = self.client.post(f"/gradely/classrooms/{self.classroom.id}/upload-students/", {"file": f},
                               format="multipart")
        self.assertEqual(res.status_code, 200, res.data)
        carla = Student.objects.get(name="Carla Cruz")
        self.assertRegex(carla.student_id, r"^\d{6}$")
        self.assertEqual(Student.objects.filter(name="Alice").count(), 1)   # existing student reused
        self.assertEqual(self.classroom.students.count(), 4)

    def test_other_teacher_cannot_scan(self):
        other = User.objects.create_user(username="o@x.com", email="o@x.com", password="pw",
                                         role=User.Role.FACULTY, is_approved=True)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f"{self.url}/answer_key/").status_code, 404)


class AccessControlTests(TestCase):
    """Teachers must only reach their own students, quizzes and account."""

    def setUp(self):
        make = lambda email: User.objects.create_user(username=email, email=email, password="pw",
                                                      role=User.Role.FACULTY, is_approved=True)
        self.t1, self.t2 = make("one@x.com"), make("two@x.com")
        subject = Subject.objects.create(name="Math", code="M6", grade_level=6)
        self.c1 = Classroom.objects.create(teacher=self.t1, subject=subject, section_name="A", school_year="26")
        self.c2 = Classroom.objects.create(teacher=self.t2, subject=subject, section_name="B", school_year="26")
        self.kid = Student.objects.create(name="Kid", student_id="260001")
        self.c1.students.add(self.kid)
        self.quiz = Quiz.objects.create(classroom=self.c1, title="Q", total_score=5)
        self.client = APIClient()

    def test_students_scoped_to_own_classes(self):
        self.client.force_authenticate(self.t2)
        self.assertEqual(self.client.patch(f"/gradely/students/{self.kid.id}/", {"name": "X"}, format="json").status_code, 404)
        self.assertEqual(self.client.get("/gradely/students/").data, [])
        self.client.force_authenticate(self.t1)
        res = self.client.patch(f"/gradely/students/{self.kid.id}/", {"student_id": "26-0001"}, format="json")
        self.assertEqual(res.status_code, 400)                      # must be 6 digits
        self.assertEqual(self.client.delete(f"/gradely/students/{self.kid.id}/").status_code, 405)

    def test_users_only_see_themselves(self):
        self.client.force_authenticate(self.t2)
        self.assertEqual([u["email"] for u in self.client.get("/gradely/users/").data], ["two@x.com"])
        self.assertEqual(self.client.get(f"/gradely/users/{self.t1.id}/").status_code, 404)

    def test_quiz_cannot_move_to_another_teachers_class(self):
        self.client.force_authenticate(self.t1)
        res = self.client.patch(f"/gradely/quizzes/{self.quiz.id}/", {"classroom": self.c2.id}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post("/gradely/quizzes/", {"title": "N", "total_score": 5, "classroom": self.c2.id}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post("/gradely/quizzes/", {"title": "N", "total_score": 101, "classroom": self.c1.id}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_item_count_locked_once_results_exist(self):
        self.client.force_authenticate(self.t1)
        self.assertEqual(self.client.patch(f"/gradely/quizzes/{self.quiz.id}/", {"total_score": 6}, format="json").status_code, 200)
        QuizResult.objects.create(quiz=self.quiz, student=self.kid, score_obtained=3)
        res = self.client.patch(f"/gradely/quizzes/{self.quiz.id}/", {"total_score": 7}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_dashboard_counts(self):
        self.client.force_authenticate(self.t1)
        data = self.client.get("/gradely/dashboard/").data
        self.assertEqual(data["classes"][0]["student_count"], 1)
        self.assertEqual(data["total_students"], 1)

    @mock.patch.object(scanner_client, "health", return_value={"ready": False, "image_storage": None})
    def test_scanner_status(self, _):
        self.client.force_authenticate(self.t1)
        self.assertEqual(self.client.get("/gradely/scanner/status/").data, {"ready": False, "image_storage": None})


class ResetDataCommandTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin@x.com", email="admin@x.com", password="pw",
                                              role=User.Role.ADMIN, is_approved=True)
        self.teacher = User.objects.create_user(username="t@x.com", email="t@x.com", password="pw",
                                                role=User.Role.FACULTY, is_approved=True)
        subject = Subject.objects.create(name="Math", code="M6", grade_level=6)
        classroom = Classroom.objects.create(teacher=self.teacher, subject=subject, section_name="A", school_year="26")
        student = Student.objects.create(name="Kid", student_id="260001")
        classroom.students.add(student)
        quiz = Quiz.objects.create(classroom=classroom, title="Q", total_score=5)
        QuizResult.objects.create(quiz=quiz, student=student, score_obtained=3)

    def run_command(self, *args):
        from django.core.management import call_command
        call_command("reset_data", *args, stdout=io.StringIO())

    def test_dry_run_deletes_nothing(self):
        self.run_command()
        self.assertEqual(Student.objects.count(), 1)
        self.assertEqual(User.objects.count(), 2)

    def test_reset_keeps_only_admins(self):
        self.run_command("--yes")
        self.assertEqual(list(User.objects.values_list("email", flat=True)), ["admin@x.com"])
        for model in (Student, Subject, Classroom, Quiz, QuizResult):
            self.assertEqual(model.objects.count(), 0)
        self.assertEqual(allocate_student_ids(1, year=2026), ["260001"])   # numbering restarts

    def test_keep_flags(self):
        self.run_command("--yes", "--keep-faculty", "--keep-subjects")
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(Subject.objects.count(), 1)
        self.assertEqual(Student.objects.count(), 0)
        self.assertEqual(Quiz.objects.count(), 0)

    def test_refuses_without_an_admin(self):
        self.admin.delete()
        self.run_command("--yes")
        self.assertEqual(Student.objects.count(), 1)


class EmailCaseTests(TestCase):
    """Phones capitalize the first letter of the email field; that must not block login."""

    def setUp(self):
        self.client = APIClient()

    def test_signup_lowercases_and_login_ignores_case(self):
        res = self.client.post("/gradely/signup/", {"email": " Sheng@School.ph ", "password": "Teach1234!",
                                                     "first_name": "S", "last_name": "P", "role": "FACULTY"},
                               format="json")
        self.assertEqual(res.status_code, 201, res.data)
        user = User.objects.get()
        self.assertEqual(user.email, "sheng@school.ph")
        user.is_approved = True
        user.save()
        for typed in ("sheng@school.ph", "Sheng@school.ph", "SHENG@SCHOOL.PH"):
            res = self.client.post("/gradely/login/", {"email": typed, "password": "Teach1234!"}, format="json")
            self.assertEqual(res.status_code, 200, (typed, res.data))
            self.assertEqual(res.data["role"], "FACULTY")

    def test_existing_mixed_case_account_can_log_in_lowercase(self):
        User.objects.create_user(username="Ana@x.com", email="Ana@x.com", password="pw12345!",
                                 role=User.Role.FACULTY, is_approved=True)
        res = self.client.post("/gradely/login/", {"email": "ana@x.com", "password": "pw12345!"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)

    def test_duplicate_differing_by_case_rejected(self):
        User.objects.create_user(username="ana@x.com", email="ana@x.com", password="pw", role=User.Role.FACULTY)
        res = self.client.post("/gradely/signup/", {"email": "ANA@x.com", "password": "Teach1234!",
                                                     "first_name": "A", "last_name": "B", "role": "FACULTY"},
                               format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("already exists", str(res.data))

    def test_unapproved_still_blocked(self):
        User.objects.create_user(username="new@x.com", email="new@x.com", password="pw12345!",
                                 role=User.Role.FACULTY, is_approved=False)
        res = self.client.post("/gradely/login/", {"email": "NEW@x.com", "password": "pw12345!"}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertIn("pending admin approval", str(res.data))


class AdminFacultyTests(TestCase):
    """Only admins can see and manage faculty accounts; admin accounts are never touched."""

    def setUp(self):
        self.admin = User.objects.create_user(username="admin@x.com", email="admin@x.com", password="pw",
                                              role=User.Role.ADMIN, is_approved=True)
        self.teacher = User.objects.create_user(username="t@x.com", email="t@x.com", password="Teach1234!",
                                                first_name="Ana", last_name="Santos",
                                                role=User.Role.FACULTY, is_approved=True)
        subject = Subject.objects.create(name="Math", code="M6", grade_level=6)
        self.classroom = Classroom.objects.create(teacher=self.teacher, subject=subject, section_name="A", school_year="26")
        self.kid = Student.objects.create(name="Kid", student_id="260001")
        self.classroom.students.add(self.kid)
        self.quiz = Quiz.objects.create(classroom=self.classroom, title="Q", total_score=5)
        QuizResult.objects.create(quiz=self.quiz, student=self.kid, score_obtained=3)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = "/gradely/admin/faculty/"

    def test_faculty_cannot_use_it(self):
        self.client.force_authenticate(self.teacher)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.delete(f"{self.url}{self.teacher.id}/").status_code, 403)

    def test_list_and_detail_with_counts(self):
        rows = self.client.get(self.url).data
        self.assertEqual([r["email"] for r in rows], ["t@x.com"])             # admins not listed
        self.assertEqual((rows[0]["class_count"], rows[0]["student_count"], rows[0]["quiz_count"]), (1, 1, 1))
        detail = self.client.get(f"{self.url}{self.teacher.id}/").data
        self.assertEqual(detail["classes"][0]["section_name"], "A")
        self.assertEqual(self.client.get(f"{self.url}{self.admin.id}/").status_code, 404)

    def test_create_edit_and_password(self):
        res = self.client.post(self.url, {"email": "New@X.com", "first_name": "N", "last_name": "T",
                                          "password": "Fresh-pass-2026"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        new = User.objects.get(email="new@x.com")
        self.assertTrue(new.is_approved)
        self.assertEqual(new.role, User.Role.FACULTY)
        self.assertNotIn("password", res.data)

        self.assertEqual(self.client.post(self.url, {"email": "t@X.com", "password": "Fresh-pass-2026"},
                                          format="json").status_code, 400)          # duplicate email
        self.assertEqual(self.client.post(self.url, {"email": "x@x.com"}, format="json").status_code, 400)

        res = self.client.patch(f"{self.url}{new.id}/", {"email": "renamed@x.com", "last_name": "Cruz"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        new.refresh_from_db()
        self.assertEqual((new.email, new.username, new.last_name), ("renamed@x.com", "renamed@x.com", "Cruz"))

        self.assertEqual(self.client.post(f"{self.url}{new.id}/set_password/", {"password": "123"},
                                          format="json").status_code, 400)
        self.assertEqual(self.client.post(f"{self.url}{new.id}/set_password/", {"password": "Another-pass-99"},
                                          format="json").status_code, 200)
        login = APIClient().post("/gradely/login/", {"email": "renamed@x.com", "password": "Another-pass-99"}, format="json")
        self.assertEqual(login.status_code, 200)

    def test_deactivate_blocks_login_with_clear_message(self):
        self.client.patch(f"{self.url}{self.teacher.id}/", {"is_active": False}, format="json")
        res = APIClient().post("/gradely/login/", {"email": "t@x.com", "password": "Teach1234!"}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertIn("deactivated", str(res.data))
        wrong = APIClient().post("/gradely/login/", {"email": "t@x.com", "password": "nope"}, format="json")
        self.assertNotIn("deactivated", str(wrong.data))                      # no hint without the password
        self.assertEqual(self.client.get(self.url, {"is_active": "false"}).data[0]["email"], "t@x.com")

    def test_delete_removes_their_classes_but_keeps_students(self):
        res = self.client.delete(f"{self.url}{self.teacher.id}/")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(User.objects.filter(id=self.teacher.id).exists())
        self.assertEqual((Classroom.objects.count(), Quiz.objects.count(), QuizResult.objects.count()), (0, 0, 0))
        self.assertTrue(Student.objects.filter(id=self.kid.id).exists())       # may be in other teachers' classes
        self.assertTrue(User.objects.filter(id=self.admin.id).exists())

    def test_login_updates_last_login(self):
        APIClient().post("/gradely/login/", {"email": "t@x.com", "password": "Teach1234!"}, format="json")
        self.assertIsNotNone(self.client.get(f"{self.url}{self.teacher.id}/").data["last_login"])
