"""EN/AF catalog for Celery DMs. Mirrors docs/wine-club/copy-catalog.md.

The scanner service has its own copy.py; gunicorn/celery must not import it.
Keep keys in sync when adding a server-sent club message.
"""

import html

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
        "club.comment.btn": "Say something",
        "club.comment.more": "and {n} more",
        "club.td.btn": "To-drink list",
        "club.to.running": (
            "{host} is running a taste-off: {title}. Score 1–5."
        ),
        "club.to.closed": "Tasting closed.",
        "club.to.score": "How was it? 1 (low) – 5 (high)",
        "club.to.n_scores": "{n} scores",
        "club.to.close": "Close tasting",
        "club.to.host_open": "Scoring is open. Close when you're done.",
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
        "club.comment.btn": "Sê iets",
        "club.comment.more": "en nog {n}",
        "club.td.btn": "To-drink-lys",
        "club.to.running": (
            "{host} hou 'n proe: {title}. Tel 1–5."
        ),
        "club.to.closed": "Proe is afgesluit.",
        "club.to.score": "Hoe was dit? 1 (swak) – 5 (sterk)",
        "club.to.n_scores": "{n} tellings",
        "club.to.close": "Sluit die proe",
        "club.to.host_open": "Telling is oop. Sluit as julle klaar is.",
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


def comment_card_text(locale, actor_name, vintage, wine_name, vote, comments) -> str:
    """Opened card plus up to 5 escaped comments. `comments` is [(name, text), ...]."""
    head = opened_text(locale, actor_name, vintage, wine_name, vote)
    if not comments:
        return head
    lines = [head, ""]
    shown = list(comments)[:5]
    for name, text in shown:
        who = html.escape(name or "")
        note = html.escape(text or "")
        lines.append(f"{who}: {note}")
    extra = len(comments) - 5
    if extra > 0:
        lines.append(t(locale, "club.comment.more", n=extra))
    return "\n".join(lines)


def trophy_text(locale, title, rankings) -> str:
    lines = [f"\U0001F3C6 {html.escape(title or '')}", ""]
    for index, row in enumerate(rankings or [], 1):
        average = row.get("average")
        avg_s = f"{average:.1f}" if isinstance(average, (int, float)) else "—"
        n = row.get("n") or 0
        label = html.escape(row.get("label") or "")
        scores = t(locale, "club.to.n_scores", n=n)
        lines.append(f"{index}. {label} — {avg_s} ({scores})")
    return "\n".join(lines)
