"""
Wipes classroom data for a fresh start while keeping admin accounts.

    python manage.py reset_data                  # dry run: shows what would be deleted
    python manage.py reset_data --yes            # actually deletes

Deleted by default: quiz results, quizzes, classrooms, students, subjects, and
all non-admin (faculty) accounts. Kept: admin accounts.

    --keep-faculty    also keep faculty accounts (so teachers don't have to sign up again)
    --keep-subjects   also keep the subject list

Student IDs restart from YY0001 automatically (they are derived from what exists).
Graded sheet images in Supabase Storage are NOT touched; empty that bucket from
the Supabase dashboard if you want them gone too.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from gradely.models import Classroom, Quiz, QuizResult, Student, Subject, User


class Command(BaseCommand):
    help = "Delete classroom data and faculty accounts, keeping admin accounts."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Actually delete. Without it, only a dry run.")
        parser.add_argument("--keep-faculty", action="store_true", help="Keep faculty accounts.")
        parser.add_argument("--keep-subjects", action="store_true", help="Keep subjects.")

    def handle(self, *args, **opts):
        db = settings.DATABASES["default"]
        self.stdout.write(f"Database: {db.get('HOST') or db.get('NAME')} / {db.get('NAME')}")

        admins = User.objects.filter(role=User.Role.ADMIN)
        if not admins.exists():
            self.stderr.write("No admin accounts found. Refusing to continue: you would be locked out.")
            return

        faculty = User.objects.exclude(role=User.Role.ADMIN)
        steps = [
            ("quiz results", QuizResult.objects.all()),
            ("quizzes", Quiz.objects.all()),
            ("classrooms", Classroom.objects.all()),
            ("students", Student.objects.all()),
        ]
        if not opts["keep_subjects"]:
            steps.append(("subjects", Subject.objects.all()))
        if not opts["keep_faculty"]:
            steps.append(("faculty accounts", faculty))
        elif opts["keep_faculty"]:
            self.stdout.write("Keeping faculty accounts.")

        verb = "Deleting" if opts["yes"] else "Would delete"
        for label, qs in steps:
            self.stdout.write(f"  {verb} {qs.count():>5} {label}")
        self.stdout.write(f"  Keeping {admins.count():>5} admin account(s): "
                          + ", ".join(admins.values_list("email", flat=True)))

        if not opts["yes"]:
            self.stdout.write(self.style.WARNING("\nDry run only. Re-run with --yes to delete."))
            return

        with transaction.atomic():
            for _, qs in steps:
                qs.delete()
        self.stdout.write(self.style.SUCCESS("\nDone. Student IDs will restart from YY0001."))
