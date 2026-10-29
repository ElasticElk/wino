"""EN/AF catalog for Celery DMs. Mirrors docs/wine-club/copy-catalog.md.

The scanner service has its own copy.py; gunicorn/celery must not import it.
Keep keys in sync when adding a server-sent club message.
"""

MESSAGES = {
    "en": {
        "club.feed.opened": "{name} opened a {vintage} {wine}",
        "club.feed.put_back": "{name} put the {vintage} {wine} back.",
        "club.feed.joined": "{name} joined",
        "club.hh.notice": (
            "Just so you know — this cellar is now shared\n"
            "inside a wine club.\n\n"
            "Only people in the club can see it, and only\n"
            "what the club allows. Whiney never shares a\n"
            "cellar any other way."
        ),
        "club.widen.body": (
            "{club} now shares the {level}.\n\n"
            "You already share a {old}. Stay to share the new level, or leave."
        ),
        "club.widen.stay": "Stay",
        "club.widen.leave": "Leave",
        "club.share.summary": "Type and count",
        "club.share.full": "Full cellar",
        "club.share.none": "Drink feed only",
        "club.end.ballot": (
            "{name} wants to end {club}.\n\n"
            "If everyone says yes, the club and its feed go away. "
            "If anyone says no, or doesn't reply in time, nothing changes."
        ),
        "club.end.yes": "Yes, end it",
        "club.end.no": "No, keep it",
        "club.end.status": (
            "Ending {club} — waiting on {n} votes.\n"
            "Yes: {yes_n} · No: {no_n}\n"
            "Waiting: {waiting}\n\n"
            "Closes {date} if not everyone has voted."
        ),
        "club.end.rejected": "{name} voted to keep {club}. The club stays.",
        "club.end.passed": "{club} has ended.",
        "club.end.expired": "Not everyone voted in time. {club} stays.",
        "club.end.cancelled": "Cancelled. {club} stays.",
        "club.end.cancel": "Cancel vote",
    },
    "af": {
        "club.feed.opened": "{name} het 'n {vintage} {wine} oopgemaak",
        "club.feed.put_back": "{name} het die {vintage} {wine} teruggesit.",
        "club.feed.joined": "{name} het aangesluit",
        "club.hh.notice": (
            "Net gou — hierdie kelder word nou binne 'n\n"
            "wynklub gedeel.\n\n"
            "Net mense in die klub kan dit sien, en net\n"
            "wat die klub toelaat. Whiney deel nooit 'n\n"
            "kelder op 'n ander manier nie."
        ),
        "club.widen.body": (
            "{club} deel nou die {level}.\n\n"
            "Jy deel al 'n {old}. Bly om die nuwe vlak te deel, of loop."
        ),
        "club.widen.stay": "Bly",
        "club.widen.leave": "Loop",
        "club.share.summary": "Tipe en aantal",
        "club.share.full": "Vol kelder",
        "club.share.none": "Net die drink-lys",
        "club.end.ballot": (
            "{name} wil {club} beëindig.\n\n"
            "As almal ja sê, verdwyn die klub en sy lys. "
            "As iemand nee sê, of nie betyds antwoord nie, verander niks."
        ),
        "club.end.yes": "Ja, beëindig dit",
        "club.end.no": "Nee, hou dit",
        "club.end.status": (
            "Beëindig {club} — wag op {n} stemme.\n"
            "Ja: {yes_n} · Nee: {no_n}\n"
            "Wag: {waiting}\n\n"
            "Sluit {date} as nie almal gestem het nie."
        ),
        "club.end.rejected": "{name} het gestem om {club} te hou. Die klub bly.",
        "club.end.passed": "{club} is beëindig.",
        "club.end.expired": "Nie almal het betyds gestem nie. {club} bly.",
        "club.end.cancelled": "Gekanselleer. {club} bly.",
        "club.end.cancel": "Kanselleer stemming",
    },
}

VOTE_GLYPH = {"down": "\U0001F44E", "up": "\U0001F44D", "meh": ""}


def locale_for_user(user) -> str:
    settings = getattr(user, "user_settings", None)
    language = ""
    if settings is not None:
        language = getattr(settings, "language", "") or ""
    raw = str(language).strip().lower().replace("_", "-")
    if raw.startswith("af"):
        return "af"
    return "en"


def t(locale: str, key: str, **kwargs) -> str:
    loc = "af" if str(locale or "").lower().startswith("af") else "en"
    catalog = MESSAGES.get(loc) or MESSAGES["en"]
    template = catalog.get(key) or MESSAGES["en"].get(key) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def wine_phrase(vintage, wine_name) -> str:
    name = (wine_name or "wine").strip() or "wine"
    if vintage:
        return f"{vintage} {name}"
    return name


def opened_text(locale, actor_name, vintage, wine_name, vote="") -> str:
    phrase = wine_phrase(vintage, wine_name)
    body = t(
        locale, "club.feed.opened",
        name=actor_name, vintage="", wine=phrase,
    )
    body = " ".join(body.split())
    glyph = VOTE_GLYPH.get(vote or "", "")
    line = f"\U0001F377 {body}"
    if glyph:
        line = f"{line} {glyph}"
    return line


def put_back_text(locale, actor_name, vintage, wine_name) -> str:
    phrase = wine_phrase(vintage, wine_name)
    body = t(
        locale, "club.feed.put_back",
        name=actor_name, vintage="", wine=phrase,
    )
    return " ".join(body.split())
