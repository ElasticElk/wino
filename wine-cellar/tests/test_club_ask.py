"""Typed club chat API: flags, privacy, fuzzy names, no N+1."""

from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from wine_cellar.apps.club.models import (
    Club,
    ClubComment,
    ClubEvent,
    ClubMembership,
    TasteOff,
    TasteOffBottle,
    TasteOffScore,
)
from wine_cellar.apps.club.peek import FORBIDDEN_PEEK_KEYS, collect_keys
from wine_cellar.apps.scanner.models import (
    Household,
    HouseholdMembership,
    ServiceConfig,
    TelegramUser,
)
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import (
    CellarAchievement,
    Grape,
    Wine,
    WineList,
    WineListItem,
)


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def _flag(key, value="on"):
    ServiceConfig.objects.update_or_create(key=key, defaults={"value": value})


def _people(django_user_model):
    hillside = Household.objects.create(name="Hillside")
    riverside = Household.objects.create(name="Riverside")
    orchard = Household.objects.create(name="Orchard")
    riley = django_user_model.objects.create_user(username="riley", password="pw")
    sam = django_user_model.objects.create_user(username="sam", password="pw")
    lee = django_user_model.objects.create_user(username="lee", password="pw")
    pat = django_user_model.objects.create_user(username="pat", password="pw")
    HouseholdMembership.objects.create(user=riley, household=hillside, role="owner")
    HouseholdMembership.objects.create(user=sam, household=hillside, role="member")
    HouseholdMembership.objects.create(user=lee, household=riverside, role="owner")
    HouseholdMembership.objects.create(user=pat, household=orchard, role="owner")
    TelegramUser.objects.create(user=riley, chat_id="100", display_name="Riley")
    TelegramUser.objects.create(user=sam, chat_id="200", display_name="Sam")
    TelegramUser.objects.create(user=lee, chat_id="300", display_name="Lee")
    TelegramUser.objects.create(user=pat, chat_id="400", display_name="Pat")
    Storage.objects.create(name="Fridge", household=hillside, user=riley, location="F")
    Storage.objects.create(name="Fridge", household=riverside, user=lee, location="F")
    Storage.objects.create(name="Fridge", household=orchard, user=pat, location="F")
    return hillside, riverside, orchard, riley, sam, lee, pat


def _fridge(household):
    return Storage.objects.get(household=household, name="Fridge")


def _add_wine(household, user, name, *, vintage=2019, grape="Merlot", wine_type="RE",
              country="ZA", price=12, comment="secret", rating=9, barcode="123"):
    wine = Wine.objects.create(
        name=name, household=household, user=user, wine_type=wine_type,
        country=country, vintage=vintage, price=price, comment=comment,
        rating=rating, critic_score=90, vivino_rating=4.2, market_price=20,
        barcode=barcode,
    )
    if grape:
        wine.grapes.add(Grape.objects.create(name=grape, user=user))
    StorageItem.objects.create(
        storage=_fridge(household), wine=wine, user=user, price=9, row=2,
    )
    return wine


def _club(owner, household, name="Thursday reds", **fields):
    _flag("feature_clubs")
    vis = fields.pop("cellar_visibility", "full")
    badges = fields.pop("badges_visible", True)
    club = Club.objects.create(
        name=name, created_by=owner, cellar_visibility=vis, badges_visible=badges,
        **fields,
    )
    ClubMembership.objects.create(
        club=club, user=owner, role="owner", household=household,
        accepted_sharing_version=club.sharing_version, is_last_used=True,
    )
    return club


def _join(club, user, household, *, accepted=None, role="member"):
    return ClubMembership.objects.create(
        club=club, user=user, role=role, household=household,
        accepted_sharing_version=(
            club.sharing_version if accepted is None else accepted
        ),
    )


def _ask(client, user, **body):
    return _auth(client, user).post(
        reverse("api-clubs-ask"), data=body, content_type="application/json",
    )


def test_flag_off_is_not_enabled(client, db, django_user_model):
    _people(django_user_model)
    riley = django_user_model.objects.get(username="riley")
    response = _ask(client, riley, kind="members")
    assert response.status_code == 404
    assert response.json()["error"] == "not_enabled"


def test_no_clubs(client, db, django_user_model):
    _people(django_user_model)
    _flag("feature_clubs")
    riley = django_user_model.objects.get(username="riley")
    response = _ask(client, riley, kind="members")
    assert response.status_code == 200
    assert response.json()["status"] == "no_clubs"


def test_members_and_my_clubs(client, db, django_user_model):
    hillside, riverside, _orchard, riley, sam, lee, _pat = _people(django_user_model)
    club = _club(riley, hillside)
    _join(club, lee, riverside)
    body = _ask(client, riley, kind="members").json()
    assert body["status"] == "ok"
    names = {m["display_name"] for m in body["members"]}
    assert names == {"Riley", "Lee"}
    assert "Pat" not in names
    clubs = _ask(client, riley, kind="my_clubs").json()
    assert clubs["clubs"][0]["name"] == "Thursday reds"


def test_non_member_cannot_address_a_club(client, db, django_user_model):
    hillside, _riverside, orchard, riley, _sam, _lee, pat = _people(django_user_model)
    club = _club(riley, hillside)
    _club(pat, orchard, name="Orchard nights")
    response = _ask(client, pat, kind="members", club_id=club.id)
    assert response.status_code == 200
    assert response.json()["status"] == "not_found"
    assert "Thursday reds" not in str(response.json())


def test_unknown_person_does_not_leak_outsiders(client, db, django_user_model):
    hillside, riverside, orchard, riley, _sam, lee, pat = _people(django_user_model)
    club = _club(riley, hillside)
    _join(club, lee, riverside)
    _add_wine(orchard, pat, "Secret Shiraz", grape="Syrah")
    body = _ask(client, riley, kind="cellar", member_name="Pat").json()
    assert body["status"] == "not_found"
    labels = {c["label"] for c in body["choices"]}
    assert labels == {"Riley", "Lee"}
    assert "Pat" not in labels
    assert "Secret" not in str(body)


def test_full_cellar_hides_forbidden_keys(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(riley, hillside, cellar_visibility="full")
    _join(club, sam, hillside)
    _join(club, lee, riverside)
    _add_wine(hillside, sam, "Hillside Merlot", grape="Merlot", vintage=2018)
    body = _ask(client, lee, kind="cellar", member_name="Sam").json()
    assert body["status"] == "ok"
    assert body["visibility"] == "full"
    assert body["count"] == 1
    keys = collect_keys(body)
    assert not (keys & FORBIDDEN_PEEK_KEYS), keys & FORBIDDEN_PEEK_KEYS
    assert body["wines"][0]["name"] == "Hillside Merlot"
    assert "secret" not in str(body)
    assert "12" not in str(body.get("wines"))


def test_summary_answers_grape_counts_not_vintages(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(riley, hillside, cellar_visibility="summary")
    _join(club, sam, hillside)
    _join(club, lee, riverside)
    _add_wine(hillside, sam, "Hillside Merlot", grape="Merlot", vintage=2015)
    grape = _ask(client, lee, kind="cellar", member_name="Sam", grape="Merlot").json()
    assert grape["status"] == "ok"
    assert grape["visibility"] == "summary"
    assert grape["count"] == 1
    assert "wines" not in grape
    vintage = _ask(
        client, lee, kind="who_has", wine_text="Barolo", vintage=2015,
    ).json()
    assert vintage["status"] == "ok"
    assert vintage["matches"] == []
    assert vintage["skipped_summary"] >= 1


def test_visibility_none_hides_cellar(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(riley, hillside, cellar_visibility="none")
    _join(club, sam, hillside)
    _join(club, lee, riverside)
    _add_wine(hillside, sam, "Hillside Merlot")
    body = _ask(client, lee, kind="cellar", member_name="Sam").json()
    assert body["status"] == "not_visible"
    assert "Merlot" not in str(body)


def test_unacked_widen_keeps_previous_level(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(
        riley, hillside, cellar_visibility="full",
        sharing_version=2, previous_cellar_visibility="summary",
    )
    _join(club, sam, hillside, accepted=1)
    _join(club, lee, riverside, accepted=1)
    _add_wine(hillside, sam, "Hillside Barolo", grape="Nebbiolo", vintage=2015)
    body = _ask(client, lee, kind="cellar", member_name="Sam").json()
    assert body["status"] == "ok"
    assert body["visibility"] == "summary"
    assert "wines" not in body


def test_global_club_never_exposes_cellars(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    _flag("feature_clubs")
    _flag("feature_global_clubs")
    club = Club.objects.create(
        name="Cape notes", created_by=riley, kind=Club.KIND_GLOBAL,
        cellar_visibility="none", badges_visible=False,
    )
    ClubMembership.objects.create(
        club=club, user=riley, role="owner", household=hillside,
        accepted_sharing_version=1, is_last_used=True,
    )
    _join(club, sam, hillside)
    _join(club, lee, riverside)
    _add_wine(hillside, sam, "Hillside Merlot")
    body = _ask(client, lee, kind="cellar", member_name="Sam").json()
    assert body["status"] == "not_visible"
    assert body["reason"] == "global"
    assert "Merlot" not in str(body)


def test_global_flag_off_hides_global_clubs(client, db, django_user_model):
    hillside, _r, _o, riley, _s, _l, _p = _people(django_user_model)
    _flag("feature_clubs")
    club = Club.objects.create(
        name="Cape notes", created_by=riley, kind=Club.KIND_GLOBAL,
        cellar_visibility="none", badges_visible=False,
    )
    ClubMembership.objects.create(
        club=club, user=riley, role="owner", household=hillside,
        accepted_sharing_version=1, is_last_used=True,
    )
    body = _ask(client, riley, kind="my_clubs").json()
    assert body["status"] == "no_clubs"


def test_who_has_full_names_a_bottle(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(riley, hillside)
    _join(club, sam, hillside)
    _join(club, lee, riverside)
    _add_wine(hillside, sam, "Barolo", grape="Nebbiolo", vintage=2015)
    _add_wine(riverside, lee, "Barolo", grape="Nebbiolo", vintage=2012, barcode="999")
    body = _ask(client, riley, kind="who_has", wine_text="Barolo", vintage=2015).json()
    assert body["status"] == "ok"
    names = {m["display_name"] for m in body["matches"]}
    assert any("Sam" in n or "Riley" in n for n in names)
    assert body["matches"][0]["wines"][0]["vintage"] == 2015
    assert all(m["wines"][0]["vintage"] == 2015 for m in body["matches"])


def test_oldest_bottle(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(riley, hillside)
    _join(club, lee, riverside)
    _add_wine(hillside, sam, "Hillside Merlot", vintage=2018)
    _add_wine(riverside, lee, "Riverside Chenin", vintage=2009, grape="Chenin Blanc")
    body = _ask(client, riley, kind="oldest").json()
    assert body["vintage"] == 2009
    assert body["wines"][0]["name"] == "Riverside Chenin"


def test_feed_and_who_drinks(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(riley, hillside)
    _join(club, lee, riverside)
    ClubEvent.objects.create(
        club=club, actor=lee, kind=ClubEvent.KIND_OPENED,
        wine_name="Kanonkop", vintage=2018, producer="Kanonkop", vote="up",
    )
    ClubEvent.objects.create(
        club=club, actor=lee, kind=ClubEvent.KIND_OPENED,
        wine_name="Chenin", vintage=2020, vote="meh",
    )
    ClubEvent.objects.create(
        club=club, actor=riley, kind=ClubEvent.KIND_OPENED,
        wine_name="Pinotage", vintage=2019, vote="up",
    )
    week = _ask(client, riley, kind="feed", member_name="Lee", period="week").json()
    assert {e["wine_name"] for e in week["events"]} == {"Kanonkop", "Chenin"}
    drinks = _ask(client, riley, kind="who_drinks").json()
    assert drinks["ranking"][0]["display_name"] == "Lee"
    assert drinks["ranking"][0]["count"] == 2
    top = _ask(client, riley, kind="top_rated", wine_text="Kanonkop").json()
    assert top["wines"][0]["name"] == "Kanonkop"
    avg = _ask(client, riley, kind="avg_rating", member_name="Lee").json()
    assert avg["vote_count"] == 2


def test_tasteoff_flag_and_winner(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(riley, hillside)
    _join(club, lee, riverside)
    skipped = _ask(client, riley, kind="tasteoff", when="next").json()
    assert skipped["status"] == "fallback"
    _flag("feature_tasteoffs")
    tasting = TasteOff.objects.create(
        club=club, host=riley, title="Friday reds",
        status=TasteOff.STATUS_CLOSED, closed_at=timezone.now(),
    )
    first = TasteOffBottle.objects.create(tasteoff=tasting, label="Bottle A")
    second = TasteOffBottle.objects.create(tasteoff=tasting, label="Bottle B")
    TasteOffScore.objects.create(tasteoff=tasting, bottle=first, user=riley, score=5)
    TasteOffScore.objects.create(tasteoff=tasting, bottle=first, user=lee, score=4)
    TasteOffScore.objects.create(tasteoff=tasting, bottle=second, user=riley, score=3)
    won = _ask(client, riley, kind="tasteoff", when="last").json()
    assert won["tasteoff"]["winner"]["labels"] == ["Bottle A"]
    friday = _ask(client, riley, kind="tasteoff", when="friday").json()
    assert friday["tasteoff"]["title"] == "Friday reds"
    upcoming = TasteOff.objects.create(
        club=club, host=lee, title="Next flight", status=TasteOff.STATUS_SETUP,
    )
    nxt = _ask(client, riley, kind="tasteoff", when="next").json()
    assert nxt["tasteoff"]["title"] == "Next flight"
    assert nxt["tasteoff"]["status"] == "setup"


def test_badges_respect_policy(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(riley, hillside, badges_visible=False)
    _join(club, sam, hillside)
    _join(club, lee, riverside)
    CellarAchievement.objects.create(
        household=hillside, user=sam, slug="first-red", subject="",
        emoji="🍷", title="First red",
    )
    hidden = _ask(client, lee, kind="badges", member_name="Sam").json()
    assert hidden["status"] == "not_visible"
    club.badges_visible = True
    club.save(update_fields=["badges_visible"])
    shown = _ask(client, lee, kind="badges", member_name="Sam").json()
    assert shown["status"] == "ok"
    assert shown["achievements"][0]["title"] == "First red"
    progress = _ask(client, lee, kind="badge_progress").json()
    assert progress["status"] == "ok"
    assert progress["closest"]


def test_personal_list_is_private_club_list_is_shared(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(riley, hillside)
    _join(club, lee, riverside)
    private = _ask(client, riley, kind="list", member_name="Lee").json()
    assert private["status"] == "fallback"
    _flag("feature_lists")
    personal = WineList.objects.create(
        owner=lee, kind=WineList.KIND_PERSONAL, name="To drink",
    )
    WineListItem.objects.create(list=personal, name="Secret Chenin", added_by=lee)
    hidden = _ask(client, riley, kind="list", member_name="Lee").json()
    assert hidden["status"] == "private"
    assert "Secret Chenin" not in str(hidden)
    club_list = WineList.objects.create(
        owner=None, kind=WineList.KIND_CLUB, club=club, name="Thursday reds",
    )
    WineListItem.objects.create(
        list=club_list, name="Shared Pinotage", added_by=lee,
    )
    shared = _ask(client, riley, kind="list").json()
    assert shared["items"][0]["name"] == "Shared Pinotage"
    own = _ask(client, lee, kind="list", member_name="Lee").json()
    assert own["scope"] == "personal"
    assert own["items"][0]["name"] == "Secret Chenin"


def test_comments_flag_and_wine_filter(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    club = _club(riley, hillside)
    _join(club, lee, riverside)
    event = ClubEvent.objects.create(
        club=club, actor=lee, kind=ClubEvent.KIND_OPENED,
        wine_name="Pinotage", vintage=2019,
    )
    ClubComment.objects.create(event=event, user=lee, text="Smoky and warm.")
    skipped = _ask(client, riley, kind="comments", wine_text="Pinotage").json()
    assert skipped["status"] == "fallback"
    _flag("feature_club_comments")
    body = _ask(client, riley, kind="comments", wine_text="Pinotage").json()
    assert body["comments"][0]["text"] == "Smoky and warm."


def test_fuzzy_member_and_ambiguous_picker(client, db, django_user_model):
    hillside, riverside, orchard, riley, sam, lee, pat = _people(django_user_model)
    TelegramUser.objects.filter(user=pat).update(display_name="Samantha")
    club = _club(riley, hillside)
    _join(club, sam, hillside)
    _join(club, lee, riverside)
    _join(club, pat, orchard)
    one = _ask(client, riley, kind="members", member_name="Lee").json()
    assert one["status"] == "ok"
    amb = _ask(client, riley, kind="cellar", member_name="Sam").json()
    assert amb["status"] == "need_member"
    labels = {c["label"] for c in amb["choices"]}
    assert "Sam" in labels
    assert "Samantha" in labels


def test_last_used_club_then_picker(client, db, django_user_model):
    hillside, riverside, _o, riley, sam, lee, _p = _people(django_user_model)
    first = _club(riley, hillside, name="Thursday reds")
    second = Club.objects.create(
        name="Weekend whites", created_by=riley, cellar_visibility="full",
    )
    ClubMembership.objects.create(
        club=second, user=riley, role="owner", household=hillside,
        accepted_sharing_version=1, is_last_used=False,
    )
    _join(first, lee, riverside)
    body = _ask(client, riley, kind="members").json()
    assert body["club"]["name"] == "Thursday reds"
    ClubMembership.objects.filter(user=riley).update(is_last_used=False)
    picker = _ask(client, riley, kind="members").json()
    assert picker["status"] == "need_club"
    labels = {c["label"] for c in picker["choices"]}
    assert labels == {"Thursday reds", "Weekend whites"}


def test_who_has_stays_bounded_with_several_members(client, db, django_user_model):
    hillside, riverside, orchard, riley, sam, lee, pat = _people(django_user_model)
    club = _club(riley, hillside)
    _join(club, lee, riverside)
    _join(club, pat, orchard)
    _add_wine(hillside, sam, "Merlot One", grape="Merlot")
    _add_wine(riverside, lee, "Merlot Two", grape="Merlot", barcode="b2")
    _add_wine(orchard, pat, "Merlot Three", grape="Merlot", barcode="b3")
    from django.db import connection

    with CaptureQueriesContext(connection) as ctx:
        body = _ask(client, riley, kind="who_has", grape="Merlot").json()
    assert body["status"] == "ok"
    assert len(body["matches"]) == 3
    assert len(ctx.captured_queries) < 30


def test_unknown_kind_falls_back(client, db, django_user_model):
    hillside, _r, _o, riley, _s, _l, _p = _people(django_user_model)
    _club(riley, hillside)
    body = _ask(client, riley, kind="invite").json()
    assert body["status"] == "fallback"
