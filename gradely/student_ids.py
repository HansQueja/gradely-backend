"""
Student IDs are 6 digits, YYNNNN, and are shaded on the answer sheet:
  YY   = last two digits of the year the student was registered in Gradely
  NNNN = registration sequence within that year (0001-9999)
e.g. 260001 is the first student registered in 2026.
"""
import re

from django.db.models import Max
from django.utils import timezone

STUDENT_ID_RE = re.compile(r"^\d{6}$")
MAX_PER_YEAR = 9999


def allocate_student_ids(count: int, year: int = None) -> list:
    """Returns `count` unused, consecutive IDs for the given (default: current) year."""
    from .models import Student

    yy = f"{(year or timezone.now().year) % 100:02d}"
    last = Student.objects.filter(student_id__regex=rf"^{yy}\d{{4}}$").aggregate(m=Max("student_id"))["m"]
    start = int(last[2:]) + 1 if last else 1
    if start + count - 1 > MAX_PER_YEAR:
        raise ValueError(f"Only {MAX_PER_YEAR} student IDs are available per year (20{yy} is full).")
    return [f"{yy}{n:04d}" for n in range(start, start + count)]
