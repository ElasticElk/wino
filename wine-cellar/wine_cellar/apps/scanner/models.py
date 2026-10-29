import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone


class Household(models.Model):
    """A household / location — the permission boundary for wine data.

    Users belong to households via HouseholdMembership.
    Wines, storages, and scanners are scoped to a household.
    """

    name = models.CharField(max_length=100)
    created = models.DateTimeField(auto_now_add=True)
    # On for new cellars — owners can still turn challenges off.
    gamification_enabled = models.BooleanField(default=True)
    empty_nudge_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class HouseholdMembership(models.Model):
    """Links a user to a household with a role."""

    ROLE_CHOICES = [
        ("owner", "Owner"),
        ("member", "Member"),
    ]

    household = models.ForeignKey(
        Household, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        get_user_model(), on_delete=models.CASCADE, related_name="household_memberships"
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="member")
    is_default = models.BooleanField(
        default=False,
        help_text="If true, this is the user's default household for API/bot interactions.",
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("household", "user")
        ordering = ["household__name"]

    def __str__(self):
        return f"{self.user} -> {self.household} ({self.role})"

    def save(self, *args, **kwargs):
        # If this is the user's first membership, make it default
        if not self.pk and not HouseholdMembership.objects.filter(user=self.user).exists():
            self.is_default = True
        super().save(*args, **kwargs)


class ServiceConfig(models.Model):
    """Key-value config read by the scanner service at startup.

    Examples: bot_token, grok_api_key, web_url.
    """

    key = models.CharField(max_length=50, unique=True)
    value = models.TextField()
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.key


class TelegramUser(models.Model):
    """Links a Telegram chat_id to a Wine Cellar user account."""

    user = models.ForeignKey(
        get_user_model(), on_delete=models.CASCADE, related_name="telegram_users"
    )
    chat_id = models.CharField(max_length=50, unique=True)
    display_name = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["display_name"]

    def __str__(self):
        return f"{self.display_name or self.chat_id} -> {self.user}"

    @property
    def default_household(self):
        """Return the user's default household, or first available."""
        membership = HouseholdMembership.objects.filter(
            user=self.user, is_default=True
        ).select_related("household").first()
        if membership:
            return membership.household
        membership = HouseholdMembership.objects.filter(
            user=self.user
        ).select_related("household").first()
        return membership.household if membership else None


def generate_api_key():
    return secrets.token_hex(32)


INVITE_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
INVITE_CODE_LENGTH = 8
INVITE_TTL = timedelta(days=30)


def generate_invite_code():
    # Same 8-char alphabet as ClubInvite (phase 2). mint_unique_code() is the
    # helper that must stay unique across both tables (open-q #12).
    return "".join(
        secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH)
    )


class HouseholdInvite(models.Model):
    """One-time Telegram deep link. Burned only after provision succeeds."""

    KIND_JOIN = "join"
    KIND_NEW = "new"
    KIND_CHOICES = [
        (KIND_JOIN, "Join existing"),
        (KIND_NEW, "New cellar"),
    ]

    code = models.CharField(max_length=8, unique=True, default=generate_invite_code)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    household = models.ForeignKey(
        Household, on_delete=models.CASCADE, related_name="invites",
        null=True, blank=True,
    )
    created_by = models.ForeignKey(
        get_user_model(), on_delete=models.CASCADE, related_name="household_invites",
    )
    created = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_by = models.ForeignKey(
        get_user_model(), on_delete=models.SET_NULL, related_name="redeemed_invites",
        null=True, blank=True,
    )

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"{self.kind}:{self.code}"

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + INVITE_TTL
        super().save(*args, **kwargs)

    @property
    def is_used(self):
        return self.used_at is not None

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at


def invite_code_taken(code: str) -> bool:
    """True if this code is already a household invite.

    Phase 2 must also check ClubInvite so a /start code cannot collide
    across household and club tables (open-q #12).
    """
    return HouseholdInvite.objects.filter(code=code).exists()


def mint_unique_code(taken=None, attempts=5):
    """Unused 8-char invite code, or None if every attempt was taken.

    HouseholdInvite uses this today. ClubInvite (phase 2) must pass a
    ``taken`` that covers both tables.
    """
    check = taken if taken is not None else invite_code_taken
    for _ in range(attempts):
        code = generate_invite_code()
        if not check(code):
            return code
    return None


class Scanner(models.Model):
    """A registered physical barcode scanner device (e.g. a Raspberry Pi)."""

    name = models.CharField(max_length=100)
    household = models.ForeignKey(
        Household, on_delete=models.CASCADE, related_name="scanners",
        null=True,  # nullable for migration, will be populated
    )
    user = models.ForeignKey(
        get_user_model(), on_delete=models.CASCADE, related_name="scanners"
    )
    api_key = models.CharField(max_length=64, unique=True, default=generate_api_key)
    notify_users = models.ManyToManyField(
        TelegramUser, blank=True, related_name="scanners"
    )
    default_storage = models.CharField(max_length=100, default="Wine Fridge")
    is_active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.household or self.user})"
