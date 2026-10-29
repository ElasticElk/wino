"""Club membership, policy, dismantle, and serialization helpers."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from wine_cellar.apps.club.models import (
    DISMANTLE_TTL,
    NAME_MAX,
    NAME_MIN,
    VISIBILITY_RANK,
    Club,
    ClubDismantleBallot,
    ClubDismantleVote,
    ClubEvent,
    ClubInvite,
    ClubMembership,
)
from wine_cellar.apps.scanner.models import TelegramUser


def display_name(user) -> str:
    tu = TelegramUser.objects.filter(user=user).first()
    if tu and tu.display_name:
        return tu.display_name
    return user.get_full_name() or user.username


def membership_of(user, club_id) -> ClubMembership | None:
    return (
        ClubMembership.objects.select_related("club", "household")
        .filter(user=user, club_id=club_id)
        .first()
    )


def set_last_used(user, membership: ClubMembership) -> None:
    ClubMembership.objects.filter(user=user, is_last_used=True).exclude(
        pk=membership.pk,
    ).update(is_last_used=False)
    if not membership.is_last_used:
        membership.is_last_used = True
        membership.save(update_fields=["is_last_used"])


def validate_club_name(name: str) -> str | None:
    cleaned = (name or "").strip()
    if len(cleaned) < NAME_MIN or len(cleaned) > NAME_MAX:
        return None
    return cleaned


def is_widen(old_vis, new_vis, old_badges, new_badges) -> bool:
    if VISIBILITY_RANK.get(new_vis, 0) > VISIBILITY_RANK.get(old_vis, 0):
        return True
    if (not old_badges) and new_badges:
        return True
    return False


def household_visible_in_any_club(household) -> bool:
    if household is None:
        return False
    return ClubMembership.objects.filter(
        household=household,
        club__cellar_visibility__in=("summary", "full"),
    ).exists()


def maybe_clear_share_notice(household) -> None:
    if household is None:
        return
    if household_visible_in_any_club(household):
        return
    if household.club_share_notice_sent_at is not None:
        household.club_share_notice_sent_at = None
        household.save(update_fields=["club_share_notice_sent_at"])


def serialize_event(event: ClubEvent, *, public=True) -> dict:
    created = event.created.isoformat() if event.created else None
    body = {
        "id": event.id,
        "kind": event.kind,
        "actor_name": display_name(event.actor) if event.actor_id else "",
        "wine_name": event.wine_name,
        "vintage": event.vintage,
        "producer": event.producer,
        "vote": event.vote,
        "created": created,
        "retracted": event.retracted,
    }
    if event.kind in (ClubEvent.KIND_TASTEOFF, ClubEvent.KIND_STAFF_NOTE):
        body["payload"] = event.payload or {}
    if not public:
        body["actor_id"] = event.actor_id
        body["item_id"] = event.item_id
        count = getattr(event, "comment_count", None)
        if count is None:
            count = event.comments.count()
        body["comment_count"] = count
    return body


def serialize_club_list_item(membership: ClubMembership) -> dict:
    club = membership.club
    return {
        "id": club.id,
        "name": club.name,
        "role": membership.role,
        "cellar_visibility": club.cellar_visibility,
        "badges_visible": club.badges_visible,
        "member_count": club.memberships.count(),
        "is_last_used": membership.is_last_used,
        "kind": club.kind,
        "frozen": club.frozen,
        "description": club.description,
    }


def serialize_club_home(membership: ClubMembership, *, events=None) -> dict:
    club = membership.club
    if events is None:
        events = list(
            ClubEvent.objects.filter(club=club, retracted_at__isnull=True)
            .select_related("actor")
            .order_by("-created")[:8]
        )
    household = None
    if membership.household_id:
        household = {
            "id": membership.household_id,
            "name": membership.household.name,
        }
    return {
        "id": club.id,
        "name": club.name,
        "kind": club.kind,
        "role": membership.role,
        "cellar_visibility": club.cellar_visibility,
        "badges_visible": club.badges_visible,
        "sharing_version": club.sharing_version,
        "accepted_sharing_version": membership.accepted_sharing_version,
        "previous_cellar_visibility": club.previous_cellar_visibility,
        "feed_muted": membership.feed_muted,
        "frozen": club.frozen,
        "description": club.description,
        "member_count": club.memberships.count(),
        "household": None if club.kind == Club.KIND_GLOBAL else household,
        "events": [serialize_event(event) for event in events],
    }


def serialize_vote(vote: ClubDismantleVote) -> dict:
    try:
        ballots = list(vote.ballots.select_related("user"))
    except ClubDismantleVote.DoesNotExist:
        ballots = []
    yes = sum(1 for b in ballots if b.choice == "yes")
    no = sum(1 for b in ballots if b.choice == "no")
    pending = [b for b in ballots if not b.choice]
    return {
        "id": vote.id,
        "club_id": vote.club_id,
        "status": vote.status,
        "expires_at": vote.expires_at.isoformat() if vote.expires_at else None,
        "yes": yes,
        "no": no,
        "pending": len(pending),
        "waiting_names": [display_name(b.user) for b in pending],
    }


def create_club(
    user, name, cellar_visibility="full", badges_visible=True, household=None,
    kind=None, description="",
):
    cleaned = validate_club_name(name)
    if cleaned is None:
        return None, "name_length"
    if Club.objects.filter(created_by=user, name=cleaned).exists():
        return None, "name_taken"
    kind = kind or Club.KIND_PRIVATE
    if kind == Club.KIND_GLOBAL:
        vis = "none"
        badges = False
    else:
        vis = cellar_visibility if cellar_visibility in VISIBILITY_RANK else "full"
        badges = True if badges_visible is None else bool(badges_visible)
    try:
        with transaction.atomic():
            club = Club.objects.create(
                name=cleaned,
                created_by=user,
                cellar_visibility=vis,
                badges_visible=badges,
                kind=kind,
                description=description or "",
            )
            membership = ClubMembership.objects.create(
                club=club,
                user=user,
                role=ClubMembership.ROLE_OWNER,
                household=household,
                accepted_sharing_version=club.sharing_version,
            )
            set_last_used(user, membership)
    except IntegrityError:
        return None, "name_taken"
    if vis in ("summary", "full") and household is not None:
        maybe_queue_share_notice(household, user.id)
    return membership, None


def apply_policy(club: Club, cellar_visibility=None, badges_visible=None):
    """Return ('widen'|'narrow'|'same', club). Caller persists."""
    new_vis = club.cellar_visibility if cellar_visibility is None else cellar_visibility
    new_badges = club.badges_visible if badges_visible is None else bool(badges_visible)
    if new_vis not in VISIBILITY_RANK:
        new_vis = club.cellar_visibility
    old_vis = club.cellar_visibility
    old_badges = club.badges_visible
    if new_vis == old_vis and new_badges == old_badges:
        return "same", club
    widening = is_widen(old_vis, new_vis, old_badges, new_badges)
    if widening:
        club.previous_cellar_visibility = old_vis
        club.previous_badges_visible = old_badges
        club.sharing_version = club.sharing_version + 1
        club.cellar_visibility = new_vis
        club.badges_visible = new_badges
        club.save()
        ClubMembership.objects.filter(
            club=club, role=ClubMembership.ROLE_OWNER,
        ).update(accepted_sharing_version=club.sharing_version)
        return "widen", club
    club.cellar_visibility = new_vis
    club.badges_visible = new_badges
    club.save()
    ClubMembership.objects.filter(club=club).update(
        accepted_sharing_version=club.sharing_version,
    )
    return "narrow", club


def leave_club(membership: ClubMembership):
    """Return ('left', None) | ('deleted', None) | ('handover', None) | ('passed', club_id)."""
    club = membership.club
    household = membership.household
    others = club.memberships.exclude(pk=membership.pk).count()
    if membership.role == ClubMembership.ROLE_OWNER and others > 0:
        return "handover", None
    vote = club.open_dismantle_vote()
    user_id = membership.user_id
    club_id = club.id
    last = others == 0
    with transaction.atomic():
        if vote is not None:
            ClubDismantleBallot.objects.filter(vote=vote, user_id=user_id).delete()
        membership.delete()
        if last:
            club.delete()
            maybe_clear_share_notice(household)
            return "deleted", club_id
        maybe_clear_share_notice(household)
        if vote is not None:
            vote = _refresh_vote(vote)
            if vote.status == ClubDismantleVote.STATUS_OPEN:
                remaining = list(vote.ballots.select_related("user"))
                members_left = club.memberships.count()
                if members_left == 0:
                    club.delete()
                    return "deleted", club_id
                if remaining and all(b.choice == "yes" for b in remaining):
                    resolve_dismantle(vote, ClubDismantleVote.STATUS_PASSED)
                    return "passed", club_id
    return "left", club_id


def handover_club(membership: ClubMembership, to_user) -> ClubMembership | None:
    if membership.role != ClubMembership.ROLE_OWNER:
        return None
    other = ClubMembership.objects.filter(club=membership.club, user=to_user).first()
    if other is None or other.pk == membership.pk:
        return None
    with transaction.atomic():
        membership.role = ClubMembership.ROLE_MEMBER
        membership.save(update_fields=["role"])
        other.role = ClubMembership.ROLE_OWNER
        other.save(update_fields=["role"])
    return other


def add_member(club: Club, user, household, *, already_ok=True):
    existing = ClubMembership.objects.filter(club=club, user=user).first()
    if existing:
        return existing, True
    membership = ClubMembership.objects.create(
        club=club,
        user=user,
        role=ClubMembership.ROLE_MEMBER,
        household=household,
        accepted_sharing_version=club.sharing_version,
    )
    ClubEvent.objects.create(
        club=club,
        actor=user,
        kind=ClubEvent.KIND_JOINED,
    )
    set_last_used(user, membership)
    return membership, False


def record_join_event_delivery(club: Club, actor):
    event = (
        ClubEvent.objects.filter(club=club, actor=actor, kind=ClubEvent.KIND_JOINED)
        .order_by("-created")
        .first()
    )
    if event is None:
        return
    from wine_cellar.apps.club.tasks import deliver_club_event

    transaction.on_commit(lambda eid=event.id: deliver_club_event.delay(eid))


def maybe_queue_share_notice(household, exclude_user_id):
    if household is None:
        return
    from wine_cellar.apps.club.tasks import club_share_notice

    transaction.on_commit(
        lambda hid=household.id, uid=exclude_user_id: club_share_notice.delay(hid, uid)
    )


def create_dismantle_vote(club: Club, proposer):
    if club.kind == Club.KIND_GLOBAL:
        return None, "global"
    if club.memberships.count() <= 1:
        return None, "sole_member"
    if club.open_dismantle_vote() is not None:
        return None, "vote_open"
    now = timezone.now()
    try:
        with transaction.atomic():
            vote = ClubDismantleVote.objects.create(
                club=club,
                proposed_by=proposer,
                expires_at=now + DISMANTLE_TTL,
            )
            members = list(club.memberships.select_related("user"))
            for mem in members:
                ClubDismantleBallot.objects.create(
                    vote=vote,
                    user=mem.user,
                    choice="yes" if mem.user_id == proposer.id else None,
                    decided_at=now if mem.user_id == proposer.id else None,
                )
    except IntegrityError:
        return None, "vote_open"
    from wine_cellar.apps.club.tasks import deliver_dismantle_ballots

    transaction.on_commit(lambda vid=vote.id: deliver_dismantle_ballots.delay(vid))
    return vote, None


def expire_if_needed(vote: ClubDismantleVote) -> ClubDismantleVote:
    if vote.status != ClubDismantleVote.STATUS_OPEN:
        return vote
    if vote.expires_at and vote.expires_at <= timezone.now():
        resolve_dismantle(vote, ClubDismantleVote.STATUS_EXPIRED)
        vote.refresh_from_db()
    return vote


def _ballot_recipients(vote: ClubDismantleVote) -> list[dict]:
    return [
        {
            "user_id": ballot.user_id,
            "chat_id": ballot.chat_id,
            "message_id": ballot.message_id,
        }
        for ballot in vote.ballots.all()
    ]


def _refresh_vote(vote: ClubDismantleVote) -> ClubDismantleVote:
    try:
        vote.refresh_from_db()
    except ClubDismantleVote.DoesNotExist:
        pass
    return vote


def resolve_dismantle(vote: ClubDismantleVote, status: str, rejector=None):
    if vote.status != ClubDismantleVote.STATUS_OPEN:
        return vote
    club = vote.club
    club_name = club.name
    recipients = _ballot_recipients(vote)
    vote.status = status
    vote.resolved_at = timezone.now()
    vote.save(update_fields=["status", "resolved_at"])
    from wine_cellar.apps.club.tasks import notify_dismantle_resolved

    transaction.on_commit(
        lambda vid=vote.id, st=status, nm=club_name, rid=(rejector.id if rejector else None), rec=recipients: (
            notify_dismantle_resolved.delay(vid, st, nm, rid, rec)
        )
    )
    if status == ClubDismantleVote.STATUS_PASSED:
        households = list(
            {m.household for m in club.memberships.select_related("household") if m.household}
        )
        club.delete()
        for household in households:
            maybe_clear_share_notice(household)
    return vote


def cast_ballot(vote: ClubDismantleVote, user, choice: str):
    vote = expire_if_needed(vote)
    if vote.status != ClubDismantleVote.STATUS_OPEN:
        return vote, "over"
    ballot = ClubDismantleBallot.objects.filter(vote=vote, user=user).first()
    if ballot is None:
        return vote, "no_ballot"
    if choice not in ("yes", "no"):
        return vote, "bad_choice"
    ballot.choice = choice
    ballot.decided_at = timezone.now()
    ballot.save(update_fields=["choice", "decided_at"])
    if choice == "no":
        resolve_dismantle(vote, ClubDismantleVote.STATUS_REJECTED, rejector=user)
        return _refresh_vote(vote), None
    remaining = ClubDismantleBallot.objects.filter(vote=vote)
    if remaining.exists() and not remaining.filter(choice__isnull=True).exists():
        if remaining.exclude(choice="yes").exists():
            return vote, None
        resolve_dismantle(vote, ClubDismantleVote.STATUS_PASSED)
        return _refresh_vote(vote), None
    from wine_cellar.apps.club.tasks import refresh_dismantle_status

    transaction.on_commit(lambda vid=vote.id: refresh_dismantle_status.delay(vid))
    return vote, None


def cancel_dismantle(vote: ClubDismantleVote):
    vote = expire_if_needed(vote)
    if vote.status != ClubDismantleVote.STATUS_OPEN:
        return vote, "over"
    resolve_dismantle(vote, ClubDismantleVote.STATUS_CANCELLED)
    vote.refresh_from_db()
    return vote, None


def close_tasteoff(tasteoff):
    """Close a taste-off, write rankings onto a feed event, and fan out DMs."""
    from django.db.models import Avg, Count

    from wine_cellar.apps.club.models import TasteOff

    if tasteoff.status == TasteOff.STATUS_CLOSED:
        return tasteoff
    with transaction.atomic():
        locked = (
            TasteOff.objects.select_for_update()
            .filter(id=tasteoff.id)
            .first()
        )
        if locked is None or locked.status == TasteOff.STATUS_CLOSED:
            return locked or tasteoff
        bottles = list(
            locked.bottles.annotate(n=Count("scores"), average=Avg("scores__score"))
        )
        rankings = []
        for bottle in bottles:
            rankings.append(
                {
                    "label": bottle.label,
                    "average": (
                        round(float(bottle.average), 1)
                        if bottle.average is not None
                        else None
                    ),
                    "n": bottle.n,
                }
            )
        rankings.sort(key=lambda r: (-(r["average"] or 0), r["label"]))
        locked.status = TasteOff.STATUS_CLOSED
        locked.closed_at = timezone.now()
        locked.save(update_fields=["status", "closed_at"])
        ClubEvent.objects.create(
            club=locked.club,
            actor=locked.host,
            kind=ClubEvent.KIND_TASTEOFF,
            wine_name=locked.title,
            payload={"title": locked.title, "rankings": rankings},
        )
        tasteoff.status = locked.status
        tasteoff.closed_at = locked.closed_at
    from wine_cellar.apps.club.tasks import deliver_tasteoff_closed

    transaction.on_commit(lambda i=locked.id: deliver_tasteoff_closed.delay(i))
    return locked


def serialize_invite(invite: ClubInvite) -> dict:
    club = invite.club
    return {
        "code": invite.code,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        "max_uses": invite.max_uses,
        "use_count": invite.use_count,
        "cellar_visibility": club.cellar_visibility,
        "badges_visible": club.badges_visible,
        "club_name": club.name,
        "inviter_display_name": display_name(invite.created_by),
        "kind": "club",
        "club": {"id": club.id, "name": club.name},
        "sharing_version": club.sharing_version,
        "expired": invite.is_expired,
        "used": invite.is_used,
        "dismantle_in_progress": club.open_dismantle_vote() is not None,
    }
