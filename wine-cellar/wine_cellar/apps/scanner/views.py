from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_not_required
from django.utils.crypto import get_random_string
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.authtoken.models import Token

from .models import Household, HouseholdMembership, Scanner, ServiceConfig, TelegramUser

User = get_user_model()


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

                telegram_users.append(
                    {
                        "chat_id": tu.chat_id,
                        "display_name": tu.display_name,
                        "api_token": token.key,
                        "user_id": tu.user.id,
                        "username": tu.user.username,
                        "household_id": household_id,
                        "household_name": household_name,
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
                scanners.append(
                    {
                        "name": sc.name,
                        "api_key": sc.api_key,
                        "api_token": token.key,
                        "user_id": sc.user.id,
                        "household_id": sc.household.id if sc.household else None,
                        "household_name": sc.household.name if sc.household else None,
                        "default_storage": sc.default_storage,
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
        members = []
        for m in HouseholdMembership.objects.filter(
            household=household,
        ).select_related("user"):
            # Find their telegram user if any
            tu = TelegramUser.objects.filter(user=m.user).first()
            members.append({
                "user_id": m.user.id,
                "username": m.user.username,
                "role": m.role,
                "is_default": m.is_default,
                "telegram_chat_id": tu.chat_id if tu else None,
                "telegram_name": tu.display_name if tu else None,
            })

        return Response({
            "id": household.id,
            "name": household.name,
            "role": membership.role,
            "members": members,
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

        # Resolve target household: explicit household_id in body,
        # X-Household header, or caller's default.
        # Staff users can manage any household directly.
        target_household_id = request.data.get("household_id")
        if target_household_id is not None and request.user.is_staff:
            # Staff can manage any household
            try:
                household = Household.objects.get(id=int(target_household_id))
            except Household.DoesNotExist:
                return Response({"error": "No household found"}, status=status.HTTP_404_NOT_FOUND)
        else:
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
                return Response({"error": "No household found"}, status=status.HTTP_404_NOT_FOUND)

            if membership.role != "owner":
                return Response({"error": "Only household owners can add members"}, status=status.HTTP_403_FORBIDDEN)

            household = membership.household

        # Check if this chat_id already exists
        existing_tu = TelegramUser.objects.filter(chat_id=chat_id).first()
        if existing_tu:
            # Check if already in this household
            existing_member = HouseholdMembership.objects.filter(
                user=existing_tu.user, household=household,
            ).first()
            if existing_member:
                return Response({"error": f"{existing_tu.display_name} is already a member"}, status=status.HTTP_409_CONFLICT)

            # Add existing user to household
            HouseholdMembership.objects.create(
                user=existing_tu.user, household=household, role="member",
            )
            # Also add them to scanner notifications
            for scanner in Scanner.objects.filter(household=household, is_active=True):
                scanner.notify_users.add(existing_tu)

            return Response({
                "status": "added",
                "user_id": existing_tu.user.id,
                "display_name": existing_tu.display_name,
                "household": household.name,
            }, status=status.HTTP_201_CREATED)

        # Create new Django user
        username = display_name.lower().replace(" ", "_")
        # Ensure unique
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1

        new_user = User.objects.create_user(
            username=username,
            password=get_random_string(24),
        )

        # Create API token
        Token.objects.create(user=new_user)

        # Create TelegramUser
        tu = TelegramUser.objects.create(
            user=new_user,
            chat_id=chat_id,
            display_name=display_name,
        )

        # Add to household
        HouseholdMembership.objects.create(
            user=new_user, household=household, role="member", is_default=True,
        )

        # Add to scanner notifications
        for scanner in Scanner.objects.filter(household=household, is_active=True):
            scanner.notify_users.add(tu)

        return Response({
            "status": "created",
            "user_id": new_user.id,
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

        # Resolve target household. Staff users can manage any household.
        target_household_id = request.data.get("household_id")
        if target_household_id is not None and request.user.is_staff:
            try:
                household = Household.objects.get(id=int(target_household_id))
            except Household.DoesNotExist:
                return Response({"error": "No household found"}, status=status.HTTP_404_NOT_FOUND)
        else:
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
                return Response({"error": "No household found"}, status=status.HTTP_404_NOT_FOUND)

            if membership.role != "owner":
                return Response({"error": "Only household owners can remove members"}, status=status.HTTP_403_FORBIDDEN)

            household = membership.household

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
            households = Household.objects.all()
        else:
            households = Household.objects.filter(
                memberships__user=request.user,
            )

        return Response({
            "households": [
                {"id": h.id, "name": h.name}
                for h in households
            ]
        })
