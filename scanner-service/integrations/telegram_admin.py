"""Staff-only Telegram admin surface.

Lives in this bot for now (scoped to staff chat IDs). A later admin bot can
import this module, point it at a second token, and drop the public handlers.
"""

from __future__ import annotations

import html
import logging

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update
from telegram.ext import CommandHandler, ContextTypes, filters

from integrations.telegram_commands import admin_commands, menu_commands

logger = logging.getLogger(__name__)


def parse_newhousehold_args(args: list[str]) -> tuple[str, str, str] | None:
    """Split `/newhousehold <name> <chat_id> <owner>` even when either has spaces.

    The chat ID is the *last* all-digit token so a name like "Cellar 2" still
    parses. Owner name is everything after that token.
    """
    digit_idxs = [i for i, a in enumerate(args) if a.lstrip("-").isdigit()]
    if not digit_idxs:
        return None
    chat_idx = digit_idxs[-1]
    if chat_idx == 0 or chat_idx == len(args) - 1:
        return None
    household = " ".join(args[:chat_idx]).strip()
    chat_id = args[chat_idx]
    owner = " ".join(args[chat_idx + 1:]).strip()
    if not household or not owner:
        return None
    return household, chat_id, owner


def parse_admin_removeuser_args(args: list[str]) -> tuple[str, str] | None:
    """Split `/removeuser <household> <chat_id>` — household may contain spaces."""
    digit_idxs = [i for i, a in enumerate(args) if a.lstrip("-").isdigit()]
    if not digit_idxs:
        return None
    chat_idx = digit_idxs[-1]
    if chat_idx == 0 or chat_idx != len(args) - 1:
        return None
    household = " ".join(args[:chat_idx]).strip()
    if not household:
        return None
    return household, args[chat_idx]


def _named_household(api, name: str) -> dict | None:
    needle = name.strip().lower()
    try:
        households = api.list_households() or []
    except Exception:
        return None
    return next(
        (h for h in households if (h.get("name") or "").lower() == needle),
        None,
    )


def register_admin_handlers(app, handler) -> None:
    """Attach staff commands. No-op if nobody is staff — guests never see these."""
    admin_ids = [int(cid) for cid in getattr(handler, "admin_chat_ids", []) if str(cid).lstrip("-").isdigit()]
    if not admin_ids:
        logger.info("[admin] no staff chat IDs — admin commands not registered")
        return
    admin_filter = filters.Chat(chat_id=admin_ids)
    handler._admin_filter = admin_filter

    def bind(fn):
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
            return await fn(update, context, handler)
        wrapped.__name__ = fn.__name__
        return wrapped

    app.add_handler(CommandHandler("admin", bind(cmd_admin), filters=admin_filter))
    app.add_handler(CommandHandler("stats", bind(cmd_stats), filters=admin_filter))
    app.add_handler(CommandHandler("chatstats", bind(cmd_chatstats), filters=admin_filter))
    app.add_handler(CommandHandler("households", bind(cmd_households), filters=admin_filter))
    app.add_handler(CommandHandler("members", bind(cmd_members), filters=admin_filter))
    app.add_handler(CommandHandler("whois", bind(cmd_whois), filters=admin_filter))
    app.add_handler(CommandHandler("newhousehold", bind(cmd_newhousehold), filters=admin_filter))
    app.add_handler(CommandHandler("adduser", bind(cmd_adduser), filters=admin_filter))
    app.add_handler(CommandHandler("removeuser", bind(cmd_removeuser), filters=admin_filter))
    app.add_handler(CommandHandler("welcome", bind(cmd_welcome), filters=admin_filter))
    app.add_handler(CommandHandler("clubpost", bind(cmd_clubpost), filters=admin_filter))
    logger.info(f"[admin] staff commands registered for {len(admin_ids)} chat(s)")


async def push_command_menus(bot, admin_chat_ids: list[str], flags: dict | None = None) -> None:
    """Default menu for everyone; public + admin for staff chats only."""
    public_cmds = menu_commands(flags)
    public = [BotCommand(c, d) for c, d in public_cmds]
    staff = [BotCommand(c, d) for c, d in public_cmds + admin_commands(flags)]
    try:
        await bot.set_my_commands(public, scope=BotCommandScopeDefault())
        logger.info("[admin] default command menu pushed")
    except Exception as e:
        logger.warning(f"[admin] could not push default command menu: {e}")
        return
    for cid in admin_chat_ids:
        try:
            await bot.set_my_commands(
                staff, scope=BotCommandScopeChat(chat_id=int(cid)),
            )
            logger.info(f"[admin] staff command menu pushed for {cid}")
        except Exception as e:
            logger.warning(f"[admin] could not push staff menu for {cid}: {e}")


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    lines = [
        "\U0001F6E0 <b>Staff tools</b>\n",
        "These only appear in your / menu. Guests never see them.",
        "",
    ]
    flags = getattr(handler, "flags", None) or {}
    for command, description in admin_commands(flags):
        if command == "admin":
            continue
        lines.append(f"/{command} \u2014 {description}")
    lines.append(
        "\nHousehold is required on add/remove — these are not scoped "
        "to your own cellar.\n"
        "<code>/invite new</code> — forwardable new-cellar link\n"
        "<code>/newhousehold Riverside 123456789 Alex</code>\n"
        "<code>/adduser Riverside 123456789 Alex</code>\n"
        "<code>/removeuser Riverside 123456789</code>\n"
        "<code>/members Riverside</code>\n"
        "<code>/whois Alex</code>\n"
        "<code>/welcome 123456789</code>"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_households(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    api = handler._get_api(update)
    try:
        households = api.list_households()
    except Exception as e:
        await update.message.reply_text(f"Could not list households: {e}")
        return
    if not households:
        await update.message.reply_text("No households yet. /newhousehold to create one.")
        return
    lines = ["\U0001F3E0 <b>Households</b>\n"]
    for h in households:
        line = f"\u2022 {h.get('name')} <code>#{h.get('id')}</code>"
        members = h.get("member_count")
        wines = h.get("wine_count")
        bottles = h.get("bottle_count")
        bits = []
        if members is not None:
            bits.append(f"{members} people")
        if wines is not None:
            bits.append(f"{wines} wines")
        if bottles is not None:
            bits.append(f"{bottles} bottles")
        if bits:
            line += " \u2014 " + ", ".join(bits)
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_newhousehold(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    parsed = parse_newhousehold_args(context.args or [])
    if not parsed:
        await update.message.reply_text(
            "Usage: /newhousehold &lt;household_name&gt; &lt;chat_id&gt; &lt;owner_name&gt;\n\n"
            "The owner must have tapped <b>Start</b> on this bot first "
            "(so we can welcome them).\n\n"
            "Example: <code>/newhousehold Riverside 123456789 Alex</code>",
            parse_mode="HTML",
        )
        return
    household_name, chat_id, owner_name = parsed
    api = handler._get_api(update)
    try:
        result = api.create_household(household_name, chat_id, owner_name)
    except Exception as e:
        await update.message.reply_text(f"Error creating household: {e}")
        return
    if "error" in result:
        await update.message.reply_text(f"\u26a0\ufe0f {result['error']}")
        return

    household = result.get("household") or {}
    household_id = household.get("id")
    household_label = household.get("name") or household_name
    new_api = None
    if handler._api_factory and household_id:
        try:
            new_api = handler._api_factory(chat_id, household_id)
        except Exception:
            new_api = None
    handler.add_chat_id(chat_id, api_client=new_api or api)
    delivered = await handler._welcome_new_member(
        context, chat_id, owner_name, household_label,
    )
    text = (
        f"\u2705 <b>{household_label}</b> created\n"
        f"Owner: {owner_name} (<code>{chat_id}</code>)"
    )
    username = (result.get("owner") or {}).get("username")
    if username:
        text += f"\nUsername: <code>{username}</code>"
    if delivered:
        text += "\n\n\U0001F4EC They've been welcomed and can add wine from a photo."
    else:
        text += (
            "\n\n\u26A0\uFE0F I couldn't message them — they haven't opened "
            "the bot yet. Ask them to search for this bot and tap "
            "<b>Start</b>, then they're good to go."
        )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    """Staff: add a member to a *named* household. Owners have a separate handler."""
    parsed = parse_newhousehold_args(context.args or [])
    if not parsed:
        await update.message.reply_text(
            "Usage: /adduser &lt;household&gt; &lt;chat_id&gt; &lt;name&gt;\n\n"
            "This is the global staff command — it will not guess your cellar. "
            "Household owners add to their own cellar with the same command, "
            "without the household name.\n\n"
            "Example: <code>/adduser Riverside 123456789 Alex</code>",
            parse_mode="HTML",
        )
        return
    household_name, chat_id, display_name = parsed
    api = handler._get_api(update)
    household = _named_household(api, household_name)
    if not household:
        await update.message.reply_text(
            f"\u26a0\ufe0f No household called <b>{household_name}</b>. "
            f"/households to list them.",
            parse_mode="HTML",
        )
        return
    household_id = household.get("id")
    try:
        result = api.add_household_member(
            chat_id, display_name, household_id=household_id,
        )
    except Exception as e:
        await update.message.reply_text(f"Error adding member: {e}")
        return
    if "error" in result:
        await update.message.reply_text(f"\u26a0\ufe0f {result['error']}")
        return

    new_api = None
    if handler._api_factory and household_id:
        try:
            new_api = handler._api_factory(chat_id, household_id)
        except Exception:
            new_api = None
    handler.add_chat_id(chat_id, api_client=new_api or api)
    household_label = result.get("household") or household.get("name") or household_name
    delivered = await handler._welcome_new_member(
        context, chat_id, display_name, household_label,
    )
    if result.get("status") == "created":
        text = (
            f"\u2705 <b>New user created:</b> {display_name}\n"
            f"Added to <b>{household_label}</b>"
        )
        if result.get("username"):
            text += f"\nUsername: <code>{result['username']}</code>"
    else:
        name = result.get("display_name") or display_name
        text = f"\u2705 <b>{name}</b> added to <b>{household_label}</b>"
    if delivered:
        text += "\n\n\U0001F4EC They've been welcomed and can add wine from a photo."
    else:
        text += (
            "\n\n\u26A0\uFE0F I couldn't message them — they haven't opened "
            "the bot yet. Ask them to search for this bot and tap "
            "<b>Start</b>, then they're good to go."
        )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    """Staff: remove a member from a *named* household."""
    parsed = parse_admin_removeuser_args(context.args or [])
    if not parsed:
        await update.message.reply_text(
            "Usage: /removeuser &lt;household&gt; &lt;chat_id&gt;\n\n"
            "This is the global staff command — it will not guess your cellar.\n\n"
            "Example: <code>/removeuser Riverside 123456789</code>",
            parse_mode="HTML",
        )
        return
    household_name, chat_id = parsed
    api = handler._get_api(update)
    household = _named_household(api, household_name)
    if not household:
        await update.message.reply_text(
            f"\u26a0\ufe0f No household called <b>{household_name}</b>. "
            f"/households to list them.",
            parse_mode="HTML",
        )
        return
    try:
        result = api.remove_household_member(
            chat_id, household_id=household.get("id"),
        )
    except Exception as e:
        await update.message.reply_text(f"Error removing member: {e}")
        return
    if "error" in result:
        await update.message.reply_text(f"\u26a0\ufe0f {result['error']}")
        return
    await update.message.reply_text(
        f"\u2705 <b>{result.get('display_name') or chat_id}</b> removed from "
        f"<b>{result.get('household') or household_name}</b>",
        parse_mode="HTML",
    )


def _format_members(household_name: str, members: list[dict]) -> str:
    lines = [f"\U0001F3E0 <b>{household_name}</b>\n"]
    if not members:
        lines.append("No members yet.")
        return "\n".join(lines)
    for m in members:
        role_icon = "\U0001F451" if m.get("role") == "owner" else "\U0001F464"
        name = m.get("telegram_name") or m.get("username") or "unknown"
        line = f"  {role_icon} {name}"
        if m.get("telegram_chat_id"):
            line += f" (<code>{m['telegram_chat_id']}</code>)"
        if m.get("role"):
            line += f" \u2014 {m['role']}"
        lines.append(line)
    return "\n".join(lines)


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    name = " ".join(context.args or []).strip()
    if not name:
        await update.message.reply_text(
            "Usage: /members &lt;household&gt;\n\n"
            "Example: <code>/members Riverside</code>",
            parse_mode="HTML",
        )
        return
    api = handler._get_api(update)
    household = _named_household(api, name)
    if not household:
        await update.message.reply_text(
            f"\u26a0\ufe0f No household called <b>{name}</b>. /households to list them.",
            parse_mode="HTML",
        )
        return
    try:
        data = api.get_household(household_id=household.get("id"))
    except Exception as e:
        await update.message.reply_text(f"Could not load members: {e}")
        return
    await update.message.reply_text(
        _format_members(data.get("name") or name, data.get("members") or []),
        parse_mode="HTML",
    )


async def cmd_whois(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text(
            "Usage: /whois &lt;chat_id or name&gt;\n\n"
            "Example: <code>/whois Alex</code> or <code>/whois 123456789</code>",
            parse_mode="HTML",
        )
        return
    api = handler._get_api(update)
    try:
        payload = api.whois(query)
    except Exception as e:
        await update.message.reply_text(f"Lookup failed: {e}")
        return
    results = payload.get("results") or []
    if not results:
        await update.message.reply_text(f"No Telegram user matching <b>{query}</b>.", parse_mode="HTML")
        return
    lines = ["\U0001F50D <b>Who is</b>\n"]
    for person in results:
        name = person.get("display_name") or person.get("username") or "unknown"
        lines.append(f"<b>{name}</b> (<code>{person.get('chat_id')}</code>)")
        if person.get("username"):
            lines.append(f"  username: <code>{person['username']}</code>")
        flags = []
        if person.get("is_staff"):
            flags.append("staff")
        if person.get("is_active") is False:
            flags.append("inactive")
        if flags:
            lines.append("  " + ", ".join(flags))
        households = person.get("households") or []
        if not households:
            lines.append("  not in any household")
        for h in households:
            role = h.get("role") or "member"
            default = " (default)" if h.get("is_default") else ""
            lines.append(f"  \u2022 {h.get('name')} \u2014 {role}{default}")
        lines.append("")
    await update.message.reply_text("\n".join(lines).rstrip(), parse_mode="HTML")


async def cmd_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    args = context.args or []
    if len(args) != 1 or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "Usage: /welcome &lt;chat_id&gt;\n\n"
            "Resends the welcome. They must have tapped <b>Start</b> first.\n\n"
            "Example: <code>/welcome 123456789</code>",
            parse_mode="HTML",
        )
        return
    chat_id = args[0]
    api = handler._get_api(update)
    display_name = "there"
    household_name = "Wine Cellar"
    try:
        payload = api.whois(chat_id)
        match = (payload.get("results") or [None])[0]
    except Exception:
        match = None
    if match:
        display_name = match.get("display_name") or match.get("username") or display_name
        households = match.get("households") or []
        default = next((h for h in households if h.get("is_default")), None)
        household_name = (default or (households[0] if households else {})).get("name") or household_name
    delivered = await handler._welcome_new_member(
        context, chat_id, display_name, household_name,
    )
    if delivered:
        await update.message.reply_text(
            f"\U0001F4EC Welcomed {display_name} (<code>{chat_id}</code>) "
            f"to <b>{household_name}</b>.",
            parse_mode="HTML",
        )
        return
    await update.message.reply_text(
        "\u26A0\uFE0F I couldn't message them — they haven't opened "
        "the bot yet. Ask them to search for this bot and tap <b>Start</b>.",
        parse_mode="HTML",
    )


def format_chat_stats(data: dict) -> str:
    asks = data.get("asks", 0)
    days = data.get("days", 30)
    if not asks:
        return f"💬 <b>Chat suggestions</b> — last {days} days\n\nNo asks yet."
    outcomes = data.get("outcomes") or {}
    votes = data.get("pick_votes") or {}
    latency = data.get("latency_ms") or {}
    shown = data.get("shown", 0)

    def pct(n):
        return f"{round(100 * n / shown)}%" if shown else "—"

    lines = [
        f"💬 <b>Chat suggestions</b> — last {days} days\n",
        f"Asks: <b>{asks}</b> ({shown} got a bottle, {data.get('questions', 0)} asked a question first)",
        "",
        "<b>What they tapped</b>",
        f"  That's the one: {outcomes.get('accepted', 0)} ({pct(outcomes.get('accepted', 0))})",
        f"  Something else taps: {data.get('something_else_taps', 0)}",
        f"  Gave up on the list: {outcomes.get('rejected', 0)}",
        f"  Walked away: {outcomes.get('abandoned', 0)}",
        f"  No tap: {outcomes.get('pending', 0)}",
        "",
        "<b>What they drank (within 48h)</b>",
        f"  The suggested bottle: <b>{data.get('opened_pick', 0)}</b> ({data.get('opened_pick_pct', 0)}%)",
        f"  A different bottle: {data.get('opened_other', 0)}",
        f"  Votes on picks: 👍 {votes.get('up', 0)}  — {votes.get('meh', 0)}  👎 {votes.get('down', 0)}"
        f"  (no vote {votes.get('none', 0)})",
        "",
        "<b>Speed</b>",
        f"  Median {_seconds(latency.get('p50'))}, slowest 10% {_seconds(latency.get('p90'))}",
        f"  Quick picks (model unavailable): {data.get('fallback', 0)}",
    ]
    models = data.get("models") or {}
    if models:
        lines.append("  Models: " + ", ".join(f"{html.escape(k, quote=False)} {v}" for k, v in models.items()))
    worst = data.get("worst") or []
    if worst:
        lines += ["", "<b>Worst asks</b>"]
        for row in worst:
            sentence = html.escape(row.get("sentence") or "", quote=False)
            why = html.escape(", ".join(row.get("why") or []), quote=False)
            hh = html.escape(row.get("household") or "", quote=False)
            lines.append(f"  • “{sentence}” — {why} <i>({hh}, {(row.get('created') or '')[:10]})</i>")
    return "\n".join(lines)


def _seconds(ms) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f}s"


async def cmd_clubpost(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    await handler._club_flow().cmd_clubpost(update, context)


async def cmd_chatstats(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    args = context.args or []
    days = 30
    if args and args[0].isdigit():
        days = int(args[0])
    api = getattr(handler, "admin_api", None) or handler._get_api(update)
    try:
        data = api.chat_stats(days)
    except Exception as e:
        logger.warning(f"[admin] chat stats failed: {e}")
        await update.message.reply_text("Couldn't load chat stats just now.")
        return
    await update.message.reply_text(format_chat_stats(data), parse_mode="HTML")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, handler) -> None:
    api = handler._get_api(update)
    try:
        data = api.stats()
    except Exception as e:
        await update.message.reply_text(f"Could not load stats: {e}")
        return
    lines = [
        "\U0001F4CA <b>All cellars</b>\n",
        f"Households: <b>{data.get('households', 0)}</b>",
        f"Telegram users: <b>{data.get('telegram_users', 0)}</b>",
        f"Wines: <b>{data.get('wines', 0)}</b>",
        f"Bottles in stock: <b>{data.get('bottles', 0)}</b>",
        (
            f"Lists: <b>{data.get('lists_open', 0)}</b> items "
            f"({data.get('lists_moved', 0)} moved)"
        ),
        (
            f"Clubs: <b>{data.get('clubs', 0)}</b> "
            f"({data.get('club_members', 0)} members), "
            f"feed events 7d: {data.get('feed_events_7d', 0)}"
        ),
    ]
    scanners = data.get("scanners") or []
    if scanners:
        lines.append("\n<b>Scanners</b>")
        for sc in scanners:
            mark = "" if sc.get("is_active", True) else " (inactive)"
            hh = sc.get("household") or "no household"
            lines.append(f"  \u2022 {sc.get('name')} \u2014 {hh}{mark}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
