"""P4: club notifications (feature_club_notify): per-club mode, quiet hours,
morning summary and daily recap, the 15-member switch, flags off."""

import datetime
from datetime import timezone as tz
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.club import notify
from wine_cellar.apps.club.models import (
    Club,
    ClubComment,
    ClubEvent,
    ClubMembership,
    ClubNotice,
    ClubThreadMute,
    TasteOff,
)
from wine_cellar.apps.club.relay import deliver_ask
from wine_cellar.apps.club.tasks import (
    deliver_club_event,
    deliver_tasteoff_closed,
    deliver_tasteoff_open,
)
from wine_cellar.apps.club.threads import enqueue_comment_notices, flush_due_notices
from wine_cellar.apps.scanner.models import ServiceConfig, TelegramUser
from wine_cellar.apps.user.models import UserSettings

POST = "wine_cellar.apps.club.threads.requests.post"
SEND = "wine_cellar.apps.club.tasks._send"
FLAGS = (
    "feature_clubs", "feature_club_comments", "feature_club_threads",
    "feature_club_relay", "feature_club_notify", "feature_tasteoffs",
)


def utc(y, mo, d, h=0, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=tz.utc)


def _enable(*keys):
    for key in keys:
        ServiceConfig.objects.update_or_create(key=key, defaults={"value": "on"})


class _Mids:
    def __init__(self):
        self.n = 900

    def __call__(self, *args, **kwargs):
        self.n += 1
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"result": {"message_id": self.n}}
        return response


@pytest.fixture
def club(db):
    User = get_user_model()
    users = {}
    people = (("riley", "100"), ("sam", "200"), ("lee", "300"), ("pat", "400"))
    for name, chat in people:
        user = User.objects.create_user(username=name, password="pw")
        TelegramUser.objects.create(user=user, chat_id=chat, display_name=name.title())
        users[name] = user
    club = Club.objects.create(name="Thursday reds", cellar_visibility="none")
    for user in users.values():
        ClubMembership.objects.create(
            club=club, user=user, role="member", accepted_sharing_version=1,
        )
    ServiceConfig.objects.update_or_create(key="bot_token", defaults={"value": "T"})
    return club, users


def _event(club, actor, wine="Syrah", vintage=2019):
    return ClubEvent.objects.create(
        club=club, actor=actor, kind=ClubEvent.KIND_OPENED,
        wine_name=wine, vintage=vintage, vote="up",
        item_id=ClubEvent.objects.count() + 1,
    )


def _mode(user, club, mode):
    m = ClubMembership.objects.get(user=user, club=club)
    m.notify_mode = mode
    m.save()


NOON = utc(2026, 6, 10, 12, 0)  # 13:00 London (BST): awake
NIGHT = utc(2026, 6, 10, 22, 30)  # 23:30 London: quiet


# -- modes and sync ---------------------------------------------------------
def test_default_mode_instant_then_digest_at_15(club):
    club_, users = club
    m = ClubMembership.objects.get(user=users["riley"], club=club_)
    assert notify.effective_mode(m) == "instant"
    assert notify.effective_mode(m, 14) == "instant"
    assert notify.effective_mode(m, 15) == "digest"


def test_explicit_choice_wins_over_the_size_default(club):
    club_, users = club
    m = ClubMembership.objects.get(user=users["riley"], club=club_)
    m.notify_mode = "instant"
    m.save()
    assert notify.effective_mode(m, 40) == "instant"
    m.notify_mode = "muted"
    m.save()
    assert notify.effective_mode(m, 3) == "muted"


def test_feed_muted_and_notify_mode_stay_in_sync(club):
    club_, users = club
    m = ClubMembership.objects.get(user=users["riley"], club=club_)
    m.feed_muted = True  # old code / the existing mute button
    m.save(update_fields=["feed_muted"])
    m.refresh_from_db()
    assert m.notify_mode == "muted" and m.feed_muted
    m.feed_muted = False  # unmute maps to instant
    m.save(update_fields=["feed_muted"])
    m.refresh_from_db()
    assert m.notify_mode == "instant" and not m.feed_muted
    m.notify_mode = "muted"
    m.save()
    m.refresh_from_db()
    assert m.feed_muted
    m.notify_mode = "digest"
    m.save()
    m.refresh_from_db()
    assert not m.feed_muted
    m.notify_mode = None
    m.save()
    m.refresh_from_db()
    assert m.feed_muted is False and m.notify_mode is None


def test_data_migration_maps_feed_muted(club):
    club_, users = club
    ClubMembership.objects.filter(user=users["pat"]).update(feed_muted=True)
    from importlib import import_module

    from django.apps import apps

    mod = import_module("wine_cellar.apps.club.migrations.0008_club_notify")
    mod.feed_muted_to_mode(apps, None)
    assert ClubMembership.objects.get(user=users["pat"]).notify_mode == "muted"
    assert ClubMembership.objects.get(user=users["sam"]).notify_mode is None


# -- quiet hours and time zones -----------------------------------------------
def _prefs(tzname="Europe/London", start=(22, 0), end=(8, 0), enabled=True):
    return notify.Prefs(
        tzname, enabled, datetime.time(*start), datetime.time(*end),
    )


def test_quiet_window_crossing_midnight():
    p = _prefs()
    # June: London is UTC+1.
    assert notify.in_quiet(p, utc(2026, 6, 10, 21, 0))  # 22:00 local
    assert notify.in_quiet(p, utc(2026, 6, 10, 23, 30))  # 00:30
    assert notify.in_quiet(p, utc(2026, 6, 11, 6, 59))  # 07:59
    assert not notify.in_quiet(p, utc(2026, 6, 11, 7, 0))  # 08:00
    assert not notify.in_quiet(p, utc(2026, 6, 10, 20, 59))  # 21:59


def test_quiet_window_same_day_and_disabled_and_empty():
    p = _prefs(start=(13, 0), end=(15, 0))
    assert notify.in_quiet(p, utc(2026, 1, 5, 14, 0))
    assert not notify.in_quiet(p, utc(2026, 1, 5, 15, 0))
    assert not notify.in_quiet(_prefs(enabled=False), utc(2026, 1, 5, 23, 0))
    assert not notify.in_quiet(_prefs(start=(8, 0), end=(8, 0)), utc(2026, 1, 5, 8, 0))


def test_next_quiet_end_across_dst_europe_london():
    p = _prefs()
    # Spring forward: 2026-03-29 01:00 UTC. 08:00 that morning is BST = 07:00 UTC.
    assert notify.next_quiet_end(p, utc(2026, 3, 28, 23, 30)) == utc(2026, 3, 29, 7, 0)
    # Autumn back: 2026-10-25. 08:00 is GMT = 08:00 UTC.
    got = notify.next_quiet_end(p, utc(2026, 10, 24, 22, 30))
    assert got == utc(2026, 10, 25, 8, 0)
    # Mid-day: the next end is tomorrow morning.
    assert notify.next_quiet_end(p, utc(2026, 6, 10, 12, 0)) == utc(2026, 6, 11, 7, 0)
    assert notify.in_quiet(p, utc(2026, 3, 29, 6, 30))  # 07:30 BST
    assert not notify.in_quiet(p, utc(2026, 3, 29, 7, 30))  # 08:30 BST


def test_other_zone_is_used():
    p = _prefs("Africa/Johannesburg")  # UTC+2
    assert notify.in_quiet(p, utc(2026, 6, 10, 20, 0))  # 22:00 local
    assert not notify.in_quiet(p, utc(2026, 6, 10, 6, 0))  # 08:00 local
    assert notify.valid_tz("Europe/Amsterdam") and not notify.valid_tz("Mars/Base")
    assert notify.valid_tz("") is False


def test_recap_time_moves_out_of_quiet_hours():
    p = _prefs()
    assert notify.next_recap(p, utc(2026, 6, 10, 12, 0)) == utc(2026, 6, 10, 17, 0)
    late = _prefs(start=(17, 0), end=(19, 0))  # 18:00 inside the window
    assert notify.next_recap(late, utc(2026, 6, 10, 12, 0)) == utc(2026, 6, 10, 18, 0)
    # Already past 18:00 local: tomorrow.
    assert notify.next_recap(p, utc(2026, 6, 10, 18, 0)) == utc(2026, 6, 11, 17, 0)


# -- the policy matrix ------------------------------------------------------------
@pytest.mark.parametrize("mode", [None, "instant", "digest", "muted"])
@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("thread_muted", [False, True])
@pytest.mark.parametrize("governance", [False, True])
def test_policy_matrix(club, mode, quiet, thread_muted, governance):
    club_, users = club
    user = users["sam"]
    _mode(user, club_, mode)
    event = _event(club_, users["riley"])
    if thread_muted:
        ClubThreadMute.objects.create(user=user, event=event)
    now = NIGHT if quiet else NOON
    got = notify.decide(user.id, club_.id, event=event, governance=governance, now=now)
    if governance:
        want = notify.SEND
    elif thread_muted or mode == "muted":
        want = notify.DROP
    elif mode == "digest":
        want = notify.DIGEST
    elif quiet:
        want = notify.HOLD
    else:
        want = notify.SEND
    assert got.action == want
    if want == notify.HOLD:
        assert got.due_at == utc(2026, 6, 11, 7, 0)
    if want == notify.DIGEST:
        assert got.due_at == (
            utc(2026, 6, 11, 17, 0) if quiet else utc(2026, 6, 10, 17, 0)
        )


def test_policy_default_mode_goes_digest_in_a_big_club(club, django_user_model):
    club_, users = club
    for i in range(12):
        u = django_user_model.objects.create_user(username=f"x{i}", password="pw")
        ClubMembership.objects.create(club=club_, user=u, accepted_sharing_version=1)
    assert club_.memberships.count() == 16
    got = notify.decide(users["sam"].id, club_.id, now=NOON)
    assert got.action == notify.DIGEST
    _mode(users["sam"], club_, "instant")
    assert notify.decide(users["sam"].id, club_.id, now=NOON).action == notify.SEND


# -- feed cards -------------------------------------------------------------------
def test_flag_off_sends_everyone_and_holds_nothing(club):
    club_, users = club
    _enable("feature_clubs")
    event = _event(club_, users["riley"])
    with patch(SEND, return_value=11) as send, patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=NIGHT,
    ):
        deliver_club_event(event.id)
    assert {c.args[1] for c in send.call_args_list} == {"200", "300", "400"}
    assert ClubNotice.objects.count() == 0


def test_card_during_quiet_hours_is_held_then_summarised(club):
    club_, users = club
    _enable(*FLAGS)
    _mode(users["lee"], club_, "muted")
    event = _event(club_, users["riley"])
    with patch(SEND, return_value=11) as send, patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=NIGHT,
    ):
        deliver_club_event(event.id)
    assert send.call_count == 0
    held = ClubNotice.objects.filter(kind="card")
    assert {n.recipient.username for n in held} == {"sam", "pat"}  # not lee, not riley
    assert all(n.hold == "quiet" and n.due_at == utc(2026, 6, 11, 7, 0) for n in held)
    # Still quiet: nothing goes.
    with patch(POST, side_effect=_Mids()) as post:
        assert notify.send_due_summaries(now=utc(2026, 6, 11, 6, 0)) == 0
        assert post.call_count == 0
        # Morning: one message each, then nothing more.
        assert notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1)) == 2
        assert notify.send_due_summaries(now=utc(2026, 6, 11, 7, 2)) == 0
    assert post.call_count == 2
    body = post.call_args_list[0].kwargs["json"]
    assert "Thursday reds" in body["text"] and "Syrah" in body["text"]
    assert ClubNotice.objects.filter(sent_at__isnull=True).count() == 0
    assert UserSettings.objects.get(user=users["sam"]).last_summary_at is not None


def test_card_retracted_before_the_summary_is_dropped(club):
    club_, users = club
    _enable(*FLAGS)
    event = _event(club_, users["riley"])
    with patch(SEND, return_value=11), patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=NIGHT,
    ):
        deliver_club_event(event.id)
        event.retracted_at = NIGHT
        event.save()
        deliver_club_event(event.id)  # put-back: nobody who never saw it
    with patch(POST, side_effect=_Mids()) as post:
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    assert post.call_count == 0
    assert ClubNotice.objects.filter(sent_at__isnull=True).count() == 0


def test_instant_awake_sends_digest_member_gets_recap(club):
    club_, users = club
    _enable(*FLAGS)
    _mode(users["sam"], club_, "digest")
    event = _event(club_, users["riley"])
    with patch(SEND, return_value=11) as send, patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=NOON,
    ):
        deliver_club_event(event.id)
    assert {c.args[1] for c in send.call_args_list} == {"300", "400"}
    n = ClubNotice.objects.get(recipient=users["sam"])
    assert n.hold == "recap" and n.due_at == utc(2026, 6, 10, 17, 0)
    with patch(POST, side_effect=_Mids()) as post:
        assert notify.send_due_summaries(now=utc(2026, 6, 10, 16, 59)) == 0
        assert notify.send_due_summaries(now=utc(2026, 6, 10, 17, 0)) == 1
    assert "recap" in post.call_args.kwargs["json"]["text"].lower()


def test_governance_joined_card_is_not_held(club):
    club_, users = club
    _enable(*FLAGS)
    _mode(users["sam"], club_, "digest")
    event = ClubEvent.objects.create(club=club_, actor=users["riley"], kind="joined")
    with patch(SEND, return_value=11) as send, patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=NIGHT,
    ):
        deliver_club_event(event.id)
    assert {c.args[1] for c in send.call_args_list} == {"200", "300", "400"}
    assert ClubNotice.objects.count() == 0


# -- reply pings, asks, reactions, taste-offs ------------------------------------
def test_reply_ping_held_in_quiet_hours_and_merged_into_summary(club):
    club_, users = club
    _enable(*FLAGS)
    event = _event(club_, users["riley"])
    c = ClubComment.objects.create(
        event=event, user=users["sam"], text="Lovely <b>one</b>",
    )
    enqueue_comment_notices(c.id)
    ClubNotice.objects.update(due_at=utc(2026, 6, 10, 22, 0))
    with patch(POST, side_effect=_Mids()) as post:
        flush_due_notices(now=NIGHT)
    assert post.call_count == 0
    assert set(ClubNotice.objects.values_list("hold", flat=True)) == {"quiet"}
    # A later reply merges into the held row.
    c2 = ClubComment.objects.create(event=event, user=users["lee"], text="Agreed")
    enqueue_comment_notices(c2.id)
    pat_row = ClubNotice.objects.get(recipient=users["pat"])
    assert sorted(pat_row.payload["comment_ids"]) == sorted([c.id, c2.id])
    with patch(POST, side_effect=_Mids()) as post:
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 5))
    texts = {x.kwargs["json"]["chat_id"]: x.kwargs["json"] for x in post.call_args_list}
    assert "2 new replies" in texts["400"]["text"]
    buttons = [b for r in texts["400"]["reply_markup"]["inline_keyboard"] for b in r]
    assert buttons[0]["text"].startswith("Thread (2)")
    assert buttons[0]["callback_data"] == f"cl:th:{club_.id}:{event.id}:0"


def test_reply_ping_digest_member_goes_to_recap_muted_gets_nothing(club):
    club_, users = club
    _enable(*FLAGS)
    _mode(users["pat"], club_, "digest")
    _mode(users["lee"], club_, "muted")
    event = _event(club_, users["riley"])
    c = ClubComment.objects.create(event=event, user=users["sam"], text="Hi")
    enqueue_comment_notices(c.id)
    ClubNotice.objects.update(due_at=NOON)
    with patch(POST, side_effect=_Mids()) as post:
        flush_due_notices(now=NOON)
    sent_to = {x.kwargs["json"]["chat_id"] for x in post.call_args_list}
    assert sent_to == {"100"}  # riley, instant and awake
    assert ClubNotice.objects.get(recipient=users["pat"]).hold == "recap"
    assert not ClubNotice.objects.filter(recipient=users["lee"]).exists()


def test_thread_muted_member_gets_no_held_item(club):
    club_, users = club
    _enable(*FLAGS)
    event = _event(club_, users["riley"])
    c = ClubComment.objects.create(event=event, user=users["sam"], text="Hi")
    enqueue_comment_notices(c.id)
    ClubThreadMute.objects.create(user=users["pat"], event=event)  # muted after
    ClubNotice.objects.exclude(recipient=users["riley"]).update(due_at=NIGHT)
    with patch(POST, side_effect=_Mids()):
        flush_due_notices(now=NIGHT)
    row = ClubNotice.objects.get(recipient=users["pat"])
    assert row.error == "skipped:thread_muted"


def test_ask_is_held_in_quiet_hours(club):
    club_, users = club
    _enable(*FLAGS)
    ev = ClubEvent.objects.create(
        club=club_, actor=users["riley"], kind="ask", payload={"text": "Port?"},
    )
    with patch(POST, side_effect=_Mids()) as post, patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=NIGHT,
    ):
        assert deliver_ask(ev.id) == 0
    assert post.call_count == 0
    assert ClubNotice.objects.filter(kind="ask", hold="quiet").count() == 3
    with patch(POST, side_effect=_Mids()) as post:
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    j = post.call_args_list[0].kwargs["json"]
    assert "asks the club: Port?" in j["text"]
    first = j["reply_markup"]["inline_keyboard"][0][0]
    assert first["callback_data"] == f"cl:rp:{ev.id}"


def test_reaction_notice_follows_quiet_hours(club):
    club_, users = club
    _enable(*FLAGS, "feature_club_reactions")
    event = _event(club_, users["riley"])
    from wine_cellar.apps.club.reactions import queue_owner_notice

    with patch("wine_cellar.apps.club.threads._schedule_flush"):
        queue_owner_notice(event, users["sam"], emoji="\U0001F60D")
    ClubNotice.objects.update(due_at=NIGHT)
    with patch(POST, side_effect=_Mids()) as post:
        flush_due_notices(now=NIGHT)
    assert post.call_count == 0
    assert ClubNotice.objects.get().hold == "quiet"
    with patch(POST, side_effect=_Mids()) as post:
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    assert "Sam reacted" in post.call_args.kwargs["json"]["text"]


def test_tasteoff_dms_are_held_and_host_is_exempt(club):
    club_, users = club
    _enable(*FLAGS)
    to = TasteOff.objects.create(
        club=club_, host=users["riley"], title="Blind Syrah",
        status=TasteOff.STATUS_SCORING,
    )
    with patch(SEND, return_value=11) as send, patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=NIGHT,
    ):
        deliver_tasteoff_open(to.id)
    assert {c.args[1] for c in send.call_args_list} == {"100"}
    assert ClubNotice.objects.filter(kind="tasteoff", hold="quiet").count() == 3
    with patch(SEND, return_value=11), patch(
        "wine_cellar.apps.club.notify.timezone.now", return_value=NIGHT,
    ):
        to.status = TasteOff.STATUS_CLOSED
        to.save()
        deliver_tasteoff_closed(to.id)
    with patch(POST, side_effect=_Mids()) as post:
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    text = post.call_args.kwargs["json"]["text"]
    assert "Blind Syrah has closed" in text and "is open" not in text


# -- summary shape ---------------------------------------------------------------
def test_summary_pagination_stays_under_telegram_limit(club):
    club_, users = club
    _enable(*FLAGS)
    for i in range(120):
        ev = _event(club_, users["riley"], wine="Wine " + "x" * 60 + str(i))
        notify.hold_notice(
            users["sam"].id, club_, ev, "card",
            notify.Decision(notify.HOLD, utc(2026, 6, 11, 7, 0)), f"card:{ev.id}:s",
        )
    with patch(POST, side_effect=_Mids()) as post:
        assert notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1)) == 1
    assert post.call_count == 1
    body = post.call_args.kwargs["json"]["text"]
    assert len(body) < 4096
    assert "more, open /club" in body
    assert ClubNotice.objects.filter(sent_at__isnull=True).count() == 0


def test_summary_buttons_cap_and_busiest_first(club):
    club_, users = club
    _enable(*FLAGS)
    for i in range(9):
        ev = _event(club_, users["riley"], wine=f"Wine{i}")
        ids = [
            ClubComment.objects.create(event=ev, user=users["lee"], text="c").id
            for _ in range(i + 1)
        ]
        ClubNotice.objects.create(
            recipient=users["sam"], club=club_, event=ev, kind="comment", hold="quiet",
            dedupe_key=f"cm:{ev.id}", payload={"comment_ids": ids},
            due_at=utc(2026, 6, 11, 7, 0),
        )
    with patch(POST, side_effect=_Mids()) as post:
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    rows = post.call_args.kwargs["json"]["reply_markup"]["inline_keyboard"]
    buttons = [b for r in rows for b in r]
    assert len(buttons) == 6
    assert buttons[0]["text"].startswith("Thread (9)")
    assert all(len(b["callback_data"].encode()) < 64 for b in buttons)


def test_summary_spans_clubs_in_one_message(club):
    club_, users = club
    _enable(*FLAGS)
    other = Club.objects.create(name="Sunday whites", cellar_visibility="none")
    ClubMembership.objects.create(
        club=other, user=users["sam"], accepted_sharing_version=1,
    )
    for c in (club_, other):
        ev = _event(c, users["riley"], wine=f"W{c.id}")
        notify.hold_notice(
            users["sam"].id, c, ev, "card",
            notify.Decision(notify.HOLD, utc(2026, 6, 11, 7, 0)), f"card:{ev.id}:s",
        )
    with patch(POST, side_effect=_Mids()) as post:
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    assert post.call_count == 1
    text = post.call_args.kwargs["json"]["text"]
    assert "Thursday reds" in text and "Sunday whites" in text


def test_telegram_error_is_recorded_not_raised(club):
    club_, users = club
    _enable(*FLAGS)
    ev = _event(club_, users["riley"])
    notify.hold_notice(
        users["sam"].id, club_, ev, "card",
        notify.Decision(notify.HOLD, utc(2026, 6, 11, 7, 0)), "card:x",
    )
    bad = MagicMock(status_code=403)
    bad.json.return_value = {"description": "Forbidden: bot was blocked"}
    with patch(POST, return_value=bad):
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    row = ClubNotice.objects.get()
    assert row.sent_at is not None and "403" in row.error


def test_summaries_are_off_with_the_flag(club):
    club_, users = club
    ev = _event(club_, users["riley"])
    ClubNotice.objects.create(
        recipient=users["sam"], club=club_, event=ev, kind="card", hold="quiet",
        dedupe_key="k", due_at=NOON,
    )
    with patch(POST, side_effect=_Mids()) as post:
        assert notify.send_due_summaries(now=utc(2026, 6, 11, 9, 0)) == 0
    assert post.call_count == 0


# -- crossing 15 ------------------------------------------------------------------
def _grow(club_, n):
    User = get_user_model()
    for i in range(n):
        u = User.objects.create_user(username=f"m{i}", password="pw")
        TelegramUser.objects.create(user=u, chat_id=f"9{i:03d}", display_name=f"M{i}")
        ClubMembership.objects.create(club=club_, user=u, accepted_sharing_version=1)


def test_crossing_15_messages_only_default_members_once(club):
    club_, users = club
    _enable(*FLAGS)
    _mode(users["riley"], club_, "instant")  # explicit: not asked
    _grow(club_, 11)
    assert club_.memberships.count() == 15
    with patch(POST, side_effect=_Mids()) as post:
        sent = notify.announce_crossing(club_.id)
        again = notify.announce_crossing(club_.id)
    assert again == 0 and sent == 14
    chats = {c.kwargs["json"]["chat_id"] for c in post.call_args_list}
    assert "100" not in chats and "200" in chats
    j = post.call_args_list[0].kwargs["json"]
    assert "now has 15 members" in j["text"]
    labels = [b["text"] for b in j["reply_markup"]["inline_keyboard"][0]]
    assert labels == ["Keep every update", "Recap is fine"]
    assert [b["callback_data"] for b in j["reply_markup"]["inline_keyboard"][0]] == [
        f"cl:nk:{club_.id}", f"cl:nr:{club_.id}",
    ]
    assert Club.objects.get(id=club_.id).notify_crossing_sent_at is not None


def test_crossing_not_at_14_or_16_or_flag_off(club):
    club_, users = club
    _grow(club_, 10)
    with patch(POST, side_effect=_Mids()) as post:
        assert notify.announce_crossing(club_.id) == 0  # flag off
        _enable(*FLAGS)
        assert notify.announce_crossing(club_.id) == 0  # 14 members
        User = get_user_model()
        for name in ("z1", "z2"):
            ClubMembership.objects.create(
                club=club_, user=User.objects.create_user(username=name, password="p"),
                accepted_sharing_version=1,
            )
        assert notify.announce_crossing(club_.id) == 0  # 16
    assert post.call_count == 0


# -- API -------------------------------------------------------------------------
def _auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    return client


def test_notify_api_flag_off_404_and_on_roundtrip(client, club):
    club_, users = club
    url = reverse("api-club-notify", kwargs={"pk": club_.id})
    _enable("feature_clubs")
    assert _auth(client, users["sam"]).get(url).status_code == 404
    _enable("feature_club_notify")
    body = _auth(client, users["sam"]).get(url).json()
    assert body["mode"] is None and body["effective"] == "instant"
    assert body["quiet"] == {
        "enabled": True, "start": "22:00", "end": "08:00", "tz": "Europe/London",
    }
    r = client.patch(
        url, data={
            "mode": "digest", "quiet_start": "23:00", "quiet_end": "07:00",
            "tz": "Africa/Johannesburg", "quiet_enabled": False,
        }, content_type="application/json",
    )
    body = r.json()
    assert body["mode"] == "digest" and body["quiet"]["tz"] == "Africa/Johannesburg"
    assert body["quiet"]["enabled"] is False and body["quiet"]["start"] == "23:00"
    bad = client.patch(url, data={"tz": "Mars/Base"}, content_type="application/json")
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_tz"
    bad = client.patch(url, data={"mode": "loud"}, content_type="application/json")
    assert bad.status_code == 400
    client.patch(url, data={"mode": "muted"}, content_type="application/json")
    assert ClubMembership.objects.get(user=users["sam"], club=club_).feed_muted
    other = get_user_model().objects.create_user(username="out", password="p")
    assert _auth(client, other).get(url).status_code == 404


def test_old_mute_button_maps_to_muted_and_instant(client, club):
    club_, users = club
    _enable("feature_clubs")
    url = reverse("api-club-membership", kwargs={"pk": club_.id})
    _auth(client, users["sam"]).patch(
        url, data={"feed_muted": True}, content_type="application/json",
    )
    m = ClubMembership.objects.get(user=users["sam"], club=club_)
    assert m.notify_mode == "muted"
    client.patch(url, data={"feed_muted": False}, content_type="application/json")
    m.refresh_from_db()
    assert m.notify_mode == "instant" and not m.feed_muted


# -- what did I miss ---------------------------------------------------------------
def test_ask_missed_needs_the_flag_and_lists_held_items(club):
    from wine_cellar.apps.club.ask import answer_club_ask

    club_, users = club
    _enable("feature_clubs")
    assert answer_club_ask(users["sam"], {"kind": "missed"}) == {"status": "fallback"}
    _enable(*FLAGS)
    ev = _event(club_, users["riley"])
    notify.hold_notice(
        users["sam"].id, club_, ev, "card",
        notify.Decision(notify.HOLD, utc(2026, 6, 11, 7, 0)), "card:m",
    )
    out = answer_club_ask(users["sam"], {"kind": "missed", "club_id": club_.id})
    assert out["status"] == "ok" and out["state"] == "held"
    assert "Syrah" in out["items"][0]["text"]
    empty = answer_club_ask(users["pat"], {"kind": "missed", "club_id": club_.id})
    assert empty["items"] == [] and empty["state"] == "none"
    # After the summary goes out, the recap is what answers.
    with patch(POST, side_effect=_Mids()):
        notify.send_due_summaries(now=utc(2026, 6, 11, 7, 1))
    later = utc(2026, 6, 11, 9, 0)
    with patch("wine_cellar.apps.club.notify.timezone.now", return_value=later):
        out = answer_club_ask(users["sam"], {"kind": "missed", "club_id": club_.id})
    assert out["state"] == "recap" and len(out["items"]) == 1
