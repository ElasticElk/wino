from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from wine_cellar.apps.scanner.models import generate_invite_code

CELLAR_VISIBILITY = [
    ("none", "Feed only"),
    ("summary", "Type and count"),
    ("full", "Full cellar"),
]
VISIBILITY_RANK = {"none": 0, "summary": 1, "full": 2}
NAME_MIN = 2
NAME_MAX = 40
DISMANTLE_TTL = timedelta(hours=48)
CLUB_INVITE_MAX_USES = 20


class Club(models.Model):
    KIND_PRIVATE = "private"
    KIND_GLOBAL = "global"
    KIND_CHOICES = [
        (KIND_PRIVATE, "Private"),
        (KIND_GLOBAL, "Global"),
    ]

    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=KIND_PRIVATE)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="clubs_founded",
    )
    created = models.DateTimeField(auto_now_add=True)
    description = models.TextField(blank=True, default="")
    cellar_visibility = models.CharField(
        max_length=8, choices=CELLAR_VISIBILITY, default="full",
    )
    badges_visible = models.BooleanField(default=True)
    sharing_version = models.PositiveIntegerField(default=1)
    previous_cellar_visibility = models.CharField(max_length=8, blank=True, default="")
    previous_badges_visible = models.BooleanField(null=True, blank=True)
    frozen = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["created_by", "name"],
                name="unique_club_name_per_founder",
            ),
            models.CheckConstraint(
                condition=models.Q(kind="private") | models.Q(cellar_visibility="none"),
                name="global_clubs_feed_only",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def member_count(self):
        return self.memberships.count()

    def open_dismantle_vote(self):
        return self.dismantle_votes.filter(status="open").first()


class ClubMembership(models.Model):
    ROLE_OWNER = "owner"
    ROLE_MEMBER = "member"
    ROLE = [
        (ROLE_OWNER, "Owner"),
        (ROLE_MEMBER, "Member"),
    ]
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="club_memberships",
    )
    role = models.CharField(max_length=10, choices=ROLE, default=ROLE_MEMBER)
    household = models.ForeignKey(
        "scanner.Household",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="club_memberships",
    )
    feed_muted = models.BooleanField(default=False)
    accepted_sharing_version = models.PositiveIntegerField(default=1)
    is_last_used = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["club", "user"], name="unique_club_member"),
            models.UniqueConstraint(
                fields=["club"],
                condition=models.Q(role="owner"),
                name="one_owner_per_club",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_last_used=True),
                name="one_last_used_club_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "household"], name="clubmem_club_hh_idx"),
            models.Index(fields=["user", "is_last_used"], name="clubmem_user_last_idx"),
        ]

    def __str__(self):
        return f"{self.user} -> {self.club} ({self.role})"


class ClubInvite(models.Model):
    code = models.CharField(max_length=8, unique=True, default=generate_invite_code)
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="invites")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    max_uses = models.PositiveSmallIntegerField(default=CLUB_INVITE_MAX_USES)
    use_count = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created"]
        indexes = [models.Index(fields=["code"], name="clubinvite_code_idx")]

    def __str__(self):
        return f"club:{self.code}"

    def save(self, *args, **kwargs):
        if not self.expires_at:
            from wine_cellar.apps.scanner.models import INVITE_TTL

            self.expires_at = timezone.now() + INVITE_TTL
        super().save(*args, **kwargs)

    @property
    def is_used(self):
        if self.revoked_at is not None:
            return True
        return self.use_count >= self.max_uses

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at


class ClubEvent(models.Model):
    KIND_OPENED = "opened"
    KIND_VOTED = "voted"
    KIND_RETRACTED = "retracted"
    KIND_JOINED = "joined"
    KIND_LEFT = "left"
    KIND_TASTEOFF = "tasteoff"
    KIND_COMMENT = "comment"
    KIND = [
        (KIND_OPENED, "Opened"),
        (KIND_VOTED, "Voted"),
        (KIND_RETRACTED, "Retracted"),
        (KIND_JOINED, "Joined"),
        (KIND_LEFT, "Left"),
        (KIND_TASTEOFF, "Taste-off"),
        (KIND_COMMENT, "Comment"),
    ]
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=12, choices=KIND)
    wine_name = models.CharField(max_length=100, blank=True, default="")
    vintage = models.PositiveIntegerField(null=True, blank=True)
    producer = models.CharField(max_length=100, blank=True, default="")
    vote = models.CharField(max_length=8, blank=True, default="")
    wine_id = models.IntegerField(null=True, blank=True)
    item_id = models.IntegerField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    retracted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["club", "-created"], name="clubevent_club_created_idx"),
            models.Index(fields=["club", "item_id", "kind"], name="clubevent_item_kind_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["club", "item_id", "kind"],
                condition=models.Q(kind="opened", item_id__isnull=False),
                name="unique_opened_per_club_item",
            ),
        ]

    @property
    def retracted(self):
        return self.retracted_at is not None


class ClubEventDelivery(models.Model):
    event = models.ForeignKey(
        ClubEvent, on_delete=models.CASCADE, related_name="deliveries",
    )
    chat_id = models.CharField(max_length=50)
    message_id = models.BigIntegerField(null=True, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    error = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "chat_id"], name="unique_event_chat"),
        ]


class ClubDismantleVote(models.Model):
    STATUS_OPEN = "open"
    STATUS_PASSED = "passed"
    STATUS_REJECTED = "rejected"
    STATUS_EXPIRED = "expired"
    STATUS_CANCELLED = "cancelled"
    STATUS = [
        (STATUS_OPEN, "Open"),
        (STATUS_PASSED, "Passed"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_CANCELLED, "Cancelled"),
    ]
    club = models.ForeignKey(
        Club, on_delete=models.CASCADE, related_name="dismantle_votes",
    )
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=STATUS, default=STATUS_OPEN)
    created = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club"],
                condition=models.Q(status="open"),
                name="one_open_dismantle_vote_per_club",
            ),
        ]


class ClubDismantleBallot(models.Model):
    CHOICE_YES = "yes"
    CHOICE_NO = "no"
    CHOICE = [(CHOICE_YES, "Yes"), (CHOICE_NO, "No")]
    vote = models.ForeignKey(
        ClubDismantleVote, on_delete=models.CASCADE, related_name="ballots",
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    choice = models.CharField(max_length=3, choices=CHOICE, null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    chat_id = models.CharField(max_length=50, blank=True, default="")
    message_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["vote", "user"], name="unique_dismantle_ballot"),
        ]


class ClubComment(models.Model):
    event = models.ForeignKey(
        ClubEvent, on_delete=models.CASCADE, related_name="comments",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
    )
    text = models.CharField(max_length=280)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created"]
        indexes = [
            models.Index(fields=["event", "created"], name="clubcomment_event_idx"),
        ]


class TasteOff(models.Model):
    STATUS_SETUP = "setup"
    STATUS_SCORING = "scoring"
    STATUS_CLOSED = "closed"
    STATUS = [
        (STATUS_SETUP, "Setup"),
        (STATUS_SCORING, "Scoring"),
        (STATUS_CLOSED, "Closed"),
    ]
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="tasteoffs")
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
    )
    title = models.CharField(max_length=80)
    status = models.CharField(max_length=8, choices=STATUS, default=STATUS_SETUP)
    created = models.DateTimeField(auto_now_add=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club"],
                condition=models.Q(status="scoring"),
                name="one_open_tasteoff_per_club",
            ),
        ]

    @property
    def is_open(self):
        return self.status in (self.STATUS_SETUP, self.STATUS_SCORING)


class TasteOffBottle(models.Model):
    tasteoff = models.ForeignKey(
        TasteOff, on_delete=models.CASCADE, related_name="bottles",
    )
    label = models.CharField(max_length=160)
    wine_id = models.IntegerField(null=True, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class TasteOffScore(models.Model):
    tasteoff = models.ForeignKey(
        TasteOff, on_delete=models.CASCADE, related_name="scores",
    )
    bottle = models.ForeignKey(
        TasteOffBottle, on_delete=models.CASCADE, related_name="scores",
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    score = models.PositiveSmallIntegerField()
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bottle", "user"], name="unique_tasteoff_score",
            ),
            models.CheckConstraint(
                condition=models.Q(score__gte=1) & models.Q(score__lte=5),
                name="tasteoff_score_1_5",
            ),
        ]
