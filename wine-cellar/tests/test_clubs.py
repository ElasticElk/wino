"""Private wine clubs: CRUD, peek privacy, invites, feed hooks, dismantle."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from wine_cellar.apps.club.models import (
    Club,
    ClubDismantleVote,
    ClubEvent,
    ClubInvite,
    ClubMembership,
)
from wine_cellar.apps.club.peek import FORBIDDEN_PEEK_KEYS, collect_keys
from wine_cellar.apps.club.tasks import (
    club_share_notice,
    club_widen_notice,
    expire_dismantle_votes,
    publish_club_event,
)
from wine_cellar.apps.scanner.models import (
    Household,
    HouseholdInvite,
    HouseholdMembership,
    ServiceConfig,
    TelegramUser,
    mint_unique_code,
)
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import Wine


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def _enable_clubs():
    ServiceConfig.objects.update_or_create(
        key="feature_clubs", defaults={"value": "on"},
    )


def _people(django_user_model, *, staff=False):
    hillside = Household.objects.create(name="Hillside")
    riverside = Household.objects.create(name="Riverside")
    riley = django_user_model.objects.create_user(
        username="riley", password="pw", is_staff=staff,
    )
    sam = django_user_model.objects.create_user(username="sam", password="pw")
    lee = django_user_model.objects.create_user(username="lee", password="pw")
    HouseholdMembership.objects.create(user=riley, household=hillside, role="owner")
    HouseholdMembership.objects.create(user=sam, household=hillside, role="member")
    HouseholdMembership.objects.create(user=lee, household=riverside, role="owner")
    TelegramUser.objects.create(user=riley, chat_id="100", display_name="Riley")
    TelegramUser.objects.create(user=sam, chat_id="200", display_name="Sam")
    TelegramUser.objects.create(user=lee, chat_id="300", display_name="Lee")
    Storage.objects.create(name="Fridge", household=hillside, user=riley, location="F")
    Storage.objects.create(name="Fridge", household=riverside, user=lee, location="F")
    return hillside, riverside, riley, sam, lee


def _fridge(household):
    return Storage.objects.get(household=household, name="Fridge")


def _create_club(client, user, name="Thursday reds", **extra):
    _enable_clubs()
    auth = _auth(client, user)
    body = {"name": name, **extra}
    return auth.post(reverse("api-clubs"), data=body, content_type="application/json")


def test_flag_off_returns_not_enabled(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    response = _auth(client, riley).get(reverse("api-clubs"))
    assert response.status_code == 404
    assert response.json()["error"] == "not_enabled"


def test_create_defaults_full_and_badges(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    response = _create_club(client, riley)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["cellar_visibility"] == "full"
    assert body["badges_visible"] is True
    assert body["role"] == "owner"
    assert body["member_count"] == 1
    assert body["kind"] == "private"


def test_founder_unique_name_409(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    assert _create_club(client, riley).status_code == 201
    again = _create_club(client, riley)
    assert again.status_code == 409
    assert again.json()["error"] == "You already have a club with that name."


def test_member_cannot_patch_policy(client, db, django_user_model):
    hillside, _riverside, riley, sam, lee = _people(django_user_model)
    created = _create_club(client, riley).json()
    club = Club.objects.get(id=created["id"])
    ClubMembership.objects.create(
        club=club, user=lee, role="member", household=_riverside,
        accepted_sharing_version=1,
    )
    patched = _auth(client, lee).patch(
        reverse("api-club-detail", kwargs={"pk": club.id}),
        data={"cellar_visibility": "none"},
        content_type="application/json",
    )
    assert patched.status_code == 403


def test_peek_summary_has_no_forbidden_keys(client, db, django_user_model):
    hillside, riverside, riley, sam, lee = _people(django_user_model)
    wine = Wine.objects.create(
        name="Merlot", household=hillside, user=riley, wine_type="RE",
        country="ZA", vintage=2019, price=12, comment="secret",
        critic_score=90, vivino_rating=4.2, market_price=20, barcode="123",
    )
    StorageItem.objects.create(
        storage=_fridge(hillside), wine=wine,
        user=riley, price=9, row=2,
    )
    created = _create_club(
        client, riley, cellar_visibility="summary", badges_visible=False,
    ).json()
    club = Club.objects.get(id=created["id"])
    ClubMembership.objects.create(
        club=club, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    peeked = _auth(client, lee).get(
        reverse("api-club-peek", kwargs={"pk": club.id, "user_id": riley.id}),
    )
    assert peeked.status_code == 200, peeked.content
    keys = collect_keys(peeked.json())
    assert not (keys & FORBIDDEN_PEEK_KEYS), keys & FORBIDDEN_PEEK_KEYS
    assert peeked.json()["visibility"] == "summary"
    assert peeked.json()["total_bottles"] == 1


def test_peek_full_has_no_forbidden_keys(client, db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model)
    wine = Wine.objects.create(
        name="Chenin Blanc", household=hillside, user=riley, wine_type="WH",
        country="ZA", vintage=2022, price=8, barcode="999",
    )
    StorageItem.objects.create(
        storage=_fridge(hillside), wine=wine, user=riley,
    )
    created = _create_club(client, riley).json()
    club = Club.objects.get(id=created["id"])
    ClubMembership.objects.create(
        club=club, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    peeked = _auth(client, lee).get(
        reverse("api-club-peek", kwargs={"pk": club.id, "user_id": riley.id}),
    )
    assert peeked.status_code == 200
    keys = collect_keys(peeked.json())
    assert not (keys & FORBIDDEN_PEEK_KEYS)
    assert peeked.json()["visibility"] == "full"
    assert peeked.json()["wines"][0]["name"] == "Chenin Blanc"
    assert "region" in peeked.json()["wines"][0]
    assert peeked.json()["wines"][0]["region"] == ""


def test_peek_none_is_404(client, db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model)
    created = _create_club(client, riley, cellar_visibility="none").json()
    club = Club.objects.get(id=created["id"])
    ClubMembership.objects.create(
        club=club, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    peeked = _auth(client, lee).get(
        reverse("api-club-peek", kwargs={"pk": club.id, "user_id": riley.id}),
    )
    assert peeked.status_code == 404
    assert peeked.json()["error"] == "not_visible"


def test_members_payload_collapses_household(client, db, django_user_model):
    hillside, _riverside, riley, sam, lee = _people(django_user_model)
    created = _create_club(client, riley).json()
    club = Club.objects.get(id=created["id"])
    ClubMembership.objects.create(
        club=club, user=sam, role="member", household=hillside,
        accepted_sharing_version=1,
    )
    listed = _auth(client, riley).get(
        reverse("api-club-members", kwargs={"pk": club.id}),
    )
    body = listed.json()
    assert len(body["members"]) == 2
    hills = [h for h in body["households"] if h["name"] == "Hillside"]
    assert len(hills) == 1
    assert set(hills[0]["user_ids"]) == {riley.id, sam.id}


def test_invite_code_not_in_household_table(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    club_id = _create_club(client, riley).json()["id"]
    minted = _auth(client, riley).post(
        reverse("api-club-invites", kwargs={"pk": club_id}),
        data={}, content_type="application/json",
    )
    assert minted.status_code == 201, minted.content
    code = minted.json()["code"]
    assert not HouseholdInvite.objects.filter(code=code).exists()
    assert ClubInvite.objects.filter(code=code).exists()


def test_preview_finds_club_after_household_miss(client, db, django_user_model):
    _people(django_user_model, staff=True)
    riley = django_user_model.objects.get(username="riley")
    club_id = _create_club(client, riley).json()["id"]
    minted = _auth(client, riley).post(
        reverse("api-club-invites", kwargs={"pk": club_id}),
        data={}, content_type="application/json",
    ).json()
    preview = _auth(client, riley).post(
        reverse("api-scanner-invites-preview"),
        data={"code": minted["code"]},
        content_type="application/json",
    )
    assert preview.status_code == 200
    assert preview.json()["kind"] == "club"
    assert preview.json()["club"]["name"] == "Thursday reds"


def test_redeem_compound_creates_household_and_club(client, db, django_user_model):
    _people(django_user_model, staff=True)
    riley = django_user_model.objects.get(username="riley")
    club_id = _create_club(client, riley).json()["id"]
    code = _auth(client, riley).post(
        reverse("api-club-invites", kwargs={"pk": club_id}),
        data={}, content_type="application/json",
    ).json()["code"]
    redeemed = _auth(client, riley).post(
        reverse("api-scanner-club-invites-redeem"),
        data={
            "code": code,
            "chat_id": "900",
            "display_name": "Pat",
            "household_name": "Pat's cellar",
        },
        content_type="application/json",
    )
    assert redeemed.status_code == 201, redeemed.content
    body = redeemed.json()
    assert body["household"]["name"] == "Pat's cellar"
    assert body["club"]["id"] == club_id
    assert body["first_cellar"] is True
    assert Household.objects.filter(name="Pat's cellar").count() == 1
    assert ClubMembership.objects.filter(club_id=club_id).count() == 2


def test_redeem_already_member_idempotent(client, db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model, staff=True)
    club_id = _create_club(client, riley).json()["id"]
    ClubMembership.objects.create(
        club_id=club_id, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    code = _auth(client, riley).post(
        reverse("api-club-invites", kwargs={"pk": club_id}),
        data={}, content_type="application/json",
    ).json()["code"]
    first = _auth(client, riley).post(
        reverse("api-scanner-club-invites-redeem"),
        data={"code": code, "chat_id": "300", "display_name": "Lee", "household_id": riverside.id},
        content_type="application/json",
    )
    assert first.status_code == 200
    assert first.json()["status"] == "already_member"
    assert ClubMembership.objects.filter(club_id=club_id, user=lee).count() == 1


def test_widen_unacked_peek_uses_previous(client, db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model)
    created = _create_club(client, riley, cellar_visibility="summary").json()
    club = Club.objects.get(id=created["id"])
    ClubMembership.objects.create(
        club=club, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    Wine.objects.create(
        name="Merlot", household=hillside, user=riley, wine_type="RE", country="ZA",
    )
    StorageItem.objects.create(
        storage=_fridge(hillside),
        wine=Wine.objects.get(name="Merlot"), user=riley,
    )
    patched = _auth(client, riley).patch(
        reverse("api-club-detail", kwargs={"pk": club.id}),
        data={"cellar_visibility": "full"},
        content_type="application/json",
    )
    assert patched.status_code == 200
    club.refresh_from_db()
    assert club.sharing_version == 2
    peeked = _auth(client, lee).get(
        reverse("api-club-peek", kwargs={"pk": club.id, "user_id": riley.id}),
    )
    assert peeked.json()["visibility"] == "summary"
    stayed = _auth(client, lee).patch(
        reverse("api-club-membership", kwargs={"pk": club.id}),
        data={"accepted_sharing_version": 2},
        content_type="application/json",
    )
    assert stayed.status_code == 200
    peeked2 = _auth(client, lee).get(
        reverse("api-club-peek", kwargs={"pk": club.id, "user_id": riley.id}),
    )
    assert peeked2.json()["visibility"] == "full"


def test_narrowing_auto_acks(client, db, django_user_model):
    _hillside, riverside, riley, _sam, lee = _people(django_user_model)
    created = _create_club(client, riley).json()
    club = Club.objects.get(id=created["id"])
    mem = ClubMembership.objects.create(
        club=club, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    _auth(client, riley).patch(
        reverse("api-club-detail", kwargs={"pk": club.id}),
        data={"cellar_visibility": "summary"},
        content_type="application/json",
    )
    mem.refresh_from_db()
    assert mem.accepted_sharing_version == club.sharing_version


def test_owner_leave_requires_handover_last_deletes(client, db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model)
    created = _create_club(client, riley).json()
    club_id = created["id"]
    ClubMembership.objects.create(
        club_id=club_id, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    blocked = _auth(client, riley).post(
        reverse("api-club-leave", kwargs={"pk": club_id}),
        data={}, content_type="application/json",
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "hand_over_required"
    handed = _auth(client, riley).post(
        reverse("api-club-handover", kwargs={"pk": club_id}),
        data={"user_id": lee.id}, content_type="application/json",
    )
    assert handed.status_code == 200
    left = _auth(client, riley).post(
        reverse("api-club-leave", kwargs={"pk": club_id}),
        data={}, content_type="application/json",
    )
    assert left.status_code == 200
    last = _auth(client, lee).post(
        reverse("api-club-leave", kwargs={"pk": club_id}),
        data={}, content_type="application/json",
    )
    assert last.status_code == 200
    assert not Club.objects.filter(id=club_id).exists()


def test_dismantle_unanimous_and_no(client, db, django_user_model):
    hillside, riverside, riley, sam, lee = _people(django_user_model)
    created = _create_club(client, riley).json()
    club = Club.objects.get(id=created["id"])
    ClubMembership.objects.create(
        club=club, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    started = _auth(client, riley).post(
        reverse("api-club-dismantle", kwargs={"pk": club.id}),
        data={}, content_type="application/json",
    )
    assert started.status_code == 201, started.content
    vote_id = started.json()["id"]
    assert started.json()["yes"] == 1
    assert started.json()["pending"] == 1
    rejected = _auth(client, lee).post(
        reverse(
            "api-club-dismantle-ballot",
            kwargs={"pk": club.id, "vote_id": vote_id},
        ),
        data={"choice": "no"}, content_type="application/json",
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert Club.objects.filter(id=club.id).exists()

    started2 = _auth(client, riley).post(
        reverse("api-club-dismantle", kwargs={"pk": club.id}),
        data={}, content_type="application/json",
    )
    assert started2.status_code == 201
    vote2 = started2.json()["id"]
    passed = _auth(client, lee).post(
        reverse(
            "api-club-dismantle-ballot",
            kwargs={"pk": club.id, "vote_id": vote2},
        ),
        data={"choice": "yes"}, content_type="application/json",
    )
    assert passed.status_code == 200
    assert not Club.objects.filter(id=club.id).exists()


def test_join_blocked_while_dismantle_open(client, db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model, staff=True)
    created = _create_club(client, riley).json()
    club = Club.objects.get(id=created["id"])
    ClubMembership.objects.create(
        club=club, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    _auth(client, riley).post(
        reverse("api-club-dismantle", kwargs={"pk": club.id}),
        data={}, content_type="application/json",
    )
    minted = _auth(client, riley).post(
        reverse("api-club-invites", kwargs={"pk": club.id}),
        data={}, content_type="application/json",
    )
    assert minted.status_code == 409
    assert minted.json()["error"] == "dismantle_in_progress"


def test_lock_club_invite_qs_has_no_join():
    from wine_cellar.apps.api.club_views import lock_club_invite_qs

    sql = str(lock_club_invite_qs("AbCdEfGh").query).upper()
    assert "FOR UPDATE" in sql or "SELECT" in sql
    assert "JOIN" not in sql


def test_publish_noop_when_flag_off(db, django_user_model):
    hillside, _riverside, riley, _sam, _lee = _people(django_user_model)
    wine = Wine.objects.create(
        name="Merlot", household=hillside, user=riley, wine_type="RE", country="ZA",
    )
    item = StorageItem.objects.create(
        storage=_fridge(hillside), wine=wine, user=riley,
        deleted=True,
    )
    club = Club.objects.create(name="Thursday reds", created_by=riley)
    ClubMembership.objects.create(
        club=club, user=riley, role="owner", household=hillside,
    )
    publish_club_event(item.id, riley.id, "opened")
    assert ClubEvent.objects.count() == 0


def test_stock_remove_creates_event_move_does_not(
    client, db, django_user_model, django_capture_on_commit_callbacks,
):
    hillside, _riverside, riley, _sam, _lee = _people(django_user_model)
    _enable_clubs()
    wine = Wine.objects.create(
        name="Merlot", household=hillside, user=riley, wine_type="RE",
        country="ZA", vintage=2019,
    )
    storage = _fridge(hillside)
    item = StorageItem.objects.create(storage=storage, wine=wine, user=riley)
    other = Storage.objects.create(
        name="Rack", household=hillside, user=riley, location="R",
    )
    club = Club.objects.create(name="Thursday reds", created_by=riley)
    ClubMembership.objects.create(
        club=club, user=riley, role="owner", household=hillside,
    )
    with django_capture_on_commit_callbacks(execute=True):
        moved = _auth(client, riley).patch(
            reverse("api-stock-move", kwargs={"pk": item.id}),
            data={"storage_name": "Rack"},
            content_type="application/json",
        )
    assert moved.status_code == 200
    assert ClubEvent.objects.count() == 0
    with django_capture_on_commit_callbacks(execute=True):
        removed = _auth(client, riley).post(
            reverse("api-stock-remove"),
            data={"item_id": item.id},
            content_type="application/json",
        )
    assert removed.status_code == 200
    assert ClubEvent.objects.filter(kind="opened", item_id=item.id).count() == 1


def test_unique_opened_per_club_item(db, django_user_model):
    hillside, _riverside, riley, _sam, _lee = _people(django_user_model)
    _enable_clubs()
    wine = Wine.objects.create(
        name="Merlot", household=hillside, user=riley, wine_type="RE", country="ZA",
    )
    item = StorageItem.objects.create(
        storage=_fridge(hillside), wine=wine, user=riley,
        deleted=True,
    )
    club = Club.objects.create(name="Thursday reds", created_by=riley)
    ClubMembership.objects.create(
        club=club, user=riley, role="owner", household=hillside,
    )
    publish_club_event(item.id, riley.id, "opened")
    publish_club_event(item.id, riley.id, "opened")
    assert ClubEvent.objects.filter(kind="opened", item_id=item.id).count() == 1


def test_share_notice_copy_has_no_names(db, django_user_model):
    from wine_cellar.apps.club.copy import MESSAGES

    en = MESSAGES["en"]["club.hh.notice"]
    af = MESSAGES["af"]["club.hh.notice"]
    for text in (en, af):
        assert "{club}" not in text
        assert "{name}" not in text
        assert "{household}" not in text
        assert "Thursday" not in text
        assert "Riley" not in text
        assert "Hillside" not in text


@patch("wine_cellar.apps.club.tasks._bot_token", return_value="token")
@patch("wine_cellar.apps.club.tasks._send")
def test_founding_notifies_other_household_members(
    mock_send, _mock_token, client, db, django_user_model, django_capture_on_commit_callbacks,
):
    mock_send.return_value = 11
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    with django_capture_on_commit_callbacks(execute=True):
        assert _create_club(client, riley).status_code == 201
    chats = {call.args[1] for call in mock_send.call_args_list}
    assert "100" not in chats
    assert "200" in chats


@patch("wine_cellar.apps.club.tasks._bot_token", return_value="token")
@patch("wine_cellar.apps.club.tasks._send")
def test_share_notice_noop_when_flag_off(mock_send, _mock_token, db, django_user_model):
    mock_send.return_value = 11
    hillside, _riverside, riley, _sam, lee = _people(django_user_model)
    club = Club.objects.create(
        name="Thursday reds", created_by=lee, cellar_visibility="full",
    )
    ClubMembership.objects.create(
        club=club, user=lee, role="owner",
        household=Household.objects.get(name="Riverside"),
    )
    ClubMembership.objects.create(
        club=club, user=riley, role="member", household=hillside,
        accepted_sharing_version=1,
    )
    club_share_notice(hillside.id, riley.id)
    mock_send.assert_not_called()
    hillside.refresh_from_db()
    assert hillside.club_share_notice_sent_at is None


@patch("wine_cellar.apps.club.tasks._bot_token", return_value="token")
@patch("wine_cellar.apps.club.tasks._send")
def test_widen_notice_noop_when_flag_off(mock_send, _mock_token, db, django_user_model):
    mock_send.return_value = 11
    hillside, _riverside, riley, sam, lee = _people(django_user_model)
    club = Club.objects.create(
        name="Thursday reds", created_by=lee, cellar_visibility="full",
        sharing_version=2, previous_cellar_visibility="summary",
    )
    ClubMembership.objects.create(
        club=club, user=lee, role="owner",
        household=Household.objects.get(name="Riverside"),
    )
    ClubMembership.objects.create(
        club=club, user=riley, role="member", household=hillside,
        accepted_sharing_version=1,
    )
    ClubMembership.objects.create(
        club=club, user=sam, role="member", household=hillside,
        accepted_sharing_version=1,
    )
    club_widen_notice(club.id)
    mock_send.assert_not_called()


def test_expire_dismantle_noop_when_flag_off(db, django_user_model):
    hillside, _riverside, riley, sam, _lee = _people(django_user_model)
    club = Club.objects.create(name="Thursday reds", created_by=riley)
    ClubMembership.objects.create(
        club=club, user=riley, role="owner", household=hillside,
    )
    ClubMembership.objects.create(
        club=club, user=sam, role="member", household=hillside,
    )
    vote = ClubDismantleVote.objects.create(
        club=club,
        proposed_by=riley,
        expires_at=timezone.now() - timedelta(hours=1),
    )
    expire_dismantle_votes()
    vote.refresh_from_db()
    assert vote.status == ClubDismantleVote.STATUS_OPEN
    assert Club.objects.filter(id=club.id).exists()


@patch("wine_cellar.apps.club.tasks._bot_token", return_value="token")
@patch("wine_cellar.apps.club.tasks._send")
def test_share_notice_sent_once_not_to_joiner(mock_send, _mock_token, db, django_user_model):
    mock_send.return_value = 11
    _enable_clubs()
    hillside, _riverside, riley, sam, lee = _people(django_user_model)
    club = Club.objects.create(
        name="Thursday reds", created_by=lee, cellar_visibility="full",
    )
    ClubMembership.objects.create(
        club=club, user=lee, role="owner", household=Household.objects.get(name="Riverside"),
    )
    ClubMembership.objects.create(
        club=club, user=riley, role="member", household=hillside,
        accepted_sharing_version=1,
    )
    club_share_notice(hillside.id, riley.id)
    chats = {call.args[1] for call in mock_send.call_args_list}
    assert "100" not in chats
    assert "200" in chats
    hillside.refresh_from_db()
    assert hillside.club_share_notice_sent_at is not None
    mock_send.reset_mock()
    club_share_notice(hillside.id, riley.id)
    mock_send.assert_not_called()


def test_mint_unique_skips_club_codes(db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    club = Club.objects.create(name="Thursday reds", created_by=riley)
    ClubInvite.objects.create(
        club=club, created_by=riley, code="Taken123",
        expires_at=timezone.now() + timedelta(days=30),
    )
    code = mint_unique_code(taken=lambda c: c == "Taken123" or c == "Taken123")
    assert code != "Taken123"


def test_global_create_403(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    response = _create_club(client, riley, **{"kind": "global"})
    assert response.status_code == 403


def test_reopen_after_retract_republishes(db, django_user_model):
    hillside, _riverside, riley, _sam, _lee = _people(django_user_model)
    _enable_clubs()
    wine = Wine.objects.create(
        name="Merlot", household=hillside, user=riley, wine_type="RE", country="ZA",
    )
    item = StorageItem.objects.create(
        storage=_fridge(hillside), wine=wine, user=riley, deleted=True,
    )
    club = Club.objects.create(name="Thursday reds", created_by=riley)
    ClubMembership.objects.create(
        club=club, user=riley, role="owner", household=hillside,
    )
    publish_club_event(item.id, riley.id, "opened")
    publish_club_event(item.id, riley.id, "retracted")
    event = ClubEvent.objects.get(kind="opened", item_id=item.id)
    assert event.retracted_at is not None
    publish_club_event(item.id, riley.id, "opened")
    event.refresh_from_db()
    assert event.retracted_at is None
    assert ClubEvent.objects.filter(kind="opened", item_id=item.id).count() == 1


def test_members_include_effective_peek(client, db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model)
    created = _create_club(client, riley, cellar_visibility="summary").json()
    club = Club.objects.get(id=created["id"])
    ClubMembership.objects.create(
        club=club, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    listed = _auth(client, riley).get(
        reverse("api-club-members", kwargs={"pk": club.id}),
    )
    by_id = {row["user_id"]: row for row in listed.json()["members"]}
    assert by_id[riley.id]["peek"] == "summary"
    assert by_id[lee.id]["peek"] == "summary"
