"""Comments, club lists, feed→To-drink, and taste-offs."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from wine_cellar.apps.api.club_views import (
    ClubsDisabled,
    ClubsEnabledMixin,
    _member_or_404,
)
from wine_cellar.apps.api.list_views import serialize_item
from wine_cellar.apps.api.views import ScannerAPIView
from wine_cellar.apps.club.models import (
    ClubComment,
    ClubEvent,
    ClubEventDelivery,
    ClubMembership,
    TasteOff,
    TasteOffBottle,
    TasteOffScore,
)
from wine_cellar.apps.club.notes import (
    NOTE_MAX,
    SAVE_MAX,
    SAVE_WINDOW,
    can_offer,
    notes_enabled,
    save_allowed,
    set_note_for_event,
    set_note_for_item,
    wine_label,
)
from wine_cellar.apps.club.peek import effective_visibility, tighter_visibility
from wine_cellar.apps.club.reactions import (
    PALETTE,
    counts_for,
    kind_reactable,
    mine_for,
    reactions_enabled,
    record_want,
    toggle_reaction,
)
from wine_cellar.apps.club.relay import (
    ASK_MAX,
    RATE_MAX,
    UNDO_SECONDS,
    create_ask,
    record_asker_copy,
    relay_enabled,
    retry_minutes,
    schedule_delivery,
    undo_ask,
)
from wine_cellar.apps.club.services import (
    TASTEOFF_REOPEN_WINDOW,
    close_tasteoff,
    display_name,
    reopen_tasteoff,
)
from wine_cellar.apps.club.threads import (
    SUMMARY,
    enqueue_comment_notices,
    is_thread_muted,
    resolve_message,
    threads_enabled,
    toggle_thread_mute,
)
from wine_cellar.apps.scanner.flags import flag_on
from wine_cellar.apps.wine.models import (
    Wine,
    WineList,
    WineListItem,
    get_or_create_personal_list,
)


class CommentsDisabled(Exception):
    pass


class TasteoffsDisabled(Exception):
    pass


class CommentsEnabledMixin(ClubsEnabledMixin):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not flag_on("feature_club_comments"):
            raise CommentsDisabled()

    def handle_exception(self, exc):
        if isinstance(exc, CommentsDisabled):
            return Response({"error": "not_enabled"}, status=status.HTTP_404_NOT_FOUND)
        return super().handle_exception(exc)


class TasteoffsEnabledMixin(ClubsEnabledMixin):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not flag_on("feature_tasteoffs"):
            raise TasteoffsDisabled()

    def handle_exception(self, exc):
        if isinstance(exc, TasteoffsDisabled):
            return Response({"error": "not_enabled"}, status=status.HTTP_404_NOT_FOUND)
        return super().handle_exception(exc)


class ClubListsEnabledMixin(ClubsEnabledMixin):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not flag_on("feature_lists"):
            raise ClubsDisabled()


def _event_in_club(club, event_id):
    return ClubEvent.objects.filter(club=club, id=event_id).first()


def _ask_hidden(event) -> bool:
    """An ask while Ask the club is off: old copies must not ping anyone."""
    return event.kind == ClubEvent.KIND_ASK and not relay_enabled()


def _ask_off_response():
    return Response({"error": "ask_off"}, status=status.HTTP_404_NOT_FOUND)


def serialize_comment(comment: ClubComment) -> dict:
    return {
        "id": comment.id,
        "event_id": comment.event_id,
        "text": comment.text,
        "display_name": display_name(comment.user) if comment.user_id else "",
        "created": comment.created.isoformat() if comment.created else None,
    }


class ClubCommentCollectionView(CommentsEnabledMixin, ScannerAPIView):
    def get(self, request, pk, event_id):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        event = _event_in_club(membership.club, event_id)
        if event is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if _ask_hidden(event):
            return _ask_off_response()
        rows = list(event.comments.select_related("user"))
        return Response(
            {"comments": [serialize_comment(c) for c in rows], "count": len(rows)}
        )

    def post(self, request, pk, event_id):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        event = _event_in_club(membership.club, event_id)
        if event is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if _ask_hidden(event):
            return _ask_off_response()
        if event.retracted:
            return Response({"error": "retracted"}, status=status.HTTP_409_CONFLICT)
        text = str((request.data or {}).get("text") or "").strip()
        if not text:
            return Response({"error": "blank"}, status=status.HTTP_400_BAD_REQUEST)
        if len(text) > 280:
            return Response(
                {"error": "too_long", "max": 280},
                status=status.HTTP_400_BAD_REQUEST,
            )
        comment = ClubComment.objects.create(
            event=event, user=request.user, text=text,
        )
        if threads_enabled():
            transaction.on_commit(
                lambda cid=comment.id: enqueue_comment_notices(cid)
            )
        else:
            from wine_cellar.apps.club.tasks import refresh_event_comment_card

            transaction.on_commit(
                lambda eid=event.id: refresh_event_comment_card.delay(eid)
            )
        return Response(serialize_comment(comment), status=status.HTTP_201_CREATED)


class ThreadsEnabledMixin(ClubsEnabledMixin):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not threads_enabled():
            raise CommentsDisabled()

    def handle_exception(self, exc):
        if isinstance(exc, CommentsDisabled):
            return Response({"error": "not_enabled"}, status=status.HTTP_404_NOT_FOUND)
        return super().handle_exception(exc)


class ClubThreadResolveView(ClubsEnabledMixin, ScannerAPIView):
    """Which event does a bot message (swipe-reply target) belong to?

    A summary or recap message is recognised even with threads off (so the
    swipe-reply gets the "Tap Reply or Thread" hint); an event needs the
    threads flag."""

    def get(self, request):
        try:
            message_id = int(request.query_params.get("message_id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        event = resolve_message(
            request.user, request.query_params.get("chat_id") or "", message_id,
        )
        if event is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if event is SUMMARY:
            return Response({"summary": True})
        if not threads_enabled():
            return Response({"error": "not_enabled"}, status=status.HTTP_404_NOT_FOUND)
        if _ask_hidden(event):
            return _ask_off_response()
        return Response(
            {
                "event_id": event.id,
                "club_id": event.club_id,
                "retracted": event.retracted,
                "muted": is_thread_muted(request.user.id, event.id),
            }
        )


class NotesDisabled(Exception):
    pass


class NotesEnabledMixin(ClubsEnabledMixin):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not notes_enabled():
            raise NotesDisabled()

    def handle_exception(self, exc):
        if isinstance(exc, NotesDisabled):
            return Response({"error": "not_enabled"}, status=status.HTTP_404_NOT_FOUND)
        return super().handle_exception(exc)


class ClubNoteOfferView(NotesEnabledMixin, ScannerAPIView):
    """After the vote on a bottle just opened: is "Add a tasting note?" worth
    asking (a real open, a club to post to, no note yet)?"""

    def get(self, request):
        try:
            item_id = int(request.query_params.get("item_id") or 0)
        except (TypeError, ValueError):
            item_id = 0
        offer = bool(item_id) and can_offer(request.user, item_id)
        body = {"offer": offer}
        if offer:
            body["wine"] = wine_label(request.user, item_id)
        return Response(body)


class ClubNoteView(NotesEnabledMixin, ScannerAPIView):
    """Add, change or remove (empty text) the caller's own tasting note, by
    ``item_id`` (just opened) or ``event_id`` (feed detail)."""

    def post(self, request):
        data = request.data or {}
        text = data.get("text")
        if not isinstance(text, str):
            return Response({"error": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
        if len(" ".join(text.split())) > NOTE_MAX:
            return Response(
                {"error": "too_long", "max": NOTE_MAX},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not save_allowed(request.user):
            return Response(
                {"error": "rate_limited", "max": SAVE_MAX,
                 "retry_minutes": SAVE_WINDOW // 60},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if data.get("event_id"):
            try:
                event = ClubEvent.objects.filter(id=int(data["event_id"])).first()
            except (TypeError, ValueError):
                event = None
            if event is None or not ClubMembership.objects.filter(
                user=request.user, club_id=event.club_id,
            ).exists():
                return Response(
                    {"error": "not_found"}, status=status.HTTP_404_NOT_FOUND,
                )
            events, error = set_note_for_event(request.user, event, text)
        else:
            try:
                item_id = int(data.get("item_id") or 0)
            except (TypeError, ValueError):
                item_id = 0
            if not item_id:
                return Response(
                    {"error": "invalid"}, status=status.HTTP_400_BAD_REQUEST,
                )
            events, error = set_note_for_item(request.user, item_id, text)
        if error == "no_event":
            return Response({"error": error}, status=status.HTTP_409_CONFLICT)
        if error:
            code = (
                status.HTTP_400_BAD_REQUEST if error == "too_long"
                else status.HTTP_404_NOT_FOUND
            )
            return Response({"error": error}, status=code)
        return Response(
            {
                "saved": True,
                "count": len(events),
                "clubs": [e.club.name for e in events],
                "removed": not text.strip(),
            }
        )


class ClubThreadMuteView(ThreadsEnabledMixin, ScannerAPIView):
    def _event(self, request, event_id):
        event = ClubEvent.objects.filter(id=event_id).first()
        if event is None:
            return None
        if not ClubMembership.objects.filter(
            user=request.user, club_id=event.club_id,
        ).exists():
            return None
        return event

    def get(self, request, event_id):
        event = self._event(request, event_id)
        if event is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"muted": is_thread_muted(request.user.id, event.id)})

    def post(self, request, event_id):
        event = self._event(request, event_id)
        if event is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        wanted = (request.data or {}).get("muted")
        muted = toggle_thread_mute(
            request.user, event, None if wanted is None else bool(wanted),
        )
        return Response({"muted": muted})


class ClubEventToListView(ClubListsEnabledMixin, ScannerAPIView):
    def post(self, request, pk, event_id):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        event = _event_in_club(membership.club, event_id)
        if event is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if event.retracted:
            return Response({"error": "retracted"}, status=status.HTTP_409_CONFLICT)
        if event.kind != ClubEvent.KIND_OPENED:
            # An ask (or any non-bottle event) has no wine to save.
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        item, created = add_event_to_list(request.user, event)
        body = serialize_item(item)
        if not created:
            body["status_code"] = 200
            return Response(body)
        return Response(body, status=status.HTTP_201_CREATED)


def add_event_to_list(user, event):
    """Snapshot an opened-wine event onto the user's personal To-drink list.
    Idempotent. Returns (item, created)."""
    lst = get_or_create_personal_list(user)
    existing = WineListItem.objects.filter(
        list=lst,
        moved_at__isnull=True,
        name=event.wine_name,
        producer=event.producer or "",
        vintage=event.vintage,
    ).first()
    if existing:
        return existing, False
    item = WineListItem.objects.create(
        list=lst,
        name=event.wine_name or "wine",
        producer=event.producer or "",
        vintage=event.vintage,
        source="club_feed",
        added_by=user,
    )
    return item, True


class ReactionsEnabledMixin(ClubsEnabledMixin):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not reactions_enabled():
            raise CommentsDisabled()

    def handle_exception(self, exc):
        if isinstance(exc, CommentsDisabled):
            return Response({"error": "not_enabled"}, status=status.HTTP_404_NOT_FOUND)
        return super().handle_exception(exc)


def _member_event(request, event_id):
    """The event, only when the caller is a member of its club (the same
    audience that was sent the card)."""
    event = (
        ClubEvent.objects.select_related("club", "actor").filter(id=event_id).first()
    )
    if event is None or not ClubMembership.objects.filter(
        user=request.user, club_id=event.club_id,
    ).exists():
        return None
    return event


class ClubEventReactView(ReactionsEnabledMixin, ScannerAPIView):
    """POST {"i": 0-4}: toggle the caller's reaction."""

    def post(self, request, event_id):
        event = _member_event(request, event_id)
        if event is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if event.retracted:
            return Response({"error": "retracted"}, status=status.HTTP_409_CONFLICT)
        if _ask_hidden(event):
            return _ask_off_response()
        if not kind_reactable(event):
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            index = int((request.data or {}).get("i"))
            if not 0 <= index < len(PALETTE):
                raise ValueError
        except (TypeError, ValueError):
            return Response({"error": "bad_index"}, status=status.HTTP_400_BAD_REQUEST)
        on = toggle_reaction(request.user, event, index)
        # Reply pings stay bare: only card copies show counts.
        show_counts = True
        message_id = (request.data or {}).get("message_id")
        chat_id = (request.data or {}).get("chat_id")
        if message_id and chat_id:
            try:
                show_counts = not ClubEventDelivery.objects.filter(
                    event=event, chat_id=str(chat_id), message_id=int(message_id),
                    kind=ClubEventDelivery.KIND_PING,
                ).exists()
            except (TypeError, ValueError):
                pass
        return Response(
            {
                "show_counts": show_counts,
                "on": on,
                "emoji": PALETTE[index],
                "counts": counts_for(event),
                "mine": mine_for(event, request.user),
            }
        )


class ClubEventWantView(ReactionsEnabledMixin, ScannerAPIView):
    """Want this: save the wine to the caller's own To-drink list."""

    def post(self, request, event_id):
        if not flag_on("feature_lists"):
            return Response({"error": "not_enabled"}, status=status.HTTP_404_NOT_FOUND)
        event = _member_event(request, event_id)
        if event is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if event.retracted:
            return Response({"error": "retracted"}, status=status.HTTP_409_CONFLICT)
        if event.kind != ClubEvent.KIND_OPENED:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        item, created = add_event_to_list(request.user, event)
        first = record_want(request.user, event)
        return Response(
            {"saved": True, "created": created, "notified": first},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class RelayEnabledMixin(ClubsEnabledMixin):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not relay_enabled():
            raise CommentsDisabled()

    def handle_exception(self, exc):
        if isinstance(exc, CommentsDisabled):
            return Response({"error": "not_enabled"}, status=status.HTTP_404_NOT_FOUND)
        return super().handle_exception(exc)


class ClubRelayAskView(RelayEnabledMixin, ScannerAPIView):
    """POST {"text"}: Ask the club. Delivery follows after the Undo window."""

    def post(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        text = str((request.data or {}).get("text") or "")
        event, err = create_ask(request.user, membership.club, text)
        if err == "duplicate":
            return Response(
                {"error": "duplicate", "event_id": event.id},
                status=status.HTTP_409_CONFLICT,
            )
        if err == "rate_limited":
            return Response(
                {
                    "error": "rate_limited",
                    "max": RATE_MAX,
                    "retry_minutes": retry_minutes(event),
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if err == "too_long":
            return Response(
                {"error": "too_long", "max": ASK_MAX},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if err == "frozen":
            return Response({"error": "frozen"}, status=status.HTTP_409_CONFLICT)
        if err in ("blank", "global"):
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        if err:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        transaction.on_commit(lambda eid=event.id: schedule_delivery(eid))
        return Response(
            {
                "event_id": event.id,
                "club_name": membership.club.name,
                "undo_seconds": UNDO_SECONDS,
            },
            status=status.HTTP_201_CREATED,
        )


class ClubAskUndoView(RelayEnabledMixin, ScannerAPIView):
    def post(self, request, event_id):
        result = undo_ask(request.user, event_id)
        if result == "already_out":
            return Response({"error": "already_out"}, status=status.HTTP_409_CONFLICT)
        if result == "not_found":
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"undone": True})


class ClubAskCopyView(RelayEnabledMixin, ScannerAPIView):
    """POST {"chat_id", "message_id"}: the asker's confirmation message is
    their own copy of the ask (swipe-replies and reply pings thread under it)."""

    def post(self, request, event_id):
        try:
            message_id = int((request.data or {}).get("message_id"))
        except (TypeError, ValueError):
            return Response(
                {"error": "bad_message"}, status=status.HTTP_400_BAD_REQUEST,
            )
        result = record_asker_copy(
            request.user, event_id, (request.data or {}).get("chat_id") or "",
            message_id,
        )
        if result == "not_found":
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"recorded": True, "already_out": result == "already_out"})


def _club_list(club):
    lst, _ = WineList.objects.get_or_create(
        kind=WineList.KIND_CLUB,
        club=club,
        defaults={"name": club.name[:40], "owner": None},
    )
    return lst


def serialize_club_list_item(item: WineListItem) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "vintage": item.vintage,
        "producer": item.producer,
        "added_by_name": display_name(item.added_by) if item.added_by_id else "",
    }


class ClubSharedListView(ClubListsEnabledMixin, ScannerAPIView):
    def get(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        lst = _club_list(membership.club)
        items = list(
            lst.items.filter(moved_at__isnull=True)
            .select_related("added_by")
            .order_by("created")
        )
        return Response(
            {
                "id": lst.id,
                "name": lst.name,
                "kind": lst.kind,
                "club_id": membership.club_id,
                "items": [serialize_club_list_item(i) for i in items],
            }
        )


class ClubSharedListItemView(ClubListsEnabledMixin, ScannerAPIView):
    def post(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        data = request.data if isinstance(request.data, dict) else {}
        name = str(data.get("name") or "").strip()[:100]
        if len(name) < 2:
            return Response(
                {"error": "name required"}, status=status.HTTP_400_BAD_REQUEST,
            )
        vintage = data.get("vintage")
        try:
            vintage = int(vintage) if vintage not in (None, "") else None
        except (TypeError, ValueError):
            vintage = None
        producer = str(data.get("producer") or "").strip()[:100]
        wine_id = data.get("wine_id")
        wine = None
        if wine_id not in (None, ""):
            wine = _peek_wine_if_full(membership, wine_id)
        lst = _club_list(membership.club)
        item = WineListItem.objects.create(
            list=lst,
            name=name,
            producer=producer,
            vintage=vintage,
            wine=wine,
            source="typed" if wine is None else "club_feed",
            added_by=request.user,
        )
        return Response(serialize_club_list_item(item), status=status.HTTP_201_CREATED)


class ClubSharedListItemDeleteView(ClubListsEnabledMixin, ScannerAPIView):
    def delete(self, request, pk, item_id):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        lst = _club_list(membership.club)
        item = lst.items.filter(id=item_id).first()
        if item is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if (
            membership.role != membership.ROLE_OWNER
            and item.added_by_id != request.user.id
        ):
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        item.delete()
        return Response({"status": "deleted"})


def _peek_wine_if_full(membership, wine_id):
    try:
        wine_id = int(wine_id)
    except (TypeError, ValueError):
        return None
    wine = Wine.objects.filter(id=wine_id).select_related("household").first()
    if wine is None or wine.household_id is None:
        return None
    from wine_cellar.apps.club.models import ClubMembership

    target = (
        ClubMembership.objects.filter(
            club=membership.club, household_id=wine.household_id,
        )
        .select_related("club")
        .first()
    )
    if target is None:
        return None
    vis = tighter_visibility(
        effective_visibility(membership.club, membership),
        effective_visibility(target.club, target),
    )
    if vis != "full":
        return None
    return wine


def serialize_bottle(
    bottle: TasteOffBottle, *, scores=None, hide_others=True, user=None
):
    body = {"id": bottle.id, "label": bottle.label, "sort_order": bottle.sort_order}
    if scores is None:
        return body
    mine = next((s for s in scores if s.user_id == (user.id if user else None)), None)
    body["your_score"] = mine.score if mine else None
    if not hide_others:
        n = len(scores)
        avg = (sum(s.score for s in scores) / n) if n else None
        body["average"] = round(avg, 1) if avg is not None else None
        body["n"] = n
        # Closed tastings only: how each member scored this wine.
        body["votes"] = sorted(
            (
                {"name": display_name(s.user) if s.user_id else "", "score": s.score}
                for s in scores
            ),
            key=lambda v: (-v["score"], v["name"].casefold()),
        )
    return body


def _tasteoff_scorers(tasteoff: TasteOff, n_bottles: int) -> list:
    """Who has scored so far (names only, never their scores), so the host
    knows whether to wait before closing."""
    counts = {}
    for user_id in tasteoff.scores.values_list("user_id", flat=True):
        counts[user_id] = counts.get(user_id, 0) + 1
    rows = []
    memberships = (
        ClubMembership.objects.filter(club_id=tasteoff.club_id)
        .select_related("user")
        .order_by("created", "id")
    )
    for membership in memberships:
        n = counts.get(membership.user_id, 0)
        rows.append({
            "name": display_name(membership.user),
            "scored": n,
            "done": n_bottles > 0 and n >= n_bottles,
        })
    return rows


def _can_reopen(tasteoff: TasteOff, user) -> bool:
    if user is None or tasteoff.status != TasteOff.STATUS_CLOSED:
        return False
    if tasteoff.closed_at is None:
        return False
    if timezone.now() - tasteoff.closed_at > TASTEOFF_REOPEN_WINDOW:
        return False
    if tasteoff.host_id != user.id and not ClubMembership.objects.filter(
        club_id=tasteoff.club_id, user=user, role=ClubMembership.ROLE_OWNER,
    ).exists():
        return False
    return not TasteOff.objects.filter(
        club_id=tasteoff.club_id,
        status__in=(TasteOff.STATUS_SETUP, TasteOff.STATUS_SCORING),
    ).exists()


def serialize_tasteoff(tasteoff: TasteOff, *, user=None, include_scores=False):
    bottles = list(tasteoff.bottles.all())
    score_map = {}
    if include_scores or tasteoff.status == TasteOff.STATUS_CLOSED:
        for score in tasteoff.scores.select_related("bottle", "user"):
            score_map.setdefault(score.bottle_id, []).append(score)
    hide = tasteoff.status != TasteOff.STATUS_CLOSED
    return {
        "id": tasteoff.id,
        "title": tasteoff.title,
        "status": tasteoff.status,
        "closed_at": tasteoff.closed_at.isoformat() if tasteoff.closed_at else None,
        "host_id": tasteoff.host_id,
        # The bot can't tell who's asking; only the host may set up or open.
        "is_host": bool(user is not None and tasteoff.host_id == user.id),
        "host_name": display_name(tasteoff.host) if tasteoff.host_id else "",
        "scorers": _tasteoff_scorers(tasteoff, len(bottles)),
        "can_reopen": _can_reopen(tasteoff, user),
        "bottles": [
            serialize_bottle(
                b,
                scores=score_map.get(b.id, []),
                hide_others=hide,
                user=user,
            )
            for b in bottles
        ],
    }


class TasteOffCollectionView(TasteoffsEnabledMixin, ScannerAPIView):
    def get(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        rows = list(
            TasteOff.objects.filter(club=membership.club).order_by("-created")[:20]
        )
        return Response(
            {
                "tasteoffs": [
                    serialize_tasteoff(row, user=request.user, include_scores=True)
                    for row in rows
                ],
            }
        )

    def post(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        existing = TasteOff.objects.filter(
            club=membership.club,
            status__in=(TasteOff.STATUS_SETUP, TasteOff.STATUS_SCORING),
        ).first()
        if existing is not None:
            return Response(
                {"error": "already_open", "id": existing.id},
                status=status.HTTP_409_CONFLICT,
            )
        title = str((request.data or {}).get("title") or "").strip()[:80]
        if len(title) < 2:
            return Response(
                {"error": "title required"}, status=status.HTTP_400_BAD_REQUEST,
            )
        tasteoff = TasteOff.objects.create(
            club=membership.club, host=request.user, title=title,
        )
        return Response(
            serialize_tasteoff(tasteoff, user=request.user),
            status=status.HTTP_201_CREATED,
        )


class TasteOffDetailView(TasteoffsEnabledMixin, ScannerAPIView):
    def get(self, request, pk, tid):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        tasteoff = TasteOff.objects.filter(club=membership.club, id=tid).first()
        if tasteoff is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            serialize_tasteoff(tasteoff, user=request.user, include_scores=True)
        )


class TasteOffBottleView(TasteoffsEnabledMixin, ScannerAPIView):
    def post(self, request, pk, tid):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        tasteoff = TasteOff.objects.filter(club=membership.club, id=tid).first()
        if tasteoff is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if tasteoff.host_id != request.user.id:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        if tasteoff.status != TasteOff.STATUS_SETUP:
            return Response({"error": "not_setup"}, status=status.HTTP_409_CONFLICT)
        if tasteoff.bottles.count() >= 8:
            return Response(
                {"error": "max_bottles"}, status=status.HTTP_400_BAD_REQUEST
            )
        label = str((request.data or {}).get("label") or "").strip()[:160]
        if len(label) < 2:
            return Response(
                {"error": "label required"}, status=status.HTTP_400_BAD_REQUEST
            )
        wine_id = (request.data or {}).get("wine_id")
        stored_wine_id = None
        if wine_id not in (None, ""):
            wine = _peek_wine_if_full(membership, wine_id)
            stored_wine_id = wine.id if wine is not None else None
        order = tasteoff.bottles.count()
        bottle = TasteOffBottle.objects.create(
            tasteoff=tasteoff, label=label, wine_id=stored_wine_id, sort_order=order,
        )
        return Response(
            {"id": bottle.id, "label": bottle.label, "sort_order": bottle.sort_order},
            status=status.HTTP_201_CREATED,
        )


class TasteOffBottleDeleteView(TasteoffsEnabledMixin, ScannerAPIView):
    def delete(self, request, pk, tid, bid):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        tasteoff = TasteOff.objects.filter(club=membership.club, id=tid).first()
        if tasteoff is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if tasteoff.host_id != request.user.id:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        if tasteoff.status != TasteOff.STATUS_SETUP:
            return Response({"error": "not_setup"}, status=status.HTTP_409_CONFLICT)
        bottle = tasteoff.bottles.filter(id=bid).first()
        if bottle is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        bottle.delete()
        return Response({"status": "deleted"})


class TasteOffOpenView(TasteoffsEnabledMixin, ScannerAPIView):
    def post(self, request, pk, tid):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        tasteoff = TasteOff.objects.filter(club=membership.club, id=tid).first()
        if tasteoff is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if tasteoff.host_id != request.user.id:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        if tasteoff.status != TasteOff.STATUS_SETUP:
            return Response({"error": "not_setup"}, status=status.HTTP_409_CONFLICT)
        if not tasteoff.bottles.exists():
            return Response({"error": "need_wine"}, status=status.HTTP_409_CONFLICT)
        try:
            with transaction.atomic():
                tasteoff.status = TasteOff.STATUS_SCORING
                tasteoff.opened_at = timezone.now()
                tasteoff.save(update_fields=["status", "opened_at"])
        except IntegrityError:
            return Response(
                {"error": "already_open"}, status=status.HTTP_409_CONFLICT,
            )
        from wine_cellar.apps.club.tasks import deliver_tasteoff_open

        transaction.on_commit(lambda i=tasteoff.id: deliver_tasteoff_open.delay(i))
        return Response(serialize_tasteoff(tasteoff, user=request.user))


class TasteOffScoreView(TasteoffsEnabledMixin, ScannerAPIView):
    def patch(self, request, pk, tid):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        tasteoff = TasteOff.objects.filter(club=membership.club, id=tid).first()
        if tasteoff is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if tasteoff.status != TasteOff.STATUS_SCORING:
            return Response({"error": "not_scoring"}, status=status.HTTP_409_CONFLICT)
        try:
            bottle_id = int((request.data or {}).get("bottle_id"))
            score = int((request.data or {}).get("score"))
        except (TypeError, ValueError):
            return Response({"error": "bad_score"}, status=status.HTTP_400_BAD_REQUEST)
        if score < 1 or score > 5:
            return Response({"error": "bad_score"}, status=status.HTTP_400_BAD_REQUEST)
        bottle = tasteoff.bottles.filter(id=bottle_id).first()
        if bottle is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        row, _ = TasteOffScore.objects.update_or_create(
            tasteoff=tasteoff, bottle=bottle, user=request.user,
            defaults={"score": score},
        )
        return Response({"bottle_id": bottle.id, "score": row.score})


class TasteOffCloseView(TasteoffsEnabledMixin, ScannerAPIView):
    def post(self, request, pk, tid):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        tasteoff = TasteOff.objects.filter(club=membership.club, id=tid).first()
        if tasteoff is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if (
            tasteoff.host_id != request.user.id
            and membership.role != membership.ROLE_OWNER
        ):
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        if tasteoff.status == TasteOff.STATUS_CLOSED:
            return Response(
                {"error": "already_closed"}, status=status.HTTP_409_CONFLICT
            )
        close_tasteoff(tasteoff)
        tasteoff.refresh_from_db()
        return Response(
            serialize_tasteoff(tasteoff, user=request.user, include_scores=True)
        )


class TasteOffReopenView(TasteoffsEnabledMixin, ScannerAPIView):
    """Host or club owner: put a closed taste-off back into scoring (24h)."""

    def post(self, request, pk, tid):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        tasteoff = TasteOff.objects.filter(club=membership.club, id=tid).first()
        if tasteoff is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if (
            tasteoff.host_id != request.user.id
            and membership.role != membership.ROLE_OWNER
        ):
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        tasteoff, err = reopen_tasteoff(tasteoff)
        if err == "not_found":
            return Response({"error": err}, status=status.HTTP_404_NOT_FOUND)
        if err:
            return Response({"error": err}, status=status.HTTP_409_CONFLICT)
        return Response(
            serialize_tasteoff(tasteoff, user=request.user, include_scores=True)
        )
