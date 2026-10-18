"""
Thin client for the Gradely Scanner API (FastAPI service in gradely-scanner-api/).

The scanner is stateless: Django sends it the quiz's answer key and the photos,
and gets back per-sheet results. Nothing is saved until the teacher reviews the
results and calls QuizViewSet.save_results.
"""
import json

import requests
from django.conf import settings


class ScannerError(Exception):
    """The scanner could not be reached or rejected the request."""


def _headers():
    return {"X-API-Key": settings.SCANNER_API_KEY} if settings.SCANNER_API_KEY else {}


def _post(path, **kwargs):
    url = f"{settings.SCANNER_API_URL}{path}"
    try:
        response = requests.post(url, headers=_headers(), timeout=settings.SCANNER_TIMEOUT, **kwargs)
    except requests.RequestException as e:
        raise ScannerError(f"Scanner service is unreachable: {e}") from e
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ScannerError(f"Scanner error ({response.status_code}): {detail}")
    return response


def health(timeout=20) -> dict:
    """
    {"ready": bool, "image_storage": bool | None}. A sleeping Hugging Face Space
    wakes up on this request; until it answers, report not ready instead of
    holding the request open past the web server's own timeout.
    """
    try:
        response = requests.get(f"{settings.SCANNER_API_URL}/health", timeout=timeout)
        response.raise_for_status()
        return {"ready": True, "image_storage": response.json().get("image_storage")}
    except (requests.RequestException, ValueError):
        return {"ready": False, "image_storage": None}


def generate_answer_sheet(num_items, title=None, subject=None, teacher=None, grade_section=None) -> bytes:
    """Returns the PDF bytes of a long-bond page with two answer sheets."""
    payload = {
        "num_items": num_items,
        "title": title,
        "subject": subject,
        "teacher": teacher,
        "grade_section": grade_section,
    }
    return _post("/answer-sheets", json=payload).content


def scan_sheets(uploaded_files, answer_key: dict) -> dict:
    """
    uploaded_files: Django UploadedFile objects (photos of answer sheets).
    answer_key:     {"1": "A", ...}
    Returns the scanner's JSON: {"total_files", "sheets_read", "failed_count", "results": [...]}.
    """
    files = [
        ("files", (f.name, f.read(), getattr(f, "content_type", None) or "application/octet-stream"))
        for f in uploaded_files
    ]
    return _post("/scan", files=files, data={"answer_key": json.dumps(answer_key)}).json()
