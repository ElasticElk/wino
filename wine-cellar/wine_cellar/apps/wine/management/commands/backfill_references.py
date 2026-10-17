from django.core.management.base import BaseCommand

from wine_cellar.apps.wine.models import Wine
from wine_cellar.apps.wine.tasks import refresh_wine_references


class Command(BaseCommand):
    help = "Queue reference lookups for wines that have never had one. At most 20."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the wine ids that would be queued.",
        )

    def handle(self, *args, **options):
        wines = list(
            Wine.objects.filter(reference_updated__isnull=True).order_by("id")[:20]
        )
        if options["dry_run"]:
            for wine in wines:
                self.stdout.write(str(wine.id))
            self.stdout.write(f"{len(wines)} wines")
            return
        for wine in wines:
            refresh_wine_references.delay(wine.id)
        self.stdout.write(f"Queued {len(wines)} wines")
