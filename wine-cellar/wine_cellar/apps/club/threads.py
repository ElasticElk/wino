"""Comment threads: reply pings through the ClubNotice outbox.

Flag ``feature_club_threads`` (needs ``feature_clubs`` and
``feature_club_comments``). A new comment queues one ClubNotice per
recipient; replies on the same event inside BATCH_WINDOW merge into the
same row and go out as one combined ping, threaded under the recipient's own
copy of the card. Telegram errors are recorded on the notice and on the
ClubEventDelivery row; nothing here raises out to the caller.
"""

from __future__ import annotations

import html
import logging
import time
from datetime import timedelta

import requests
from django.db import IntegrityError, transaction
from django.utils import timezone

from wine_cellar.apps.club.copy import locale_for_user, opened_text, t, wine_phrase
from wine_cellar.apps.club.models import (
    ClubComment,
    ClubEvent,
    ClubEventDelivery,
    ClubMembership,
    ClubNotice,
    ClubThreadMute,
)
from wine_cellar.apps.scanner.flags import flag_on
from wine_cellar.apps.scanner.models import TelegramUser

logger = logging.getLogger(__name__)

BATCH_WINDOW = timedelta(minutes=2)
# Per recipient: at most this many pings in RATE_WINDOW; the rest wait.
RATE_LIMIT_MAX = 6
RATE_WINDOW = timedelta(seconds=60)
FLUSH_LIMIT = 200
# Telegram caps a message at 4096 characters; leave room for the quote line.
PING_BODY_CHARS = 3500
ERROR_MAX = 200


def threads_enabled() -> bool:
    return (
        flag_on("feature_clubs")
        and flag_on("feature_club_comments")
        and flag_on("feature_club_threads")
    )


def is_thread_muted(user_id, event_id) -> bool:
    return ClubThreadMute.objects.filter(user_id=user_id, event_id=event_id).exists()


def toggle_thread_mute(user, event, muted=None) -> bool:
    """Flip (or set) the caller's mute on a thread. Returns the new state."""
    existing = ClubThreadMute.objects.filter(user=user, event=event)
    current = existing.exists()
    want = (not current) if muted is None else bool(muted)
    if want and not current:
        try:
            ClubThreadMute.objects.create(user=user, event=event)
        except IntegrityError:
            pass
    elif not want and current:
        existing.delete()
    return want


def resolve_message(user, chat_id, message_id):
    """Event a bot message in ``chat_id`` belongs to, if ``user`` may see it."""
    if not chat_id or not message_id:
        return None
    if not TelegramUser.objects.filter(
        user=user, chat_id=str(chat_id), is_active=True,
    ).exists():
        return None
    delivery = (
        ClubEventDelivery.objects.filter(
            chat_id=str(chat_id), message_id=message_id,
        )
        .select_related("event", "event__club")
        .order_by("-id")
        .first()
    )
    if delivery is None:
        return None
    if not ClubMembership.objects.filter(
        user=user, club_id=delivery.event.club_id,
    ).exists():
        return None
    return delivery.event


def _merge_into_pending(event, comment, uid) -> bool:
    """Add the comment to the recipient's unsent notice. False when none."""
    notice = (
        ClubNotice.objects.select_for_update()
        .filter(
            recipient_id=uid, event=event,
            kind=ClubNotice.KIND_COMMENT, sent_at__isnull=True,
        )
        .order_by("-id")
        .first()
    )
    if notice is None:
        return False
    ids = list((notice.payload or {}).get("comment_ids") or [])
    if comment.id not in ids:
        ids.append(comment.id)
        notice.payload = {"comment_ids": ids}
        notice.save(update_fields=["payload"])
    return True


def _queue_for(event, comment, uid, now) -> None:
    with transaction.atomic():
        if _merge_into_pending(event, comment, uid):
            return
        try:
            with transaction.atomic():
                ClubNotice.objects.get_or_create(
                    dedupe_key=f"cm:{event.id}:{uid}:{comment.id}",
                    defaults={
                        "recipient_id": uid,
                        "club": event.club,
                        "event": event,
                        "kind": ClubNotice.KIND_COMMENT,
                        "payload": {"comment_ids": [comment.id]},
                        "due_at": now + BATCH_WINDOW,
                    },
                )
        except IntegrityError:
            # A concurrent enqueue made the pending row first: merge into it.
            _merge_into_pending(event, comment, uid)


def enqueue_comment_notices(comment_id) -> int:
    """Queue (or merge into) one ping per recipient. Returns recipients."""
    if not threads_enabled():
        return 0
    comment = (
        ClubComment.objects.select_related("event", "event__club")
        .filter(id=comment_id)
        .first()
    )
    if comment is None:
        return 0
    event = comment.event
    if event.club.frozen:
        return 0
    muted = set(
        ClubThreadMute.objects.filter(event=event).values_list("user_id", flat=True)
    )
    members = ClubMembership.objects.filter(club=event.club, feed_muted=False)
    if comment.user_id:
        members = members.exclude(user_id=comment.user_id)
    now = timezone.now()
    queued = 0
    for membership in members:
        uid = membership.user_id
        if uid in muted:
            continue
        _queue_for(event, comment, uid, now)
        queued += 1
    if queued:
        _schedule_flush()
    return queued


def _schedule_flush() -> None:
    try:
        from wine_cellar.apps.club.tasks import flush_club_notices

        flush_club_notices.apply_async(
            countdown=int(BATCH_WINDOW.total_seconds()) + 2,
        )
    except Exception:
        logger.warning("could not schedule flush_club_notices", exc_info=True)


def _event_line(event, loc) -> str:
    from wine_cellar.apps.club.services import display_name

    actor = display_name(event.actor) if event.actor_id else ""
    if event.kind == ClubEvent.KIND_OPENED and not event.retracted:
        return opened_text(
            loc, html.escape(actor), event.vintage,
            html.escape(event.wine_name), event.vote,
        )
    return "\U0001F377 " + html.escape(
        wine_phrase(event.vintage, event.wine_name) if event.wine_name else actor
    )


def _markup(event, loc, muted: bool, thread: bool = False) -> dict:
    from wine_cellar.apps.club.reactions import reaction_rows

    rows = list(reaction_rows(event))
    if thread:
        rows.append([{
            "text": t(loc, "club.thread.btn_view", n=_comment_count(event)),
            "callback_data": f"cl:th:{event.club_id}:{event.id}:0",
        }])
    return {
        "inline_keyboard": rows + [[
            {
                "text": t(loc, "club.thread.btn_reply"),
                "callback_data": f"cl:rp:{event.id}",
            },
            {
                "text": t(
                    loc,
                    "club.thread.btn_unmute" if muted else "club.thread.btn_mute",
                ),
                "callback_data": f"cl:mt:{event.id}",
            },
        ]]
    }


def _post(token, chat_id, text, markup, reply_to=None):
    """sendMessage. Returns (message_id | None, error string). Never raises."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if markup is not None:
        payload["reply_markup"] = markup
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(2):
        try:
            response = requests.post(url, json=payload, timeout=15)
        except requests.RequestException as exc:
            return None, f"network:{type(exc).__name__}"[:ERROR_MAX]
        status = response.status_code
        if status == 429 and attempt == 0:
            retry = 1
            try:
                retry = int(
                    (response.json().get("parameters") or {}).get("retry_after") or 1
                )
            except (ValueError, TypeError, AttributeError):
                retry = 1
            time.sleep(min(max(retry, 1), 5))
            continue
        try:
            body = response.json()
        except ValueError:
            body = {}
        if status == 200:
            mid = (body.get("result") or {}).get("message_id")
            return mid, ""
        description = str((body or {}).get("description") or "")
        return None, f"{status}:{description}"[:ERROR_MAX]
    return None, "429:rate_limited"


def _reply_target(event, chat_id):
    card = (
        ClubEventDelivery.objects.filter(
            event=event, chat_id=chat_id, kind=ClubEventDelivery.KIND_CARD,
            message_id__isnull=False,
        )
        .order_by("-id")
        .first()
    )
    if card is not None:
        return card.message_id
    ping = (
        ClubEventDelivery.objects.filter(
            event=event, chat_id=chat_id, kind=ClubEventDelivery.KIND_PING,
            message_id__isnull=False,
        )
        .order_by("-id")
        .first()
    )
    return ping.message_id if ping is not None else None


def _record(event, chat_id, message_id, error):
    try:
        ClubEventDelivery.objects.create(
            event=event, kind=ClubEventDelivery.KIND_PING,
            chat_id=chat_id, message_id=message_id, error=error[:ERROR_MAX],
        )
    except Exception:
        logger.warning("could not record ping delivery", exc_info=True)


def _comment_count(event) -> int:
    return ClubComment.objects.filter(event=event).count()


def _fit_comments(comments, loc):
    """Newest comments that fit PING_BODY_CHARS (after escaping), oldest
    first, plus a note for the rest. Returns (text, hidden_count)."""
    lines = [_comment_lines([c]) for c in comments]
    kept, size = [], 0
    for line in reversed(lines):
        if kept and size + len(line) + 1 > PING_BODY_CHARS:
            break
        if not kept and len(line) > PING_BODY_CHARS:
            line = line[:PING_BODY_CHARS]
        kept.append(line)
        size += len(line) + 1
    kept.reverse()
    hidden = len(lines) - len(kept)
    text = "\n".join(kept)
    if hidden:
        text += "\n" + t(loc, "club.thread.more", n=hidden)
    return text, hidden


def _comment_lines(comments) -> str:
    from wine_cellar.apps.club.services import display_name

    lines = []
    for comment in comments:
        who = html.escape(display_name(comment.user) if comment.user_id else "")
        lines.append(f"\U0001F4AC {who}: {html.escape(comment.text or '')}")
    return "\n".join(lines)


def _reply_target_gone(error: str) -> bool:
    low = error.lower()
    return error.startswith("400") and (
        "message to be replied not found" in low
        or "reply message not found" in low
    )


def _send_notice(notice, token) -> str:
    """Deliver one claimed notice. Returns an error summary ('' when fine)."""
    event = notice.event
    user = notice.recipient
    membership = ClubMembership.objects.filter(
        user=user, club_id=event.club_id,
    ).first()
    if membership is None or membership.feed_muted:
        return "skipped:club_muted"
    if is_thread_muted(user.id, event.id):
        return "skipped:thread_muted"
    if event.retracted:
        return "skipped:retracted"
    if notice.kind == ClubNotice.KIND_REACTION:
        return _send_reaction_notice(notice, token)
    ids = (notice.payload or {}).get("comment_ids") or []
    comments = list(
        ClubComment.objects.filter(id__in=ids)
        .exclude(user_id=user.id)
        .select_related("user")
        .order_by("created", "id")
    )
    if not comments:
        return "skipped:empty"
    chats = list(
        TelegramUser.objects.filter(user=user, is_active=True)
        .values_list("chat_id", flat=True)
        .distinct()
    )
    if not chats:
        return "skipped:no_chat"
    loc = locale_for_user(user)
    body, hidden = _fit_comments(comments, loc)
    markup = _markup(event, loc, muted=False, thread=bool(hidden))
    errors = []
    for chat_id in chats:
        reply_to = _reply_target(event, chat_id)
        text = body
        if not reply_to:
            quote = t(loc, "club.thread.about", line=_event_line(event, loc))
            text = f"{quote}\n\n{body}"
        mid, error = _post(token, chat_id, text, markup, reply_to=reply_to)
        if mid is None and reply_to and _reply_target_gone(error):
            # Reply target gone: resend fresh, quoting the card line.
            quote = t(loc, "club.thread.about", line=_event_line(event, loc))
            text = f"{quote}\n\n{body}"
            mid, error = _post(token, chat_id, text, markup)
        _record(event, chat_id, mid, error)
        if error:
            errors.append(f"{chat_id}:{error}")
    return "; ".join(errors)[:ERROR_MAX]


def _send_reaction_notice(notice, token) -> str:
    """Quiet message to the event owner: who reacted / saved. No buttons."""
    from wine_cellar.apps.club.reactions import render_notice

    event, user = notice.event, notice.recipient
    loc = locale_for_user(user)
    text = render_notice(notice, loc)
    if not text:
        return "skipped:empty"
    chats = list(
        TelegramUser.objects.filter(user=user, is_active=True)
        .values_list("chat_id", flat=True)
        .distinct()
    )
    if not chats:
        return "skipped:no_chat"
    errors = []
    for chat_id in chats:
        mid, error = _post(token, chat_id, text, None)
        _record(event, chat_id, mid, error)
        if error:
            errors.append(f"{chat_id}:{error}")
    return "; ".join(errors)[:ERROR_MAX]


def flush_due_notices(now=None) -> int:
    """Send every due notice once. Returns how many were sent or skipped."""
    from wine_cellar.apps.club.reactions import reactions_enabled
    from wine_cellar.apps.wine.tasks import _bot_token

    kinds = []
    if threads_enabled():
        kinds.append(ClubNotice.KIND_COMMENT)
    if reactions_enabled():
        kinds.append(ClubNotice.KIND_REACTION)
    if not kinds:
        return 0
    token = _bot_token()
    if not token:
        logger.warning("flush_club_notices: bot_token missing")
        return 0
    now = now or timezone.now()
    due = list(
        ClubNotice.objects.filter(
            sent_at__isnull=True, due_at__lte=now, kind__in=kinds,
        )
        .select_related("recipient", "event", "event__club", "event__actor")
        .order_by("due_at", "id")[:FLUSH_LIMIT]
    )
    done = 0
    for notice in due:
        try:
            recent = ClubNotice.objects.filter(
                recipient_id=notice.recipient_id,
                sent_at__gte=now - RATE_WINDOW,
            ).count()
            if recent >= RATE_LIMIT_MAX:
                ClubNotice.objects.filter(id=notice.id, sent_at__isnull=True).update(
                    due_at=now + RATE_WINDOW,
                )
                continue
            with transaction.atomic():
                locked = (
                    ClubNotice.objects.select_for_update(of=("self",))
                    .select_related("recipient", "event", "event__club", "event__actor")
                    .filter(id=notice.id, sent_at__isnull=True)
                    .first()
                )
                if locked is None:
                    continue
                locked.sent_at = now
                locked.save(update_fields=["sent_at"])
            # The payload is read under the claim, so a comment merged after
            # the due list was built is still in this ping.
            error = _send_notice(locked, token)
            if error:
                ClubNotice.objects.filter(id=notice.id).update(error=error)
            done += 1
        except Exception as exc:
            logger.warning("club notice %s failed", notice.id, exc_info=True)
            ClubNotice.objects.filter(id=notice.id).update(
                error=f"error:{type(exc).__name__}"[:ERROR_MAX],
            )
    return done
