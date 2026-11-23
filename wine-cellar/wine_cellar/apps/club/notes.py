"""Tasting notes (flag ``feature_tasting_notes``).

A note is the member's own words about a bottle they opened. It lives on the
opened ClubEvent of every club the bottle was posted to (``ClubEvent.note``)
and shows wherever that event shows the wine: the feed card (edited in place),
the thread head, summaries, recaps, pings and typed questions. A retracted
event never shows (and loses) its note.

Open events show the wine at every sharing level, "Drink feed only" included
(the level only limits what the cellar peek shows), so a note is shown
wherever the open itself is. A frozen club gets no cards, so it is not an
audience.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from wine_cellar.apps.club.models import Club, ClubEvent, ClubMembership
from wine_cellar.apps.scanner.flags import flag_on

logger = logging.getLogger(__name__)

NOTE_MAX = 280


def notes_enabled() -> bool:
    return flag_on("feature_clubs") and flag_on("feature_tasting_notes")


def clean(text) -> str:
    return " ".join(str(text or "").split())


def visible_note(event) -> str:
    """The note to show for ``event``, or '' (flag off, not an open, retracted)."""
    if event is None or not notes_enabled():
        return ""
    if event.kind != ClubEvent.KIND_OPENED or event.retracted_at is not None:
        return ""
    return clean(event.note)


def has_audience(user) -> bool:
    """True when a bottle this member opens posts to a club: they belong to
    one that is not frozen (the same rule publish_club_event applies)."""
    return ClubMembership.objects.filter(
        user=user, club__frozen=False,
    ).exclude(club__kind=Club.KIND_GLOBAL).exists()


def _item_for(user, item_id):
    from wine_cellar.apps.scanner.households import get_user_household
    from wine_cellar.apps.storage.models import StorageItem

    household = get_user_household(user)
    if household is None:
        return None
    return StorageItem.objects.filter(
        id=item_id, wine__household=household,
    ).first()


def can_offer(user, item_id) -> bool:
    """Offer "Add a tasting note?" for a bottle just opened: the item is
    really out of the cellar (a drank removal, not an undone one), the
    member has a club for it to post to, and it has no note yet."""
    if not notes_enabled():
        return False
    item = _item_for(user, item_id)
    if item is None or not item.deleted:
        return False
    if not has_audience(user):
        return False
    return not ClubEvent.objects.filter(
        item_id=item.id, kind=ClubEvent.KIND_OPENED, actor=user,
    ).exclude(note="").exists()


def wine_label(user, item_id) -> str:
    """"2019 Merlot" for the offer line ('' when unknown)."""
    item = _item_for(user, item_id)
    wine = getattr(item, "wine", None)
    if wine is None:
        return ""
    return " ".join(
        str(part) for part in (wine.vintage, wine.name) if part
    )


def events_for_item(user, item_id):
    return ClubEvent.objects.filter(
        item_id=item_id, kind=ClubEvent.KIND_OPENED, actor=user,
        retracted_at__isnull=True,
    ).exclude(club__kind=Club.KIND_GLOBAL).select_related("club")


NOTE_REFRESH_DEBOUNCE = 20  # seconds between card-edit passes per event
SAVE_MAX = 10  # saves per user per SAVE_WINDOW
SAVE_WINDOW = 600  # seconds


def _refresh_key(event_id) -> str:
    return f"club:noterefresh:{event_id}"


def clear_refresh_key(event_id) -> None:
    try:
        cache.delete(_refresh_key(event_id))
    except Exception:
        pass


def schedule_note_refresh(event_id) -> None:
    """One card-edit pass per event per NOTE_REFRESH_DEBOUNCE seconds."""
    try:
        if not cache.add(_refresh_key(event_id), 1, NOTE_REFRESH_DEBOUNCE + 15):
            return
    except Exception:
        logger.warning("note refresh dedupe unavailable", exc_info=True)
    try:
        from wine_cellar.apps.club.tasks import refresh_event_note

        refresh_event_note.apply_async(
            args=[event_id], countdown=NOTE_REFRESH_DEBOUNCE,
        )
    except Exception:
        logger.warning("could not schedule refresh_event_note", exc_info=True)


def save_allowed(user) -> bool:
    """Per-user rate limit on note saves (counts every save attempt)."""
    key = f"club:notesave:{user.id}"
    try:
        cache.add(key, 0, SAVE_WINDOW)
        return cache.incr(key) <= SAVE_MAX
    except Exception:
        logger.warning("note rate limit unavailable", exc_info=True)
        return True


def _refresh_after_commit(event_ids) -> None:
    def _go(ids=list(event_ids)):
        for eid in ids:
            schedule_note_refresh(eid)

    transaction.on_commit(_go)


def apply_note(events, text) -> int:
    """Put ``text`` ('' removes it) on each event, then queue the card edits.
    Returns how many events changed."""
    text = clean(text)
    now = timezone.now()
    ids = []
    for event in events:
        if clean(event.note) == text:
            continue
        event.note = text
        event.note_added_at = now if text else None
        event.save(update_fields=["note", "note_added_at"])
        ids.append(event.id)
    if ids:
        _refresh_after_commit(ids)
    return len(ids)


def set_note_for_item(user, item_id, text):
    """(events, error). error: not_found / no_event / too_long / empty."""
    text = clean(text)
    if len(text) > NOTE_MAX:
        return [], "too_long"
    item = _item_for(user, item_id)
    if item is None or not item.deleted:
        return [], "not_found"
    events = list(events_for_item(user, item.id))
    if not events:
        return [], "no_event"
    apply_note(events, text)
    return events, ""


def set_note_for_event(user, event, text):
    """The member's own event and, with it, the same bottle's other clubs."""
    text = clean(text)
    if len(text) > NOTE_MAX:
        return [], "too_long"
    if event.actor_id != user.id or event.kind != ClubEvent.KIND_OPENED:
        return [], "not_found"
    if event.retracted_at is not None or event.club.kind == Club.KIND_GLOBAL:
        return [], "not_found"
    if event.item_id:
        events = list(events_for_item(user, event.item_id))
    else:
        events = [event]
    apply_note(events, text)
    return events, ""
