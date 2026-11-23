"""Ask the club (flag ``feature_club_relay``).

A member types one message; it becomes a ClubEvent of kind ``ask`` (payload
``{"text": ...}``, actor = the asker) and is sent to every other member of
the club as "<name> asks the club: <text>" with Reply and the reaction
palette. Delivery is delayed by UNDO_SECONDS so the asker can take it back;
each copy is recorded as a ClubEventDelivery of kind ``ask`` so a swipe-reply
maps back to the event and replies thread like any other (club/threads.py).

Asks are not cellar data: every member sees them whatever the sharing level,
and only the text and the asker's name ever leave this module.
"""

from __future__ import annotations

import html
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from wine_cellar.apps.club.copy import locale_for_user, t
from wine_cellar.apps.club.models import (
    Club,
    ClubEvent,
    ClubEventDelivery,
    ClubMembership,
)
from wine_cellar.apps.club.threads import threads_enabled
from wine_cellar.apps.scanner.flags import flag_on
from wine_cellar.apps.scanner.models import TelegramUser

logger = logging.getLogger(__name__)

ASK_MAX = 280
UNDO_SECONDS = 60
RATE_MAX = 3
RATE_WINDOW = timedelta(hours=1)
DEDUPE_WINDOW = timedelta(minutes=10)


def relay_enabled() -> bool:
    """Replies are threads, so the relay needs the thread flags too."""
    return threads_enabled() and flag_on("feature_club_relay")


def ask_text(event) -> str:
    return str((event.payload or {}).get("text") or "")


def ask_line(event, loc) -> str:
    """'<name> asks the club: <text>', both escaped."""
    from wine_cellar.apps.club.services import display_name

    name = display_name(event.actor) if event.actor_id else ""
    return t(
        loc, "club.ask.line",
        name=html.escape(name), text=html.escape(ask_text(event)),
    )


def ask_markup(event, loc) -> dict:
    """Reaction palette (when on) and Reply."""
    from wine_cellar.apps.club.reactions import reaction_rows

    rows = list(reaction_rows(event))
    rows.append([{
        "text": t(loc, "club.thread.btn_reply"),
        "callback_data": f"cl:rp:{event.id}",
    }])
    return {"inline_keyboard": rows}


def create_ask(user, club, text):
    """Returns (event, error). error is one of not_enabled, global, blank,
    too_long, rate_limited, duplicate; ``duplicate`` also returns the event
    already sent. Scheduling delivery is left to the caller's on_commit."""
    if not relay_enabled():
        return None, "not_enabled"
    if club.kind == Club.KIND_GLOBAL:
        return None, "global"
    text = (text or "").strip()
    if not text:
        return None, "blank"
    if len(text) > ASK_MAX:
        return None, "too_long"
    with transaction.atomic():
        # Serialise one member's asks in one club (rate limit and dedupe).
        membership = (
            ClubMembership.objects.select_for_update()
            .filter(user=user, club=club)
            .first()
        )
        if membership is None:
            return None, "not_member"
        now = timezone.now()
        mine = ClubEvent.objects.filter(
            club=club, actor=user, kind=ClubEvent.KIND_ASK,
            created__gte=now - RATE_WINDOW,
        ).order_by("created")
        dup = mine.filter(
            created__gte=now - DEDUPE_WINDOW, payload__text=text,
        ).first()
        if dup is not None:
            return dup, "duplicate"
        recent = list(mine)
        if len(recent) >= RATE_MAX:
            return recent[0], "rate_limited"
        event = ClubEvent.objects.create(
            club=club, actor=user, kind=ClubEvent.KIND_ASK,
            payload={"text": text},
        )
    return event, ""


def retry_minutes(oldest_event) -> int:
    """Minutes until the oldest ask in the window falls out of it."""
    wait = oldest_event.created + RATE_WINDOW - timezone.now()
    return max(1, int(wait.total_seconds() // 60) + 1)


def schedule_delivery(event_id) -> None:
    try:
        from wine_cellar.apps.club.tasks import deliver_club_ask

        deliver_club_ask.apply_async(args=[event_id], countdown=UNDO_SECONDS)
    except Exception:
        logger.warning("could not schedule deliver_club_ask", exc_info=True)


def undo_ask(user, event_id):
    """Returns 'undone', 'already_out' or 'not_found'. Only the asker."""
    with transaction.atomic():
        event = (
            ClubEvent.objects.select_for_update()
            .filter(id=event_id, kind=ClubEvent.KIND_ASK, actor=user)
            .first()
        )
        if event is None:
            return "not_found"
        if (event.payload or {}).get("sent_at"):
            return "already_out"
        event.delete()  # cascades unsent delivery rows
    return "undone"


def deliver_ask(event_id) -> int:
    """Send the ask to every member but the asker. Returns copies sent.
    Claims the event first, so a second run (or a late Undo) is harmless."""
    from wine_cellar.apps.club.threads import _post
    from wine_cellar.apps.wine.tasks import _bot_token

    if not relay_enabled():
        return 0
    with transaction.atomic():
        event = (
            ClubEvent.objects.select_for_update(of=("self",))
            .select_related("club", "actor")
            .filter(id=event_id, kind=ClubEvent.KIND_ASK)
            .first()
        )
        if event is None or (event.payload or {}).get("sent_at"):
            return 0
        if event.club.frozen:
            return 0
        payload = dict(event.payload or {})
        payload["sent_at"] = timezone.now().isoformat()
        event.payload = payload
        event.save(update_fields=["payload"])
    token = _bot_token()
    if not token:
        logger.warning("deliver_club_ask: bot_token missing")
        return 0
    members = (
        ClubMembership.objects.filter(club=event.club, feed_muted=False)
        .exclude(user_id=event.actor_id)
        .select_related("user")
    )
    seen = set()
    sent = 0
    for membership in members:
        loc = locale_for_user(membership.user)
        text = ask_line(event, loc)
        markup = ask_markup(event, loc)
        for tu in TelegramUser.objects.filter(user=membership.user, is_active=True):
            if tu.chat_id in seen:
                continue
            seen.add(tu.chat_id)
            mid, error = _post(token, tu.chat_id, text, markup)
            try:
                ClubEventDelivery.objects.create(
                    event=event, kind=ClubEventDelivery.KIND_ASK,
                    chat_id=tu.chat_id, message_id=mid, error=error[:200],
                )
            except Exception:
                logger.warning("could not record ask delivery", exc_info=True)
            if mid is not None:
                sent += 1
    return sent
