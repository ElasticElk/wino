from collections import defaultdict

from django.core.management.base import BaseCommand

from wine_cellar.apps.wine.models import Wine


def _normalise(name: str) -> str:
    return " ".join((name or "").lower().split())


class Command(BaseCommand):
    help = (
        "Print same-household wines whose names match "
        "once case and spacing are ignored."
    )

    def handle(self, *args, **options):
        groups = defaultdict(list)
        for wine in Wine.objects.select_related("household").order_by("id"):
            key = (wine.household_id, _normalise(wine.name))
            if key[1]:
                groups[key].append(wine)
        pairs = 0
        for wines in groups.values():
            if len(wines) < 2:
                continue
            for index, left in enumerate(wines):
                for right in wines[index + 1:]:
                    household = left.household.name if left.household_id else ""
                    self.stdout.write(
                        f"{household}: #{left.id} {left.name}"
                        f" / #{right.id} {right.name}"
                    )
                    pairs += 1
        self.stdout.write(f"{pairs} pairs")
