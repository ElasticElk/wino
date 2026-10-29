"""Phase 4: staff-run global clubs. Feed only, no cellar peek."""

from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.club.models import Club, ClubEvent, ClubMembership
from wine_cellar.apps.club.tasks import publish_club_event
from wine_cellar.apps.scanner.models import (
    Household,
    HouseholdMembership,
    ServiceConfig,
    TelegramUser,
)
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import Wine


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def _enable(*keys):
    for key in keys:
        ServiceConfig.objects.update_or_create(key=key, defaults={"value": "on"})


def _people(django_user_model):
    hillside = Household.objects.create(name="Hillside")
    riverside = Household.objects.create(name="Riverside")
    riley = django_user_model.objects.create_user(
        username="riley", password="pw", is_staff=True,
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


def _create_global(client, staff, name="New World Cab", description="Cabs that drink well now."):
    _enable("feature_clubs", "feature_global_clubs")
    return _auth(client, staff).post(
        reverse("api-clubs-global"),
        data={"name": name, "description": description},
        content_type="application/json",
    )


def test_flag_off_discover_404(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    _enable("feature_clubs")
    response = _auth(client, riley).get(reverse("api-clubs-discover"))
    assert response.status_code == 404
    assert response.json()["error"] == "not_enabled"


def test_non_staff_cannot_found_global(client, db, django_user_model):
    _people(django_user_model)
    lee = django_user_model.objects.get(username="lee")
    _enable("feature_clubs", "feature_global_clubs")
    response = _auth(client, lee).post(
        reverse("api-clubs-global"),
        data={"name": "New World Cab"},
        content_type="application/json",
    )
    assert response.status_code == 403


def test_staff_create_forces_none_and_no_badges(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    response = _create_global(client, riley)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["kind"] == "global"
    assert body["cellar_visibility"] == "none"
    assert body["badges_visible"] is False
    assert body["description"] == "Cabs that drink well now."
    assert body["frozen"] is False


def test_constraint_rejects_global_with_summary(db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    with pytest.raises(IntegrityError):
        Club.objects.create(
            name="New World Cab",
            created_by=riley,
            kind=Club.KIND_GLOBAL,
            cellar_visibility="summary",
        )


def test_self_join_private_403(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    lee = django_user_model.objects.get(username="lee")
    _enable("feature_clubs", "feature_global_clubs")
    created = _auth(client, riley).post(
        reverse("api-clubs"),
        data={"name": "Thursday reds"},
        content_type="application/json",
    )
    assert created.status_code == 201
    joined = _auth(client, lee).post(
        reverse("api-club-join", kwargs={"pk": created.json()["id"]}),
        data={},
        content_type="application/json",
    )
    assert joined.status_code == 403
    assert joined.json()["error"] == "invite_only"


def test_self_join_global_201(client, db, django_user_model):
    hillside, _riverside, riley, _sam, lee = _people(django_user_model)
    created = _create_global(client, riley)
    club_id = created.json()["id"]
    joined = _auth(client, lee).post(
        reverse("api-club-join", kwargs={"pk": club_id}),
        data={},
        content_type="application/json",
    )
    assert joined.status_code == 201, joined.content
    body = joined.json()
    assert body["kind"] == "global"
    assert body["cellar_visibility"] == "none"
    assert body.get("household") is None
    again = _auth(client, lee).post(
        reverse("api-club-join", kwargs={"pk": club_id}),
        data={},
        content_type="application/json",
    )
    assert again.status_code == 200
    hillside.refresh_from_db()
    assert hillside.club_share_notice_sent_at is None


def test_peek_404_on_global(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    lee = django_user_model.objects.get(username="lee")
    created = _create_global(client, riley)
    club_id = created.json()["id"]
    _auth(client, lee).post(
        reverse("api-club-join", kwargs={"pk": club_id}),
        data={},
        content_type="application/json",
    )
    peek = _auth(client, riley).get(
        reverse("api-club-peek", kwargs={"pk": club_id, "user_id": lee.id}),
    )
    assert peek.status_code == 404
    badges = _auth(client, riley).get(
        reverse("api-club-badges", kwargs={"pk": club_id, "user_id": lee.id}),
    )
    assert badges.status_code == 404
    members = _auth(client, riley).get(
        reverse("api-club-members", kwargs={"pk": club_id}),
    )
    for row in members.json()["members"]:
        assert row["peek"] == "none"
        assert row["badges"] is False
        assert row["household_id"] is None
        assert row["household_name"] == ""
    assert members.json()["households"] == []


def test_owner_cannot_widen_global(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    created = _create_global(client, riley)
    patched = _auth(client, riley).patch(
        reverse("api-club-detail", kwargs={"pk": created.json()["id"]}),
        data={"cellar_visibility": "full"},
        content_type="application/json",
    )
    assert patched.status_code == 400
    assert patched.json()["error"] == "global_clubs_are_feed_only"
    kinded = _auth(client, riley).patch(
        reverse("api-club-detail", kwargs={"pk": created.json()["id"]}),
        data={"kind": "private"},
        content_type="application/json",
    )
    assert kinded.status_code == 400
    assert kinded.json()["error"] == "kind_locked"


def test_global_invite_rejected(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    created = _create_global(client, riley)
    minted = _auth(client, riley).post(
        reverse("api-club-invites", kwargs={"pk": created.json()["id"]}),
        data={},
        content_type="application/json",
    )
    assert minted.status_code == 400


def test_dismantle_global_rejected(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    lee = django_user_model.objects.get(username="lee")
    created = _create_global(client, riley)
    club_id = created.json()["id"]
    _auth(client, lee).post(
        reverse("api-club-join", kwargs={"pk": club_id}),
        data={},
        content_type="application/json",
    )
    vote = _auth(client, riley).post(
        reverse("api-club-dismantle", kwargs={"pk": club_id}),
        data={},
        content_type="application/json",
    )
    assert vote.status_code == 400
    assert vote.json()["error"] == "global"


def test_kick_removes_membership_not_owner(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    lee = django_user_model.objects.get(username="lee")
    created = _create_global(client, riley)
    club_id = created.json()["id"]
    _auth(client, lee).post(
        reverse("api-club-join", kwargs={"pk": club_id}),
        data={},
        content_type="application/json",
    )
    kicked = _auth(client, riley).post(
        reverse("api-club-kick", kwargs={"pk": club_id, "user_id": lee.id}),
        data={},
        content_type="application/json",
    )
    assert kicked.status_code == 200
    assert kicked.json()["status"] == "kicked"
    assert not ClubMembership.objects.filter(club_id=club_id, user=lee).exists()
    assert Club.objects.filter(id=club_id).exists()
    owner = _auth(client, riley).post(
        reverse("api-club-kick", kwargs={"pk": club_id, "user_id": riley.id}),
        data={},
        content_type="application/json",
    )
    assert owner.status_code == 403


def test_blank_staff_note_400(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    created = _create_global(client, riley)
    blank = _auth(client, riley).post(
        reverse("api-club-notes", kwargs={"pk": created.json()["id"]}),
        data={"text": "  "},
        content_type="application/json",
    )
    assert blank.status_code == 400


@patch("wine_cellar.apps.club.tasks._bot_token", return_value="token")
@patch("wine_cellar.apps.club.tasks._send")
def test_staff_note_skips_muted(
    mock_send, _mock_token, client, db, django_user_model,
    django_capture_on_commit_callbacks,
):
    mock_send.return_value = 11
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    sam = django_user_model.objects.get(username="sam")
    lee = django_user_model.objects.get(username="lee")
    created = _create_global(client, riley)
    club_id = created.json()["id"]
    _auth(client, lee).post(
        reverse("api-club-join", kwargs={"pk": club_id}),
        data={},
        content_type="application/json",
    )
    _auth(client, sam).post(
        reverse("api-club-join", kwargs={"pk": club_id}),
        data={},
        content_type="application/json",
    )
    ClubMembership.objects.filter(club_id=club_id, user=sam).update(feed_muted=True)
    with django_capture_on_commit_callbacks(execute=True):
        posted = _auth(client, riley).post(
            reverse("api-club-notes", kwargs={"pk": club_id}),
            data={"text": "This week: Cabs that actually drink well now."},
            content_type="application/json",
        )
    assert posted.status_code == 201, posted.content
    chats = {call.args[1] for call in mock_send.call_args_list}
    assert "300" in chats
    assert "200" not in chats
    assert "100" not in chats
    event = ClubEvent.objects.get(id=posted.json()["event_id"])
    assert event.kind == ClubEvent.KIND_STAFF_NOTE
    text = mock_send.call_args.args[2]
    assert "New World Cab" in text
    assert "Cabs that actually drink well now" in text


def test_frozen_publish_noop_and_join_409(client, db, django_user_model):
    hillside, _riverside, riley, _sam, lee = _people(django_user_model)
    created = _create_global(client, riley)
    club_id = created.json()["id"]
    _auth(client, lee).post(
        reverse("api-club-join", kwargs={"pk": club_id}),
        data={},
        content_type="application/json",
    )
    frozen = _auth(client, riley).patch(
        reverse("api-club-detail", kwargs={"pk": club_id}),
        data={"frozen": True},
        content_type="application/json",
    )
    assert frozen.status_code == 200
    assert frozen.json()["frozen"] is True
    wine = Wine.objects.create(
        name="Cabernet", household=hillside, user=riley, wine_type="RE", country="ZA",
    )
    item = StorageItem.objects.create(
        storage=_fridge(hillside), wine=wine, user=riley, deleted=True,
    )
    publish_club_event(item.id, riley.id, "opened")
    assert ClubEvent.objects.filter(club_id=club_id, kind="opened").count() == 0
    note = _auth(client, riley).post(
        reverse("api-club-notes", kwargs={"pk": club_id}),
        data={"text": "Paused."},
        content_type="application/json",
    )
    assert note.status_code == 409
    sam = django_user_model.objects.get(username="sam")
    blocked = _auth(client, sam).post(
        reverse("api-club-join", kwargs={"pk": club_id}),
        data={},
        content_type="application/json",
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "frozen"


def test_discover_lists_globals(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    lee = django_user_model.objects.get(username="lee")
    created = _create_global(client, riley)
    listed = _auth(client, lee).get(reverse("api-clubs-discover"))
    assert listed.status_code == 200
    rows = listed.json()["clubs"]
    assert len(rows) == 1
    assert rows[0]["id"] == created.json()["id"]
    assert rows[0]["joined"] is False
    assert rows[0]["frozen"] is False
    assert rows[0]["member_count"] == 1
    _auth(client, lee).post(
        reverse("api-club-join", kwargs={"pk": created.json()["id"]}),
        data={},
        content_type="application/json",
    )
    listed = _auth(client, lee).get(reverse("api-clubs-discover"))
    assert listed.json()["clubs"][0]["joined"] is True


@patch("wine_cellar.apps.club.tasks._bot_token", return_value="token")
@patch("wine_cellar.apps.club.tasks._send")
def test_global_join_does_not_heads_up_household(
    mock_send, _mock_token, client, db, django_user_model,
    django_capture_on_commit_callbacks,
):
    mock_send.return_value = 11
    hillside, _riverside, riley, _sam, lee = _people(django_user_model)
    created = _create_global(client, riley)
    with django_capture_on_commit_callbacks(execute=True):
        _auth(client, lee).post(
            reverse("api-club-join", kwargs={"pk": created.json()["id"]}),
            data={},
            content_type="application/json",
        )
    texts = [call.args[2] for call in mock_send.call_args_list]
    for text in texts:
        assert "shared inside a wine club" not in text
        assert "kelder word nou binne" not in text
    hillside.refresh_from_db()
    assert hillside.club_share_notice_sent_at is None


def test_staff_can_freeze_without_membership(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    lee = django_user_model.objects.get(username="lee")
    _enable("feature_clubs", "feature_global_clubs")
    private = _auth(client, lee).post(
        reverse("api-clubs"),
        data={"name": "Thursday reds"},
        content_type="application/json",
    )
    club_id = private.json()["id"]
    frozen = _auth(client, riley).patch(
        reverse("api-club-detail", kwargs={"pk": club_id}),
        data={"frozen": True},
        content_type="application/json",
    )
    assert frozen.status_code == 200
    assert frozen.json()["frozen"] is True
    Club.objects.get(id=club_id).refresh_from_db()
    assert Club.objects.get(id=club_id).frozen is True
