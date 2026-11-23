"""P5: tasting notes (feature_tasting_notes) and the P4 carry-over fixes."""

import datetime
from datetime import timezone as tz
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.club import notify
from wine_cellar.apps.club.copy import comment_card_text, opened_text
from wine_cellar.apps.club.models import (
    Club,
    ClubEvent,
    ClubEventDelivery,
    ClubMembership,
    ClubNotice,
)
from wine_cellar.apps.club.notes import NOTE_MAX, can_offer, visible_note
from wine_cellar.apps.club.tasks import (
    deliver_club_event,
    publish_club_event,
    refresh_event_note,
)
from wine_cellar.apps.club.threads import _event_line
from wine_cellar.apps.scanner.models import (
    Household,
    HouseholdMembership,
    ServiceConfig,
    TelegramUser,
)
from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import Wine

SEND = "wine_cellar.apps.club.tasks._send"
POST = "wine_cellar.apps.club.threads.requests.post"
NOTE_DELAY = "wine_cellar.apps.club.tasks.refresh_event_note.apply_async"
FLAGS = ("feature_clubs", "feature_tasting_notes")


def utc(y, mo, d, h=0, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=tz.utc)


NOON = utc(2026, 6, 10, 12, 0)
NIGHT = utc(2026, 6, 10, 22, 30)


def _enable(*keys):
    for key in keys:
        ServiceConfig.objects.update_or_create(key=key, defaults={"value": "on"})


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


class _Mids:
    def __init__(self):
        self.n = 700

    def __call__(self, *args, **kwargs):
        self.n += 1
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"result": {"message_id": self.n}}
        return response


@pytest.fixture
def world(db):
    User = get_user_model()
    users, houses = {}, {}
    people = (("riley", "100"), ("sam", "200"), ("lee", "300"), ("pat", "400"))
    for name, chat in people:
        user = User.objects.create_user(username=name, password="pw")
        TelegramUser.objects.create(user=user, chat_id=chat, display_name=name.title())
        house = Household.objects.create(name=f"{name} house")
        HouseholdMembership.objects.create(user=user, household=house, role="owner")
        Storage.objects.create(name="Fridge", household=house, user=user, location="F")
        users[name], houses[name] = user, house
    club = Club.objects.create(name="Thursday reds", cellar_visibility="none")
    for user in users.values():
        ClubMembership.objects.create(
            club=club, user=user, role="member", accepted_sharing_version=1,
        )
    ServiceConfig.objects.update_or_create(key="bot_token", defaults={"value": "T"})
    return club, users, houses


def _item(users, houses, who="riley", deleted=True):
    wine = Wine.objects.create(
        name="Syrah", household=houses[who], user=users[who], wine_type="RE",
        vintage=2019,
    )
    item = StorageItem.objects.create(
        storage=Storage.objects.filter(household=houses[who]).first(), wine=wine,
        user=users[who],
    )
    if deleted:
        item.deleted = True
        item.save()
    return item


def _event(club, actor, item=None, wine="Syrah"):
    return ClubEvent.objects.create(
        club=club, actor=actor, kind=ClubEvent.KIND_OPENED,
        wine_name=wine, vintage=2019, vote="up",
        item_id=item.id if item else ClubEvent.objects.count() + 1,
    )


def _post_note(client, user, **body):
    return _auth(client, user).post(
        reverse("api-club-note"), data=body, content_type="application/json",
    )


def _offer(client, user, item):
    return _auth(client, user).get(reverse("api-club-note-offer"), {"item_id": item.id})


# -- offer ----------------------------------------------------------------
def test_offer_only_for_a_real_open_by_a_club_member(client, world, django_user_model):
    club, users, houses = world
    _enable(*FLAGS)
    item = _item(users, houses)
    assert _offer(client, users["riley"], item).json() == {
        "offer": True, "wine": "2019 Syrah",
    }
    # Undone / never drunk (the bottle is back in the cellar): no offer.
    back = _item(users, houses, deleted=False)
    assert _offer(client, users["riley"], back).json() == {"offer": False}
    # Someone in no club: nothing to post to, so no nag.
    loner = django_user_model.objects.create_user(username="kim", password="pw")
    house = Household.objects.create(name="kim house")
    HouseholdMembership.objects.create(user=loner, household=house, role="owner")
    Storage.objects.create(name="Fridge", household=house, user=loner, location="F")
    wine = Wine.objects.create(name="X", household=house, user=loner, wine_type="RE")
    out = StorageItem.objects.create(
        storage=Storage.objects.filter(household=house).first(), wine=wine, user=loner,
    )
    out.deleted = True
    out.save()
    assert _offer(client, loner, out).json() == {"offer": False}
    # Someone else's bottle is not theirs to note.
    assert _offer(client, users["sam"], item).json() == {"offer": False}


def test_offer_not_for_frozen_club_or_when_already_noted(client, world):
    club, users, houses = world
    _enable(*FLAGS)
    item = _item(users, houses)
    club.frozen = True
    club.save()
    assert _offer(client, users["riley"], item).json() == {"offer": False}
    club.frozen = False
    club.save()
    _event(club, users["riley"], item).__class__.objects.update(note="Plums")
    assert _offer(client, users["riley"], item).json() == {"offer": False}


def test_flags_off_means_404_and_no_offer(client, world):
    club, users, houses = world
    item = _item(users, houses)
    assert _offer(client, users["riley"], item).status_code == 404
    r = _post_note(client, users["riley"], item_id=item.id, text="x")
    assert r.status_code == 404
    _enable("feature_clubs")  # clubs on, notes off
    assert _offer(client, users["riley"], item).json()["error"] == "not_enabled"
    assert not can_offer(users["riley"], item.id)


# -- storing ----------------------------------------------------------------
def test_note_lands_on_every_club_event_of_the_bottle(
    client, world, django_capture_on_commit_callbacks,
):
    club, users, houses = world
    _enable(*FLAGS)
    other = Club.objects.create(name="Sunday whites", cellar_visibility="full")
    ClubMembership.objects.create(
        club=other, user=users["riley"], role="owner", accepted_sharing_version=1,
    )
    item = _item(users, houses)
    one, two = _event(club, users["riley"], item), _event(other, users["riley"], item)
    with patch(NOTE_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
        r = _post_note(
            client, users["riley"], item_id=item.id, text="  Plums,   pepper  ",
        )
    assert r.status_code == 200 and r.json()["count"] == 2
    for event in (one, two):
        event.refresh_from_db()
        assert event.note == "Plums, pepper" and event.note_added_at is not None
    assert {c.kwargs["args"][0] for c in delay.call_args_list} == {one.id, two.id}
    assert all(c.kwargs["countdown"] == 20 for c in delay.call_args_list)
    assert r.json()["clubs"] == ["Thursday reds", "Sunday whites"] or set(
        r.json()["clubs"]
    ) == {"Thursday reds", "Sunday whites"}


@pytest.mark.parametrize("level", ["none", "summary", "full"])
def test_note_shows_at_every_sharing_level(client, world, level):
    club, users, houses = world
    _enable(*FLAGS)
    club.cellar_visibility = level
    club.save()
    item = _item(users, houses)
    event = _event(club, users["riley"], item)
    with patch(NOTE_DELAY):
        _post_note(client, users["riley"], item_id=item.id, text="Lovely")
    feed = _auth(client, users["sam"]).get(
        reverse("api-club-feed", kwargs={"pk": club.id}),
    ).json()["events"]
    row = next(e for e in feed if e["id"] == event.id)
    assert row["note"] == "Lovely" and not row.get("mine")
    mine = _auth(client, users["riley"]).get(
        reverse("api-club-feed", kwargs={"pk": club.id}),
    ).json()["events"]
    assert next(e for e in mine if e["id"] == event.id)["mine"] is True


def test_note_validation_and_ownership(client, world):
    club, users, houses = world
    _enable(*FLAGS)
    item = _item(users, houses)
    event = _event(club, users["riley"], item)
    r = _post_note(client, users["riley"], item_id=item.id, text="x" * (NOTE_MAX + 1))
    assert r.status_code == 400 and r.json()["error"] == "too_long"
    # Not the actor: cannot touch it, even as a club member.
    r = _post_note(client, users["sam"], event_id=event.id, text="mine")
    assert r.status_code == 404
    # No event for the bottle yet (the open has not posted).
    fresh = _item(users, houses)
    r = _post_note(client, users["riley"], item_id=fresh.id, text="x")
    assert r.status_code == 409
    # Not in the club: 404.
    out = _item(users, houses, who="sam")
    r = _post_note(client, users["riley"], item_id=out.id, text="x")
    assert r.status_code == 404
    event.refresh_from_db()
    assert event.note == ""


def test_edit_and_remove_own_note_from_feed_detail(
    client, world, django_capture_on_commit_callbacks,
):
    club, users, houses = world
    _enable(*FLAGS)
    item = _item(users, houses)
    event = _event(club, users["riley"], item)
    with patch(NOTE_DELAY) as delay, django_capture_on_commit_callbacks(execute=True):
        _post_note(client, users["riley"], event_id=event.id, text="First")
        _post_note(client, users["riley"], event_id=event.id, text="Second")
        r = _post_note(client, users["riley"], event_id=event.id, text="")
    assert r.json()["removed"] is True
    event.refresh_from_db()
    assert event.note == "" and event.note_added_at is None
    assert delay.call_count == 1  # debounced: one edit pass per event


def test_retract_hides_and_clears_the_note_and_reopen_starts_clean(world):
    club, users, houses = world
    _enable(*FLAGS)
    item = _item(users, houses)
    event = _event(club, users["riley"], item)
    event.note = "Plums"
    event.save()
    assert visible_note(event) == "Plums"
    with patch("wine_cellar.apps.club.tasks.deliver_club_event.delay"):
        publish_club_event(item.id, users["riley"].id, "retracted")
        event.refresh_from_db()
        assert event.retracted_at is not None and event.note == ""
        assert visible_note(event) == ""
        event.note = "stale"  # even a leftover value never shows
        assert visible_note(event) == ""
        publish_club_event(item.id, users["riley"].id, "opened")  # restore
    event.refresh_from_db()
    assert event.retracted_at is None and event.note == ""


def test_flag_off_hides_stored_notes_everywhere(client, world):
    club, users, houses = world
    _enable("feature_clubs")
    item = _item(users, houses)
    event = _event(club, users["riley"], item)
    event.note = "Plums"
    event.save()
    feed = _auth(client, users["sam"]).get(
        reverse("api-club-feed", kwargs={"pk": club.id}),
    ).json()["events"]
    assert "note" not in feed[0] and "mine" not in feed[0]
    assert "Plums" not in _event_line(event, "en")
    with patch(SEND) as send:
        assert refresh_event_note(event.id) == 0
    assert send.call_count == 0


# -- card, thread, summary --------------------------------------------------
def test_card_text_quotes_and_escapes_the_note():
    text = opened_text("en", "Riley", 2019, "Syrah", "up", note="Plums <b> & pepper")
    lines = text.split("\n")
    assert lines[0].startswith("\U0001F377")
    assert lines[1] == "“Plums &lt;b&gt; &amp; pepper”"
    assert "“" not in opened_text("en", "Riley", 2019, "Syrah", "up")
    card = comment_card_text(
        "af", "Riley", 2019, "Syrah", "up", [("Sam", "Nice")], note="Plums",
    )
    assert card.split("\n")[1] == "“Plums”" and "Sam: Nice" in card


def test_note_edits_existing_cards_in_place_silently(world):
    club, users, houses = world
    _enable(*FLAGS)
    item = _item(users, houses)
    event = _event(club, users["riley"], item)
    with patch(SEND, return_value=11):
        deliver_club_event(event.id)
    assert ClubEventDelivery.objects.filter(event=event).count() == 3
    event.note = "Plums & pepper"
    event.save()
    with patch(SEND, return_value=11) as send:
        assert refresh_event_note(event.id) == 3
    assert {c.args[1] for c in send.call_args_list} == {"200", "300", "400"}
    assert all(c.kwargs["message_id"] == 11 for c in send.call_args_list)
    assert all("Plums &amp; pepper" in c.args[2] for c in send.call_args_list)
    # A member with no card yet (muted the feed) gets nothing new.
    ClubEventDelivery.objects.filter(chat_id="400").delete()
    with patch(SEND, return_value=11) as send:
        assert refresh_event_note(event.id) == 2
    # Removing the note edits the quote away.
    event.note = ""
    event.save()
    with patch(SEND, return_value=11) as send:
        refresh_event_note(event.id)
    assert all("“" not in c.args[2] for c in send.call_args_list)
    # A retracted event is left alone.
    event.retracted_at = NOON
    event.save()
    with patch(SEND, return_value=11) as send:
        assert refresh_event_note(event.id) == 0
    assert send.call_count == 0


def test_new_cards_carry_the_note_and_summaries_quote_it(world):
    club, users, houses = world
    _enable(*FLAGS, "feature_club_notify")
    event = _event(club, users["riley"], _item(users, houses))
    event.note = "Plums & pepper, long finish"
    event.created = NOON  # the tests below patch the clock to June
    event.save()
    with patch(SEND, return_value=11) as send, patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=NIGHT,
    ):
        deliver_club_event(event.id)  # quiet hours: held for the summary
    assert send.call_count == 0
    with patch(POST, side_effect=_Mids()) as post:
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    assert "Plums &amp; pepper" in post.call_args_list[0].kwargs["json"]["text"]
    with patch(SEND, return_value=12) as send:
        deliver_club_event(event.id)  # already summarised: no second push
    assert send.call_count == 0
    event.retracted_at = NOON
    assert _event_line(event, "en").count("“") == 0


def test_long_notes_are_clipped_in_pings():
    event = ClubEvent(
        kind=ClubEvent.KIND_OPENED, wine_name="Syrah", vintage=2019, vote="",
        note="x" * 280,
    )
    with patch("wine_cellar.apps.club.notes.notes_enabled", return_value=True):
        line = _event_line(event, "en")
    assert len(line) < 200 and "…" in line


# -- typed questions ---------------------------------------------------------
def test_ask_sees_notes_only_with_the_flag(client, world):
    club, users, houses = world
    _enable("feature_clubs")
    event = _event(club, users["riley"], _item(users, houses))
    event.note = "Plums and pepper"
    event.save()
    ClubMembership.objects.exclude(user=users["riley"]).delete()
    url = reverse("api-clubs-ask")

    def ask(kind="feed"):
        return _auth(client, users["riley"]).post(
            url, data={"kind": kind, "wine_text": "pepper"},
            content_type="application/json",
        ).json()

    assert ask()["events"] == []  # note text is not searchable while off
    _enable("feature_tasting_notes", "feature_club_comments")
    body = ask()
    assert body["events"][0]["tasting_note"] == "Plums and pepper"
    assert body["events"][0]["wine_name"] == "Syrah"
    assert ask("comments")["tasting_notes"][0]["note"] == "Plums and pepper"
    event.retracted_at = NOON
    event.save()
    assert ask("comments")["tasting_notes"] == []


# -- P4 carry-over fixes ------------------------------------------------------
def test_summary_score_button_is_english_in_every_locale(world):
    from wine_cellar.apps.club.models import TasteOff
    from wine_cellar.apps.club.tasks import deliver_tasteoff_open
    from wine_cellar.apps.user.models import UserSettings

    club, users, houses = world
    _enable("feature_clubs", "feature_club_notify", "feature_tasteoffs")
    UserSettings.objects.update_or_create(
        user=users["sam"], defaults={"language": "af"},
    )
    to = TasteOff.objects.create(
        club=club, host=users["riley"], title="Blind Syrah",
        status=TasteOff.STATUS_SCORING,
    )
    with patch(SEND, return_value=11), patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=NIGHT,
    ):
        deliver_tasteoff_open(to.id)
    with patch(POST, side_effect=_Mids()) as post:
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    sam = next(
        c for c in post.call_args_list if c.kwargs["json"]["chat_id"] == "200"
    )
    rows = sam.kwargs["json"]["reply_markup"]["inline_keyboard"]
    labels = [b["text"] for r in rows for b in r]
    assert any(label.startswith("Score") for label in labels)
    assert not any("Punt" in label for label in labels)


def test_summary_reply_is_recognised_with_threads_off(client, world):
    club, users, houses = world
    _enable("feature_clubs", "feature_club_notify")  # threads OFF
    event = _event(club, users["riley"])
    notify.hold_notice(
        users["sam"].id, club, event, "card",
        notify.Decision(notify.HOLD, utc(2026, 6, 11, 5, 0)), "card:sw",
    )
    with patch(POST, side_effect=_Mids()):
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 0))
    row = ClubEventDelivery.objects.get(kind="summary", chat_id="200")
    url = reverse("api-club-thread-resolve")
    q = {"chat_id": "200", "message_id": row.message_id}
    r = _auth(client, users["sam"]).get(url, q)
    assert r.status_code == 200 and r.json() == {"summary": True}
    # Member and chat auth stay: someone else's chat does not resolve it.
    assert _auth(client, users["lee"]).get(url, q).status_code == 404
    # An event message still needs the threads flag.
    ClubEventDelivery.objects.create(event=event, chat_id="200", message_id=999)
    r = _auth(client, users["sam"]).get(url, {"chat_id": "200", "message_id": 999})
    assert r.status_code == 404 and r.json()["error"] == "not_enabled"


def _deliver_at(event, now, mid=11):
    with patch(SEND, return_value=mid) as send, patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=now,
    ):
        deliver_club_event(event.id)
    return send


def test_restored_card_after_a_dropped_summary_line_is_new(world):
    club, users, houses = world
    _enable(*FLAGS, "feature_club_notify")
    event = _event(club, users["riley"], _item(users, houses))
    _deliver_at(event, NIGHT)  # held for sam, lee, pat
    assert ClubNotice.objects.filter(kind="card").count() == 3
    event.retracted_at = NIGHT
    event.save()
    with patch(POST, side_effect=_Mids()):
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))  # line dropped
    assert ClubNotice.objects.filter(sent_at__isnull=True).count() == 0
    # Restored (the open path re-stamps created): a fresh card, decided again.
    event.retracted_at = None
    event.created = utc(2026, 6, 11, 12, 0)
    event.save()
    send = _deliver_at(event, utc(2026, 6, 11, 12, 5))
    assert {c.args[1] for c in send.call_args_list} == {"200", "300", "400"}
    # ...and in quiet hours it is held again, not pushed.
    event.created = utc(2026, 6, 12, 22, 40)
    event.save()
    ClubEventDelivery.objects.filter(event=event).delete()
    send = _deliver_at(event, utc(2026, 6, 12, 22, 45))
    assert send.call_count == 0
    assert ClubNotice.objects.filter(kind="card", sent_at__isnull=True).count() == 3


def test_summarised_card_is_not_pushed_again_on_a_plain_rerun(world):
    club, users, houses = world
    _enable(*FLAGS, "feature_club_notify")
    event = _event(club, users["riley"], _item(users, houses))
    event.created = NOON  # the clock is patched to June below
    event.save()
    _deliver_at(event, NIGHT)
    with patch(POST, side_effect=_Mids()):
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    assert _deliver_at(event, utc(2026, 6, 11, 9, 0)).call_count == 0


def test_second_chat_goes_through_decide_not_pushed_in_quiet_hours(world):
    club, users, houses = world
    _enable(*FLAGS, "feature_club_notify")
    TelegramUser.objects.create(user=users["sam"], chat_id="201", display_name="Sam2")
    event = _event(club, users["riley"], _item(users, houses))
    _deliver_at(event, NOON)  # both of sam's chats get it
    ClubEventDelivery.objects.filter(chat_id="201").delete()  # a new chat later
    event.vote = "down"
    event.save()
    send = _deliver_at(event, NIGHT)
    sent_to = {c.args[1] for c in send.call_args_list}
    assert "200" in sent_to  # existing copy: silent edit
    assert "201" not in sent_to  # no copy: held, not pushed at night
    assert ClubNotice.objects.filter(
        recipient=users["sam"], kind="card", sent_at__isnull=True,
    ).exists()
    # Awake again: that chat gets a normal card.
    send = _deliver_at(event, utc(2026, 6, 11, 9, 0))
    assert "201" not in {c.args[1] for c in send.call_args_list}  # held row waits


# -- Telegram HTML escaping: exactly once ------------------------------------
def test_user_strings_are_escaped_exactly_once(world):
    from wine_cellar.apps.club.models import ClubComment
    from wine_cellar.apps.club.tasks import (
        club_widen_notice,
        notify_kicked,
        refresh_event_comment_card,
    )

    club, users, houses = world
    _enable(*FLAGS, "feature_club_comments")
    nasty = "&<b>"
    esc = "&amp;&lt;b&gt;"
    club.name = f"C{nasty}"
    club.save()
    TelegramUser.objects.filter(user=users["riley"]).update(display_name=f"A{nasty}")
    item = _item(users, houses)
    event = _event(club, users["riley"], item, wine=f"W{nasty}")
    event.note = "Plums"
    event.save()
    ClubComment.objects.create(event=event, user=users["sam"], text="Hi")

    def texts(send):
        return [c.args[2] for c in send.call_args_list]

    with patch(SEND, return_value=11) as send:
        deliver_club_event(event.id)
    assert send.call_count
    for text in texts(send):
        assert f"A{esc}" in text and f"W{esc}" in text
        assert "&amp;amp;" not in text and "<b>" not in text
    with patch(SEND, return_value=11) as send:
        refresh_event_note(event.id)
        refresh_event_comment_card(event.id)
    assert send.call_count
    for text in texts(send):
        assert f"A{esc}" in text and f"W{esc}" in text
        assert "&amp;amp;" not in text and "<b>" not in text
    with patch(SEND, return_value=11) as send:
        club_widen_notice(club.id)
        notify_kicked(club.name, ["100"])
    assert send.call_count
    for text in texts(send):
        assert f"C{esc}" in text
        assert "&amp;amp;" not in text and "<b>" not in text


def test_global_clubs_get_no_note_and_saves_are_rate_limited(
    client, world, django_capture_on_commit_callbacks,
):
    from wine_cellar.apps.club.notes import SAVE_MAX

    club, users, houses = world
    _enable(*FLAGS)
    glob = Club.objects.create(
        name="Everyone", kind=Club.KIND_GLOBAL, cellar_visibility="none",
    )
    ClubMembership.objects.create(
        club=glob, user=users["riley"], role="member", accepted_sharing_version=1,
    )
    item = _item(users, houses)
    one, g = _event(club, users["riley"], item), _event(glob, users["riley"], item)
    with patch(NOTE_DELAY), django_capture_on_commit_callbacks(execute=True):
        r = _post_note(client, users["riley"], item_id=item.id, text="Plums")
    assert r.json()["clubs"] == ["Thursday reds"]
    one.refresh_from_db()
    g.refresh_from_db()
    assert one.note == "Plums" and g.note == ""
    r = _post_note(client, users["riley"], event_id=g.id, text="Nope")
    assert r.status_code == 404
    for _ in range(SAVE_MAX):
        r = _post_note(client, users["riley"], item_id=item.id, text="again")
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
