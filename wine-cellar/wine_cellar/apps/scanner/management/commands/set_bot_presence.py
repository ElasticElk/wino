"""Push the bot's description, short description and command menu to Telegram.

These live on Telegram's servers, not in this repo, so without something like
this the only record of what they say is the bot itself. The description is the
highest-leverage text in the system: it's what an unprovisioned person reads on
the empty chat screen before they tap Start.

The profile picture is deliberately absent — the Bot API has no method for it;
it can only be set through @BotFather with /setuserpic.

    python manage.py set_bot_presence [--dry-run]
"""

import requests
from django.core.management.base import BaseCommand, CommandError

from wine_cellar.apps.scanner.models import ServiceConfig

DESCRIPTION = (
    "I look after your wine cellar. Send me a photo of a bottle and I'll add it "
    "with tasting notes and a price. Send me a photo of a wine list and I'll "
    "tell you what to order."
)

SHORT_DESCRIPTION = (
    "Photograph a bottle to add it, or a wine list to get a recommendation."
)

# Everyday commands only. /adduser, /removeuser, /pending and /flush still work
# by typing; they'd just be noise in the menu for people who never use them.
COMMANDS = [
    ("fridge", "What's in the cellar"),
    ("search", "Find a wine by name"),
    ("stock", "Browse by type, country or grape"),
    ("status", "Inventory summary"),
    ("storages", "Where wine is kept"),
    ("household", "Your household and members"),
    ("switch", "Switch household"),
    ("help", "Everything I can do"),
]


class Command(BaseCommand):
    help = "Set the Telegram bot's description and command menu."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would be sent without changing anything.",
        )

    def handle(self, *args, **options):
        try:
            token = ServiceConfig.objects.get(key="bot_token").value
        except ServiceConfig.DoesNotExist:
            raise CommandError("ServiceConfig 'bot_token' is not set")

        payloads = [
            ("setMyDescription", {"description": DESCRIPTION}),
            ("setMyShortDescription", {"short_description": SHORT_DESCRIPTION}),
            ("setMyCommands", {
                "commands": [{"command": c, "description": d} for c, d in COMMANDS],
            }),
        ]

        if options["dry_run"]:
            for method, payload in payloads:
                self.stdout.write(f"{method}: {payload}")
            self.stdout.write(self.style.WARNING("dry run — nothing sent"))
            return

        base = f"https://api.telegram.org/bot{token}"
        for method, payload in payloads:
            response = requests.post(f"{base}/{method}", json=payload, timeout=15)
            if not response.ok:
                raise CommandError(f"{method} failed: {response.text[:200]}")
            self.stdout.write(self.style.SUCCESS(f"{method} OK"))
