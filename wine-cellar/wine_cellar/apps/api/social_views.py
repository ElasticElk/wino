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
    TasteOff,
    TasteOffBottle,
    TasteOffScore,
)
from wine_cellar.apps.club.peek import effective_visibility, tighter_visibility
from wine_cellar.apps.club.services import close_tasteoff, display_name
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
        from wine_cellar.apps.club.tasks import refresh_event_comment_card

        transaction.on_commit(
            lambda eid=event.id: refresh_event_comment_card.delay(eid)
        )
        return Response(serialize_comment(comment), status=status.HTTP_201_CREATED)


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
        lst = get_or_create_personal_list(request.user)
        existing = WineListItem.objects.filter(
            list=lst,
            moved_at__isnull=True,
            name=event.wine_name,
            producer=event.producer or "",
            vintage=event.vintage,
        ).first()
        if existing:
            body = serialize_item(existing)
            body["status_code"] = 200
            return Response(body)
        item = WineListItem.objects.create(
            list=lst,
            name=event.wine_name or "wine",
            producer=event.producer or "",
            vintage=event.vintage,
            source="club_feed",
            added_by=request.user,
        )
        return Response(serialize_item(item), status=status.HTTP_201_CREATED)


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


def serialize_bottle(bottle: TasteOffBottle, *, scores=None, hide_others=True, user=None):
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
    return body


def serialize_tasteoff(tasteoff: TasteOff, *, user=None, include_scores=False):
    bottles = list(tasteoff.bottles.all())
    score_map = {}
    if include_scores or tasteoff.status == TasteOff.STATUS_CLOSED:
        for score in tasteoff.scores.select_related("bottle"):
            score_map.setdefault(score.bottle_id, []).append(score)
    hide = tasteoff.status != TasteOff.STATUS_CLOSED
    return {
        "id": tasteoff.id,
        "title": tasteoff.title,
        "status": tasteoff.status,
        "host_id": tasteoff.host_id,
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
            TasteOff.objects.filter(club=membership.club).order_by("-created")[:8]
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
        return Response(serialize_tasteoff(tasteoff, user=request.user, include_scores=True))


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
            return Response({"error": "max_bottles"}, status=status.HTTP_400_BAD_REQUEST)
        label = str((request.data or {}).get("label") or "").strip()[:160]
        if len(label) < 2:
            return Response({"error": "label required"}, status=status.HTTP_400_BAD_REQUEST)
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
        if tasteoff.bottles.count() < 2:
            return Response({"error": "need_two"}, status=status.HTTP_409_CONFLICT)
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
            return Response({"error": "already_closed"}, status=status.HTTP_409_CONFLICT)
        close_tasteoff(tasteoff)
        tasteoff.refresh_from_db()
        return Response(serialize_tasteoff(tasteoff, user=request.user, include_scores=True))
