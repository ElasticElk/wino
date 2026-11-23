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
    ClubNotice,
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
    too_long, rate_limited, frozen, duplicate; ``duplicate`` also returns the event
    already sent. Scheduling delivery is left to the caller's on_commit."""
    if not relay_enabled():
        return None, "not_enabled"
    if club.kind == Club.KIND_GLOBAL:
        return None, "global"
    if club.frozen:
        return None, "frozen"
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


def asker_chat_ids(event) -> set:
    return {
        str(c) for c in TelegramUser.objects.filter(
            user_id=event.actor_id, is_active=True,
        ).values_list("chat_id", flat=True)
    }


def record_asker_copy(user, event_id, chat_id, message_id) -> str:
    """The asker's confirmation message becomes their copy of the ask.
    Returns 'ok', 'not_found' or 'already_out'."""
    event = ClubEvent.objects.filter(
        id=event_id, kind=ClubEvent.KIND_ASK, actor=user,
    ).first()
    if event is None or str(chat_id) not in {
        str(c) for c in TelegramUser.objects.filter(
            user=user, is_active=True,
        ).values_list("chat_id", flat=True)
    }:
        return "not_found"
    ClubEventDelivery.objects.get_or_create(
        event=event, kind=ClubEventDelivery.KIND_ASK,
        chat_id=str(chat_id), message_id=message_id,
    )
    # A delivery that landed between the lookup and the insert must still
    # edit this copy.
    event.refresh_from_db()
    if (event.payload or {}).get("sent_at"):
        _edit_asker_copies(event)  # reported after delivery: catch up
        return "already_out"
    return "ok"


def _edit_text(token, chat_id, message_id, text, markup) -> str:
    """editMessageText. '' when fine or harmless, else an error."""
    import requests

    payload = {
        "chat_id": chat_id, "message_id": message_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
        "reply_markup": markup or {"inline_keyboard": []},
    }
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    try:
        response = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as exc:
        return f"network:{type(exc).__name__}"
    if response.status_code == 200:
        return ""
    try:
        description = str((response.json() or {}).get("description") or "")
    except ValueError:
        description = ""
    low = description.lower()
    if "message is not modified" in low or "message to edit not found" in low:
        return ""
    return f"{response.status_code}:{description}"[:200]


def _edit_asker_copies(event, token=None) -> None:
    """'Sent to <club>: <text>' with Reply and the palette, Undo gone."""
    from wine_cellar.apps.wine.tasks import _bot_token

    token = token or _bot_token()
    if not token:
        return
    loc = locale_for_user(event.actor)
    text = t(
        loc, "club.ask.sent_copy",
        club=html.escape(event.club.name), text=html.escape(ask_text(event)),
    )
    markup = ask_markup(event, loc)
    chats = asker_chat_ids(event)
    for delivery in event.deliveries.filter(
        kind=ClubEventDelivery.KIND_ASK, message_id__isnull=False,
    ):
        if str(delivery.chat_id) not in chats:
            continue
        try:
            error = _edit_text(
                token, delivery.chat_id, delivery.message_id, text, markup,
            )
        except Exception:
            logger.warning("asker copy edit failed", exc_info=True)
            continue
        if error:
            ClubEventDelivery.objects.filter(id=delivery.id).update(error=error[:200])


def _edit_frozen_copies(event, token) -> None:
    loc = locale_for_user(event.actor)
    text = t(loc, "club.global.frozen")
    chats = asker_chat_ids(event)
    for delivery in event.deliveries.filter(
        kind=ClubEventDelivery.KIND_ASK, message_id__isnull=False,
    ):
        if str(delivery.chat_id) in chats:
            _edit_text(token, delivery.chat_id, delivery.message_id, text, None)


STUCK_AFTER = timedelta(seconds=UNDO_SECONDS)
STUCK_GIVE_UP = timedelta(days=1)


def deliver_stuck_asks(now=None) -> int:
    """Safety net for a lost countdown task: asks still unsent a minute after
    they were made (an Undo deletes the event, so none here were undone)."""
    if not relay_enabled():
        return 0
    now = now or timezone.now()
    ids = list(
        ClubEvent.objects.filter(
            kind=ClubEvent.KIND_ASK,
            created__lte=now - STUCK_AFTER, created__gte=now - STUCK_GIVE_UP,
        )
        .exclude(payload__has_key="sent_at")
        .values_list("id", flat=True)[:50]
    )
    done = 0
    for event_id in ids:
        try:
            done += 1 if deliver_ask(event_id) else 0
        except Exception:
            logger.warning("stuck ask delivery failed", exc_info=True)
    return done


def deliver_ask(event_id) -> int:
    """Send the ask to every member but the asker. Returns copies sent.
    Claims the event first, so a second run (or a late Undo) is harmless."""
    from wine_cellar.apps.club.threads import _post
    from wine_cellar.apps.wine.tasks import _bot_token

    if not relay_enabled():
        return 0
    token = _bot_token()
    if not token:
        # Leave sent_at unset so the every-minute sweep retries.
        logger.warning("deliver_club_ask: bot_token missing")
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
        frozen = bool(event.club.frozen)
        payload = dict(event.payload or {})
        payload["sent_at"] = timezone.now().isoformat()
        if frozen:
            # Stamped skipped, so the every-minute sweep stops retrying.
            payload["skipped"] = "frozen"
        event.payload = payload
        event.save(update_fields=["payload"])
    if frozen:
        try:
            _edit_frozen_copies(event, token)
        except Exception:
            logger.warning("asker frozen copy edit failed", exc_info=True)
        return 0
    try:
        _edit_asker_copies(event, token)
    except Exception:
        logger.warning("asker copy edit failed", exc_info=True)
    members = (
        ClubMembership.objects.filter(club=event.club, feed_muted=False)
        .exclude(user_id=event.actor_id)
        .select_related("user")
    )
    seen = set()
    sent = 0
    from wine_cellar.apps.club import notify

    policy_on = notify.notify_enabled()
    for membership in members:
        if policy_on:
            decision = notify.decide(
                membership.user_id, event.club_id, event=event,
                membership=membership,
            )
            if decision.action == notify.DROP:
                continue
            if decision.action != notify.SEND:
                notify.hold_notice(
                    membership.user_id, event.club, event, ClubNotice.KIND_ASK,
                    decision, f"ask:{event.id}:{membership.user_id}",
                )
                continue
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
