"""Telegram slash-menu catalogues.

Telegram cannot put commands in categories — the / menu is a flat list. Staff
see extra commands because we set a per-chat command scope for their chat ID,
not because Telegram has an "admin menu". Keep this module the source of truth
so a later admin bot can import the same lists.
"""

# Everyday commands. Pushed as the default menu for every chat.
PUBLIC_COMMANDS = [
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

# Extra commands, shown only in staff chats (appended after the public list).
# Household owners still type /adduser and /removeuser for *their* cellar;
# the staff versions of those names require a household argument.
ADMIN_COMMANDS = [
    ("admin", "Staff tools"),
    ("stats", "Every cellar at a glance"),
    ("chatstats", "How chat suggestions are landing"),
    ("households", "List every household"),
    ("members", "Members of a named household"),
    ("whois", "Find someone by name or chat ID"),
    ("newcellar", "Send a link to start a new cellar"),
    ("newhousehold", "Create a household and its owner"),
    ("adduser", "Add a member to a named household"),
    ("removeuser", "Remove a member from a named household"),
    ("welcome", "Resend the welcome to a chat ID"),
]


LIST_COMMAND = ("list", "Things to drink or buy")
CLUB_COMMAND = ("club", "Wine clubs")


def menu_commands(flags: dict | None = None) -> list[tuple[str, str]]:
    """Public / menu. Flagged commands are omitted until their flag is on."""
    commands = list(PUBLIC_COMMANDS)
    if flags and flags.get("feature_lists"):
        after = next(
            (i for i, (name, _) in enumerate(commands) if name == "recent"),
            0,
        )
        commands.insert(after + 1, LIST_COMMAND)
    if flags and flags.get("feature_clubs"):
        after = next(
            (i for i, (name, _) in enumerate(commands) if name == "invite"),
            len(commands) - 1,
        )
        commands.insert(after + 1, CLUB_COMMAND)
    return commands


def public_command_payload(flags: dict | None = None) -> list[dict]:
    return [{"command": c, "description": d} for c, d in menu_commands(flags)]


def staff_command_payload(flags: dict | None = None) -> list[dict]:
    return [
        {"command": c, "description": d}
        for c, d in menu_commands(flags) + ADMIN_COMMANDS
    ]
