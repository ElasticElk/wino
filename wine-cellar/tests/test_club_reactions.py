"""P2: reactions, Want this, owner-only batched pings, debounced count edits."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from wine_cellar.apps.club.models import (
    Club,
    ClubEvent,
    ClubEventDelivery,
    ClubMembership,
    ClubNotice,
    ClubReaction,
    ClubThreadMute,
    ClubWant,
)
from wine_cellar.apps.club.reactions import PALETTE
from wine_cellar.apps.club.tasks import _opened_markup, refresh_event_reactions
from wine_cellar.apps.club.threads import BATCH_WINDOW, flush_due_notices
from wine_cellar.apps.scanner.models import ServiceConfig, TelegramUser
from wine_cellar.apps.wine.models import WineListItem

POST = "wine_cellar.apps.club.threads.requests.post"
EDIT = "requests.post"
SCHEDULE = "wine_cellar.apps.club.tasks.refresh_event_reactions.apply_async"


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def _enable(*keys):
    for key in keys:
        ServiceConfig.objects.update_or_create(key=key, defaults={"value": "on"})


def _reply(status=200, description=""):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = (
        {"result": {"message_id": 900}}
        if status == 200
        else {"description": description}
    )
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
    club = Club.objects.create(name="Thursday reds")
    for user in users.values():
        ClubMembership.objects.create(
            club=club, user=user, role="member", accepted_sharing_version=1,
        )
    event = ClubEvent.objects.create(
        club=club, actor=users["riley"], kind=ClubEvent.KIND_OPENED,
        wine_name="Merlot", vintage=2019,
    )
    for chat, mid in (("200", 21), ("300", 31), ("400", 41)):
        ClubEventDelivery.objects.create(event=event, chat_id=chat, message_id=mid)
    _enable("feature_clubs", "feature_club_reactions")
    ServiceConfig.objects.update_or_create(key="bot_token", defaults={"value": "T"})
    return club, event, users


def _react(client, user, event, i=0):
    url = reverse("api-club-event-react", kwargs={"event_id": event.id})
    return _auth(client, user).post(url, data={"i": i}, content_type="application/json")


def _want(client, user, event):
    url = reverse("api-club-event-want", kwargs={"event_id": event.id})
    return _auth(client, user).post(url, data={}, content_type="application/json")


def _later():
    return timezone.now() + BATCH_WINDOW + timedelta(seconds=5)


def test_tap_toggles_and_returns_counts(client, club):
    _, event, users = club
    with patch(SCHEDULE):
        body = _react(client, users["sam"], event, 0).json()
        assert body["on"] is True and body["counts"] == [1, 0, 0, 0, 0]
        _react(client, users["lee"], event, 0)
        _react(client, users["lee"], event, 1)
        body = _react(client, users["sam"], event, 0).json()
    assert body["on"] is False and body["counts"] == [1, 1, 0, 0, 0]
    assert ClubReaction.objects.count() == 2
    assert body["mine"] == []


def test_bad_index_and_non_member_and_outsider(client, club, django_user_model):
    _, event, users = club
    assert _react(client, users["sam"], event, 9).status_code == 400
    stranger = django_user_model.objects.create_user(username="stranger", password="pw")
    assert _react(client, stranger, event, 0).status_code == 404
    assert ClubReaction.objects.count() == 0


def test_flag_off_404_and_card_markup_unchanged(client, club):
    _, event, users = club
    ServiceConfig.objects.filter(key="feature_club_reactions").delete()
    assert _react(client, users["sam"], event, 0).status_code == 404
    assert _want(client, users["sam"], event).status_code == 404
    _enable("feature_club_comments", "feature_lists")
    markup = _opened_markup(event, "en")
    assert [[b["callback_data"] for b in r] for r in markup["inline_keyboard"]] == [
        [f"cl:cm:{event.id}", f"cl:td:{event.id}"],
    ]


def test_card_markup_one_row_with_counts_and_want_replaces_to_drink(client, club):
    _, event, users = club
    _enable("feature_club_comments", "feature_lists")
    with patch(SCHEDULE):
        for name in ("sam", "lee", "pat"):
            _react(client, users[name], event, 0)
    rows = _opened_markup(event, "en")["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == [
        f"{PALETTE[0]} 3", PALETTE[1], PALETTE[2], PALETTE[3], PALETTE[4],
    ]
    assert rows[0][0]["callback_data"] == f"cl:rx:{event.id}:0"
    assert [b["callback_data"] for b in rows[1]] == [
        f"cl:cm:{event.id}", f"cl:wt:{event.id}",
    ]
    assert rows[1][1]["text"] == "Want this"
    assert all(len(b["callback_data"].encode()) < 64 for r in rows for b in r)
    ServiceConfig.objects.filter(key="feature_lists").delete()
    rows = _opened_markup(event, "en")["inline_keyboard"]
    assert len(rows) == 2
    assert [b["callback_data"] for b in rows[1]] == [f"cl:cm:{event.id}"]


def test_owner_pinged_once_in_batch_never_self_or_club(client, club):
    _, event, users = club
    with patch(SCHEDULE):
        _react(client, users["sam"], event, 0)
        _react(client, users["pat"], event, 0)
        _react(client, users["pat"], event, 1)
        _react(client, users["riley"], event, 2)  # the owner's own tap
    notices = ClubNotice.objects.all()
    assert notices.count() == 1
    assert notices[0].recipient == users["riley"]
    assert notices[0].kind == ClubNotice.KIND_REACTION
    with patch(POST, return_value=_reply()) as post:
        assert flush_due_notices(now=timezone.now()) == 0
        flush_due_notices(now=_later())
    assert post.call_count == 1
    sent = post.call_args.kwargs["json"]
    assert sent["chat_id"] == "100"
    assert sent["text"] == (
        "Sam and Pat reacted \U0001F377\U0001F60D to your 2019 Merlot"
    )
    assert "reply_markup" not in sent


def test_unreact_in_window_removes_line_and_empty_notice(client, club):
    _, event, users = club
    with patch(SCHEDULE):
        _react(client, users["sam"], event, 0)
        _react(client, users["pat"], event, 1)
        _react(client, users["sam"], event, 0)
    with patch(POST, return_value=_reply()) as post:
        flush_due_notices(now=_later())
    assert post.call_args.kwargs["json"]["text"] == (
        "Pat reacted \U0001F60D to your 2019 Merlot"
    )
    # everything undone before the flush: nothing is sent
    with patch(SCHEDULE):
        _react(client, users["sam"], event, 0)
        _react(client, users["sam"], event, 0)
    assert not ClubNotice.objects.filter(sent_at__isnull=True).exists()


def test_unreact_after_send_sends_nothing_more(client, club):
    _, event, users = club
    with patch(SCHEDULE):
        _react(client, users["sam"], event, 0)
        with patch(POST, return_value=_reply()):
            flush_due_notices(now=_later())
        _react(client, users["sam"], event, 0)
    assert not ClubNotice.objects.filter(sent_at__isnull=True).exists()


def test_owner_feed_muted_and_thread_muted_get_nothing(client, club):
    club_, event, users = club
    with patch(SCHEDULE):
        _react(client, users["sam"], event, 0)
        mine = ClubMembership.objects.filter(club=club_, user=users["riley"])
        mine.update(feed_muted=True)
        with patch(POST, return_value=_reply()) as post:
            flush_due_notices(now=_later())
        assert post.call_count == 0
        mine.update(feed_muted=False)
        ClubThreadMute.objects.create(user=users["riley"], event=event)
        _react(client, users["pat"], event, 0)
        with patch(POST, return_value=_reply()) as post:
            flush_due_notices(now=_later())
        assert post.call_count == 0


def test_want_this_saves_to_tappers_list_idempotent_and_tells_owner(client, club):
    _, event, users = club
    _enable("feature_lists")
    first = _want(client, users["sam"], event)
    assert first.status_code == 201
    again = _want(client, users["sam"], event)
    assert again.status_code == 200
    saved = WineListItem.objects.filter(added_by=users["sam"], name="Merlot")
    assert saved.count() == 1
    assert not WineListItem.objects.filter(added_by=users["riley"]).exists()
    assert ClubWant.objects.count() == 1
    _react(client, users["pat"], event, 0)
    assert ClubNotice.objects.count() == 1
    with patch(SCHEDULE), patch(POST, return_value=_reply()) as post:
        flush_due_notices(now=_later())
    text = post.call_args.kwargs["json"]["text"]
    assert text.splitlines() == [
        "Pat reacted \U0001F377 to your 2019 Merlot",
        "Sam saved it to their To-drink list",
    ]


def test_want_only_notice_names_the_wine(client, club):
    _, event, users = club
    _enable("feature_lists")
    _want(client, users["sam"], event)
    with patch(POST, return_value=_reply()) as post:
        flush_due_notices(now=_later())
    assert post.call_args.kwargs["json"]["text"] == (
        "Sam saved your 2019 Merlot to their To-drink list"
    )


def test_want_lists_off_404_and_owner_own_want_silent(client, club):
    _, event, users = club
    assert _want(client, users["sam"], event).status_code == 404
    assert ClubWant.objects.count() == 0
    _enable("feature_lists")
    assert _want(client, users["riley"], event).status_code == 201
    assert ClubNotice.objects.count() == 0


def test_retracted_event_changes_nothing(client, club):
    _, event, users = club
    _enable("feature_lists")
    event.retracted_at = timezone.now()
    event.save()
    assert _react(client, users["sam"], event, 0).status_code == 409
    assert _want(client, users["sam"], event).status_code == 409
    assert not ClubReaction.objects.exists() and not ClubWant.objects.exists()
    assert not WineListItem.objects.filter(added_by=users["sam"]).exists()
    assert not ClubNotice.objects.exists()


def test_taps_debounced_to_one_scheduled_pass(client, club):
    _, event, users = club
    with patch(SCHEDULE) as schedule:
        for i in range(4):
            _react(client, users["sam"], event, i)
        _react(client, users["lee"], event, 0)
    assert schedule.call_count == 1
    assert schedule.call_args.kwargs["countdown"] == 30


def test_refresh_pass_edits_cards_and_tolerates_harmless_errors(client, club):
    _, event, users = club
    with patch(SCHEDULE):
        _react(client, users["sam"], event, 0)
    with patch(EDIT, return_value=_reply()) as post:
        assert refresh_event_reactions(event.id) == 3
    chats = sorted(c.kwargs["json"]["chat_id"] for c in post.call_args_list)
    assert chats == ["200", "300", "400"]
    first = post.call_args_list[0].kwargs["json"]
    assert post.call_args_list[0].args[0].endswith("/editMessageReplyMarkup")
    assert first["reply_markup"]["inline_keyboard"][0][0]["text"] == f"{PALETTE[0]} 1"
    for desc in (
        "Bad Request: message is not modified",
        "Bad Request: message to edit not found",
    ):
        with patch(EDIT, return_value=_reply(400, desc)):
            assert refresh_event_reactions(event.id) == 3  # harmless: counted as done
    with patch(EDIT, return_value=_reply(403, "Forbidden: bot was blocked")):
        assert refresh_event_reactions(event.id) == 0
    # the debounce key is released so the next tap schedules again
    with patch(SCHEDULE) as schedule:
        _react(client, users["lee"], event, 1)
    assert schedule.call_count == 1


def test_refresh_skips_retracted_and_flag_off(client, club):
    _, event, users = club
    with patch(EDIT) as post:
        ServiceConfig.objects.filter(key="feature_club_reactions").delete()
        assert refresh_event_reactions(event.id) == 0
        _enable("feature_club_reactions")
        event.retracted_at = timezone.now()
        event.save()
        assert refresh_event_reactions(event.id) == 0
    assert post.call_count == 0


def test_comment_ping_markup_has_palette(club):
    from wine_cellar.apps.club.threads import _markup

    _, event, _ = club
    rows = _markup(event, "en", muted=False)["inline_keyboard"]
    assert rows[0][0]["callback_data"] == f"cl:rx:{event.id}:0"
    assert [b["text"] for b in rows[-1]] == ["Reply", "Mute thread"]
    ServiceConfig.objects.filter(key="feature_club_reactions").delete()
    rows = _markup(event, "en", muted=False)["inline_keyboard"]
    assert len(rows) == 1


def test_migration_merge_keys_on_kind(db):
    constraint = next(
        c for c in ClubNotice._meta.constraints if c.name == "clubnotice_one_pending"
    )
    assert constraint.fields == ("recipient", "event", "kind")


def test_comment_and_reaction_pending_can_coexist(club):
    club_, event, users = club
    ClubNotice.objects.create(
        recipient=users["riley"], club=club_, event=event, kind="comment",
        dedupe_key="a", due_at=timezone.now(),
    )
    ClubNotice.objects.create(
        recipient=users["riley"], club=club_, event=event, kind="reaction",
        dedupe_key="b", due_at=timezone.now(),
    )
    assert ClubNotice.objects.count() == 2


def test_ask_sees_reactions_and_wants_only_with_flag(client, club):
    club_, event, users = club
    _enable("feature_club_comments", "feature_lists")
    with patch(SCHEDULE):
        _react(client, users["sam"], event, 0)
        _react(client, users["pat"], event, 0)
    _want(client, users["lee"], event)
    ask = reverse("api-clubs-ask")
    payload = {"kind": "comments", "club_id": club_.id, "wine_text": "merlot"}
    body = _auth(client, users["sam"]).post(
        ask, data=payload, content_type="application/json",
    ).json()
    assert body["status"] == "ok"
    summary = body["reactions"][0]
    assert summary["reactions"] == [
        {"emoji": PALETTE[0], "count": 2, "by": ["Sam", "Pat"]},
    ]
    assert summary["wanted_by"] == ["Lee"]
    ServiceConfig.objects.filter(key="feature_club_reactions").delete()
    body = _auth(client, users["sam"]).post(
        ask, data=payload, content_type="application/json",
    ).json()
    assert "reactions" not in body


def test_copy_en_af():
    from wine_cellar.apps.club.copy import MESSAGES

    en = {k for k in MESSAGES["en"] if k.startswith("club.rx.")}
    assert en and en == {k for k in MESSAGES["af"] if k.startswith("club.rx.")}
    assert MESSAGES["af"]["club.rx.want_btn"] == "Want this"


def test_afrikaans_owner_notice(client, club):
    _, event, users = club
    TelegramUser.objects.filter(user=users["riley"]).update(language="af") if hasattr(
        TelegramUser, "language"
    ) else None
    with patch(SCHEDULE):
        _react(client, users["sam"], event, 0)
    with patch(POST, return_value=_reply()) as post:
        flush_due_notices(now=_later())
    assert "Sam" in post.call_args.kwargs["json"]["text"]


def test_user_text_is_escaped(client, club):
    _, event, users = club
    TelegramUser.objects.filter(user=users["sam"]).update(display_name="<i>Sam</i>")
    event.wine_name = "<b>Merlot</b>"
    event.save()
    with patch(SCHEDULE):
        _react(client, users["sam"], event, 0)
    with patch(POST, return_value=_reply()) as post:
        flush_due_notices(now=_later())
    text = post.call_args.kwargs["json"]["text"]
    assert "<b>" not in text and "<i>" not in text and "&lt;" in text
