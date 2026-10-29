"""Club REST API. Keyed by request.user, never by X-Household as the club id."""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from django.contrib.auth.decorators import login_not_required
from django.utils.decorators import method_decorator

from wine_cellar.apps.api.views import ScannerAPIView
from wine_cellar.apps.club.models import (
    CLUB_INVITE_MAX_USES,
    VISIBILITY_RANK,
    Club,
    ClubDismantleVote,
    ClubEvent,
    ClubInvite,
    ClubMembership,
)
from wine_cellar.apps.club.peek import (
    effective_badges,
    effective_visibility,
    peek_full,
    peek_summary,
    tighter_visibility,
)
from wine_cellar.apps.club.services import (
    add_member,
    apply_policy,
    cancel_dismantle,
    cast_ballot,
    create_club,
    create_dismantle_vote,
    expire_if_needed,
    handover_club,
    leave_club,
    maybe_queue_share_notice,
    membership_of,
    record_join_event_delivery,
    serialize_club_home,
    serialize_club_list_item,
    serialize_event,
    serialize_invite,
    serialize_vote,
    set_last_used,
)
from wine_cellar.apps.scanner.flags import flag_on
from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.scanner.models import (
    INVITE_TTL,
    Household,
    HouseholdMembership,
    mint_unique_code,
)
from wine_cellar.apps.scanner.views import (
    _attach_to_household,
    _get_or_create_telegram_user,
    _name_taken_response,
    _redeem_user_payload,
    mask_invite_code,
)
from wine_cellar.apps.storage.models import Storage
from wine_cellar.apps.wine.achievements import (
    month_tally,
    progress,
    serialize_achievement,
)
from wine_cellar.apps.wine.models import CellarAchievement

User = get_user_model()
logger = logging.getLogger(__name__)


class ClubsDisabled(Exception):
    pass


class ClubsEnabledMixin:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not flag_on("feature_clubs"):
            raise ClubsDisabled()

    def handle_exception(self, exc):
        if isinstance(exc, ClubsDisabled):
            return Response(
                {"error": "not_enabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return super().handle_exception(exc)


def _member_or_404(request, club_id):
    membership = membership_of(request.user, club_id)
    if membership is None:
        return None, Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
    return membership, None


def lock_club_invite_qs(code):
    """Lock the invite row only. Do not join club under FOR UPDATE."""
    return ClubInvite.objects.select_for_update().filter(code=code)


class ClubCollectionView(ClubsEnabledMixin, ScannerAPIView):
    def get(self, request):
        memberships = (
            ClubMembership.objects.filter(user=request.user)
            .select_related("club")
            .order_by("-is_last_used", "club__name")
        )
        return Response(
            {"clubs": [serialize_club_list_item(m) for m in memberships]}
        )

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        if str(data.get("kind") or "").lower() == Club.KIND_GLOBAL:
            return Response(
                {"error": "staff_only"},
                status=status.HTTP_403_FORBIDDEN,
            )
        vis = data.get("cellar_visibility")
        if vis in (None, ""):
            vis = "full"
        if vis not in VISIBILITY_RANK:
            return Response(
                {"error": "invalid_visibility"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        badges = data.get("badges_visible")
        if badges is None or badges == "":
            badges = True
        household = None
        try:
            household = self.get_household(request)
        except Household.DoesNotExist:
            household = get_user_household(request.user)
        membership, err = create_club(
            request.user,
            data.get("name") or "",
            cellar_visibility=vis,
            badges_visible=bool(badges),
            household=household,
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
            },
            status=status.HTTP_201_CREATED,
        )


class ClubDetailView(ClubsEnabledMixin, ScannerAPIView):
    def get(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        set_last_used(request.user, membership)
        return Response(serialize_club_home(membership))

    def patch(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        if membership.role != ClubMembership.ROLE_OWNER:
            return Response(
                {"error": "forbidden"},
                status=status.HTTP_403_FORBIDDEN,
            )
        club = membership.club
        if club.kind == Club.KIND_GLOBAL:
            vis = request.data.get("cellar_visibility")
            if vis not in (None, "", "none"):
                return Response(
                    {"error": "global_clubs_are_feed_only"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        vis = request.data.get("cellar_visibility")
        badges = request.data.get("badges_visible")
        if vis is None and badges is None:
            return Response(serialize_club_home(membership))
        change, club = apply_policy(club, vis, badges)
        if change == "widen":
            from wine_cellar.apps.club.tasks import club_widen_notice
            from django.db import transaction as db_tx

            db_tx.on_commit(lambda cid=club.id: club_widen_notice.delay(cid))
        membership.refresh_from_db()
        return Response(serialize_club_home(membership))


class ClubMembershipView(ClubsEnabledMixin, ScannerAPIView):
    def patch(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        data = request.data if isinstance(request.data, dict) else {}
        fields = []
        if "feed_muted" in data:
            membership.feed_muted = bool(data.get("feed_muted"))
            fields.append("feed_muted")
        if "household_id" in data:
            try:
                hid = int(data.get("household_id"))
            except (TypeError, ValueError):
                return Response(
                    {"error": "invalid_household"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not HouseholdMembership.objects.filter(
                user=request.user, household_id=hid,
            ).exists():
                return Response(
                    {"error": "invalid_household"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            membership.household_id = hid
            fields.append("household_id")
        if "accepted_sharing_version" in data:
            try:
                version = int(data.get("accepted_sharing_version"))
            except (TypeError, ValueError):
                return Response(
                    {"error": "invalid_version"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if version != membership.club.sharing_version:
                return Response(
                    {"error": "stale_version"},
                    status=status.HTTP_409_CONFLICT,
                )
            membership.accepted_sharing_version = version
            fields.append("accepted_sharing_version")
        if fields:
            membership.save(update_fields=list(dict.fromkeys(fields)))
        membership.refresh_from_db()
        return Response(serialize_club_home(membership))


class ClubLeaveView(ClubsEnabledMixin, ScannerAPIView):
    def post(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        result, _club_id = leave_club(membership)
        if result == "handover":
            return Response(
                {"error": "hand_over_required"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({"status": result})


class ClubHandoverView(ClubsEnabledMixin, ScannerAPIView):
    def post(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        if membership.role != ClubMembership.ROLE_OWNER:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        try:
            user_id = int(request.data.get("user_id"))
        except (TypeError, ValueError):
            return Response(
                {"error": "user_id required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target = User.objects.filter(id=user_id).first()
        if target is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        other = handover_club(membership, target)
        if other is None:
            return Response(
                {"error": "not_a_member"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        membership.refresh_from_db()
        return Response(serialize_club_home(membership))


class ClubFeedView(ClubsEnabledMixin, ScannerAPIView):
    def get(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 5), 50))
        except (TypeError, ValueError):
            limit = 5
        try:
            offset = max(0, int(request.query_params.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        qs = ClubEvent.objects.filter(club=membership.club).select_related("actor")
        from django.db.models import Count

        qs = qs.annotate(comment_count=Count("comments"))
        event_id = request.query_params.get("event_id")
        if event_id not in (None, ""):
            try:
                qs = qs.filter(id=int(event_id))
            except (TypeError, ValueError):
                qs = qs.none()
        count = qs.count()
        events = list(qs.order_by("-created")[offset : offset + limit])
        return Response(
            {
                "count": count,
                "offset": offset,
                "limit": limit,
                "events": [serialize_event(e, public=False) for e in events],
            }
        )


class ClubMembersView(ClubsEnabledMixin, ScannerAPIView):
    def get(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        rows = list(
            ClubMembership.objects.filter(club=membership.club)
            .select_related("user", "household", "club")
            .order_by("created")
        )
        from wine_cellar.apps.club.services import display_name

        members = []
        households = {}
        viewer_vis = effective_visibility(membership.club, membership)
        viewer_badges = effective_badges(membership.club, membership)
        for row in rows:
            members.append(
                {
                    "user_id": row.user_id,
                    "display_name": display_name(row.user),
                    "role": row.role,
                    "household_id": row.household_id,
                    "household_name": row.household.name if row.household_id else "",
                    "peek": tighter_visibility(
                        viewer_vis, effective_visibility(row.club, row),
                    ),
                    "badges": viewer_badges and effective_badges(row.club, row),
                }
            )
            if row.household_id:
                bucket = households.setdefault(
                    row.household_id,
                    {
                        "household_id": row.household_id,
                        "name": row.household.name,
                        "user_ids": [],
                    },
                )
                bucket["user_ids"].append(row.user_id)
        return Response(
            {"members": members, "households": list(households.values())}
        )


class ClubPeekView(ClubsEnabledMixin, ScannerAPIView):
    def get(self, request, pk, user_id):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        target = (
            ClubMembership.objects.select_related("club", "household")
            .filter(club=membership.club, user_id=user_id)
            .first()
        )
        if target is None or target.household_id is None:
            return Response(
                {"error": "not_visible"},
                status=status.HTTP_404_NOT_FOUND,
            )
        vis = tighter_visibility(
            effective_visibility(target.club, membership),
            effective_visibility(target.club, target),
        )
        if vis == "none":
            return Response(
                {"error": "not_visible"},
                status=status.HTTP_404_NOT_FOUND,
            )
        household = target.household
        body = peek_full(household) if vis == "full" else peek_summary(household)
        return Response(body)


class ClubBadgesView(ClubsEnabledMixin, ScannerAPIView):
    def get(self, request, pk, user_id):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        target = (
            ClubMembership.objects.select_related("club", "household")
            .filter(club=membership.club, user_id=user_id)
            .first()
        )
        if target is None or target.household_id is None:
            return Response(
                {"error": "not_visible"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not (
            effective_badges(target.club, membership)
            and effective_badges(target.club, target)
        ):
            return Response(
                {"error": "not_visible"},
                status=status.HTTP_404_NOT_FOUND,
            )
        household = target.household
        if not household.gamification_enabled:
            return Response(
                {"error": "not_visible"},
                status=status.HTTP_404_NOT_FOUND,
            )
        unlocks = CellarAchievement.objects.filter(household=household)[:50]
        return Response(
            {
                "enabled": True,
                "achievements": [serialize_achievement(a) for a in unlocks],
                "this_month": month_tally(household),
                "progress": progress(household),
            }
        )


class ClubInviteCreateView(ClubsEnabledMixin, ScannerAPIView):
    def post(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        if membership.club.open_dismantle_vote() is not None:
            return Response(
                {"error": "dismantle_in_progress"},
                status=status.HTTP_409_CONFLICT,
            )
        invite = _mint_club_invite(request.user, membership.club)
        if invite is None:
            return Response(
                {"error": "could not mint a unique invite code"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(serialize_invite(invite), status=status.HTTP_201_CREATED)


class ClubInviteCancelView(ClubsEnabledMixin, ScannerAPIView):
    def post(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        invite = (
            ClubInvite.objects.filter(
                club=membership.club,
                created_by=request.user,
                revoked_at__isnull=True,
                use_count=0,
                expires_at__gt=timezone.now(),
            )
            .order_by("-created")
            .first()
        )
        if invite is None:
            return Response(
                {"error": "No unused invite to cancel"},
                status=status.HTTP_404_NOT_FOUND,
            )
        payload = serialize_invite(invite)
        payload["status"] = "cancelled"
        invite.delete()
        return Response(payload)


def _mint_club_invite(user, club):
    for _ in range(5):
        code = mint_unique_code()
        if not code:
            break
        try:
            return ClubInvite.objects.create(
                club=club,
                created_by=user,
                code=code,
                expires_at=timezone.now() + INVITE_TTL,
                max_uses=CLUB_INVITE_MAX_USES,
            )
        except IntegrityError:
            continue
    logger.error("could not mint a unique club invite code")
    return None


class ClubDismantleView(ClubsEnabledMixin, ScannerAPIView):
    def get(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        vote = membership.club.open_dismantle_vote()
        if vote is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        vote = expire_if_needed(vote)
        if vote.status != ClubDismantleVote.STATUS_OPEN:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_vote(vote))

    def post(self, request, pk):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        if membership.role != ClubMembership.ROLE_OWNER:
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        vote, err = create_dismantle_vote(membership.club, request.user)
        if err == "sole_member":
            return Response({"error": "sole_member"}, status=status.HTTP_409_CONFLICT)
        if err == "vote_open":
            return Response({"error": "vote_open"}, status=status.HTTP_409_CONFLICT)
        return Response(serialize_vote(vote), status=status.HTTP_201_CREATED)


class ClubDismantleBallotView(ClubsEnabledMixin, ScannerAPIView):
    def post(self, request, pk, vote_id):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        vote = ClubDismantleVote.objects.filter(
            id=vote_id, club=membership.club,
        ).first()
        if vote is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        choice = str(request.data.get("choice") or "").strip().lower()
        vote, err = cast_ballot(vote, request.user, choice)
        if err == "over":
            return Response({"error": "vote_closed"}, status=status.HTTP_409_CONFLICT)
        if err == "no_ballot":
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if err == "bad_choice":
            return Response({"error": "bad_choice"}, status=status.HTTP_400_BAD_REQUEST)
        if vote.status == ClubDismantleVote.STATUS_PASSED:
            return Response({"status": "passed", **serialize_vote(vote)})
        return Response(serialize_vote(vote))


class ClubDismantleCancelView(ClubsEnabledMixin, ScannerAPIView):
    def post(self, request, pk, vote_id):
        membership, error = _member_or_404(request, pk)
        if error:
            return error
        vote = ClubDismantleVote.objects.filter(
            id=vote_id, club=membership.club,
        ).first()
        if vote is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if (
            vote.proposed_by_id != request.user.id
            and not request.user.is_staff
        ):
            return Response({"error": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
        vote, err = cancel_dismantle(vote)
        if err == "over":
            return Response({"error": "vote_closed"}, status=status.HTTP_409_CONFLICT)
        return Response(serialize_vote(vote))


@method_decorator(login_not_required, name="dispatch")
class ClubInviteRedeemView(APIView):
    """Staff: provision a club membership, compounding a new cellar if needed."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAdminUser]

    def post(self, request):
        if not flag_on("feature_clubs"):
            return Response({"error": "not_enabled"}, status=status.HTTP_404_NOT_FOUND)
        code = (request.data.get("code") or "").strip()
        chat_id = str(request.data.get("chat_id") or "").strip()
        display_name = (request.data.get("display_name") or "").strip()
        household_name = (request.data.get("household_name") or "").strip()
        raw_hh = request.data.get("household_id")
        household_id = None
        if raw_hh not in (None, ""):
            try:
                household_id = int(raw_hh)
            except (TypeError, ValueError):
                return Response(
                    {"error": "household_id must be an integer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        if not code:
            return Response({"error": "code is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not chat_id:
            return Response({"error": "chat_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not display_name:
            return Response(
                {"error": "display_name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return self._redeem(
                code, chat_id, display_name, household_name, household_id,
            )
        except Exception:
            logger.exception("club invite redeem failed")
            raise

    def _redeem(self, code, chat_id, display_name, household_name, household_id):
        with transaction.atomic():
            invite = lock_club_invite_qs(code).first()
            if invite is None:
                return Response(
                    {"error": "Invite not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            club = Club.objects.get(pk=invite.club_id)
            if club.open_dismantle_vote() is not None:
                return Response(
                    {"error": "dismantle_in_progress"},
                    status=status.HTTP_409_CONFLICT,
                )
            if invite.is_expired or invite.revoked_at is not None:
                return Response(
                    {"error": "This invite was already used or has expired"},
                    status=status.HTTP_410_GONE,
                )
            if invite.use_count >= invite.max_uses:
                return Response(
                    {"error": "This invite was already used or has expired"},
                    status=status.HTTP_410_GONE,
                )

            from wine_cellar.apps.scanner.models import TelegramUser

            existing_tu = TelegramUser.objects.filter(chat_id=chat_id).first()
            if existing_tu is None and not household_name:
                return Response(
                    {"error": "household_name is required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            created_cellar = False
            if existing_tu is None:
                if Household.objects.filter(name__iexact=household_name).exists():
                    return _name_taken_response(household_name, display_name)
                household = Household.objects.create(name=household_name)
                tu, created, _username = _get_or_create_telegram_user(
                    chat_id, display_name, source_user=None,
                )
                _attach_to_household(tu, household, "owner")
                storage = Storage.objects.filter(household=household).first()
                if storage is not None and storage.name != "Cellar":
                    storage.name = "Cellar"
                    storage.save(update_fields=["name"])
                elif storage is None:
                    Storage.objects.create(
                        name="Cellar",
                        user=tu.user,
                        household=household,
                        description="Bottles whose location hasn't been set yet",
                        location="Cellar",
                        rows=0,
                        columns=0,
                    )
                created_cellar = True
            else:
                tu, created, _username = _get_or_create_telegram_user(
                    chat_id, display_name, source_user=invite.created_by,
                )
                household = None
                if household_id:
                    household = Household.objects.filter(
                        id=household_id,
                        memberships__user=tu.user,
                    ).first()
                if household is None:
                    household = get_user_household(tu.user)
                if household is None:
                    return Response(
                        {"error": "household_name is required"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            membership, already = add_member(club, tu.user, household)
            if not already:
                invite.use_count = invite.use_count + 1
                if invite.used_at is None:
                    invite.used_at = timezone.now()
                invite.save(update_fields=["use_count", "used_at"])
                logger.info(
                    "club invite redeemed invite_id=%s club_id=%s code=%s",
                    invite.id, club.id, mask_invite_code(code),
                )
                record_join_event_delivery(club, tu.user)
                if club.cellar_visibility in ("summary", "full"):
                    maybe_queue_share_notice(household, tu.user_id)

            payload = _redeem_user_payload(
                tu, household, membership.role,
                created=created, already_member=already,
            )
            payload["club"] = {
                "id": club.id,
                "name": club.name,
                "cellar_visibility": club.cellar_visibility,
                "badges_visible": club.badges_visible,
            }
            payload["role"] = membership.role
            payload["accepted_sharing_version"] = membership.accepted_sharing_version
            payload["first_cellar"] = created_cellar
            from wine_cellar.apps.scanner.models import TelegramUser as TU

            inviter = TU.objects.filter(user=invite.created_by).first()
            payload["created_by_chat_id"] = inviter.chat_id if inviter else None
            if inviter:
                payload["created_by_display_name"] = inviter.display_name
            if already:
                payload["status"] = "already_member"
            return Response(
                payload,
                status=status.HTTP_200_OK if already else status.HTTP_201_CREATED,
            )
