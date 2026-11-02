"""
Deletes graded answer-sheet images older than the retention period from Supabase Storage.

    python manage.py purge_scanned_images                 # uses SCANNED_IMAGE_RETENTION_DAYS (default 3)
    python manage.py purge_scanned_images --days 7
    python manage.py purge_scanned_images --dry-run       # only shows what would be deleted

Scores and answers are kept; results just lose the link to the deleted picture.
This also runs automatically in the background (see gradely/image_retention.py).
"""
from django.core.management.base import BaseCommand

from gradely import image_retention


class Command(BaseCommand):
    help = "Delete old graded answer-sheet images from Supabase Storage."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Delete images older than this many days.")
        parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted.")

    def handle(self, *args, **opts):
        report = image_retention.purge(days=opts["days"], dry_run=opts["dry_run"])
        if report.skipped_reason:
            self.stderr.write(report.skipped_reason)
            return
        verb = "Would delete" if report.dry_run else "Deleted"
        self.stdout.write(f"Checked {report.checked} file(s). {verb} {len(report.deleted)} image(s).")
        if not report.dry_run and report.deleted:
            self.stdout.write(f"Removed the image link from {report.results_cleared} saved result(s).")
