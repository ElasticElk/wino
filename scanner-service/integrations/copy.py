"""Bot dialogue catalog.

Slash commands and the Telegram / menu stay English. Message copy (cards,
buttons on messages, DMs, errors) is keyed here so a third locale is a
catalog addition, not a handler rewrite.

English is the source of truth. Missing locale or key falls back to English.
"""

from __future__ import annotations

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "invite.join.share": (
            "Join {name} on Whiney.\n\n"
            "Tap this, then Start:\n"
            "{link}\n\n"
            "Forward this, or tap Share.\n\n"
            "Works until {date}, one use."
        ),
        "invite.share.btn": "Share invite",
        "invite.expiry.fallback": "One use, lasts 30 days.",
        "invite.mint.fail": "Couldn't make that invite. Try again in a minute.",
        "invite.mint.bad_hh": "Couldn't make that invite.",
        "invite.fresh": (
            "That invite has expired or was already used. Ask them for a fresh link."
        ),
        "invite.unreachable": (
            "Couldn't reach the cellar just now — tap the link again in a minute."
        ),
    },
    "af": {
        "invite.join.share": (
            "Sluit by {name} aan op Whiney.\n\n"
            "Tik hier, dan Start:\n"
            "{link}\n\n"
            "Stuur aan, of tik Deel.\n\n"
            "Werk tot {date}, een keer."
        ),
        "invite.share.btn": "Deel uitnodiging",
        "invite.expiry.fallback": "Een keer, hou 30 dae.",
        "invite.mint.fail": (
            "Kon nie daai uitnodiging maak nie. Probeer oor 'n minuut."
        ),
        "invite.mint.bad_hh": "Kon nie daai uitnodiging maak nie.",
        "invite.fresh": (
            "Daai uitnodiging het verval of is al gebruik. Vra vir 'n nuwe skakel."
        ),
        "invite.unreachable": (
            "Kon nie die kelder nou bereik nie — tik die skakel oor 'n minuut weer."
        ),
    },
}


def normalize_locale(value: str | None) -> str:
    """Map UserSettings.language / Telegram language_code onto a catalog locale.

    en* → en, af* → af. Anything else (de-DE, fr-FR, unset) → en until that
    locale is added to MESSAGES.
    """
    if not isinstance(value, str):
        return "en"
    raw = value.strip().lower().replace("_", "-")
    if raw.startswith("af"):
        return "af"
    if raw.startswith("en"):
        return "en"
    return "en"


def locale_from_telegram(language_code) -> str:
    return normalize_locale(language_code if isinstance(language_code, str) else None)


def t(locale: str, key: str, **kwargs) -> str:
    loc = normalize_locale(locale)
    catalog = MESSAGES.get(loc) or MESSAGES["en"]
    template = catalog.get(key) or MESSAGES["en"].get(key) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def catalog_keys(locale: str = "en") -> set[str]:
    return set(MESSAGES.get(normalize_locale(locale), {}))


def missing_af_keys() -> set[str]:
    return catalog_keys("en") - catalog_keys("af")
