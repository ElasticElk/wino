import logging
import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_not_required
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.user.models import UserSettings
from wine_cellar.apps.wine.achievements import (
    backfill,
    month_tally,
    progress,
    serialize_achievement,
)
from wine_cellar.apps.wine.models import CellarAchievement, Wine
from wine_cellar.apps.wine.taste import record_intent, serialize_snapshot

from .models import (
    INVITE_TTL,
    Household, HouseholdInvite, HouseholdMembership, Scanner, ServiceConfig, TelegramUser,
)

User = get_user_model()
logger = logging.getLogger(__name__)


def lock_invite_qs(code):
    """Lock the invite row only. Do not join household — it is null on a
    new-cellar invite, and Postgres rejects FOR UPDATE on that outer join.
    """
    return HouseholdInvite.objects.select_for_update().filter(code=code)


def _unique_username(display_name: str) -> str:
    username = (display_name.lower().replace(" ", "_") or "user")[:30]
    base = username
    counter = 1
    while User.objects.filter(username=username).exists():
        username = f"{base}_{counter}"
        counter += 1
    return username


def mask_invite_code(code: str) -> str:
    """Log codes without writing the secret. Last two chars are enough to match."""
    code = code or ""
    if len(code) < 2:
        return "…"
    return f"…{code[-2:]}"


def ensure_user_settings(user, source_user=None):
    """Every Telegram user needs a settings row or Grok prices fall back to EUR.

    Join copies the inviter so they share the household's money. New cellars
    start at EUR with no country — the bot asks on first /start.
    """
    existing = UserSettings.objects.filter(user=user).first()
    if existing:
        return existing
    currency = "EUR"
    country = ""
    if source_user is not None:
        src = UserSettings.objects.filter(user=source_user).first()
        if src:
            currency = src.currency or "EUR"
            country = src.country or ""
    return UserSettings.objects.create(
        user=user, currency=currency, country=country,
    )


def _get_or_create_telegram_user(chat_id: str, display_name: str, source_user=None):
    """Return (telegram_user, created, username)."""
    existing = TelegramUser.objects.filter(chat_id=chat_id).first()
    if existing:
        Token.objects.get_or_create(user=existing.user)
        ensure_user_settings(existing.user, source_user=source_user)
        return existing, False, existing.user.username
    username = _unique_username(display_name)
    new_user = User.objects.create_user(
        username=username,
        password=get_random_string(24),
    )
    Token.objects.create(user=new_user)
    tu = TelegramUser.objects.create(
        user=new_user,
        chat_id=chat_id,
        display_name=display_name,
    )
    ensure_user_settings(new_user, source_user=source_user)
    return tu, True, username


def _resolve_member_household(request):
    """Owners act on a cellar they belong to. Staff must name one.

    Staff tokens carry an X-Household header for their own cellar, so silently
    falling back to that would dump a global /adduser into the wrong household.
    """
    target_household_id = request.data.get("household_id")
    if request.user.is_staff:
        if target_household_id is None or str(target_household_id).strip() == "":
            return None, Response(
                {"error": "household_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            household = Household.objects.get(id=int(target_household_id))
        except (Household.DoesNotExist, ValueError, TypeError):
            return None, Response(
                {"error": "No household found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return household, None

    if target_household_id is not None:
        membership = HouseholdMembership.objects.filter(
            user=request.user, household_id=int(target_household_id),
        ).select_related("household").first()
    else:
        header_id = request.META.get("HTTP_X_HOUSEHOLD")
        if header_id:
            membership = HouseholdMembership.objects.filter(
                user=request.user, household_id=int(header_id),
            ).select_related("household").first()
        else:
            membership = HouseholdMembership.objects.filter(
                user=request.user, is_default=True,
            ).select_related("household").first()

    if not membership:
        return None, Response(
            {"error": "No household found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    if membership.role != "owner":
        return None, Response(
            {"error": "Only household owners can manage members"},
            status=status.HTTP_403_FORBIDDEN,
        )
    return membership.household, None


def _resolve_join_household(request):
    """Any member (or owner) of the named cellar may mint a join invite.

    Staff must pass household_id — their X-Household is their own cellar, and
    silently using it would mint a link into the wrong house.
    """
    target_household_id = request.data.get("household_id")
    if request.user.is_staff:
        if target_household_id is None or str(target_household_id).strip() == "":
            return None, Response(
                {"error": "household_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            household = Household.objects.get(id=int(target_household_id))
        except (Household.DoesNotExist, ValueError, TypeError):
            return None, Response(
                {"error": "No household found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return household, None

    if target_household_id is not None:
        try:
            hid = int(target_household_id)
        except (ValueError, TypeError):
            return None, Response(
                {"error": "No household found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        membership = HouseholdMembership.objects.filter(
            user=request.user, household_id=hid,
        ).select_related("household").first()
    else:
        header_id = request.META.get("HTTP_X_HOUSEHOLD")
        if header_id:
            membership = HouseholdMembership.objects.filter(
                user=request.user, household_id=int(header_id),
            ).select_related("household").first()
        else:
            membership = HouseholdMembership.objects.filter(
                user=request.user, is_default=True,
            ).select_related("household").first()
            if not membership:
                membership = HouseholdMembership.objects.filter(
                    user=request.user,
                ).select_related("household").first()

    if not membership:
        if target_household_id is not None:
            return None, Response(
                {"error": "Only household members can invite"},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None, Response(
            {"error": "No household found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return membership.household, None


def _inviter_display_name(user):
    tu = TelegramUser.objects.filter(user=user).first()
    if tu and tu.display_name:
        return tu.display_name
    return user.get_full_name() or user.username


def _serialize_invite(invite):
    payload = {
        "code": invite.code,
        "kind": invite.kind,
        "expires_at": invite.expires_at.isoformat(),
        "used": invite.used_at is not None,
        "expired": timezone.now() >= invite.expires_at,
        "inviter_display_name": _inviter_display_name(invite.created_by),
    }
    if invite.household_id:
        payload["household"] = {
            "id": invite.household_id,
            "name": invite.household.name,
        }
        payload["household_name"] = invite.household.name
    else:
        payload["household"] = None
        payload["household_name"] = None
    return payload


def _user_token_key(user):
    token, _ = Token.objects.get_or_create(user=user)
    return token.key


NAME_TAKEN = "That name's taken \u2014 pick another"
HOUSEHOLD_NAME_MAX = 40


def suggest_household_name(taken: str, display_name: str = "") -> str:
    """A free variant that is never the taken name itself."""
    taken = (taken or "").strip()
    first = (display_name or "").strip().split()[:1]
    first = first[0] if first else ""
    candidates = []
    if first:
        candidates.append(f"{taken} ({first})")
    candidates.extend(f"{taken} {n}" for n in range(2, 50))
    seen = set()
    for cand in candidates:
        cand = cand.strip()[:HOUSEHOLD_NAME_MAX].rstrip()
        key = cand.casefold()
        if not cand or key == taken.casefold() or key in seen:
            continue
        seen.add(key)
        if not Household.objects.filter(name__iexact=cand).exists():
            return cand
    suffix = secrets.token_hex(2)
    base = taken[: HOUSEHOLD_NAME_MAX - len(suffix) - 1].rstrip()
    return f"{base} {suffix}"


def _name_taken_response(name, display_name=""):
    # Names are global; don't echo the taken one back to a stranger.
    return Response(
        {"error": NAME_TAKEN, "suggested_name": suggest_household_name(name, display_name)},
        status=status.HTTP_409_CONFLICT,
    )


def _attach_to_household(tu, household, role):
    """Membership + scanner notify. Returns the membership."""
    membership, _ = HouseholdMembership.objects.get_or_create(
        user=tu.user, household=household,
        defaults={"role": role},
    )
    for scanner in Scanner.objects.filter(household=household, is_active=True):
        scanner.notify_users.add(tu)
    return membership


def _redeem_user_payload(tu, household, role, *, created, already_member=False):
    settings = getattr(tu.user, "user_settings", None)
    return {
        "status": "already_member" if already_member else "redeemed",
        "token": _user_token_key(tu.user),
        "user_id": tu.user.id,
        "username": tu.user.username,
        "display_name": tu.display_name,
        "chat_id": tu.chat_id,
        "created": created,
        "role": role,
        "household": {"id": household.id, "name": household.name},
        "household_id": household.id,
        "household_name": household.name,
        "already_member": already_member,
        "currency": (settings.currency if settings else None) or "EUR",
        "country": (settings.country if settings else None) or "",
    }


def _serialize_members(household):
    members = []
    for m in HouseholdMembership.objects.filter(
        household=household,
    ).select_related("user"):
        tu = TelegramUser.objects.filter(user=m.user).first()
        members.append({
            "user_id": m.user.id,
            "username": m.user.username,
            "role": m.role,
            "is_default": m.is_default,
            "is_staff": m.user.is_staff,
            "telegram_chat_id": tu.chat_id if tu else None,
            "telegram_name": tu.display_name if tu else None,
        })
    return members


@method_decorator(login_not_required, name="dispatch")
class ScannerConfigView(APIView):
    """Endpoint for the scanner service to fetch all config from the DB.

    Requires admin (staff) user token -- this exposes API tokens for all users.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAdminUser]

    def get(self, request):
        # Global service config
        config = {c.key: c.value for c in ServiceConfig.objects.all()}

        # Telegram users with their Wine Cellar API tokens and household info
        telegram_users = []
        for tu in TelegramUser.objects.filter(is_active=True).select_related("user"):
            token = Token.objects.filter(user=tu.user).first()
            if token:
                # Find user's default household
                membership = HouseholdMembership.objects.filter(
                    user=tu.user, is_default=True,
                ).select_related("household").first()
                if not membership:
                    membership = HouseholdMembership.objects.filter(
                        user=tu.user,
                    ).select_related("household").first()

                household_id = membership.household.id if membership else None
                household_name = membership.household.name if membership else None

                # All households this user belongs to
                all_households = list(
                    HouseholdMembership.objects.filter(user=tu.user)
                    .select_related("household")
                    .values_list("household_id", "household__name", "is_default")
                )

                # User's currency + country (used by Grok for price estimates)
                currency = "EUR"
                country = ""
                if hasattr(tu.user, "user_settings"):
                    currency = tu.user.user_settings.currency or "EUR"
                    country = tu.user.user_settings.country or ""

                telegram_users.append(
                    {
                        "chat_id": tu.chat_id,
                        "display_name": tu.display_name,
                        "api_token": token.key,
                        "user_id": tu.user.id,
                        "username": tu.user.username,
                        # Operational notices (service up/down) go to staff only,
                        # not broadcast to every household member.
                        "is_staff": tu.user.is_staff,
                        "household_id": household_id,
                        "household_name": household_name,
                        "currency": currency,
                        "country": country,
                        "households": [
                            {"id": h[0], "name": h[1], "is_default": h[2]}
                            for h in all_households
                        ],
                    }
                )

        # Registered scanners
        scanners = []
        for sc in Scanner.objects.filter(is_active=True).select_related("user", "household"):
            token = Token.objects.filter(user=sc.user).first()
            if token:
                sc_currency = "EUR"
                sc_country = ""
                if hasattr(sc.user, "user_settings"):
                    sc_currency = sc.user.user_settings.currency or "EUR"
                    sc_country = sc.user.user_settings.country or ""
                scanners.append(
                    {
                        "name": sc.name,
                        "api_key": sc.api_key,
                        "api_token": token.key,
                        "user_id": sc.user.id,
                        "household_id": sc.household.id if sc.household else None,
                        "household_name": sc.household.name if sc.household else None,
                        "default_storage": sc.default_storage,
                        "currency": sc_currency,
                        "country": sc_country,
                        "notify_chat_ids": list(
                            sc.notify_users.values_list("chat_id", flat=True)
                        ),
                    }
                )

        return Response(
            {
                "config": config,
                "telegram_users": telegram_users,
                "scanners": scanners,
            }
        )


@method_decorator(login_not_required, name="dispatch")
class HouseholdView(APIView):
    """View the current user's household and its members."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.user.is_staff:
            hid = request.query_params.get("household_id")
            if hid:
                try:
                    household = Household.objects.get(id=int(hid))
                except (Household.DoesNotExist, ValueError, TypeError):
                    return Response(
                        {"error": "No household found"},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                return Response({
                    "id": household.id,
                    "name": household.name,
                    "role": "staff",
                    "gamification_enabled": household.gamification_enabled,
                    "members": _serialize_members(household),
                })

        # Get household from X-Household header or default
        header_id = request.META.get("HTTP_X_HOUSEHOLD")
        if header_id:
            membership = HouseholdMembership.objects.filter(
                user=request.user, household_id=int(header_id),
            ).select_related("household").first()
        else:
            membership = HouseholdMembership.objects.filter(
                user=request.user, is_default=True,
            ).select_related("household").first()
            if not membership:
                membership = HouseholdMembership.objects.filter(
                    user=request.user,
                ).select_related("household").first()

        if not membership:
            return Response({"error": "No household found"}, status=status.HTTP_404_NOT_FOUND)

        household = membership.household
        return Response({
            "id": household.id,
            "name": household.name,
            "role": membership.role,
            "gamification_enabled": household.gamification_enabled,
            "members": _serialize_members(household),
        })

    def patch(self, request):
        """Owner (or staff) can turn cellar challenges on or off."""
        header_id = request.META.get("HTTP_X_HOUSEHOLD")
        if request.user.is_staff and request.data.get("household_id"):
            try:
                household = Household.objects.get(id=int(request.data["household_id"]))
            except (Household.DoesNotExist, ValueError, TypeError):
                return Response(
                    {"error": "No household found"}, status=status.HTTP_404_NOT_FOUND,
                )
            role = "staff"
        else:
            if header_id:
                membership = HouseholdMembership.objects.filter(
                    user=request.user, household_id=int(header_id),
                ).select_related("household").first()
            else:
                membership = HouseholdMembership.objects.filter(
                    user=request.user, is_default=True,
                ).select_related("household").first() or HouseholdMembership.objects.filter(
                    user=request.user,
                ).select_related("household").first()
            if not membership:
                return Response(
                    {"error": "No household found"}, status=status.HTTP_404_NOT_FOUND,
                )
            if membership.role != "owner" and not request.user.is_staff:
                return Response(
                    {"error": "Only household owners can change this"},
                    status=status.HTTP_403_FORBIDDEN,
                )
            household = membership.household
            role = membership.role

        if "gamification_enabled" not in request.data:
            return Response(
                {"error": "gamification_enabled is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        enabled = request.data.get("gamification_enabled")
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
        else:
            enabled = bool(enabled)
        household.gamification_enabled = enabled
        household.save(update_fields=["gamification_enabled"])
        filled = 0
        if enabled:
            filled = backfill(household, request.user)
        return Response({
            "id": household.id,
            "name": household.name,
            "role": role,
            "gamification_enabled": household.gamification_enabled,
            "backfilled": filled,
        })


@method_decorator(login_not_required, name="dispatch")
class HouseholdAddMemberView(APIView):
    """Add a member to the current user's household via Telegram chat_id.

    Creates a Django user + TelegramUser + HouseholdMembership + API token.
    Only household owners can add members.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        chat_id = request.data.get("chat_id", "").strip()
        display_name = request.data.get("display_name", "").strip()

        if not chat_id:
            return Response({"error": "chat_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not display_name:
            return Response({"error": "display_name is required"}, status=status.HTTP_400_BAD_REQUEST)

        household, error = _resolve_member_household(request)
        if error is not None:
            return error

        existing_tu = TelegramUser.objects.filter(chat_id=chat_id).first()
        if existing_tu:
            existing_member = HouseholdMembership.objects.filter(
                user=existing_tu.user, household=household,
            ).first()
            if existing_member:
                return Response({"error": f"{existing_tu.display_name} is already a member"}, status=status.HTTP_409_CONFLICT)

            _attach_to_household(existing_tu, household, "member")
            ensure_user_settings(existing_tu.user, source_user=request.user)
            return Response({
                "status": "added",
                "user_id": existing_tu.user.id,
                "display_name": existing_tu.display_name,
                "household": household.name,
            }, status=status.HTTP_201_CREATED)

        tu, created, username = _get_or_create_telegram_user(
            chat_id, display_name, source_user=request.user,
        )
        _attach_to_household(tu, household, "member")

        return Response({
            "status": "created",
            "user_id": tu.user.id,
            "username": username,
            "display_name": display_name,
            "household": household.name,
        }, status=status.HTTP_201_CREATED)


@method_decorator(login_not_required, name="dispatch")
class HouseholdRemoveMemberView(APIView):
    """Remove a member from the current user's household.

    Only household owners can remove members. Cannot remove yourself.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        chat_id = request.data.get("chat_id", "").strip()
        if not chat_id:
            return Response({"error": "chat_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        household, error = _resolve_member_household(request)
        if error is not None:
            return error

        # Find the telegram user
        tu = TelegramUser.objects.filter(chat_id=chat_id).first()
        if not tu:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

        # Can't remove yourself
        if tu.user == request.user:
            return Response({"error": "Cannot remove yourself"}, status=status.HTTP_400_BAD_REQUEST)

        # Remove from household
        removed = HouseholdMembership.objects.filter(
            user=tu.user, household=household,
        ).delete()

        if not removed[0]:
            return Response({"error": "User is not a member of this household"}, status=status.HTTP_404_NOT_FOUND)

        # Remove from scanner notifications
        for scanner in Scanner.objects.filter(household=household):
            scanner.notify_users.remove(tu)

        return Response({
            "status": "removed",
            "display_name": tu.display_name,
            "household": household.name,
        })


@method_decorator(login_not_required, name="dispatch")
class HouseholdsListView(APIView):
    """List households the current user belongs to.

    Admin (staff) users see all households.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.user.is_staff:
            households = []
            for h in Household.objects.all():
                households.append({
                    "id": h.id,
                    "name": h.name,
                    "member_count": h.memberships.count(),
                    "wine_count": h.wines.count(),
                    "bottle_count": StorageItem.objects.filter(
                        storage__household=h, deleted=False,
                    ).count(),
                })
            return Response({"households": households})

        households = Household.objects.filter(
            memberships__user=request.user,
        )
        return Response({
            "households": [
                {"id": h.id, "name": h.name}
                for h in households
            ]
        })

    def post(self, request):
        """Staff-only: create a household and optionally provision its owner."""
        if not request.user.is_staff:
            return Response(
                {"error": "Only staff can create a household"},
                status=status.HTTP_403_FORBIDDEN,
            )
        name = (request.data.get("name") or "").strip()
        chat_id = (request.data.get("chat_id") or "").strip()
        display_name = (request.data.get("display_name") or "").strip()
        if not name:
            return Response({"error": "name is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not chat_id:
            return Response({"error": "chat_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not display_name:
            return Response(
                {"error": "display_name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if Household.objects.filter(name__iexact=name).exists():
            return _name_taken_response(name, display_name)

        household = Household.objects.create(name=name)
        tu, created, username = _get_or_create_telegram_user(
            chat_id, display_name, source_user=request.user,
        )
        _attach_to_household(tu, household, "owner")

        return Response({
            "status": "created",
            "household": {"id": household.id, "name": household.name},
            "owner": {
                "user_id": tu.user.id,
                "username": username,
                "display_name": tu.display_name or display_name,
                "chat_id": tu.chat_id,
                "created": created,
            },
        }, status=status.HTTP_201_CREATED)


def _mint_invite(user, kind, household=None):
    for _ in range(5):
        try:
            return HouseholdInvite.objects.create(
                kind=kind,
                household=household,
                created_by=user,
                expires_at=timezone.now() + INVITE_TTL,
            )
        except IntegrityError:
            continue
    raise IntegrityError("could not mint a unique invite code")


@method_decorator(login_not_required, name="dispatch")
class HouseholdInviteCreateView(APIView):
    """Mint a one-time invite. Join: any member. New cellar: staff only."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        kind = (request.data.get("kind") or "").strip().lower()
        if kind == HouseholdInvite.KIND_NEW:
            if not request.user.is_staff:
                return Response(
                    {"error": "Only staff can mint a new-cellar invite"},
                    status=status.HTTP_403_FORBIDDEN,
                )
            invite = _mint_invite(request.user, HouseholdInvite.KIND_NEW)
            return Response(_serialize_invite(invite), status=status.HTTP_201_CREATED)

        if kind != HouseholdInvite.KIND_JOIN:
            return Response(
                {"error": "kind must be join or new"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        household, error = _resolve_join_household(request)
        if error is not None:
            return error
        invite = _mint_invite(request.user, HouseholdInvite.KIND_JOIN, household)
        return Response(_serialize_invite(invite), status=status.HTTP_201_CREATED)


@method_decorator(login_not_required, name="dispatch")
class HouseholdInvitePreviewView(APIView):
    """Validate a code without burning it. Staff only — the bot's admin client.

    POST body carries the code so it never appears in the access log path.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAdminUser]

    def post(self, request):
        code = (request.data.get("code") or "").strip()
        if not code:
            return Response({"error": "code is required"}, status=status.HTTP_400_BAD_REQUEST)
        invite = (
            HouseholdInvite.objects.select_related("household", "created_by")
            .filter(code=code)
            .first()
        )
        if invite is None:
            return Response({"error": "Invite not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_invite(invite))


@method_decorator(login_not_required, name="dispatch")
class HouseholdInviteRedeemView(APIView):
    """Provision from an unused code. Burned only after provision succeeds.

    Staff only: the response carries the token of whichever user owns the
    posted chat_id, so only the bot's admin client may call it.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAdminUser]

    def post(self, request):
        code = (request.data.get("code") or "").strip()
        chat_id = str(request.data.get("chat_id") or "").strip()
        display_name = (request.data.get("display_name") or "").strip()
        household_name = (request.data.get("household_name") or "").strip()

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
            return self._redeem(request, code, chat_id, display_name, household_name)
        except Exception:
            logger.exception("invite redeem failed")
            raise

    def _redeem(self, request, code, chat_id, display_name, household_name):
        with transaction.atomic():
            invite = lock_invite_qs(code).first()
            if invite is None:
                return Response(
                    {"error": "Invite not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if invite.used_at is not None:
                # Same chat retrying after a lost 201: hand the token back so
                # the bot can hot-add. Anyone else still sees used/expired.
                return self._replay(invite, chat_id)
            if timezone.now() >= invite.expires_at:
                return Response(
                    {"error": "This invite was already used or has expired"},
                    status=status.HTTP_410_GONE,
                )

            if invite.kind == HouseholdInvite.KIND_NEW:
                if not household_name:
                    return Response(
                        {"error": "household_name is required"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
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
                invite.used_at = timezone.now()
                invite.used_by = tu.user
                invite.household = household
                invite.save(update_fields=["used_at", "used_by", "household"])
                logger.info(
                    "invite redeemed invite_id=%s kind=new household_id=%s code=%s",
                    invite.id, household.id, mask_invite_code(code),
                )
                payload = _redeem_user_payload(tu, household, "owner", created=created)
                payload["first_cellar"] = True
                payload["created_by_chat_id"] = None
                inviter = TelegramUser.objects.filter(user=invite.created_by).first()
                if inviter:
                    payload["created_by_chat_id"] = inviter.chat_id
                    payload["created_by_display_name"] = inviter.display_name
                return Response(payload, status=status.HTTP_201_CREATED)

            household = invite.household
            if household is None:
                return Response(
                    {"error": "Invite not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            tu, created, _username = _get_or_create_telegram_user(
                chat_id, display_name, source_user=invite.created_by,
            )
            existing = HouseholdMembership.objects.filter(
                user=tu.user, household=household,
            ).first()
            if existing:
                # Don't burn it — they already belong here, and a second tap
                # should still say so rather than look used-up.
                payload = _redeem_user_payload(
                    tu, household, existing.role, created=False, already_member=True,
                )
            else:
                membership = _attach_to_household(tu, household, "member")
                invite.used_at = timezone.now()
                invite.used_by = tu.user
                invite.save(update_fields=["used_at", "used_by"])
                logger.info(
                    "invite redeemed invite_id=%s kind=join household_id=%s code=%s",
                    invite.id, household.id, mask_invite_code(code),
                )
                payload = _redeem_user_payload(
                    tu, household, membership.role, created=created,
                )
            inviter = TelegramUser.objects.filter(user=invite.created_by).first()
            if inviter:
                payload["created_by_chat_id"] = inviter.chat_id
                payload["created_by_display_name"] = inviter.display_name
            payload["bottle_count"] = StorageItem.objects.filter(
                storage__household=household, deleted=False,
            ).count()
            return Response(payload, status=status.HTTP_200_OK)

    def _replay(self, invite, chat_id):
        """Idempotent redeem for the chat that already burned this code."""
        gone = Response(
            {"error": "This invite was already used or has expired"},
            status=status.HTTP_410_GONE,
        )
        tu = TelegramUser.objects.filter(chat_id=chat_id).first()
        if tu is None or invite.used_by_id != tu.user_id:
            return gone
        household = invite.household
        membership = None
        if household is not None:
            membership = HouseholdMembership.objects.filter(
                user=tu.user, household=household,
            ).first()
        else:
            # Older new-cellar rows left household NULL after redeem.
            membership = (
                HouseholdMembership.objects.filter(user=tu.user)
                .select_related("household")
                .order_by("-created")
                .first()
            )
            household = membership.household if membership else None
        if household is None or membership is None:
            return gone
        payload = _redeem_user_payload(
            tu, household, membership.role, created=False, already_member=True,
        )
        inviter = TelegramUser.objects.filter(user=invite.created_by).first()
        if inviter:
            payload["created_by_chat_id"] = inviter.chat_id
            payload["created_by_display_name"] = inviter.display_name
        if invite.kind == HouseholdInvite.KIND_JOIN:
            payload["bottle_count"] = StorageItem.objects.filter(
                storage__household=household, deleted=False,
            ).count()
        logger.info(
            "invite replay invite_id=%s chat_id=%s code=%s",
            invite.id, chat_id, mask_invite_code(invite.code),
        )
        return Response(payload, status=status.HTTP_200_OK)


@method_decorator(login_not_required, name="dispatch")
class HouseholdInviteCancelView(APIView):
    """Drop the caller's newest unexpired, unused invite.

    With household_id, that cellar's invite wins; failing that, a household-less
    new-cellar invite (staff /invite new then /invite cancel). Expired rows stay.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        live = (
            HouseholdInvite.objects.filter(
                created_by=request.user,
                used_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .select_related("household")
            .order_by("-created")
        )
        raw_id = request.data.get("household_id")
        if raw_id is not None and str(raw_id).strip() != "":
            try:
                hid = int(raw_id)
            except (ValueError, TypeError):
                return Response(
                    {"error": "No household found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            invite = live.filter(household_id=hid).first() or live.filter(
                kind=HouseholdInvite.KIND_NEW, household__isnull=True,
            ).first()
        else:
            invite = live.first()
        if invite is None:
            return Response(
                {"error": "No unused invite to cancel"},
                status=status.HTTP_404_NOT_FOUND,
            )
        payload = {
            "status": "cancelled",
            "code": invite.code,
            "kind": invite.kind,
            "household_name": invite.household.name if invite.household_id else None,
        }
        invite.delete()
        return Response(payload)


@method_decorator(login_not_required, name="dispatch")
class HouseholdAchievementsView(APIView):
    """List cellar challenge unlocks. Empty and flagged off when the household hasn't opted in."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        header_id = request.META.get("HTTP_X_HOUSEHOLD")
        household_id = request.query_params.get("household_id") or header_id
        requested = None
        if household_id:
            try:
                requested = int(household_id)
            except (TypeError, ValueError):
                requested = None
        household = get_user_household(request.user, household_id=requested)
        if household is None and request.user.is_staff and requested:
            household = Household.objects.filter(id=requested).first()
        if household is None:
            return Response(
                {"error": "No household found"}, status=status.HTTP_404_NOT_FOUND,
            )
        if not household.gamification_enabled:
            return Response({
                "enabled": False,
                "achievements": [],
                "this_month": {"standings": []},
                "progress": {"tracks": [], "missing": [], "close": []},
            })
        unlocks = CellarAchievement.objects.filter(household=household)[:50]
        return Response({
            "enabled": True,
            "achievements": [serialize_achievement(a) for a in unlocks],
            "this_month": month_tally(household),
            "progress": progress(household),
        })


def _household_for_request(request):
    header_id = request.META.get("HTTP_X_HOUSEHOLD")
    household_id = request.query_params.get("household_id") or header_id
    if request.method == "POST":
        household_id = request.data.get("household_id") or household_id
    requested = None
    if household_id:
        try:
            requested = int(household_id)
        except (TypeError, ValueError):
            requested = None
    household = get_user_household(request.user, household_id=requested)
    if household is None and request.user.is_staff and requested:
        household = Household.objects.filter(id=requested).first()
    return household


@method_decorator(login_not_required, name="dispatch")
class HouseholdTasteView(APIView):
    """Inferred cellar taste + shop/list looking. Not ratings."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        household = _household_for_request(request)
        if household is None:
            return Response(
                {"error": "No household found"}, status=status.HTTP_404_NOT_FOUND,
            )
        return Response(serialize_snapshot(household))

    def post(self, request):
        household = _household_for_request(request)
        if household is None:
            return Response(
                {"error": "No household found"}, status=status.HTTP_404_NOT_FOUND,
            )
        kind = (request.data.get("kind") or "").strip().lower()
        payload = request.data.get("payload")
        if not isinstance(payload, dict):
            payload = {}
            wine = request.data.get("wine")
            if isinstance(wine, dict):
                payload = wine
            wines = request.data.get("wines")
            if isinstance(wines, list):
                payload = {
                    "scene": request.data.get("scene") or "",
                    "wines": wines[:20],
                    "recommendation": request.data.get("recommendation"),
                }
        intent = record_intent(household, request.user, kind, payload)
        if intent is None:
            return Response(
                {"error": "kind must be look or winelist"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(serialize_snapshot(household), status=status.HTTP_201_CREATED)


@method_decorator(login_not_required, name="dispatch")
class ScannerWhoisView(APIView):
    """Staff-only: find a Telegram user by chat ID or name."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAdminUser]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if not q:
            return Response({"error": "q is required"}, status=status.HTTP_400_BAD_REQUEST)

        users = TelegramUser.objects.select_related("user")
        if q.lstrip("-").isdigit():
            users = users.filter(chat_id=q)
        else:
            users = users.filter(
                Q(display_name__icontains=q)
                | Q(user__username__icontains=q)
                | Q(chat_id=q)
            )

        results = []
        for tu in users[:20]:
            memberships = HouseholdMembership.objects.filter(
                user=tu.user,
            ).select_related("household")
            results.append({
                "chat_id": tu.chat_id,
                "display_name": tu.display_name,
                "username": tu.user.username,
                "is_staff": tu.user.is_staff,
                "is_active": tu.is_active,
                "households": [
                    {
                        "id": m.household.id,
                        "name": m.household.name,
                        "role": m.role,
                        "is_default": m.is_default,
                    }
                    for m in memberships
                ],
            })
        return Response({"results": results})


@method_decorator(login_not_required, name="dispatch")
class ScannerStatsView(APIView):
    """Staff-only: counts across every household. No tokens or API keys."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAdminUser]

    def get(self, request):
        scanners = [
            {
                "name": sc.name,
                "household": sc.household.name if sc.household else None,
                "is_active": sc.is_active,
            }
            for sc in Scanner.objects.select_related("household")
        ]
        return Response({
            "households": Household.objects.count(),
            "telegram_users": TelegramUser.objects.filter(is_active=True).count(),
            "wines": Wine.objects.filter(household__isnull=False).count(),
            "bottles": StorageItem.objects.filter(
                deleted=False, storage__household__isnull=False,
            ).count(),
            "scanners": scanners,
        })


COUNTRY_CURRENCY = {
    "AU": "AUD", "NZ": "NZD", "ZA": "ZAR", "GB": "GBP", "US": "USD",
    "FR": "EUR", "IT": "EUR", "ES": "EUR", "DE": "EUR", "PT": "EUR",
    "AT": "EUR", "AR": "USD", "CL": "USD",
}


@method_decorator(login_not_required, name="dispatch")
class ScannerSettingsView(APIView):
    """The bot writes country/currency after asking on first /start."""

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        country = (request.data.get("country") or "").strip().upper()
        if country == "SKIP":
            country = ""
        currency = (request.data.get("currency") or "").strip().upper()
        if not currency:
            currency = COUNTRY_CURRENCY.get(country, "EUR")
        from django.conf import settings as django_settings
        allowed = {code for code, _ in django_settings.CURRENCIES}
        if currency not in allowed:
            currency = "EUR"
        row = ensure_user_settings(request.user)
        row.country = country[:2]
        row.currency = currency
        row.save(update_fields=["country", "currency"])
        return Response({"currency": row.currency, "country": row.country})
