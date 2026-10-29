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

from wine_cellar.apps.scanner.flags import flag_on
from wine_cellar.apps.scanner.models import ServiceConfig, TelegramUser

DESCRIPTION = (
    "I look after your wine cellar. A friend can send you a link to join theirs, "
    "or to start your own. Then send a photo of a bottle and I'll add it — "
    "or a wine list and I'll tell you what to order."
)

SHORT_DESCRIPTION = (
    "Photograph a bottle to add it, or a wine list to get a recommendation."
)

# Everyday commands only. /adduser, /removeuser, /pending and /flush still work
# by typing; they'd just be noise in the menu for people who never use them.
# Keep in sync with scanner-service/integrations/telegram_commands.py
COMMANDS = [
    ("fridge", "What's in the cellar"),
    ("recent", "Recent bottles"),
    ("search", "Find a wine by name"),
    ("stock", "Browse by type, country or grape"),
    ("status", "Inventory summary"),
    ("storages", "Where wine is kept"),
    ("household", "Your household and members"),
    ("switch", "Switch household"),
    ("invite", "Invite someone to your cellar"),
    ("help", "Everything I can do"),
]

# Shown only in staff chats via BotCommandScopeChat. Telegram has no categories;
# this is a second list appended for those chat IDs.
ADMIN_COMMANDS = [
    ("admin", "Staff tools"),
    ("stats", "Every cellar at a glance"),
    ("households", "List every household"),
    ("members", "Members of a named household"),
    ("whois", "Find someone by name or chat ID"),
    ("newcellar", "Send a link to start a new cellar"),
    ("newhousehold", "Create a household and its owner"),
    ("adduser", "Add a member to a named household"),
    ("removeuser", "Remove a member from a named household"),
    ("welcome", "Resend the welcome to a chat ID"),
    ("clubpost", "Post to a global club"),
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

        commands = list(COMMANDS)
        if flag_on("feature_lists"):
            after = next(
                (i for i, (name, _) in enumerate(commands) if name == "recent"),
                0,
            )
            commands.insert(after + 1, ("list", "Things to drink or buy"))
        if flag_on("feature_clubs"):
            after = next(
                (i for i, (name, _) in enumerate(commands) if name == "invite"),
                len(commands) - 1,
            )
            commands.insert(after + 1, ("club", "Wine clubs"))
        public = [{"command": c, "description": d} for c, d in commands]
        staff = [
            {"command": c, "description": d}
            for c, d in commands + ADMIN_COMMANDS
        ]
        staff_chat_ids = list(
            TelegramUser.objects.filter(
                is_active=True, user__is_staff=True,
            ).values_list("chat_id", flat=True)
        )
        payloads = [
            ("setMyDescription", {"description": DESCRIPTION}),
            ("setMyShortDescription", {"short_description": SHORT_DESCRIPTION}),
            ("setMyCommands", {"commands": public}),
        ]
        for chat_id in staff_chat_ids:
            try:
                scoped_id = int(chat_id)
            except (TypeError, ValueError):
                continue
            payloads.append((
                "setMyCommands",
                {
                    "commands": staff,
                    "scope": {"type": "chat", "chat_id": scoped_id},
                },
            ))

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
            scope = payload.get("scope") or {}
            extra = f" chat {scope.get('chat_id')}" if scope else ""
            self.stdout.write(self.style.SUCCESS(f"{method}{extra} OK"))
