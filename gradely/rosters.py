"""
Matching roster rows to students.

A student keeps one 6-digit ID across all their classes, so an imported name is matched to an
existing student when possible. Names alone are ambiguous (two "Juan Dela Cruz" in one school),
so:
  * a row with a `student_id` always means that exact student (or a new student with that ID);
  * a name that appears twice in one file is two different students;
  * a name is matched to a student already in this class first, then to the oldest other one,
    with a warning when more than one existing student has that name.
"""
from collections import defaultdict

from .models import Student
from .student_ids import STUDENT_ID_RE, allocate_student_ids


def clean_name(raw):
    name = " ".join(str(raw or "").split())
    return "" if name.lower() == "nan" else name.title()


def clean_id(raw):
    text = str(raw if raw is not None else "").strip()
    if text.lower() == "nan":
        return ""
    return text[:-2] if text.endswith(".0") else text   # Excel turns 260001 into 260001.0


def resolve_rows(classroom, rows):
    """
    rows: [(line, name, student_id or "")] in file order (line = spreadsheet row, for messages). Creates the students that don't exist yet.
    Returns (students, created_count, warnings). Call inside a transaction.
    """
    warnings = []
    in_class = set(classroom.students.values_list("id", flat=True))
    given_ids = [sid for _, _, sid in rows if sid]
    by_id = {s.student_id: s for s in Student.objects.filter(student_id__in=given_ids)}

    candidates = defaultdict(list)
    for s in Student.objects.filter(name__in=[n for _, n, sid in rows if not sid]).order_by("id"):
        candidates[s.name].append(s)
    for pool in candidates.values():
        pool.sort(key=lambda s: s.id not in in_class)   # students already in this class first

    students, used, new_with_id, new_names = [], set(), [], []
    for line, name, sid in rows:
        if sid:
            if not STUDENT_ID_RE.match(sid):
                warnings.append(f"Row {line}: student ID '{sid}' must be 6 digits; {name} was skipped.")
            elif sid in by_id:
                student = by_id[sid]
                if student.name.lower() != name.lower():
                    warnings.append(f"Row {line}: ID {sid} belongs to {student.name}, so that name was kept.")
                if student.id not in used:
                    used.add(student.id)
                    students.append(student)
            elif sid not in {s for _, s in new_with_id}:
                new_with_id.append((name, sid))
            continue

        pool = [s for s in candidates[name] if s.id not in used]
        if not pool:
            new_names.append(name)
            continue
        student = pool[0]
        if len(candidates[name]) > 1 and student.id not in in_class:
            warnings.append(f"{name} matched {len(candidates[name])} existing students; used ID {student.student_id}. "
                            "Add a student_id column to choose a specific one.")
        used.add(student.id)
        students.append(student)

    created = Student.objects.bulk_create([Student(name=n, student_id=sid) for n, sid in new_with_id])
    # allocate after the explicit IDs exist so the sequence continues past them
    new_ids = allocate_student_ids(len(new_names))
    created += Student.objects.bulk_create([Student(name=n, student_id=sid) for n, sid in zip(new_names, new_ids)])
    return students + created, len(created), warnings


def same_name_students(name):
    """Existing students with this name, for the teacher to pick from when adding one by hand."""
    return list(Student.objects.filter(name__iexact=clean_name(name)).prefetch_related("classrooms__subject"))
