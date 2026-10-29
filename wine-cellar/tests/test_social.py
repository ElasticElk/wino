"""Phase 3: comments, feed→To-drink, club lists, taste-offs."""

from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from wine_cellar.apps.club.copy import comment_card_text
from wine_cellar.apps.club.models import (
    Club,
    ClubEvent,
    ClubMembership,
    TasteOff,
    TasteOffBottle,
    TasteOffScore,
)
from wine_cellar.apps.club.services import close_tasteoff
from wine_cellar.apps.club.tasks import close_stale_tasteoffs
from wine_cellar.apps.scanner.models import (
    Household,
    HouseholdMembership,
    ServiceConfig,
    TelegramUser,
)
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import Wine, WineList, WineListItem


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
    riley = django_user_model.objects.create_user(username="riley", password="pw")
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


def _club_with_member(client, riley, lee, riverside):
    _enable("feature_clubs")
    created = _auth(client, riley).post(
        reverse("api-clubs"),
        data={"name": "Thursday reds"},
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    club = Club.objects.get(id=created.json()["id"])
    ClubMembership.objects.create(
        club=club, user=lee, role="member", household=riverside,
        accepted_sharing_version=1,
    )
    return club


def _opened_event(club, actor, wine_name="Merlot", vintage=2019, producer="Coastal"):
    return ClubEvent.objects.create(
        club=club,
        actor=actor,
        kind=ClubEvent.KIND_OPENED,
        wine_name=wine_name,
        vintage=vintage,
        producer=producer,
    )


def test_comments_flag_off_404(client, db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    event = _opened_event(club, riley)
    response = _auth(client, lee).post(
        reverse("api-club-event-comments", kwargs={"pk": club.id, "event_id": event.id}),
        data={"text": "nice"},
        content_type="application/json",
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_enabled"


def test_comment_cap_blank_and_non_member(client, db, django_user_model):
    hillside, riverside, riley, sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    _enable("feature_club_comments")
    event = _opened_event(club, riley)
    url = reverse("api-club-event-comments", kwargs={"pk": club.id, "event_id": event.id})
    too_long = _auth(client, lee).post(
        url, data={"text": "x" * 281}, content_type="application/json",
    )
    assert too_long.status_code == 400
    assert too_long.json()["error"] == "too_long"
    blank = _auth(client, lee).post(
        url, data={"text": "  "}, content_type="application/json",
    )
    assert blank.status_code == 400
    assert blank.json()["error"] == "blank"
    outsider = _auth(client, sam).post(
        url, data={"text": "hello"}, content_type="application/json",
    )
    assert outsider.status_code == 404
    ok = _auth(client, lee).post(
        url, data={"text": "we had that in Stellenbosch"}, content_type="application/json",
    )
    assert ok.status_code == 201, ok.content
    assert ok.json()["display_name"] == "Lee"
    listed = _auth(client, riley).get(url)
    assert listed.status_code == 200
    assert listed.json()["count"] == 1


def test_comment_on_retracted_is_409(client, db, django_user_model):
    _hillside, riverside, riley, _sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    _enable("feature_club_comments")
    event = _opened_event(club, riley)
    event.retracted_at = timezone.now()
    event.save(update_fields=["retracted_at"])
    response = _auth(client, lee).post(
        reverse("api-club-event-comments", kwargs={"pk": club.id, "event_id": event.id}),
        data={"text": "too late"},
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json()["error"] == "retracted"


def test_comment_card_escapes_html():
    text = comment_card_text(
        "en", "Riley", 2019, "Merlot", "down",
        [("<b>Sam</b>", "<script>x</script>")],
    )
    assert "<script>" not in text
    assert "&lt;b&gt;Sam&lt;/b&gt;" in text
    assert "&lt;script&gt;x&lt;/script&gt;" in text


def test_to_list_writes_caller_personal_not_stock_or_partner(client, db, django_user_model):
    hillside, riverside, riley, sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    _enable("feature_lists")
    event = _opened_event(club, riley)
    url = reverse("api-club-event-to-list", kwargs={"pk": club.id, "event_id": event.id})
    added = _auth(client, lee).post(url, data={}, content_type="application/json")
    assert added.status_code == 201, added.content
    assert added.json()["source"] == "club_feed"
    assert added.json()["name"] == "Merlot"
    assert WineList.objects.filter(owner=lee, kind="personal").count() == 1
    assert WineList.objects.filter(owner=riley).count() == 0
    assert WineList.objects.filter(owner=sam).count() == 0
    assert WineListItem.objects.filter(list__owner=lee).count() == 1
    assert StorageItem.objects.filter(deleted=False).count() == 0
    again = _auth(client, lee).post(url, data={}, content_type="application/json")
    assert again.status_code == 200
    assert WineListItem.objects.filter(list__owner=lee, moved_at__isnull=True).count() == 1


def test_to_list_404_if_lists_flag_off(client, db, django_user_model):
    _hillside, riverside, riley, _sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    event = _opened_event(club, riley)
    response = _auth(client, lee).post(
        reverse("api-club-event-to-list", kwargs={"pk": club.id, "event_id": event.id}),
        data={},
        content_type="application/json",
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_enabled"


def test_club_list_names_only_and_remove_rules(client, db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    _enable("feature_lists")
    wine = Wine.objects.create(
        name="Chenin Blanc", household=hillside, user=riley, wine_type="WH",
        country="ZA", vintage=2022, price=12, barcode="111",
    )
    StorageItem.objects.create(storage=_fridge(hillside), wine=wine, user=riley)
    listed = _auth(client, lee).get(reverse("api-club-list", kwargs={"pk": club.id}))
    assert listed.status_code == 200
    assert listed.json()["kind"] == "club"
    assert listed.json()["club_id"] == club.id
    added = _auth(client, lee).post(
        reverse("api-club-list-items", kwargs={"pk": club.id}),
        data={
            "name": "Chenin Blanc",
            "vintage": 2022,
            "producer": "Coastal Cellars",
            "wine_id": wine.id,
        },
        content_type="application/json",
    )
    assert added.status_code == 201, added.content
    body = added.json()
    assert body["name"] == "Chenin Blanc"
    assert "price" not in body
    assert "wine_id" not in body
    item_id = body["id"]
    lst = WineList.objects.get(kind="club", club=club)
    assert lst.owner_id is None
    forbidden = _auth(client, lee).delete(
        reverse("api-club-list-item-delete", kwargs={"pk": club.id, "item_id": item_id}),
    )
    # adder may delete
    assert forbidden.status_code == 200
    other = _auth(client, riley).post(
        reverse("api-club-list-items", kwargs={"pk": club.id}),
        data={"name": "Syrah", "vintage": 2020},
        content_type="application/json",
    )
    other_id = other.json()["id"]
    blocked = _auth(client, lee).delete(
        reverse("api-club-list-item-delete", kwargs={"pk": club.id, "item_id": other_id}),
    )
    assert blocked.status_code == 403
    owner_del = _auth(client, riley).delete(
        reverse("api-club-list-item-delete", kwargs={"pk": club.id, "item_id": other_id}),
    )
    assert owner_del.status_code == 200


def test_club_list_ignores_wine_id_when_summary(client, db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    club.cellar_visibility = "summary"
    club.save(update_fields=["cellar_visibility"])
    _enable("feature_lists")
    wine = Wine.objects.create(
        name="Chenin Blanc", household=hillside, user=riley, wine_type="WH",
        vintage=2022,
    )
    StorageItem.objects.create(storage=_fridge(hillside), wine=wine, user=riley)
    added = _auth(client, lee).post(
        reverse("api-club-list-items", kwargs={"pk": club.id}),
        data={"name": "Chenin Blanc", "vintage": 2022, "wine_id": wine.id},
        content_type="application/json",
    )
    assert added.status_code == 201
    item = WineListItem.objects.get(id=added.json()["id"])
    assert item.wine_id is None


def test_tasteoff_scores_and_one_open(client, db, django_user_model):
    _hillside, riverside, riley, _sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    _enable("feature_tasteoffs")
    created = _auth(client, lee).post(
        reverse("api-club-tasteoffs", kwargs={"pk": club.id}),
        data={"title": "Thursday reds · 5 Nov"},
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    tid = created.json()["id"]
    again = _auth(client, riley).post(
        reverse("api-club-tasteoffs", kwargs={"pk": club.id}),
        data={"title": "Another"},
        content_type="application/json",
    )
    assert again.status_code == 409
    assert again.json()["error"] == "already_open"
    assert again.json()["id"] == tid
    bottles = reverse("api-club-tasteoff-bottles", kwargs={"pk": club.id, "tid": tid})
    b1 = _auth(client, lee).post(
        bottles, data={"label": "Chenin Blanc 2022"}, content_type="application/json",
    )
    b2 = _auth(client, lee).post(
        bottles, data={"label": "Merlot 2019"}, content_type="application/json",
    )
    assert b1.status_code == 201 and b2.status_code == 201
    opened = _auth(client, lee).post(
        reverse("api-club-tasteoff-open", kwargs={"pk": club.id, "tid": tid}),
        data={},
        content_type="application/json",
    )
    assert opened.status_code == 200, opened.content
    bad = _auth(client, riley).patch(
        reverse("api-club-tasteoff-scores", kwargs={"pk": club.id, "tid": tid}),
        data={"bottle_id": b1.json()["id"], "score": 6},
        content_type="application/json",
    )
    assert bad.status_code == 400
    scored = _auth(client, riley).patch(
        reverse("api-club-tasteoff-scores", kwargs={"pk": club.id, "tid": tid}),
        data={"bottle_id": b1.json()["id"], "score": 4},
        content_type="application/json",
    )
    assert scored.status_code == 200
    hidden = _auth(client, lee).get(
        reverse("api-club-tasteoff-detail", kwargs={"pk": club.id, "tid": tid}),
    )
    bottles_out = hidden.json()["bottles"]
    riley_view = next(b for b in bottles_out if b["id"] == b1.json()["id"])
    # host sees own scores only until close; riley's score is hidden from lee
    assert riley_view.get("average") is None
    closed = _auth(client, lee).post(
        reverse("api-club-tasteoff-close", kwargs={"pk": club.id, "tid": tid}),
        data={},
        content_type="application/json",
    )
    assert closed.status_code == 200, closed.content
    event = ClubEvent.objects.get(club=club, kind=ClubEvent.KIND_TASTEOFF)
    assert event.payload["title"] == "Thursday reds · 5 Nov"
    assert event.payload["rankings"]
    assert StorageItem.objects.filter(deleted=False).count() == 0


def test_tasteoff_score_constraint(db, django_user_model):
    hillside, riverside, riley, _sam, lee = _people(django_user_model)
    _enable("feature_clubs", "feature_tasteoffs")
    club = Club.objects.create(name="Thursday reds", created_by=riley)
    ClubMembership.objects.create(
        club=club, user=riley, role="owner", household=hillside,
        accepted_sharing_version=1,
    )
    tasteoff = TasteOff.objects.create(club=club, host=riley, title="Flight")
    bottle = TasteOffBottle.objects.create(tasteoff=tasteoff, label="Chenin", sort_order=0)
    with pytest.raises(IntegrityError):
        TasteOffScore.objects.create(
            tasteoff=tasteoff, bottle=bottle, user=riley, score=6,
        )


def test_tasteoff_need_two_and_auto_close(client, db, django_user_model):
    _hillside, riverside, riley, _sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    _enable("feature_tasteoffs")
    created = _auth(client, lee).post(
        reverse("api-club-tasteoffs", kwargs={"pk": club.id}),
        data={"title": "Solo"},
        content_type="application/json",
    )
    tid = created.json()["id"]
    _auth(client, lee).post(
        reverse("api-club-tasteoff-bottles", kwargs={"pk": club.id, "tid": tid}),
        data={"label": "Only one"},
        content_type="application/json",
    )
    opened = _auth(client, lee).post(
        reverse("api-club-tasteoff-open", kwargs={"pk": club.id, "tid": tid}),
        data={},
        content_type="application/json",
    )
    assert opened.status_code == 409
    assert opened.json()["error"] == "need_two"
    _auth(client, lee).post(
        reverse("api-club-tasteoff-bottles", kwargs={"pk": club.id, "tid": tid}),
        data={"label": "Second"},
        content_type="application/json",
    )
    opened = _auth(client, lee).post(
        reverse("api-club-tasteoff-open", kwargs={"pk": club.id, "tid": tid}),
        data={},
        content_type="application/json",
    )
    assert opened.status_code == 200
    TasteOff.objects.filter(id=tid).update(
        opened_at=timezone.now() - timedelta(days=8),
    )
    close_stale_tasteoffs()
    tasteoff = TasteOff.objects.get(id=tid)
    assert tasteoff.status == TasteOff.STATUS_CLOSED
    assert ClubEvent.objects.filter(club=club, kind=ClubEvent.KIND_TASTEOFF).exists()


def test_non_host_cannot_add_bottle(client, db, django_user_model):
    _hillside, riverside, riley, _sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    _enable("feature_tasteoffs")
    created = _auth(client, lee).post(
        reverse("api-club-tasteoffs", kwargs={"pk": club.id}),
        data={"title": "Thursday reds · 5 Nov"},
        content_type="application/json",
    )
    tid = created.json()["id"]
    blocked = _auth(client, riley).post(
        reverse("api-club-tasteoff-bottles", kwargs={"pk": club.id, "tid": tid}),
        data={"label": "Chenin Blanc 2022"},
        content_type="application/json",
    )
    assert blocked.status_code == 403


def test_owner_can_close_after_host(client, db, django_user_model):
    _hillside, riverside, riley, _sam, lee = _people(django_user_model)
    club = _club_with_member(client, riley, lee, riverside)
    _enable("feature_tasteoffs")
    created = _auth(client, lee).post(
        reverse("api-club-tasteoffs", kwargs={"pk": club.id}),
        data={"title": "Thursday reds · 5 Nov"},
        content_type="application/json",
    )
    tid = created.json()["id"]
    bottles = reverse("api-club-tasteoff-bottles", kwargs={"pk": club.id, "tid": tid})
    _auth(client, lee).post(bottles, data={"label": "A 2022"}, content_type="application/json")
    _auth(client, lee).post(bottles, data={"label": "B 2019"}, content_type="application/json")
    _auth(client, lee).post(
        reverse("api-club-tasteoff-open", kwargs={"pk": club.id, "tid": tid}),
        data={}, content_type="application/json",
    )
    closed = _auth(client, riley).post(
        reverse("api-club-tasteoff-close", kwargs={"pk": club.id, "tid": tid}),
        data={}, content_type="application/json",
    )
    assert closed.status_code == 200


def test_close_tasteoff_helper_idempotent(db, django_user_model):
    hillside, _riverside, riley, _sam, _lee = _people(django_user_model)
    club = Club.objects.create(name="Thursday reds", created_by=riley)
    ClubMembership.objects.create(
        club=club, user=riley, role="owner", household=hillside,
        accepted_sharing_version=1,
    )
    tasteoff = TasteOff.objects.create(
        club=club, host=riley, title="Flight", status=TasteOff.STATUS_SCORING,
        opened_at=timezone.now(),
    )
    TasteOffBottle.objects.create(tasteoff=tasteoff, label="Chenin", sort_order=0)
    first = close_tasteoff(tasteoff)
    second = close_tasteoff(first)
    assert first.status == TasteOff.STATUS_CLOSED
    assert second.status == TasteOff.STATUS_CLOSED
    assert ClubEvent.objects.filter(club=club, kind=ClubEvent.KIND_TASTEOFF).count() == 1
