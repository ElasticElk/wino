"""Global (staff-run) clubs: discover, join, notes, kick."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count
from rest_framework import status
from rest_framework.response import Response

from wine_cellar.apps.api.club_views import ClubsEnabledMixin
from wine_cellar.apps.api.views import ScannerAPIView
from wine_cellar.apps.club.models import Club, ClubEvent, ClubMembership
from wine_cellar.apps.club.services import (
    add_member,
    create_club,
    serialize_club_home,
)
from wine_cellar.apps.scanner.flags import flag_on
from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.scanner.models import TelegramUser


class GlobalClubsDisabled(Exception):
    pass


class GlobalClubsEnabledMixin(ClubsEnabledMixin):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not flag_on("feature_global_clubs"):
            raise GlobalClubsDisabled()

    def handle_exception(self, exc):
        if isinstance(exc, GlobalClubsDisabled):
            return Response(
                {"error": "not_enabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return super().handle_exception(exc)


def _staff(request):
    return bool(getattr(request.user, "is_staff", False))


class GlobalClubCreateView(GlobalClubsEnabledMixin, ScannerAPIView):
    def post(self, request):
        if not _staff(request):
            return Response(
                {"error": "staff_only"},
                status=status.HTTP_403_FORBIDDEN,
            )
        data = request.data if isinstance(request.data, dict) else {}
        description = str(data.get("description") or "").strip()
        household = get_user_household(request.user)
        membership, err = create_club(
            request.user,
            data.get("name") or "",
            cellar_visibility="none",
            badges_visible=False,
            household=household,
            kind=Club.KIND_GLOBAL,
            description=description,
        )
        if err == "name_length":
            return Response(
                {"error": "name must be 2–40 characters"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if err == "name_taken":
            return Response(
                {"error": "You already have a club with that name."},
                status=status.HTTP_409_CONFLICT,
            )
        club = membership.club
        return Response(
            {
                "id": club.id,
                "name": club.name,
                "kind": club.kind,
                "role": membership.role,
                "cellar_visibility": club.cellar_visibility,
                "badges_visible": club.badges_visible,
                "sharing_version": club.sharing_version,
                "member_count": 1,
                "description": club.description,
                "frozen": club.frozen,
            },
            status=status.HTTP_201_CREATED,
        )


class ClubDiscoverView(GlobalClubsEnabledMixin, ScannerAPIView):
    def get(self, request):
        joined_ids = set(
            ClubMembership.objects.filter(user=request.user).values_list(
                "club_id", flat=True,
            )
        )
        clubs = (
            Club.objects.filter(kind=Club.KIND_GLOBAL)
            .annotate(n_members=Count("memberships"))
            .order_by("name")
        )
        rows = []
        for club in clubs:
            rows.append(
                {
                    "id": club.id,
                    "name": club.name,
                    "description": club.description,
                    "member_count": club.n_members,
                    "joined": club.id in joined_ids,
                    "frozen": club.frozen,
                }
            )
        return Response({"clubs": rows})


class ClubJoinView(GlobalClubsEnabledMixin, ScannerAPIView):
    def post(self, request, pk):
        club = Club.objects.filter(id=pk).first()
        if club is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if club.kind != Club.KIND_GLOBAL:
            return Response(
                {"error": "invite_only"},
                status=status.HTTP_403_FORBIDDEN,
            )
        existing = ClubMembership.objects.filter(club=club, user=request.user).first()
        if existing is not None:
            return Response(serialize_club_home(existing))
        if club.frozen:
            return Response(
                {"error": "frozen"},
                status=status.HTTP_409_CONFLICT,
            )
        household = get_user_household(request.user)
        try:
            membership, already = add_member(
                club, request.user, household, already_ok=True,
            )
        except IntegrityError:
            existing = ClubMembership.objects.filter(
                club=club, user=request.user,
            ).first()
            if existing is not None:
                return Response(serialize_club_home(existing))
            raise
        if already:
            return Response(serialize_club_home(membership))
        from wine_cellar.apps.club.services import record_join_event_delivery

        record_join_event_delivery(club, request.user)
        return Response(
            serialize_club_home(membership),
            status=status.HTTP_201_CREATED,
        )


class ClubNoteView(GlobalClubsEnabledMixin, ScannerAPIView):
    def post(self, request, pk):
        if not _staff(request):
            return Response(
                {"error": "staff_only"},
                status=status.HTTP_403_FORBIDDEN,
            )
        club = Club.objects.filter(id=pk).first()
        if club is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if club.kind != Club.KIND_GLOBAL:
            return Response(
                {"error": "not_global"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if club.frozen:
            return Response(
                {"error": "frozen"},
                status=status.HTTP_409_CONFLICT,
            )
        text = str((request.data or {}).get("text") or "").strip()
        if not text:
            return Response({"error": "blank"}, status=status.HTTP_400_BAD_REQUEST)
        event = ClubEvent.objects.create(
            club=club,
            actor=request.user,
            kind=ClubEvent.KIND_STAFF_NOTE,
            payload={"text": text[:1000]},
        )
        from wine_cellar.apps.club.tasks import deliver_club_event

        transaction.on_commit(lambda eid=event.id: deliver_club_event.delay(eid))
        return Response({"event_id": event.id}, status=status.HTTP_201_CREATED)


class ClubKickView(GlobalClubsEnabledMixin, ScannerAPIView):
    def post(self, request, pk, user_id):
        if not _staff(request):
            return Response(
                {"error": "staff_only"},
                status=status.HTTP_403_FORBIDDEN,
            )
        club = Club.objects.filter(id=pk).first()
        if club is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        target = ClubMembership.objects.filter(club=club, user_id=user_id).first()
        if target is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if target.role == ClubMembership.ROLE_OWNER:
            return Response(
                {"error": "forbidden"},
                status=status.HTTP_403_FORBIDDEN,
            )
        chats = list(
            TelegramUser.objects.filter(user=target.user, is_active=True).values_list(
                "chat_id", flat=True,
            )
        )
        club_name = club.name
        target.delete()
        from wine_cellar.apps.club.tasks import notify_kicked

        transaction.on_commit(
            lambda n=club_name, c=chats: notify_kicked.delay(n, c)
        )
        return Response({"status": "kicked"})
