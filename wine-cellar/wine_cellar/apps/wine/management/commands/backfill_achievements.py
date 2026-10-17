from django.core.management.base import BaseCommand

from wine_cellar.apps.scanner.models import Household, HouseholdMembership
from wine_cellar.apps.wine.achievements import backfill


class Command(BaseCommand):
    help = (
        "Award already-earned badges silently so the next bottle "
        "does not burst-notify an established cellar."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List households that would be backfilled.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        total = 0
        households = Household.objects.filter(gamification_enabled=True)
        for household in households:
            owner = (
                HouseholdMembership.objects.filter(
                    household=household, role="owner",
                )
                .select_related("user")
                .first()
            )
            if owner is None:
                owner = (
                    HouseholdMembership.objects.filter(household=household)
                    .select_related("user")
                    .first()
                )
            if owner is None:
                self.stdout.write(f"hh {household.id} {household.name}: no members, skip")
                continue
            if dry:
                self.stdout.write(f"would backfill hh {household.id} {household.name}")
                continue
            n = backfill(household, owner.user)
            total += n
            self.stdout.write(f"hh {household.id} {household.name}: {n} badges")
        self.stdout.write(f"{total} badges awarded")
