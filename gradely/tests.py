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
