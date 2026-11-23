"""P1: comment threads, reply pings (ClubNotice outbox), swipe-reply mapping."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
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
    ClubThreadMute,
)
from wine_cellar.apps.club.tasks import refresh_event_comment_card
from wine_cellar.apps.club.threads import (
    BATCH_WINDOW,
    RATE_LIMIT_MAX,
    enqueue_comment_notices,
    flush_due_notices,
)
from wine_cellar.apps.scanner.models import ServiceConfig, TelegramUser

POST = "wine_cellar.apps.club.threads.requests.post"


def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def _enable(*keys):
    for key in keys:
        ServiceConfig.objects.update_or_create(key=key, defaults={"value": "on"})


def _reply(status=200, mid=900, description=""):
    response = MagicMock()
    response.status_code = status
    if status == 200:
        response.json.return_value = {"result": {"message_id": mid}}
    else:
        response.json.return_value = {"description": description}
    return response


@pytest.fixture
def club(db, django_user_model):
    users = {}
    people = (("riley", "100"), ("sam", "200"), ("lee", "300"), ("pat", "400"))
    for name, chat in people:
        user = django_user_model.objects.create_user(username=name, password="pw")
        TelegramUser.objects.create(
            user=user, chat_id=chat, display_name=name.title(),
        )
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
    for name, chat, mid in (("sam", "200", 21), ("lee", "300", 31), ("pat", "400", 41)):
        ClubEventDelivery.objects.create(event=event, chat_id=chat, message_id=mid)
    _enable("feature_clubs", "feature_club_comments", "feature_club_threads")
    ServiceConfig.objects.update_or_create(key="bot_token", defaults={"value": "T"})
    return club, event, users


def _comment(event, user, text="lovely"):
    return ClubComment.objects.create(event=event, user=user, text=text)


def _later():
    return timezone.now() + BATCH_WINDOW + timedelta(seconds=5)


def _calls_to(post, chat_id):
    return [
        c.kwargs["json"] for c in post.call_args_list
        if c.kwargs["json"]["chat_id"] == chat_id
    ]


def test_recipients_skip_author_muted_membership_and_muted_thread(club):
    club_, event, users = club
    ClubMembership.objects.filter(club=club_, user=users["sam"]).update(
        feed_muted=True,
    )
    ClubThreadMute.objects.create(user=users["pat"], event=event)
    comment = _comment(event, users["lee"])
    assert enqueue_comment_notices(comment.id) == 1
    assert list(ClubNotice.objects.values_list("recipient__username", flat=True)) == [
        "riley",
    ]


def test_flag_off_queues_nothing(club):
    club_, event, users = club
    ServiceConfig.objects.filter(key="feature_club_threads").delete()
    assert enqueue_comment_notices(_comment(event, users["lee"]).id) == 0
    assert ClubNotice.objects.count() == 0


def test_batches_replies_into_one_ping_threaded_under_card(club):
    club_, event, users = club
    enqueue_comment_notices(_comment(event, users["lee"], "first <b>").id)
    enqueue_comment_notices(_comment(event, users["pat"], "second").id)
    assert ClubNotice.objects.filter(recipient=users["sam"]).count() == 1
    with patch(POST, return_value=_reply(mid=901)) as post:
        assert flush_due_notices(now=timezone.now()) == 0  # not yet due
        assert post.call_count == 0
        flush_due_notices(now=_later())
    sam = _calls_to(post, "200")
    assert len(sam) == 1
    payload = sam[0]
    assert payload["reply_to_message_id"] == 21
    assert "Lee: first &lt;b&gt;" in payload["text"]
    assert "Pat: second" in payload["text"]
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["Reply", "Mute thread"]
    assert all(len(b["callback_data"].encode()) < 64 for b in buttons)
    assert buttons[0]["callback_data"] == f"cl:rp:{event.id}"
    assert buttons[1]["callback_data"] == f"cl:mt:{event.id}"
    ping = ClubEventDelivery.objects.get(chat_id="200", kind="ping")
    assert ping.message_id == 901 and ping.error == ""
    # riley (the actor, no card copy) gets a fresh message quoting the card
    riley = _calls_to(post, "100")[0]
    assert "reply_to_message_id" not in riley
    assert "Merlot" in riley["text"]
    assert ClubNotice.objects.filter(sent_at__isnull=True).count() == 0


def test_second_flush_does_not_resend(club):
    club_, event, users = club
    enqueue_comment_notices(_comment(event, users["lee"]).id)
    with patch(POST, return_value=_reply()) as post:
        flush_due_notices(now=_later())
        first = post.call_count
        flush_due_notices(now=_later())
    assert post.call_count == first


def test_reply_target_rejected_resends_fresh_with_quote(club):
    club_, event, users = club
    enqueue_comment_notices(_comment(event, users["lee"], "hi").id)

    def fake(url, json=None, timeout=None):
        if json["chat_id"] == "200" and "reply_to_message_id" in json:
            return _reply(400, description="Bad Request: reply message not found")
        return _reply(mid=910)

    with patch(POST, side_effect=fake) as post:
        flush_due_notices(now=_later())
    sam = _calls_to(post, "200")
    assert len(sam) == 2
    assert "reply_to_message_id" not in sam[1]
    assert "Merlot" in sam[1]["text"]
    assert ClubEventDelivery.objects.get(chat_id="200", kind="ping").message_id == 910


def test_blocked_user_error_recorded_never_raises(club):
    club_, event, users = club
    enqueue_comment_notices(_comment(event, users["lee"]).id)
    blocked = _reply(403, description="Forbidden: bot was blocked")
    with patch(POST, return_value=blocked):
        flush_due_notices(now=_later())
    assert "403" in ClubNotice.objects.get(recipient=users["sam"]).error
    ping = ClubEventDelivery.objects.get(chat_id="200", kind="ping")
    assert ping.message_id is None and "403" in ping.error


def test_network_error_recorded(club):
    import requests

    club_, event, users = club
    enqueue_comment_notices(_comment(event, users["lee"]).id)
    with patch(POST, side_effect=requests.ConnectionError("boom")):
        flush_due_notices(now=_later())
    assert "network" in ClubNotice.objects.get(recipient=users["sam"]).error


def test_mute_after_queue_skips_send(club):
    club_, event, users = club
    enqueue_comment_notices(_comment(event, users["lee"]).id)
    ClubThreadMute.objects.create(user=users["sam"], event=event)
    with patch(POST, return_value=_reply()) as post:
        flush_due_notices(now=_later())
    assert _calls_to(post, "200") == []
    assert "thread_muted" in ClubNotice.objects.get(recipient=users["sam"]).error


def test_unmute_label_not_needed_rate_limit_defers(club):
    club_, event, users = club
    enqueue_comment_notices(_comment(event, users["lee"]).id)
    now = timezone.now()
    later = now + BATCH_WINDOW + timedelta(seconds=10)
    other = ClubEvent.objects.create(club=club_, actor=users["lee"], kind="opened")
    for i in range(RATE_LIMIT_MAX):
        ClubNotice.objects.create(
            recipient=users["sam"], club=club_, event=other,
            dedupe_key=f"x{i}", due_at=now, sent_at=later - timedelta(seconds=5),
        )
    with patch(POST, return_value=_reply()) as post:
        flush_due_notices(now=later)
    assert _calls_to(post, "200") == []
    notice = ClubNotice.objects.get(
        recipient=users["sam"], dedupe_key__startswith="cm:",
    )
    assert notice.sent_at is None and notice.due_at > later


def test_comment_api_queues_pings_and_skips_card_edit(
    client, club, django_capture_on_commit_callbacks,
):
    club_, event, users = club
    url = reverse(
        "api-club-event-comments", kwargs={"pk": club_.id, "event_id": event.id},
    )
    with patch("wine_cellar.apps.club.tasks._send") as send:
        with django_capture_on_commit_callbacks(execute=True):
            response = _auth(client, users["lee"]).post(
                url, data={"text": "great"}, content_type="application/json",
            )
        assert response.status_code == 201
        assert send.call_count == 0
    assert ClubNotice.objects.filter(event=event).count() == 3
    assert not ClubNotice.objects.filter(recipient=users["lee"]).exists()


def test_old_card_edit_path_off_with_threads_on(club):
    club_, event, users = club
    _comment(event, users["lee"])
    with patch("wine_cellar.apps.club.tasks._send") as send:
        refresh_event_comment_card(event.id)
    assert send.call_count == 0
    ServiceConfig.objects.filter(key="feature_club_threads").delete()
    with patch("wine_cellar.apps.club.tasks._send") as send:
        refresh_event_comment_card(event.id)
    assert send.call_count == 3  # cards only


def test_flag_off_comment_api_unchanged(client, club):
    club_, event, users = club
    ServiceConfig.objects.filter(key="feature_club_threads").delete()
    url = reverse(
        "api-club-event-comments", kwargs={"pk": club_.id, "event_id": event.id},
    )
    with patch("wine_cellar.apps.club.tasks._send"):
        response = _auth(client, users["lee"]).post(
            url, data={"text": "great"}, content_type="application/json",
        )
    assert response.status_code == 201
    assert ClubNotice.objects.count() == 0


def test_resolve_maps_ping_back_to_event(client, club):
    club_, event, users = club
    ClubEventDelivery.objects.create(
        event=event, kind="ping", chat_id="200", message_id=777,
    )
    url = reverse("api-club-thread-resolve")
    ok = _auth(client, users["sam"]).get(url, {"chat_id": "200", "message_id": 777})
    assert ok.status_code == 200
    assert ok.json()["event_id"] == event.id
    card = _auth(client, users["sam"]).get(url, {"chat_id": "200", "message_id": 21})
    assert card.json()["event_id"] == event.id
    other = _auth(client, users["lee"]).get(url, {"chat_id": "200", "message_id": 777})
    assert other.status_code == 404
    unknown = _auth(client, users["sam"]).get(url, {"chat_id": "200", "message_id": 5})
    assert unknown.status_code == 404


def test_resolve_and_mute_404_when_flag_off(client, club):
    club_, event, users = club
    ServiceConfig.objects.filter(key="feature_club_threads").delete()
    r = _auth(client, users["sam"]).get(
        reverse("api-club-thread-resolve"), {"chat_id": "200", "message_id": 21},
    )
    assert r.status_code == 404
    m = _auth(client, users["sam"]).post(
        reverse("api-club-thread-mute", kwargs={"event_id": event.id}),
    )
    assert m.status_code == 404


def test_mute_toggle_api(client, club, django_user_model):
    club_, event, users = club
    url = reverse("api-club-thread-mute", kwargs={"event_id": event.id})
    assert _auth(client, users["sam"]).post(url).json() == {"muted": True}
    assert _auth(client, users["sam"]).get(url).json() == {"muted": True}
    assert _auth(client, users["sam"]).post(url).json() == {"muted": False}
    outsider = django_user_model.objects.create_user(username="zed", password="pw")
    assert _auth(client, outsider).post(url).status_code == 404


def test_ask_comments_includes_full_thread(client, club):
    club_, event, users = club
    for i in range(5):
        _comment(event, users["lee"], f"note {i}")
    ask = reverse("api-clubs-ask")
    body = _auth(client, users["sam"]).post(
        ask,
        data={"kind": "comments", "club_id": club_.id, "wine_text": "merlot"},
        content_type="application/json",
    ).json()
    assert body["status"] == "ok"
    assert len(body["threads"]) == 1
    comments = body["threads"][0]["comments"]
    assert len(comments) == 5 and comments[-1]["text"] == "note 4"
    ServiceConfig.objects.filter(key="feature_club_threads").delete()
    body = _auth(client, users["sam"]).post(
        ask,
        data={"kind": "comments", "club_id": club_.id, "wine_text": "merlot"},
        content_type="application/json",
    ).json()
    assert "threads" not in body


def test_thread_copy_en_af():
    from wine_cellar.apps.club.copy import t

    assert t("af", "club.thread.about", line="x").startswith("Oor:")
    assert t("en", "club.thread.btn_mute") == t("af", "club.thread.btn_mute")


def test_comment_merged_after_due_list_is_not_lost(club):
    from wine_cellar.apps.club import threads as th

    club_, event, users = club
    first = _comment(event, users["lee"], "first")
    enqueue_comment_notices(first.id)
    second = _comment(event, users["pat"], "second-late")
    real = th._send_notice

    def merge_then_send(notice, token):
        return real(notice, token)

    # Merge between the due read and the claim: patch the claim's first read.
    orig = th.ClubNotice.objects.select_for_update

    def racing(*a, **k):
        notice = ClubNotice.objects.filter(recipient=users["sam"]).first()
        if notice is not None and second.id not in notice.payload["comment_ids"]:
            ClubNotice.objects.filter(id=notice.id).update(
                payload={"comment_ids": [first.id, second.id]},
            )
        return orig(*a, **k)

    with patch(POST, return_value=_reply(mid=950)) as post, patch.object(
        th.ClubNotice.objects, "select_for_update", side_effect=racing,
    ):
        flush_due_notices(now=_later())
    text = _calls_to(post, "200")[0]["text"]
    assert "first" in text and "second-late" in text


def test_long_ping_is_capped_with_thread_button(club):
    club_, event, users = club
    for i in range(30):
        _comment(event, users["lee"], f"n{i:02d} " + "x" * 270)
    ids = list(ClubComment.objects.values_list("id", flat=True))
    for cid in ids:
        enqueue_comment_notices(cid)
    with patch(POST, return_value=_reply(mid=960)) as post:
        flush_due_notices(now=_later())
    payload = _calls_to(post, "200")[0]
    assert len(payload["text"]) < 4096
    assert "n29" in payload["text"] and "n00" not in payload["text"]
    assert "more. Tap Thread to read them all" in payload["text"]
    first_row = payload["reply_markup"]["inline_keyboard"][0]
    assert first_row[0]["text"].startswith("Thread (")
    assert first_row[0]["callback_data"] == f"cl:th:{club_.id}:{event.id}:0"


def test_other_400_does_not_resend_and_403_recorded(club):
    club_, event, users = club
    enqueue_comment_notices(_comment(event, users["lee"], "hi").id)
    bad = _reply(400, description="Bad Request: can't parse entities")
    with patch(POST, return_value=bad) as post:
        flush_due_notices(now=_later())
    assert len(_calls_to(post, "200")) == 1
    assert "400" in ClubNotice.objects.get(recipient=users["sam"]).error


def test_retracted_event_sends_no_ping(club):
    club_, event, users = club
    enqueue_comment_notices(_comment(event, users["lee"], "hi").id)
    ClubEvent.objects.filter(id=event.id).update(retracted_at=timezone.now())
    with patch(POST, return_value=_reply()) as post:
        flush_due_notices(now=_later())
    assert post.call_count == 0
    assert ClubNotice.objects.get(recipient=users["sam"]).error == "skipped:retracted"


def test_concurrent_enqueue_keeps_one_pending_notice(club):
    from django.db import IntegrityError, transaction

    from wine_cellar.apps.club import threads as th

    club_, event, users = club
    enqueue_comment_notices(_comment(event, users["lee"], "a").id)
    assert ClubNotice.objects.filter(recipient=users["sam"]).count() == 1
    with pytest.raises(IntegrityError), transaction.atomic():
        ClubNotice.objects.create(
            recipient=users["sam"], club=club_, event=event,
            dedupe_key="other", due_at=timezone.now(),
        )
    # Lost race: first merge look-up sees nothing, create hits the constraint.
    second = _comment(event, users["pat"], "b")
    real = th._merge_into_pending
    calls = {"n": 0}

    def fake(event_, comment_, uid):
        calls["n"] += 1
        return False if calls["n"] == 1 else real(event_, comment_, uid)

    with patch.object(th, "_merge_into_pending", side_effect=fake):
        th._queue_for(event, second, users["sam"].id, timezone.now())
    notice = ClubNotice.objects.get(recipient=users["sam"])
    assert second.id in notice.payload["comment_ids"]
