"""P3: Ask the club (feature_club_relay): ask events, delayed delivery with
Undo, threaded replies, rate limit, dedupe, consumer guards, flag-off paths."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from wine_cellar.apps.club.models import (
    Club,
    ClubComment,
    ClubEvent,
    ClubEventDelivery,
    ClubMembership,
    ClubNotice,
)
from wine_cellar.apps.club.reactions import PALETTE
from wine_cellar.apps.club.relay import deliver_ask
from wine_cellar.apps.club.tasks import (
    deliver_club_event,
    refresh_event_comment_card,
    refresh_event_reactions,
)
from wine_cellar.apps.club.threads import BATCH_WINDOW, _markup, flush_due_notices
from wine_cellar.apps.scanner.models import ServiceConfig, TelegramUser

POST = "wine_cellar.apps.club.threads.requests.post"
EDIT = "requests.post"
SCHEDULE = "wine_cellar.apps.api.social_views.schedule_delivery"
RX_SCHEDULE = "wine_cellar.apps.club.tasks.refresh_event_reactions.apply_async"
FLAGS = (
    "feature_clubs", "feature_club_comments", "feature_club_threads",
    "feature_club_relay",
)


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def _enable(*keys):
    for key in keys:
        ServiceConfig.objects.update_or_create(key=key, defaults={"value": "on"})


class _Mids:
    """Telegram sendMessage replies with a fresh message id each time."""

    def __init__(self):
        self.n = 500

    def __call__(self, *args, **kwargs):
        self.n += 1
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"result": {"message_id": self.n}}
        return response


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def club(db, django_user_model):
    users = {}
    people = (("riley", "100"), ("sam", "200"), ("lee", "300"), ("pat", "400"))
    for name, chat in people:
        user = django_user_model.objects.create_user(username=name, password="pw")
        TelegramUser.objects.create(user=user, chat_id=chat, display_name=name.title())
        users[name] = user
    club = Club.objects.create(name="Thursday reds", cellar_visibility="none")
    for user in users.values():
        ClubMembership.objects.create(
            club=club, user=user, role="member", accepted_sharing_version=1,
        )
    _enable(*FLAGS)
    ServiceConfig.objects.update_or_create(key="bot_token", defaults={"value": "T"})
    return club, users


def _ask(client, user, club, text="Anyone tried the new Chenin?"):
    url = reverse("api-club-ask", kwargs={"pk": club.id})
    with patch(SCHEDULE):
        return _auth(client, user).post(
            url, data={"text": text}, content_type="application/json",
        )


def _undo(client, user, event_id):
    url = reverse("api-club-ask-undo", kwargs={"event_id": event_id})
    return _auth(client, user).post(url, data={}, content_type="application/json")


def _comment(client, user, club, event, text):
    url = reverse(
        "api-club-event-comments", kwargs={"pk": club.id, "event_id": event.id},
    )
    response = _auth(client, user).post(
        url, data={"text": text}, content_type="application/json",
    )
    if response.status_code == 201:
        # The view queues pings on commit; the test DB never commits.
        from wine_cellar.apps.club.threads import enqueue_comment_notices

        enqueue_comment_notices(response.json()["id"])
    return response


# -- carry-over fixes ---------------------------------------------------------
def test_reaction_notice_is_silent_but_comment_ping_is_not(client, club):
    club_, users = club
    _enable("feature_club_reactions")
    event = ClubEvent.objects.create(
        club=club_, actor=users["riley"], kind=ClubEvent.KIND_OPENED,
        wine_name="Merlot", vintage=2019,
    )
    react = reverse("api-club-event-react", kwargs={"event_id": event.id})
    with patch(RX_SCHEDULE):
        _auth(client, users["sam"]).post(
            react, data={"i": 0}, content_type="application/json",
        )
    _comment(client, users["lee"], club_, event, "Lovely")
    with patch(POST, side_effect=_Mids()) as post:
        flush_due_notices(now=timezone.now() + BATCH_WINDOW + timedelta(seconds=5))
    quiet = [
        c.kwargs["json"] for c in post.call_args_list
        if "reply_markup" not in c.kwargs["json"]
    ]
    assert quiet and all(q.get("disable_notification") for q in quiet)
    pings = [
        c.kwargs["json"] for c in post.call_args_list
        if "reply_markup" in c.kwargs["json"]
    ]
    assert pings and not any("disable_notification" in p for p in pings)


def test_comment_ping_palette_has_no_counts(club):
    club_, users = club
    _enable("feature_club_reactions")
    event = ClubEvent.objects.create(
        club=club_, actor=users["riley"], kind=ClubEvent.KIND_OPENED,
        wine_name="Merlot", vintage=2019,
    )
    from wine_cellar.apps.club.models import ClubReaction

    ClubReaction.objects.create(event=event, user=users["sam"], emoji=PALETTE[0])
    rows = _markup(event, "en", muted=False)["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == list(PALETTE)
    from wine_cellar.apps.club.tasks import _opened_markup

    card = _opened_markup(event, "en")["inline_keyboard"][0]
    assert card[0]["text"] == f"{PALETTE[0]} 1"


# -- create / validate --------------------------------------------------------
def test_ask_creates_event_and_schedules_delivery(
    client, club, django_capture_on_commit_callbacks,
):
    club_, users = club
    url = reverse("api-club-ask", kwargs={"pk": club_.id})
    with patch(SCHEDULE) as schedule, django_capture_on_commit_callbacks(execute=True):
        response = _auth(client, users["riley"]).post(
            url, data={"text": " Bring a white? "}, content_type="application/json",
        )
    assert response.status_code == 201
    body = response.json()
    assert body["club_name"] == "Thursday reds" and body["undo_seconds"] == 60
    event = ClubEvent.objects.get(id=body["event_id"])
    assert event.kind == ClubEvent.KIND_ASK and event.actor == users["riley"]
    assert event.payload["text"] == "Bring a white?"
    assert ClubEventDelivery.objects.count() == 0  # nothing out before the window
    schedule.assert_called_once()


def test_ask_validation_and_membership(client, club, django_user_model):
    club_, users = club
    assert _ask(client, users["sam"], club_, "   ").status_code == 400
    too_long = _ask(client, users["sam"], club_, "x" * 281)
    assert too_long.status_code == 400 and too_long.json()["max"] == 280
    stranger = django_user_model.objects.create_user(username="stranger", password="pw")
    assert _ask(client, stranger, club_).status_code == 404
    assert ClubEvent.objects.filter(kind=ClubEvent.KIND_ASK).count() == 0


def test_flag_off_404_and_nothing_created(client, club):
    club_, users = club
    ServiceConfig.objects.filter(key="feature_club_relay").delete()
    assert _ask(client, users["sam"], club_).status_code == 404
    assert _undo(client, users["sam"], 1).status_code == 404
    assert ClubEvent.objects.count() == 0


def test_relay_needs_threads_flag(client, club):
    club_, users = club
    ServiceConfig.objects.filter(key="feature_club_threads").delete()
    assert _ask(client, users["sam"], club_).status_code == 404


def test_global_club_cannot_be_asked(client, club):
    _, users = club
    feed = Club.objects.create(
        name="Staff feed", kind=Club.KIND_GLOBAL, cellar_visibility="none",
    )
    ClubMembership.objects.create(
        club=feed, user=users["sam"], role="member", accepted_sharing_version=1,
    )
    response = _ask(client, users["sam"], feed)
    assert response.status_code == 400 and response.json()["error"] == "global"
    assert ClubEvent.objects.filter(kind=ClubEvent.KIND_ASK).count() == 0


def test_rate_limit_three_per_hour_and_window_slides(client, club):
    club_, users = club
    for n in range(3):
        assert _ask(client, users["sam"], club_, f"Question {n}").status_code == 201
    blocked = _ask(client, users["sam"], club_, "Question 4")
    assert blocked.status_code == 429
    assert blocked.json()["max"] == 3 and blocked.json()["retry_minutes"] >= 1
    assert _ask(client, users["lee"], club_, "Mine").status_code == 201  # per member
    other = Club.objects.create(name="Other", cellar_visibility="none")
    ClubMembership.objects.create(
        club=other, user=users["sam"], role="member", accepted_sharing_version=1,
    )
    assert _ask(client, users["sam"], other, "Elsewhere").status_code == 201  # per club
    ClubEvent.objects.filter(club=club_, actor=users["sam"]).update(
        created=timezone.now() - timedelta(hours=1, minutes=1),
    )
    assert _ask(client, users["sam"], club_, "Question 5").status_code == 201


def test_dedupe_identical_text_for_ten_minutes(client, club):
    club_, users = club
    first = _ask(client, users["sam"], club_, "Same thing")
    again = _ask(client, users["sam"], club_, "Same thing")
    assert again.status_code == 409 and again.json()["error"] == "duplicate"
    assert again.json()["event_id"] == first.json()["event_id"]
    assert _ask(client, users["lee"], club_, "Same thing").status_code == 201
    ClubEvent.objects.filter(id=first.json()["event_id"]).update(
        created=timezone.now() - timedelta(minutes=11),
    )
    assert _ask(client, users["sam"], club_, "Same thing").status_code == 201


# -- delivery -----------------------------------------------------------------
def test_delivery_to_everyone_but_asker_with_reply_and_palette(client, club):
    club_, users = club
    _enable("feature_club_reactions")
    ClubMembership.objects.filter(user=users["pat"]).update(feed_muted=True)
    event_id = _ask(client, users["riley"], club_, "<b>Port</b> & cheese?").json()[
        "event_id"
    ]
    with patch(POST, side_effect=_Mids()) as post:
        assert deliver_ask(event_id) == 2
    sent = {c.kwargs["json"]["chat_id"]: c.kwargs["json"] for c in post.call_args_list}
    assert set(sent) == {"200", "300"}  # not the asker, not the muted member
    assert sent["200"]["text"] == (
        "Riley asks the club: &lt;b&gt;Port&lt;/b&gt; &amp; cheese?"
    )
    rows = sent["200"]["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["callback_data"] == f"cl:rx:{event_id}:0"
    assert [b["text"] for b in rows[0]] == list(PALETTE)
    assert rows[-1] == [{"text": "Reply", "callback_data": f"cl:rp:{event_id}"}]
    deliveries = ClubEventDelivery.objects.filter(event_id=event_id)
    assert {d.chat_id for d in deliveries} == {"200", "300"}
    assert {d.kind for d in deliveries} == {ClubEventDelivery.KIND_ASK}
    assert all(d.message_id for d in deliveries)
    with patch(POST, side_effect=_Mids()) as post:
        assert deliver_ask(event_id) == 0  # idempotent
    assert post.call_count == 0


def test_delivery_without_reactions_flag_has_only_reply(client, club):
    club_, users = club
    event_id = _ask(client, users["riley"], club_).json()["event_id"]
    with patch(POST, side_effect=_Mids()) as post:
        deliver_ask(event_id)
    rows = post.call_args.kwargs["json"]["reply_markup"]["inline_keyboard"]
    assert [b["text"] for row in rows for b in row] == ["Reply"]


def test_delivery_in_afrikaans_and_error_recorded(client, club):
    club_, users = club
    from wine_cellar.apps.user.views import get_user_settings

    prefs = get_user_settings(users["sam"])
    prefs.language = "af"
    prefs.save()
    event_id = _ask(client, users["riley"], club_, "Wyn?").json()["event_id"]
    bad = MagicMock()
    bad.status_code = 403
    bad.json.return_value = {"description": "Forbidden: bot was blocked"}

    seen = {}

    def fake(url, json=None, timeout=None):
        seen[json["chat_id"]] = json["text"]
        return bad if json["chat_id"] == "300" else _Mids()()

    with patch(POST, side_effect=fake):
        assert deliver_ask(event_id) == 2  # 300 is blocked, 200 and 400 got it
    assert seen["200"] == "Riley vra die klub: Wyn?"
    assert seen["300"] == "Riley asks the club: Wyn?"
    texts = {
        d.chat_id: d for d in ClubEventDelivery.objects.filter(event_id=event_id)
    }
    assert texts["300"].message_id is None and "403" in texts["300"].error
    assert texts["200"].error == ""


def test_afrikaans_line():
    from wine_cellar.apps.club.copy import t

    assert t("af", "club.ask.line", name="Sam", text="Wyn?") == "Sam vra die klub: Wyn?"
    line = t("en", "club.ask.line", name="Sam", text="Wine?")
    assert line == "Sam asks the club: Wine?"


def test_undo_before_delivery_deletes_everything(client, club):
    club_, users = club
    event_id = _ask(client, users["riley"], club_).json()["event_id"]
    assert _undo(client, users["sam"], event_id).status_code == 404  # not the asker
    assert _undo(client, users["riley"], event_id).json() == {"undone": True}
    assert not ClubEvent.objects.filter(id=event_id).exists()
    with patch(POST, side_effect=_Mids()) as post:
        assert deliver_ask(event_id) == 0  # a late task finds nothing
    assert post.call_count == 0
    assert _undo(client, users["riley"], event_id).status_code == 404
    # An undone ask does not count towards the hourly limit.
    for n in range(3):
        assert _ask(client, users["riley"], club_, f"Again {n}").status_code == 201


def test_undo_after_delivery_says_already_out(client, club):
    club_, users = club
    event_id = _ask(client, users["riley"], club_).json()["event_id"]
    with patch(POST, side_effect=_Mids()):
        deliver_ask(event_id)
    response = _undo(client, users["riley"], event_id)
    assert response.status_code == 409 and response.json()["error"] == "already_out"
    assert ClubEvent.objects.filter(id=event_id).exists()


def test_delivery_task_flag_off_sends_nothing(client, club):
    club_, users = club
    event_id = _ask(client, users["riley"], club_).json()["event_id"]
    ServiceConfig.objects.filter(key="feature_club_relay").delete()
    with patch(POST, side_effect=_Mids()) as post:
        assert deliver_ask(event_id) == 0
    assert post.call_count == 0


# -- replies thread like phase 1 ----------------------------------------------
def test_reply_pings_everyone_but_author_including_asker(client, club):
    club_, users = club
    event_id = _ask(client, users["riley"], club_, "Cellar night?").json()["event_id"]
    with patch(POST, side_effect=_Mids()):
        deliver_ask(event_id)
    event = ClubEvent.objects.get(id=event_id)
    copies = {
        d.chat_id: d.message_id for d in ClubEventDelivery.objects.filter(event=event)
    }
    first = _comment(client, users["sam"], club_, event, "Count me in")
    assert first.status_code == 201
    assert _comment(client, users["sam"], club_, event, "Friday?").status_code == 201
    assert set(ClubNotice.objects.values_list("recipient__username", flat=True)) == {
        "riley", "lee", "pat",
    }
    with patch(POST, side_effect=_Mids()) as post:
        flush_due_notices(now=timezone.now() + BATCH_WINDOW + timedelta(seconds=5))
    sent = {c.kwargs["json"]["chat_id"]: c.kwargs["json"] for c in post.call_args_list}
    assert set(sent) == {"100", "300", "400"}
    assert sent["300"]["reply_to_message_id"] == copies["300"]  # under their own copy
    assert "reply_to_message_id" not in sent["100"]  # the asker has no copy
    assert "Cellar night?" in sent["100"]["text"]
    assert "Riley asks" in sent["100"]["text"]
    assert "Count me in" in sent["300"]["text"] and "Friday?" in sent["300"]["text"]
    assert [b["text"] for b in sent["300"]["reply_markup"]["inline_keyboard"][-1]] == [
        "Reply", "Mute thread",
    ]
    # The ping itself is a swipe-reply target for that event.
    from wine_cellar.apps.club.threads import resolve_message

    ping = ClubEventDelivery.objects.filter(
        event=event, kind=ClubEventDelivery.KIND_PING, chat_id="100",
    ).first()
    assert resolve_message(users["riley"], "100", ping.message_id) == event


def test_swipe_reply_target_resolves_to_ask(client, club):
    club_, users = club
    event_id = _ask(client, users["riley"], club_).json()["event_id"]
    with patch(POST, side_effect=_Mids()):
        deliver_ask(event_id)
    copy = ClubEventDelivery.objects.get(event_id=event_id, chat_id="200")
    response = _auth(client, users["sam"]).get(
        reverse("api-club-thread-resolve"),
        {"chat_id": "200", "message_id": copy.message_id},
    )
    assert response.status_code == 200 and response.json()["event_id"] == event_id


def test_muted_thread_gets_no_reply_ping(client, club):
    from wine_cellar.apps.club.threads import toggle_thread_mute

    club_, users = club
    event_id = _ask(client, users["riley"], club_).json()["event_id"]
    event = ClubEvent.objects.get(id=event_id)
    toggle_thread_mute(users["lee"], event, True)
    _comment(client, users["sam"], club_, event, "Hi")
    assert "lee" not in set(
        ClubNotice.objects.values_list("recipient__username", flat=True)
    )


def test_reaction_to_ask_quietly_tells_the_asker(client, club):
    club_, users = club
    _enable("feature_club_reactions")
    event_id = _ask(client, users["riley"], club_, "Port?").json()["event_id"]
    with patch(POST, side_effect=_Mids()):
        deliver_ask(event_id)
    react = reverse("api-club-event-react", kwargs={"event_id": event_id})
    with patch(RX_SCHEDULE):
        assert _auth(client, users["sam"]).post(
            react, data={"i": 1}, content_type="application/json",
        ).status_code == 200
    with patch(POST, side_effect=_Mids()) as post:
        flush_due_notices(now=timezone.now() + BATCH_WINDOW + timedelta(seconds=5))
    sent = post.call_args.kwargs["json"]
    assert sent["chat_id"] == "100" and sent["disable_notification"] is True
    assert sent["text"] == f"Sam reacted {PALETTE[1]} to your question"


def test_count_refresh_edits_ask_copies(client, club):
    club_, users = club
    _enable("feature_club_reactions")
    event_id = _ask(client, users["riley"], club_).json()["event_id"]
    with patch(POST, side_effect=_Mids()):
        deliver_ask(event_id)
    response = MagicMock(status_code=200)
    with patch(EDIT, return_value=response) as post:
        assert refresh_event_reactions(event_id) == 3
    markup = post.call_args.kwargs["json"]["reply_markup"]["inline_keyboard"]
    assert markup[-1][0]["callback_data"] == f"cl:rp:{event_id}"


# -- feed, sharing, consumers -------------------------------------------------
def test_feed_shows_ask_text_and_name_only_at_every_sharing_level(client, club):
    club_, users = club
    assert club_.cellar_visibility == "none"
    event_id = _ask(client, users["riley"], club_, "Port?").json()["event_id"]
    feed = reverse("api-club-feed", kwargs={"pk": club_.id})
    body = _auth(client, users["lee"]).get(feed).json()
    row = next(e for e in body["events"] if e["id"] == event_id)
    assert row["kind"] == "ask" and row["actor_name"] == "Riley"
    assert row["payload"] == {"text": "Port?"}
    assert row["wine_name"] == "" and row["vintage"] is None and row["producer"] == ""
    home = _auth(client, users["lee"]).get(
        reverse("api-club-detail", kwargs={"pk": club_.id}),
    ).json()
    assert any(e["kind"] == "ask" for e in home["events"])


def test_feed_and_home_hide_asks_when_flag_off(client, club):
    club_, users = club
    _ask(client, users["riley"], club_, "Port?")
    ServiceConfig.objects.filter(key="feature_club_relay").delete()
    feed = _auth(client, users["lee"]).get(
        reverse("api-club-feed", kwargs={"pk": club_.id}),
    ).json()
    assert feed["count"] == 0
    home = _auth(client, users["lee"]).get(
        reverse("api-club-detail", kwargs={"pk": club_.id}),
    ).json()
    assert home["events"] == []


def test_feed_detail_has_full_thread(client, club):
    club_, users = club
    event_id = _ask(client, users["riley"], club_, "Port?").json()["event_id"]
    event = ClubEvent.objects.get(id=event_id)
    _comment(client, users["sam"], club_, event, "Yes")
    feed = reverse("api-club-feed", kwargs={"pk": club_.id})
    body = _auth(client, users["lee"]).get(feed, {"event_id": event_id}).json()
    assert body["events"][0]["comment_count"] == 1
    comments = _auth(client, users["lee"]).get(
        reverse(
            "api-club-event-comments", kwargs={"pk": club_.id, "event_id": event_id},
        )
    ).json()
    assert [c["text"] for c in comments["comments"]] == ["Yes"]


def test_deliver_club_event_ignores_asks(client, club):
    club_, users = club
    event = ClubEvent.objects.create(
        club=club_, actor=users["riley"], kind=ClubEvent.KIND_ASK,
        payload={"text": "Port?"},
    )
    with patch("wine_cellar.apps.club.tasks._send") as send:
        deliver_club_event(event.id)
    assert send.call_count == 0 and not ClubEventDelivery.objects.exists()


def test_ask_is_not_a_bottle_want_to_list_comment_card(client, club):
    club_, users = club
    _enable("feature_lists", "feature_club_reactions")
    event_id = _ask(client, users["riley"], club_, "Port?").json()["event_id"]
    want = reverse("api-club-event-want", kwargs={"event_id": event_id})
    assert _auth(client, users["sam"]).post(
        want, data={}, content_type="application/json",
    ).status_code == 404
    to_list = reverse(
        "api-club-event-to-list", kwargs={"pk": club_.id, "event_id": event_id},
    )
    assert _auth(client, users["sam"]).post(
        to_list, data={}, content_type="application/json",
    ).status_code == 404
    from wine_cellar.apps.wine.models import WineListItem

    assert WineListItem.objects.count() == 0
    ServiceConfig.objects.filter(key="feature_club_threads").delete()
    with patch(EDIT) as edit:
        refresh_event_comment_card(event_id)
    assert edit.call_count == 0


def test_ask_events_are_never_published_as_bottle_events(client, club):
    """The opened-bottle consumers filter on kind; an ask matches none."""
    club_, users = club
    _ask(client, users["riley"], club_, "Port?")
    from wine_cellar.apps.club.ask import _answer_feed, _club_bundle

    member = ClubMembership.objects.get(user=users["riley"])
    meta = _club_bundle(member)
    body = _answer_feed(users["sam"], meta, [member], None, "feed", {})
    assert body["events"] == []
    body = _answer_feed(users["sam"], meta, [member], None, "who_drinks", {})
    assert body["ranking"] == []


def test_typed_questions_see_asks_only_with_flag(client, club):
    club_, users = club
    _enable("feature_club_reactions")
    asked = _ask(client, users["riley"], club_, "Port or Madeira?")
    event_id = asked.json()["event_id"]
    event = ClubEvent.objects.get(id=event_id)
    _comment(client, users["sam"], club_, event, "Madeira")
    url = reverse("api-clubs-ask")
    payload = {"kind": "comments", "club_id": club_.id}
    body = _auth(client, users["lee"]).post(
        url, data=payload, content_type="application/json",
    ).json()
    assert body["asks"] == [
        {
            "asked_by": "Riley",
            "text": "Port or Madeira?",
            "created": body["asks"][0]["created"],
            "replies": [{"display_name": "Sam", "text": "Madeira"}],
        }
    ]
    assert body["comments"] == []  # the reply lives on the ask, not as a wine comment
    ServiceConfig.objects.filter(key="feature_club_relay").delete()
    body = _auth(client, users["lee"]).post(
        url, data=payload, content_type="application/json",
    ).json()
    assert "asks" not in body


def test_typed_questions_never_show_asks_of_global_clubs(client, club):
    _, users = club
    _enable("feature_global_clubs")
    feed = Club.objects.create(
        name="Staff feed", kind=Club.KIND_GLOBAL, cellar_visibility="none",
    )
    ClubMembership.objects.create(
        club=feed, user=users["sam"], role="member", accepted_sharing_version=1,
    )
    ClubEvent.objects.create(
        club=feed, actor=users["riley"], kind=ClubEvent.KIND_ASK,
        payload={"text": "stray"},
    )
    body = _auth(client, users["sam"]).post(
        reverse("api-clubs-ask"),
        data={"kind": "comments", "club_id": feed.id},
        content_type="application/json",
    ).json()
    assert "asks" not in body


def test_club_web_detail_lists_asks_only_with_flag(client, club):
    club_, users = club
    _ask(client, users["riley"], club_, "Port?")
    client.force_login(users["lee"])
    url = reverse("club-detail", kwargs={"pk": club_.id})
    assert "asked the club" in client.get(url).content.decode()
    ServiceConfig.objects.filter(key="feature_club_relay").delete()
    assert "asked the club" not in client.get(url).content.decode()


def test_comment_text_is_escaped_in_thread_ping_of_ask(client, club):
    club_, users = club
    event_id = _ask(client, users["riley"], club_, "<i>x</i>").json()["event_id"]
    event = ClubEvent.objects.get(id=event_id)
    _comment(client, users["sam"], club_, event, "<script>")
    with patch(POST, side_effect=_Mids()) as post:
        flush_due_notices(now=timezone.now() + BATCH_WINDOW + timedelta(seconds=5))
    text = post.call_args_list[0].kwargs["json"]["text"]
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert ClubComment.objects.filter(event=event).count() == 1
