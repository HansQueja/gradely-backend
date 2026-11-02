"""
Downloadable quiz results.

  write_report(quiz, writer)   the CSV report (stats, item analysis, student results)
  build_results_zip(quiz)      a ZIP with that report, every student's answers and the
                               graded sheet images, ready for a teacher to keep or share

Graded images live in Supabase Storage (or on the scanner when it stores them locally) and
are deleted after SCANNED_IMAGE_RETENTION_DAYS, so the ZIP is how a teacher keeps them.
"""
import csv
import io
import logging
import re
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

IMAGE_TIMEOUT = 20                     # seconds per image download
IMAGE_MAX_BYTES = 15 * 1024 * 1024     # graded sheets are ~0.5 MB; anything bigger isn't one
DOWNLOAD_WORKERS = 8
SPOOL_MAX_BYTES = 20 * 1024 * 1024     # build the ZIP in memory up to this size, then on disk
_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _items(quiz):
    return [str(i) for i in range(1, quiz.total_score + 1)]


def _safe_name(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_") or "quiz"


def write_report(quiz, writer):
    """The CSV report: quiz info, statistics, item analysis, then every student in the class."""
    writer.writerow(['QUIZ REPORT', quiz.title])
    writer.writerow(['Classroom', quiz.classroom.section_name])
    writer.writerow(['Subject', quiz.classroom.subject.code])
    writer.writerow(['Date Created', quiz.created_at.strftime('%Y-%m-%d')])
    writer.writerow([])

    writer.writerow(['STATISTICS'])
    writer.writerow(['Total Students', quiz.attendees_count])
    writer.writerow(['Mean Score', round(quiz.mean_score, 2)])
    writer.writerow(['Highest Score', quiz.max_score])
    writer.writerow(['Lowest Score', quiz.min_score])
    writer.writerow(['Perfect Score', quiz.total_score])
    writer.writerow([])

    all_results = list(quiz.results.all())
    total_respondents = len(all_results)
    if total_respondents > 0:
        correct_counts = {}
        for res in all_results:
            for q_num, details in (res.student_answers or {}).items():
                correct_counts.setdefault(q_num, 0)
                if details.get('correct') is True:
                    correct_counts[q_num] += 1

        writer.writerow(['ITEM ANALYSIS (Difficulty Index)'])
        writer.writerow(['Question #', 'Correct Count', 'Success Rate (%)'])
        for q in sorted(correct_counts, key=lambda x: int(x) if x.isdigit() else x):
            count = correct_counts[q]
            writer.writerow([q, count, f"{round(count / total_respondents * 100, 1)}%"])
        writer.writerow([])

    writer.writerow(['STUDENT RESULTS'])
    writer.writerow(['Student ID', 'Name', 'Score', 'Percentage', 'Date Taken'])
    existing_results = {res.student_id: res for res in all_results}
    for student in quiz.classroom.students.all().order_by('name'):
        result = existing_results.get(student.id)
        if result:
            pct = round(result.score_obtained / quiz.total_score * 100, 1)
            writer.writerow([student.student_id, student.name, result.score_obtained, f"{pct}%",
                             result.date_taken.strftime('%Y-%m-%d %H:%M')])
        else:
            writer.writerow([student.student_id, student.name, "N/A", "N/A", "Not Taken"])


def _write_answers(quiz, results, writer):
    """One row per student with the letter they chose on every item, plus which items were wrong."""
    items = _items(quiz)
    key = quiz.answer_key or {}
    writer.writerow(['Student ID', 'Name', 'Score', 'Percentage', 'Wrong Items'] + [f"Q{i}" for i in items])
    writer.writerow(['', 'ANSWER KEY', quiz.total_score, '100%', ''] + [key.get(i, '') for i in items])
    for result in results:
        answers = result.student_answers or {}
        wrong = [i for i in items if not (answers.get(i) or {}).get('correct')]
        pct = round(result.score_obtained / quiz.total_score * 100, 1)
        score = result.score_obtained
        writer.writerow([result.student.student_id, result.student.name,
                         int(score) if score == int(score) else score, f"{pct}%",
                         " ".join(wrong)]
                        + [(answers.get(i) or {}).get('selected', '-') for i in items])


def _allowed_image_hosts():
    """Images are only fetched from where the scanner stores them, never from any URL a client sent."""
    hosts = set()
    for url in (settings.SUPABASE_URL, settings.SCANNER_API_URL):
        if url:
            hosts.add(urlparse(url).hostname)
    return hosts


def _is_allowed(url, hosts):
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return parsed.hostname in hosts or parsed.hostname.endswith(".supabase.co")


def _fetch_image(url):
    """Returns (bytes, extension) or None when the image is gone or unreachable."""
    try:
        with requests.get(url, timeout=IMAGE_TIMEOUT, stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                return None
            content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if content_type not in _IMAGE_TYPES:
                return None
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data += chunk
                if len(data) > IMAGE_MAX_BYTES:
                    return None
            return bytes(data), _IMAGE_TYPES[content_type]
    except requests.RequestException as e:
        logger.warning("Could not download graded image %s: %s", url, e)
        return None


def zip_filename(quiz):
    return f"{_safe_name(quiz.title)}_{_safe_name(quiz.classroom.section_name)}_Results.zip"


def build_results_zip(quiz):
    """
    Returns (file, summary) where file is a ZIP opened at position 0:
      report.csv, answers.csv, graded_sheets/<ID>_<Name>.jpg, README.txt
    summary = {"students": n, "images": n, "missing_images": [names]}.
    """
    results = sorted(quiz.results.select_related('student'), key=lambda r: r.student.name.lower())
    hosts = _allowed_image_hosts()
    to_fetch = [r for r in results if _is_allowed(r.scanned_image_url, hosts)]
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        images = dict(zip((r.id for r in to_fetch), pool.map(lambda r: _fetch_image(r.scanned_image_url), to_fetch)))

    folder = zip_filename(quiz)[:-len(".zip")]
    out = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    missing = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, write in (("report.csv", lambda w: write_report(quiz, w)),
                            ("answers.csv", lambda w: _write_answers(quiz, results, w))):
            text = io.StringIO()
            write(csv.writer(text))
            # utf-8-sig so Excel shows names like "Peña" correctly
            archive.writestr(f"{folder}/{name}", text.getvalue().encode("utf-8-sig"))

        for result in results:
            image = images.get(result.id)
            if image is None:
                missing.append(f"{result.student.student_id} {result.student.name}")
                continue
            data, ext = image
            # JPEGs are already compressed; storing them is faster and the ZIP is no bigger
            archive.writestr(f"{folder}/graded_sheets/{result.student.student_id}_{_safe_name(result.student.name)}{ext}",
                             data, compress_type=zipfile.ZIP_STORED)

        archive.writestr(f"{folder}/README.txt", _readme(quiz, len(results), missing).encode("utf-8"))

    out.seek(0)
    return out, {"students": len(results), "images": len(results) - len(missing), "missing_images": missing}


def _readme(quiz, result_count, missing):
    lines = [
        f"{quiz.title} - {quiz.classroom.subject.code} {quiz.classroom.section_name}",
        "",
        "report.csv      statistics, item analysis and every student's score",
        "answers.csv     the letter each student shaded on every item (- means blank or unreadable)",
        "graded_sheets/  each student's checked answer sheet",
        "",
        f"Results: {result_count}. Graded sheets included: {result_count - len(missing)}.",
    ]
    if missing:
        days = settings.SCANNED_IMAGE_RETENTION_DAYS
        reason = (f"Graded sheet images are deleted {days} days after scanning; their scores are kept."
                  if days else "Their images were not stored or could not be downloaded.")
        lines += ["", f"No graded sheet for {len(missing)} student(s). {reason}"] + [f"  {m}" for m in missing]
    return "\n".join(lines) + "\n"
