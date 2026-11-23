"""Reactions and Want this on club cards (flag ``feature_club_reactions``).

A tap toggles one ClubReaction row. The owner of the event (its actor) is
told through the ClubNotice outbox (kind=reaction): taps inside the batch
window merge into one row and go out as one quiet message, and an un-react
inside the window removes its line. Members' delivered cards are re-edited
with the new counts by a debounced Celery task (one pass per event per
REFRESH_DEBOUNCE seconds).
"""

from __future__ import annotations

import html
import logging
import uuid

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from wine_cellar.apps.club.copy import t, wine_phrase
from wine_cellar.apps.club.models import (
    ClubEvent,
    ClubNotice,
    ClubReaction,
    ClubWant,
)
from wine_cellar.apps.scanner.flags import flag_on

logger = logging.getLogger(__name__)

PALETTE = ("\U0001F377", "\U0001F60D", "\U0001F44D", "\U0001F602", "\U0001F914")
REFRESH_DEBOUNCE = 30  # seconds between count-edit passes per event


def reactions_enabled() -> bool:
    return flag_on("feature_clubs") and flag_on("feature_club_reactions")


REACTABLE_KINDS = (ClubEvent.KIND_OPENED, ClubEvent.KIND_ASK)


def reactable(event) -> bool:
    return event.kind in REACTABLE_KINDS and not event.retracted


def counts_for(event) -> list[int]:
    rows = ClubReaction.objects.filter(event=event).values_list("emoji", flat=True)
    tally = {e: 0 for e in PALETTE}
    for emoji in rows:
        if emoji in tally:
            tally[emoji] += 1
    return [tally[e] for e in PALETTE]


def mine_for(event, user) -> list[int]:
    have = set(
        ClubReaction.objects.filter(event=event, user=user).values_list(
            "emoji", flat=True,
        )
    )
    return [i for i, e in enumerate(PALETTE) if e in have]


def reaction_row(event, counts=None, with_counts=True) -> list[dict]:
    """The compact emoji row: '🍷 3' when the count is above zero. With
    ``with_counts`` False (reply pings) the bare palette is returned."""
    if not with_counts:
        counts = [0] * len(PALETTE)
    elif counts is None:
        counts = counts_for(event)
    return [
        {
            "text": f"{emoji} {n}" if n else emoji,
            "callback_data": f"cl:rx:{event.id}:{i}",
        }
        for i, (emoji, n) in enumerate(zip(PALETTE, counts))
    ]


def reaction_rows(event, with_counts=True) -> list[list[dict]]:
    """Palette rows for a keyboard ([] when off or not reactable)."""
    if not reactions_enabled() or not reactable(event):
        return []
    return [reaction_row(event, with_counts=with_counts)]


def toggle_reaction(user, event, index) -> bool:
    """Flip the user's reaction. Returns True when it is now on."""
    emoji = PALETTE[index]
    with transaction.atomic():
        existing = ClubReaction.objects.filter(event=event, user=user, emoji=emoji)
        if existing.exists():
            existing.delete()
            on = False
        else:
            try:
                with transaction.atomic():
                    ClubReaction.objects.create(event=event, user=user, emoji=emoji)
                on = True
            except IntegrityError:
                on = True
    queue_owner_notice(event, user, emoji=emoji, on=on)
    schedule_count_refresh(event.id)
    return on


def record_want(user, event) -> bool:
    """Remember a Want this; True the first time (the owner is told once)."""
    try:
        with transaction.atomic():
            _, created = ClubWant.objects.get_or_create(event=event, user=user)
    except IntegrityError:
        created = False
    if created:
        queue_owner_notice(event, user, want=True)
    return created


# -- owner notices -----------------------------------------------------------
def _apply(payload, uid, emoji, on, want):
    rx = {k: list(v) for k, v in (payload.get("rx") or {}).items()}
    wants = list(payload.get("wants") or [])
    key = str(uid)
    if want:
        if uid not in wants:
            wants.append(uid)
    elif emoji:
        have = rx.get(key, [])
        if on and emoji not in have:
            have.append(emoji)
        elif not on and emoji in have:
            have.remove(emoji)
        if have:
            rx[key] = have
        else:
            rx.pop(key, None)
    return {"rx": rx, "wants": wants}


def queue_owner_notice(event, user, emoji=None, on=True, want=False) -> None:
    """Merge a reaction / want into the owner's pending notice. Never to the
    tapper themself and never to anyone else."""
    owner_id = event.actor_id
    if not reactions_enabled() or not owner_id or owner_id == user.id:
        return
    from wine_cellar.apps.club.threads import BATCH_WINDOW, _schedule_flush

    for _ in range(2):
        try:
            with transaction.atomic():
                notice = (
                    ClubNotice.objects.select_for_update()
                    .filter(
                        recipient_id=owner_id, event=event,
                        kind=ClubNotice.KIND_REACTION, sent_at__isnull=True,
                    )
                    .first()
                )
                if notice is not None:
                    notice.payload = _apply(
                        notice.payload or {}, user.id, emoji, on, want,
                    )
                    if not notice.payload["rx"] and not notice.payload["wants"]:
                        notice.delete()
                    else:
                        notice.save(update_fields=["payload"])
                    return
                if not on and not want:
                    return  # nothing pending to take the line out of
                ClubNotice.objects.create(
                    recipient_id=owner_id,
                    club=event.club,
                    event=event,
                    kind=ClubNotice.KIND_REACTION,
                    dedupe_key=f"rx:{event.id}:{owner_id}:{uuid.uuid4().hex[:12]}",
                    payload=_apply({}, user.id, emoji, on, want),
                    due_at=timezone.now() + BATCH_WINDOW,
                )
            _schedule_flush()
            return
        except IntegrityError:
            continue  # a concurrent tap made the pending row first: merge


def _names(loc, names: list[str]) -> str:
    names = [html.escape(n) for n in names]
    if len(names) == 1:
        return names[0]
    if len(names) <= 3:
        return f"{', '.join(names[:-1])} {t(loc, 'club.rx.and')} {names[-1]}"
    rest = len(names) - 2
    more = t(loc, "club.rx.others", n=rest)
    return f"{names[0]}, {names[1]} {t(loc, 'club.rx.and')} {more}"


def render_notice(notice, loc) -> str:
    """Quiet owner message for a pending reaction notice ('' when empty)."""
    from django.contrib.auth import get_user_model

    from wine_cellar.apps.club.services import display_name

    event = notice.event
    if event.kind == ClubEvent.KIND_ASK:
        return _render_ask_notice(notice, loc)
    payload = notice.payload or {}
    rx = payload.get("rx") or {}
    wants = [u for u in (payload.get("wants") or []) if u != notice.recipient_id]
    uids = [int(u) for u in rx] + [u for u in wants]
    users = {u.id: u for u in get_user_model().objects.filter(id__in=uids)}

    def who(ids):
        return [display_name(users[i]) for i in ids if i in users]

    rx_ids = [int(u) for u in rx if int(u) != notice.recipient_id]
    wine = html.escape(wine_phrase(event.vintage, event.wine_name))
    lines = []
    if rx_ids:
        used = {e for u in rx_ids for e in rx[str(u)]}
        emojis = "".join(e for e in PALETTE if e in used)
        lines.append(
            t(
                loc, "club.rx.ping",
                names=_names(loc, who(rx_ids)), emojis=emojis, wine=wine,
            )
        )
    want_names = who(wants)
    if want_names:
        key = "club.rx.want_it" if lines else "club.rx.want_wine"
        lines.append(t(loc, key, names=_names(loc, want_names), wine=wine))
    return "\n".join(lines)


def _render_ask_notice(notice, loc) -> str:
    """Quiet message to the asker: who reacted to their question."""
    from django.contrib.auth import get_user_model

    from wine_cellar.apps.club.services import display_name

    rx = (notice.payload or {}).get("rx") or {}
    ids = [int(u) for u in rx if int(u) != notice.recipient_id]
    if not ids:
        return ""
    users = {u.id: u for u in get_user_model().objects.filter(id__in=ids)}
    names = [display_name(users[i]) for i in ids if i in users]
    if not names:
        return ""
    used = {e for u in ids for e in rx[str(u)]}
    emojis = "".join(e for e in PALETTE if e in used)
    return t(loc, "club.rx.ping_ask", names=_names(loc, names), emojis=emojis)


# -- debounced count edits ---------------------------------------------------
def _refresh_key(event_id) -> str:
    return f"club:rxrefresh:{event_id}"


def schedule_count_refresh(event_id) -> None:
    """One edit pass per event per REFRESH_DEBOUNCE seconds (dedupe key)."""
    try:
        if not cache.add(_refresh_key(event_id), 1, timeout=REFRESH_DEBOUNCE + 15):
            return
    except Exception:
        logger.warning("reaction refresh dedupe unavailable", exc_info=True)
    try:
        from wine_cellar.apps.club.tasks import refresh_event_reactions

        refresh_event_reactions.apply_async(
            args=[event_id], countdown=REFRESH_DEBOUNCE,
        )
    except Exception:
        logger.warning("could not schedule refresh_event_reactions", exc_info=True)


def clear_refresh_key(event_id) -> None:
    try:
        cache.delete(_refresh_key(event_id))
    except Exception:
        pass
