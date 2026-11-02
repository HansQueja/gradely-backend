"""
Deletes graded answer-sheet images from Supabase Storage after a few days, so the
bucket doesn't fill up. Scores and per-item answers stay in the database; only the
picture goes, and the result's `scanned_image_url` is cleared so nothing links to it.

Configured with SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_BUCKET and
SCANNED_IMAGE_RETENTION_DAYS (0 turns it off). Runs:
  - automatically, at most every few hours, after teachers scan or save results
    (see maybe_purge_in_background), and
  - on demand: python manage.py purge_scanned_images [--days N] [--dry-run]
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests
from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
DELETE_BATCH = 100
AUTO_PURGE_INTERVAL = 6 * 60 * 60   # seconds between automatic runs (per server process)

_last_auto_run = 0.0
_lock = threading.Lock()


@dataclass
class PurgeReport:
    checked: int = 0
    deleted: list = field(default_factory=list)
    results_cleared: int = 0
    dry_run: bool = False
    skipped_reason: str = ""


def is_configured() -> bool:
    return bool(settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY and settings.SCANNED_IMAGE_RETENTION_DAYS > 0)


def _headers():
    key = settings.SUPABASE_SERVICE_KEY
    return {"Authorization": f"Bearer {key}", "apikey": key}


def _storage_url(path):
    return f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/{path}"


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _old_objects(cutoff):
    """Names of files in the bucket created before `cutoff`, oldest first."""
    bucket = settings.SUPABASE_BUCKET
    offset, names, checked = 0, [], 0
    while True:
        response = requests.post(
            _storage_url(f"object/list/{bucket}"),
            headers=_headers(),
            json={"prefix": "", "limit": PAGE_SIZE, "offset": offset,
                  "sortBy": {"column": "created_at", "order": "asc"}},
            timeout=30,
        )
        response.raise_for_status()
        page = response.json()
        checked += len(page)
        for obj in page:
            created = _parse_time(obj.get("created_at"))
            if obj.get("id") is None or created is None:   # folders have no id
                continue
            if created >= cutoff:
                return names, checked                     # sorted: everything after is newer
            names.append(obj["name"])
        if len(page) < PAGE_SIZE:
            return names, checked
        offset += PAGE_SIZE


def _delete(names):
    bucket = settings.SUPABASE_BUCKET
    for i in range(0, len(names), DELETE_BATCH):
        response = requests.delete(
            _storage_url(f"object/{bucket}"),
            headers=_headers(),
            json={"prefixes": names[i:i + DELETE_BATCH]},
            timeout=30,
        )
        response.raise_for_status()


def _clear_links(names):
    """Clears scanned_image_url on results whose image file was deleted. Returns how many."""
    from .models import QuizResult

    gone = set(names)
    stale = []
    for result in QuizResult.objects.exclude(scanned_image_url__isnull=True).exclude(scanned_image_url=""):
        filename = result.scanned_image_url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if filename in gone:
            result.scanned_image_url = None
            stale.append(result)
    QuizResult.objects.bulk_update(stale, ["scanned_image_url"])
    return len(stale)


def purge(days=None, dry_run=False) -> PurgeReport:
    report = PurgeReport(dry_run=dry_run)
    days = settings.SCANNED_IMAGE_RETENTION_DAYS if days is None else days
    if not (settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY):
        report.skipped_reason = "SUPABASE_URL / SUPABASE_SERVICE_KEY are not set."
        return report
    if days <= 0:
        report.skipped_reason = "Image retention is turned off (SCANNED_IMAGE_RETENTION_DAYS=0)."
        return report

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    names, report.checked = _old_objects(cutoff)
    report.deleted = names
    if names and not dry_run:
        _delete(names)
        report.results_cleared = _clear_links(names)
    return report


def maybe_purge_in_background():
    """Starts a purge in a background thread if the last one was long enough ago."""
    global _last_auto_run
    if not is_configured():
        return
    with _lock:
        if time.monotonic() - _last_auto_run < AUTO_PURGE_INTERVAL and _last_auto_run:
            return
        _last_auto_run = time.monotonic()

    def run():
        try:
            report = purge()
            if report.deleted:
                logger.info("Deleted %d old answer-sheet images (%d results unlinked).",
                            len(report.deleted), report.results_cleared)
        except Exception:
            logger.exception("Purging old answer-sheet images failed")
        finally:
            close_old_connections()

    threading.Thread(target=run, name="purge-scanned-images", daemon=True).start()
