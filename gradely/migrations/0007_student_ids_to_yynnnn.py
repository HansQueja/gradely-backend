"""
Converts student IDs from the old "YY-XXXXXX" format (e.g. 26-000001) to the
6-digit "YYNNNN" format shaded on the answer sheets (e.g. 260001).
IDs that don't fit (sequence above 9999, or another format) are left unchanged
and reported, so they can be fixed by hand.
"""
import re

from django.db import migrations

OLD_RE = re.compile(r"^(\d{2})-(\d{6})$")
NEW_RE = re.compile(r"^(\d{2})(\d{4})$")


def forwards(apps, schema_editor):
    Student = apps.get_model("gradely", "Student")
    taken = set(Student.objects.values_list("student_id", flat=True))
    skipped = []
    for student in Student.objects.all():
        match = OLD_RE.match(student.student_id)
        if not match:
            if not NEW_RE.match(student.student_id):
                skipped.append(student.student_id)
            continue
        yy, seq = match.group(1), int(match.group(2))
        new_id = f"{yy}{seq:04d}"
        if seq > 9999 or new_id in taken:
            skipped.append(student.student_id)
            continue
        taken.discard(student.student_id)
        taken.add(new_id)
        student.student_id = new_id
        student.save(update_fields=["student_id"])
    if skipped:
        print(f"\n  Student IDs left unchanged (fix manually): {', '.join(skipped)}")


def backwards(apps, schema_editor):
    Student = apps.get_model("gradely", "Student")
    for student in Student.objects.all():
        match = NEW_RE.match(student.student_id)
        if match:
            student.student_id = f"{match.group(1)}-{int(match.group(2)):06d}"
            student.save(update_fields=["student_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("gradely", "0006_quizresult_scanned_image_url"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
