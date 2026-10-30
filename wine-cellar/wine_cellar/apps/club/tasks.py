"""Celery fan-out for club feed, heads-up, widen, and dismantle DMs."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone

from wine_cellar.apps.club.copy import (
    comment_card_text,
    locale_for_user,
    opened_text,
    put_back_text,
    t,
    trophy_text,
)
from wine_cellar.apps.club.dates import format_until
from wine_cellar.apps.club.models import (
    Club,
    ClubDismantleBallot,
    ClubDismantleVote,
    ClubEvent,
    ClubEventDelivery,
    ClubMembership,
    TasteOff,
)
from wine_cellar.apps.scanner.flags import flag_on
from wine_cellar.apps.scanner.models import Household, TelegramUser
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.wine.tasks import _bot_token, _send_telegram

logger = logging.getLogger(__name__)
User = get_user_model()


def _level_label(locale, visibility: str) -> str:
    key = {
        "full": "club.share.full",
        "summary": "club.share.summary",
        "none": "club.share.none",
    }.get(visibility or "none", "club.share.none")
    return t(locale, key).lower()


def _actor_chats(user_id) -> set[str]:
    return set(
        TelegramUser.objects.filter(user_id=user_id, is_active=True).values_list(
            "chat_id", flat=True,
        )
    )


def _send(
    token, chat_id, text, purpose="club", reply_markup=None, message_id=None,
):
    return _send_telegram(
        token,
        chat_id,
        text,
        purpose=purpose,
        reply_markup=reply_markup,
        message_id=message_id,
    )


def _opened_markup(event, loc):
    if event.kind != ClubEvent.KIND_OPENED or event.retracted:
        return None
    row = []
    if flag_on("feature_club_comments"):
        row.append(
            {
                "text": t(loc, "club.comment.btn"),
                "callback_data": f"cl:cm:{event.id}",
            }
        )
    if flag_on("feature_lists"):
        row.append(
            {
                "text": t(loc, "club.td.btn"),
                "callback_data": f"cl:td:{event.id}",
            }
        )
    if not row:
        return None
    return {"inline_keyboard": [row]}


@shared_task(name="publish_club_event")
def publish_club_event(item_id, actor_id, kind):
    if not flag_on("feature_clubs"):
        return
    memberships = list(
        ClubMembership.objects.filter(user_id=actor_id).select_related("club")
    )
    if not memberships:
        return
    item = (
        StorageItem.objects.select_related("wine")
        .filter(id=item_id)
        .first()
    )
    wine = item.wine if item is not None else None
    wine_name = wine.name if wine is not None else ""
    vintage = wine.vintage if wine is not None else None
    producer = (wine.producer or "") if wine is not None else ""
    wine_id = wine.id if wine is not None else None
    vote = (item.vote or "") if item is not None else ""
    memberships = [m for m in memberships if not m.club.frozen]

    for membership in memberships:
        club = membership.club
        if kind == ClubEvent.KIND_OPENED or kind == "opened":
            defaults = {
                "actor_id": actor_id,
                "wine_name": wine_name,
                "vintage": vintage,
                "producer": producer,
                "wine_id": wine_id,
                "vote": vote,
            }
            try:
                event, created = ClubEvent.objects.get_or_create(
                    club=club,
                    item_id=item_id,
                    kind=ClubEvent.KIND_OPENED,
                    defaults=defaults,
                )
            except IntegrityError:
                event = ClubEvent.objects.filter(
                    club=club, item_id=item_id, kind=ClubEvent.KIND_OPENED,
                ).first()
                created = False
            if event is None:
                continue
            if created:
                deliver_club_event.delay(event.id)
            elif event.retracted_at:
                event.retracted_at = None
                event.actor_id = actor_id
                event.wine_name = wine_name
                event.vintage = vintage
                event.producer = producer
                event.wine_id = wine_id
                event.vote = vote
                event.created = timezone.now()
                event.save()
                deliver_club_event.delay(event.id)
        elif kind in (ClubEvent.KIND_VOTED, "voted"):
            event = ClubEvent.objects.filter(
                club=club, item_id=item_id, kind=ClubEvent.KIND_OPENED,
            ).first()
            if event is None:
                continue
            event.vote = vote
            event.save(update_fields=["vote"])
            deliver_club_event.delay(event.id)
        elif kind in (ClubEvent.KIND_RETRACTED, "retracted"):
            event = ClubEvent.objects.filter(
                club=club, item_id=item_id, kind=ClubEvent.KIND_OPENED,
            ).first()
            if event is None or event.retracted_at:
                continue
            event.retracted_at = timezone.now()
            event.save(update_fields=["retracted_at"])
            deliver_club_event.delay(event.id)


@shared_task(name="deliver_club_event")
def deliver_club_event(event_id):
    if not flag_on("feature_clubs"):
        return
    event = (
        ClubEvent.objects.select_related("club", "actor")
        .filter(id=event_id)
        .first()
    )
    if event is None:
        return
    if event.kind in (ClubEvent.KIND_TASTEOFF, ClubEvent.KIND_COMMENT):
        return
    if event.club.frozen and event.kind != ClubEvent.KIND_STAFF_NOTE:
        return
    token = _bot_token()
    if not token:
        logger.warning("deliver_club_event: bot_token missing")
        return
    actor_chats = _actor_chats(event.actor_id) if event.actor_id else set()
    members = (
        ClubMembership.objects.filter(club=event.club, feed_muted=False)
        .select_related("user")
    )
    seen = set()
    for membership in members:
        chats = TelegramUser.objects.filter(
            user=membership.user, is_active=True,
        )
        for tu in chats:
            if tu.chat_id in actor_chats or tu.chat_id in seen:
                continue
            seen.add(tu.chat_id)
            loc = locale_for_user(membership.user)
            actor = ""
            if event.actor_id:
                from wine_cellar.apps.club.services import display_name

                actor = display_name(event.actor)
            markup = None
            if event.kind == ClubEvent.KIND_STAFF_NOTE:
                import html as html_mod

                note = html_mod.escape((event.payload or {}).get("text") or "")
                text = (
                    f"\U0001F30D {html_mod.escape(event.club.name)}\n\n{note}"
                )
            elif event.kind == ClubEvent.KIND_JOINED:
                text = t(loc, "club.feed.joined", name=actor)
            elif event.retracted:
                text = put_back_text(loc, actor, event.vintage, event.wine_name)
            else:
                text = opened_text(
                    loc, actor, event.vintage, event.wine_name, event.vote,
                )
                markup = _opened_markup(event, loc)
            delivery = ClubEventDelivery.objects.filter(
                event=event, chat_id=tu.chat_id,
            ).first()
            message_id = delivery.message_id if delivery else None
            result = _send(
                token, tu.chat_id, text,
                purpose="club_event",
                reply_markup=markup,
                message_id=message_id,
            )
            error = ""
            new_mid = message_id
            if result is False or result is None:
                error = "send_failed"
                if message_id:
                    result = _send(token, tu.chat_id, text, purpose="club_event")
                    error = "" if result not in (False, None) else "send_failed"
            if isinstance(result, int):
                new_mid = result
            if delivery is None:
                try:
                    ClubEventDelivery.objects.create(
                        event=event,
                        chat_id=tu.chat_id,
                        message_id=new_mid,
                        error=error,
                    )
                except IntegrityError:
                    ClubEventDelivery.objects.filter(
                        event=event, chat_id=tu.chat_id,
                    ).update(message_id=new_mid, error=error)
            else:
                delivery.message_id = new_mid
                delivery.error = error
                delivery.save(update_fields=["message_id", "error"])


@shared_task(name="club_share_notice")
def club_share_notice(household_id, exclude_user_id):
    if not flag_on("feature_clubs"):
        return
    household = Household.objects.filter(id=household_id).first()
    if household is None:
        return
    visible = ClubMembership.objects.filter(
        household=household,
        club__cellar_visibility__in=("summary", "full"),
    ).exists()
    if not visible:
        return
    if household.club_share_notice_sent_at is not None:
        return
    token = _bot_token()
    if not token:
        logger.warning("club_share_notice: bot_token missing")
        return
    chats = TelegramUser.objects.filter(
        is_active=True,
        user__household_memberships__household=household,
    ).exclude(user_id=exclude_user_id).distinct()
    sent = False
    for tu in chats:
        loc = locale_for_user(tu.user)
        text = t(loc, "club.hh.notice")
        if _send(token, tu.chat_id, text, purpose="club_share_notice") not in (
            False, None,
        ):
            sent = True
    if sent or not chats.exists():
        household.club_share_notice_sent_at = timezone.now()
        household.save(update_fields=["club_share_notice_sent_at"])


@shared_task(name="club_widen_notice")
def club_widen_notice(club_id):
    if not flag_on("feature_clubs"):
        return
    club = Club.objects.filter(id=club_id).first()
    if club is None:
        return
    token = _bot_token()
    if not token:
        logger.warning("club_widen_notice: bot_token missing")
        return
    owner_ids = set(
        ClubMembership.objects.filter(
            club=club, role=ClubMembership.ROLE_OWNER,
        ).values_list("user_id", flat=True)
    )
    members = ClubMembership.objects.filter(club=club).select_related("user")
    for membership in members:
        if membership.user_id in owner_ids:
            continue
        loc = locale_for_user(membership.user)
        text = t(
            loc,
            "club.widen.body",
            club=club.name,
            level=_level_label(loc, club.cellar_visibility),
            old=_level_label(loc, club.previous_cellar_visibility or "none"),
        )
        markup = {
            "inline_keyboard": [[
                {
                    "text": t(loc, "club.widen.stay"),
                    "callback_data": f"cl:w:{club.id}:stay",
                },
                {
                    "text": t(loc, "club.widen.leave"),
                    "callback_data": f"cl:w:{club.id}:leave",
                },
            ]],
        }
        for tu in TelegramUser.objects.filter(user=membership.user, is_active=True):
            _send(
                token, tu.chat_id, text,
                purpose="club_widen",
                reply_markup=markup,
            )


def _vote_status_text(vote, locale, club_name=None):
    from wine_cellar.apps.club.services import serialize_vote

    body = serialize_vote(vote)
    name = club_name or (vote.club.name if vote.club_id else "")
    waiting = ", ".join(body["waiting_names"]) or "—"
    when = format_until(vote.expires_at) or body.get("expires_at") or ""
    return t(
        locale,
        "club.end.status",
        club=name,
        n=body["pending"],
        yes_n=body["yes"],
        no_n=body["no"],
        waiting=waiting,
        date=when,
    )


@shared_task(name="deliver_dismantle_ballots")
def deliver_dismantle_ballots(vote_id):
    if not flag_on("feature_clubs"):
        return
    vote = (
        ClubDismantleVote.objects.select_related("club", "proposed_by")
        .filter(id=vote_id)
        .first()
    )
    if vote is None or vote.status != ClubDismantleVote.STATUS_OPEN:
        return
    token = _bot_token()
    if not token:
        logger.warning("deliver_dismantle_ballots: bot_token missing")
        return
    from wine_cellar.apps.club.services import display_name

    proposer_name = display_name(vote.proposed_by)
    club_name = vote.club.name
    for ballot in vote.ballots.select_related("user"):
        loc = locale_for_user(ballot.user)
        tu = TelegramUser.objects.filter(user=ballot.user, is_active=True).first()
        if tu is None:
            continue
        if ballot.user_id == vote.proposed_by_id:
            text = _vote_status_text(vote, loc, club_name)
            markup = {
                "inline_keyboard": [[
                    {
                        "text": t(loc, "club.end.cancel"),
                        "callback_data": f"cl:zc:{vote.id}",
                    },
                ]],
            }
        else:
            text = t(
                loc, "club.end.ballot",
                name=proposer_name, club=club_name,
            )
            markup = {
                "inline_keyboard": [[
                    {
                        "text": t(loc, "club.end.yes"),
                        "callback_data": f"cl:dy:{vote.id}",
                    },
                    {
                        "text": t(loc, "club.end.no"),
                        "callback_data": f"cl:dn:{vote.id}",
                    },
                ]],
            }
        result = _send(
            token, tu.chat_id, text,
            purpose="club_dismantle",
            reply_markup=markup,
        )
        if isinstance(result, int):
            ballot.chat_id = tu.chat_id
            ballot.message_id = result
            ballot.save(update_fields=["chat_id", "message_id"])


@shared_task(name="refresh_dismantle_status")
def refresh_dismantle_status(vote_id):
    if not flag_on("feature_clubs"):
        return
    vote = ClubDismantleVote.objects.select_related("club", "proposed_by").filter(
        id=vote_id,
    ).first()
    if vote is None or vote.status != ClubDismantleVote.STATUS_OPEN:
        return
    token = _bot_token()
    if not token:
        return
    proposer_ballot = vote.ballots.filter(user_id=vote.proposed_by_id).first()
    if proposer_ballot is None or not proposer_ballot.chat_id or not proposer_ballot.message_id:
        return
    loc = locale_for_user(vote.proposed_by)
    text = _vote_status_text(vote, loc)
    markup = {
        "inline_keyboard": [[
            {
                "text": t(loc, "club.end.cancel"),
                "callback_data": f"cl:zc:{vote.id}",
            },
        ]],
    }
    _send(
        token, proposer_ballot.chat_id, text,
        purpose="club_dismantle_status",
        reply_markup=markup,
        message_id=proposer_ballot.message_id,
    )


@shared_task(name="notify_dismantle_resolved")
def notify_dismantle_resolved(vote_id, status, club_name, rejector_id=None, recipients=None):
    if not flag_on("feature_clubs"):
        return
    if recipients:
        rows = list(recipients)
    else:
        rows = [
            {
                "user_id": ballot.user_id,
                "chat_id": ballot.chat_id,
                "message_id": ballot.message_id,
            }
            for ballot in ClubDismantleBallot.objects.filter(vote_id=vote_id)
        ]
    if not rows:
        return
    token = _bot_token()
    if not token:
        return
    rejector_name = ""
    if rejector_id:
        user = User.objects.filter(id=rejector_id).first()
        if user is not None:
            from wine_cellar.apps.club.services import display_name

            rejector_name = display_name(user)
    key = {
        ClubDismantleVote.STATUS_PASSED: "club.end.passed",
        ClubDismantleVote.STATUS_REJECTED: "club.end.rejected",
        ClubDismantleVote.STATUS_EXPIRED: "club.end.expired",
        ClubDismantleVote.STATUS_CANCELLED: "club.end.cancelled",
    }.get(status, "club.end.cancelled")
    for row in rows:
        chat_id = row.get("chat_id") or ""
        user_id = row.get("user_id")
        if not chat_id and user_id:
            tu = TelegramUser.objects.filter(user_id=user_id, is_active=True).first()
            chat_id = tu.chat_id if tu else ""
        if not chat_id:
            continue
        user = User.objects.filter(id=user_id).first() if user_id else None
        loc = locale_for_user(user) if user is not None else "en"
        text = t(loc, key, name=rejector_name, club=club_name)
        _send(
            token, chat_id, text,
            purpose="club_dismantle_done",
            message_id=row.get("message_id"),
            reply_markup={"inline_keyboard": []},
        )


@shared_task(name="expire_dismantle_votes")
def expire_dismantle_votes():
    if not flag_on("feature_clubs"):
        return
    now = timezone.now()
    open_votes = ClubDismantleVote.objects.filter(
        status=ClubDismantleVote.STATUS_OPEN, expires_at__lte=now,
    )
    from wine_cellar.apps.club.services import resolve_dismantle

    for vote in open_votes:
        resolve_dismantle(vote, ClubDismantleVote.STATUS_EXPIRED)


def _comment_pairs(event):
    from wine_cellar.apps.club.services import display_name

    pairs = []
    for comment in event.comments.select_related("user"):
        name = display_name(comment.user) if comment.user_id else ""
        pairs.append((name, comment.text))
    return pairs


@shared_task(name="refresh_event_comment_card")
def refresh_event_comment_card(event_id):
    if not flag_on("feature_clubs") or not flag_on("feature_club_comments"):
        return
    event = (
        ClubEvent.objects.select_related("club", "actor")
        .filter(id=event_id)
        .first()
    )
    if event is None or event.retracted:
        return
    comments = _comment_pairs(event)
    if len(comments) > 3:
        return
    token = _bot_token()
    if not token:
        return
    from wine_cellar.apps.club.services import display_name

    actor = display_name(event.actor) if event.actor_id else ""
    for delivery in event.deliveries.exclude(message_id__isnull=True):
        tu = TelegramUser.objects.filter(
            chat_id=delivery.chat_id, is_active=True,
        ).select_related("user").first()
        loc = locale_for_user(tu.user) if tu is not None else "en"
        text = comment_card_text(
            loc, actor, event.vintage, event.wine_name, event.vote, comments,
        )
        markup = _opened_markup(event, loc)
        _send(
            token, delivery.chat_id, text,
            purpose="club_comment",
            reply_markup=markup,
            message_id=delivery.message_id,
        )


def _score_markup(tasteoff, loc):
    keyboard = []
    for bottle in tasteoff.bottles.all():
        row = []
        for score in range(1, 6):
            label = str(score)
            if score == 1:
                label = f"1 · {(bottle.label or 'wine')[:28]}"
            row.append(
                {
                    "text": label,
                    "callback_data": f"cl:ts:{tasteoff.id}:{bottle.id}:{score}",
                }
            )
        keyboard.append(row)
    return {"inline_keyboard": keyboard} if keyboard else None


@shared_task(name="deliver_tasteoff_open")
def deliver_tasteoff_open(tasteoff_id):
    if not flag_on("feature_tasteoffs"):
        return
    tasteoff = (
        TasteOff.objects.select_related("club", "host")
        .prefetch_related("bottles")
        .filter(id=tasteoff_id)
        .first()
    )
    if tasteoff is None or tasteoff.status != TasteOff.STATUS_SCORING:
        return
    token = _bot_token()
    if not token:
        logger.warning("deliver_tasteoff_open: bot_token missing")
        return
    from wine_cellar.apps.club.services import display_name

    host_name = display_name(tasteoff.host) if tasteoff.host_id else ""
    host_chats = _actor_chats(tasteoff.host_id) if tasteoff.host_id else set()
    members = (
        ClubMembership.objects.filter(club=tasteoff.club, feed_muted=False)
        .select_related("user")
    )
    seen = set()
    for membership in members:
        for tu in TelegramUser.objects.filter(user=membership.user, is_active=True):
            if tu.chat_id in seen:
                continue
            seen.add(tu.chat_id)
            loc = locale_for_user(membership.user)
            if tu.chat_id in host_chats:
                markup = {
                    "inline_keyboard": [[
                        {
                            "text": t(loc, "club.to.close"),
                            "callback_data": f"cl:tc:{tasteoff.id}",
                        },
                    ]],
                }
                _send(
                    token, tu.chat_id, t(loc, "club.to.host_open"),
                    purpose="tasteoff_host",
                    reply_markup=markup,
                )
                continue
            text = t(
                loc, "club.to.running",
                host=host_name, title=tasteoff.title,
            )
            _send(
                token, tu.chat_id, text,
                purpose="tasteoff_open",
                reply_markup=_score_markup(tasteoff, loc),
            )


@shared_task(name="deliver_tasteoff_closed")
def deliver_tasteoff_closed(tasteoff_id):
    if not flag_on("feature_tasteoffs"):
        return
    tasteoff = (
        TasteOff.objects.select_related("club", "host")
        .filter(id=tasteoff_id)
        .first()
    )
    if tasteoff is None:
        return
    token = _bot_token()
    if not token:
        logger.warning("deliver_tasteoff_closed: bot_token missing")
        return
    event = (
        ClubEvent.objects.filter(
            club=tasteoff.club, kind=ClubEvent.KIND_TASTEOFF, wine_name=tasteoff.title,
        )
        .order_by("-created")
        .first()
    )
    rankings = (event.payload or {}).get("rankings") if event else []
    members = ClubMembership.objects.filter(
        club=tasteoff.club, feed_muted=False,
    ).select_related("user")
    seen = set()
    for membership in members:
        for tu in TelegramUser.objects.filter(user=membership.user, is_active=True):
            if tu.chat_id in seen:
                continue
            seen.add(tu.chat_id)
            loc = locale_for_user(membership.user)
            text = trophy_text(loc, tasteoff.title, rankings)
            _send(token, tu.chat_id, text, purpose="tasteoff_closed")


@shared_task(name="notify_kicked")
def notify_kicked(club_name, chat_ids):
    if not flag_on("feature_clubs"):
        return
    token = _bot_token()
    if not token or not chat_ids:
        return
    for chat_id in chat_ids:
        tu = TelegramUser.objects.filter(
            chat_id=str(chat_id), is_active=True,
        ).select_related("user").first()
        loc = locale_for_user(tu.user) if tu is not None else "en"
        _send(
            token, chat_id, t(loc, "club.global.kicked", name=club_name),
            purpose="club_kicked",
        )


@shared_task(name="close_stale_tasteoffs")
def close_stale_tasteoffs():
    if not flag_on("feature_clubs") or not flag_on("feature_tasteoffs"):
        return
    cutoff = timezone.now() - timedelta(days=7)
    stale = TasteOff.objects.filter(
        status=TasteOff.STATUS_SCORING, opened_at__lte=cutoff,
    )
    from wine_cellar.apps.club.services import close_tasteoff

    for tasteoff in stale:
        close_tasteoff(tasteoff)
