"""Club notification policy (flag ``feature_club_notify``).

Every club push except governance goes through ``decide``: the member's mode
for that club (instant / digest / muted, default instant or digest from
DIGEST_MEMBERS members), a muted thread, and the member's quiet hours.

* SEND   - deliver now, as before.
* HOLD   - instant mode during quiet hours: kept as a ClubNotice row and sent
           in the morning summary when quiet hours end.
* DIGEST - digest mode: kept as a row and sent in the daily recap.
* DROP   - muted club or muted thread: nothing is pushed (it stays in /club).

The beat task ``send_club_summaries`` sends one message per member that
holds every due row, grouped by club. Governance messages (dismantle votes,
sharing-change Stay/Leave, ownership handover, invites and joins) never come
through here. With the flag off nothing here is consulted.
"""

from __future__ import annotations

import datetime
import functools
import html
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from wine_cellar.apps.club.copy import locale_for_user, t
from wine_cellar.apps.club.models import (
    Club,
    ClubComment,
    ClubEvent,
    ClubEventDelivery,
    ClubMembership,
    ClubNotice,
    ClubThreadMute,
)
from wine_cellar.apps.scanner.flags import flag_on
from wine_cellar.apps.scanner.models import TelegramUser

logger = logging.getLogger(__name__)

DIGEST_MEMBERS = 15
RECAP_HOUR = 18  # local time of the daily recap, moved out of quiet hours
SUMMARY_MAX_CHARS = 3900  # Telegram caps a message at 4096
SUMMARY_MAX_BUTTONS = 6
DEFAULT_START = datetime.time(22, 0)
DEFAULT_END = datetime.time(8, 0)

SEND = "send"
HOLD = "hold"
DIGEST = "digest"
DROP = "drop"

COMMON_ZONES = (
    "Africa/Johannesburg",
    "Europe/London",
    "Europe/Amsterdam",
    "America/New_York",
    "Australia/Sydney",
)
QUIET_PRESETS = ((22, 8), (23, 7), (21, 7))

# Default zone by UserSettings.country when the member never set one.
COUNTRY_TZ = {
    "ZA": "Africa/Johannesburg", "GB": "Europe/London", "IE": "Europe/Dublin",
    "NL": "Europe/Amsterdam", "BE": "Europe/Brussels", "LU": "Europe/Luxembourg",
    "FR": "Europe/Paris", "DE": "Europe/Berlin", "AT": "Europe/Vienna",
    "CH": "Europe/Zurich", "IT": "Europe/Rome", "ES": "Europe/Madrid",
    "PT": "Europe/Lisbon", "DK": "Europe/Copenhagen", "SE": "Europe/Stockholm",
    "NO": "Europe/Oslo", "FI": "Europe/Helsinki", "PL": "Europe/Warsaw",
    "CZ": "Europe/Prague", "GR": "Europe/Athens", "HU": "Europe/Budapest",
    "RO": "Europe/Bucharest", "US": "America/New_York", "CA": "America/Toronto",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland", "NA": "Africa/Windhoek",
    "BW": "Africa/Gaborone", "ZW": "Africa/Harare", "KE": "Africa/Nairobi",
}


def notify_enabled() -> bool:
    return flag_on("feature_clubs") and flag_on("feature_club_notify")


# -- modes --------------------------------------------------------------------
def effective_mode(membership, member_count=None) -> str:
    """The mode that applies: an explicit choice wins, else instant, or
    digest once the club has DIGEST_MEMBERS members."""
    if membership.feed_muted:  # a write that skipped save() still mutes
        return ClubMembership.NOTIFY_MUTED
    if membership.notify_mode:
        return membership.notify_mode
    if member_count is None:
        member_count = ClubMembership.objects.filter(
            club_id=membership.club_id,
        ).count()
    if member_count >= DIGEST_MEMBERS:
        return ClubMembership.NOTIFY_DIGEST
    # Sticky: once the club has crossed (and the member was told), the default
    # stays digest even if the club later drops below DIGEST_MEMBERS.
    if Club.objects.filter(
        id=membership.club_id, notify_crossing_sent_at__isnull=False,
    ).exists():
        return ClubMembership.NOTIFY_DIGEST
    return ClubMembership.NOTIFY_INSTANT


def set_mode(membership, mode) -> None:
    membership.notify_mode = mode if mode in dict(ClubMembership.NOTIFY_MODES) else None
    membership.save(update_fields=["notify_mode"])


# -- quiet hours ----------------------------------------------------------------
@dataclass
class Prefs:
    tz: str
    enabled: bool
    start: datetime.time
    end: datetime.time

    @property
    def zone(self) -> ZoneInfo:
        return _zone(self.tz)


def _zone(name) -> ZoneInfo:
    for candidate in (name, settings.TIME_ZONE):
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
            continue
    return ZoneInfo("UTC")


@functools.lru_cache(maxsize=1)
def _zone_index():
    import zoneinfo

    keys = sorted(zoneinfo.available_timezones())
    full = {k.lower(): k for k in keys}
    city: dict = {}
    for k in keys:
        if "/" in k:
            city.setdefault(k.rsplit("/", 1)[1].lower(), k)
    return full, city


def resolve_tz(name):
    """Canonical zoneinfo key for what a member typed, or None. Case does not
    matter, and a bare city ('johannesburg', 'New York') finds its key."""
    name = str(name or "").strip()
    if not name or len(name) > 64:
        return None
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        pass
    full, city = _zone_index()
    low = name.lower().replace(" ", "_")
    return full.get(low) or city.get(low)


def valid_tz(name) -> bool:
    return resolve_tz(name) is not None


def _service_default_tz() -> str:
    from wine_cellar.apps.scanner.models import ServiceConfig

    value = (
        ServiceConfig.objects.filter(key="default_time_zone")
        .values_list("value", flat=True).first()
    )
    return str(value or "").strip()


def default_tz(row, user=None) -> str:
    """Zone for a member with no explicit quiet_tz: country, then ServiceConfig
    default_time_zone, then settings.TIME_ZONE. Language never decides it."""
    country = str(getattr(row, "country", "") or "").upper()
    if COUNTRY_TZ.get(country):
        return COUNTRY_TZ[country]
    configured = resolve_tz(_service_default_tz())
    return configured or settings.TIME_ZONE


def tz_city(name) -> str:
    return str(name or "").rsplit("/", 1)[-1].replace("_", " ")


def prefs_for(user_id) -> Prefs:
    from wine_cellar.apps.user.models import UserSettings

    row = UserSettings.objects.filter(user_id=user_id).first()
    if row is None:
        return Prefs(default_tz(None), True, DEFAULT_START, DEFAULT_END)
    # Equal start and end is "no window": report it as off everywhere.
    return Prefs(
        row.quiet_tz or default_tz(row),
        bool(row.quiet_enabled) and row.quiet_start != row.quiet_end,
        row.quiet_start, row.quiet_end,
    )


def in_quiet(prefs: Prefs, now) -> bool:
    """True when ``now`` falls inside the member's quiet window (which may
    cross midnight). Equal start and end mean no window."""
    if not prefs.enabled or prefs.start == prefs.end:
        return False
    local = now.astimezone(prefs.zone).time().replace(tzinfo=None)
    if prefs.start < prefs.end:
        return prefs.start <= local < prefs.end
    return local >= prefs.start or local < prefs.end


def _next_at(prefs: Prefs, now, at: datetime.time):
    """Next UTC instant strictly after ``now`` at wall-clock ``at`` in the
    member's zone (date arithmetic on naive dates, so DST is respected)."""
    zone = prefs.zone
    day = now.astimezone(zone).date()
    for _ in range(3):
        cand = datetime.datetime.combine(day, at).replace(tzinfo=zone)
        cand_utc = cand.astimezone(datetime.timezone.utc)
        if cand_utc > now:
            return cand_utc
        day += datetime.timedelta(days=1)
    return now + datetime.timedelta(days=1)


def next_quiet_end(prefs: Prefs, now):
    return _next_at(prefs, now, prefs.end)


def next_recap(prefs: Prefs, now):
    """Next daily recap time, pushed to the end of quiet hours if it lands
    inside them."""
    due = _next_at(prefs, now, datetime.time(RECAP_HOUR, 0))
    if in_quiet(prefs, due):
        due = next_quiet_end(prefs, due)
    return due


# -- the policy -------------------------------------------------------------------
@dataclass
class Decision:
    action: str
    due_at: object = None
    reason: str = ""


def decide(
    user_id, club_id, *, event=None, governance=False, now=None,
    membership=None, member_count=None, join=False, bypass_digest=False,
) -> Decision:
    """What to do with one push for one member. See the module docstring.

    ``join``: join cards and the 15-member message skip the digest (they are
    sent, or held in quiet hours) but a muted club still drops them.
    ``bypass_digest``: replies to the member's own content skip the digest
    and go out promptly; quiet hours and thread mutes still apply."""
    if governance:
        return Decision(SEND, reason="governance")
    now = now or timezone.now()
    if membership is None:
        membership = ClubMembership.objects.filter(
            user_id=user_id, club_id=club_id,
        ).first()
    if membership is None:
        return Decision(DROP, reason="not_member")
    if event is not None and ClubThreadMute.objects.filter(
        user_id=user_id, event_id=event.id,
    ).exists():
        return Decision(DROP, reason="thread_muted")
    mode = effective_mode(membership, member_count)
    if mode == ClubMembership.NOTIFY_MUTED:
        return Decision(DROP, reason="muted")
    prefs = prefs_for(user_id)
    if mode == ClubMembership.NOTIFY_DIGEST and not (join or bypass_digest):
        return Decision(DIGEST, next_recap(prefs, now), "digest")
    if in_quiet(prefs, now):
        return Decision(HOLD, next_quiet_end(prefs, now), "quiet")
    return Decision(SEND, reason="instant")


def is_own_content_reply(user_id, event) -> bool:
    """A reply in a thread the member owns (their bottle or ask) or has
    commented in."""
    if event is None:
        return False
    if event.actor_id == user_id:
        return True
    return ClubComment.objects.filter(event=event, user_id=user_id).exists()


def hold_notice(recipient_id, club, event, kind, decision, key, payload=None):
    """Keep a push for the summary. Deduped on ``key``; never raises."""
    hold = (
        ClubNotice.HOLD_RECAP if decision.action == DIGEST else ClubNotice.HOLD_QUIET
    )
    try:
        with transaction.atomic():
            row, created = ClubNotice.objects.get_or_create(
                dedupe_key=key[:120],
                defaults={
                    "recipient_id": recipient_id,
                    "club": club,
                    "event": event,
                    "kind": kind,
                    "hold": hold,
                    "payload": payload or {},
                    "due_at": decision.due_at or timezone.now(),
                },
            )
        return row if created else None
    except IntegrityError:
        return None


def has_held(user_id, event_id, kind) -> bool:
    return ClubNotice.objects.filter(
        recipient_id=user_id, event_id=event_id, kind=kind,
    ).exclude(hold="").exists()


# -- summary ------------------------------------------------------------------
def _short(text, n=28) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _line_for(notice, loc):
    """(html line, plain text, thread_count, event) for one held row, or None
    when the row is dropped (retracted, muted, nothing left)."""
    from wine_cellar.apps.club.reactions import render_notice
    from wine_cellar.apps.club.threads import _event_line

    event = notice.event
    user = notice.recipient
    if notice.kind == ClubNotice.KIND_TASTEOFF:
        return _tasteoff_line(notice, loc)
    if notice.kind == ClubNotice.KIND_CROSSING:
        text = t(
            loc, "club.notify.crossing",
            club=html.escape(notice.club.name), n=DIGEST_MEMBERS,
        )
        return (text, html.unescape(text), 0, None)
    if event is None or event.retracted:
        return None
    if ClubThreadMute.objects.filter(user=user, event=event).exists():
        return None
    if notice.kind == ClubNotice.KIND_CARD:
        if event.kind == ClubEvent.KIND_JOINED:
            from wine_cellar.apps.club.services import display_name

            line = t(
                loc, "club.feed.joined",
                name=html.escape(display_name(event.actor)) if event.actor_id else "",
            )
            return (line, html.unescape(line), 0, None)
        if event.kind == ClubEvent.KIND_STAFF_NOTE:
            note = html.escape((event.payload or {}).get("text") or "")
            return ("\U0001F30D " + note, note, 0, event)
        line = _event_line(event, loc)
        return (line, html.unescape(line), 0, event)
    if notice.kind == ClubNotice.KIND_ASK:
        line = _event_line(event, loc)
        return (line, html.unescape(line), 0, event)
    if notice.kind == ClubNotice.KIND_COMMENT:
        ids = (notice.payload or {}).get("comment_ids") or []
        n = ClubComment.objects.filter(id__in=ids).exclude(user_id=user.id).count()
        if not n:
            return None
        about = _event_line(event, loc)
        line = t(loc, "club.notify.replies", n=n, line=about)
        return (line, html.unescape(line), n, event)
    if notice.kind == ClubNotice.KIND_REACTION:
        text = render_notice(notice, loc)
        if not text:
            return None
        line = "\U0001F60D " + text.replace("\n", " ")
        return (line, html.unescape(line), 0, event)
    return None


def _tasteoff_line(notice, loc):
    from wine_cellar.apps.club.models import TasteOff

    payload = notice.payload or {}
    title = html.escape(str(payload.get("title") or ""))
    phase = payload.get("phase")
    if phase == "open":
        status = TasteOff.objects.filter(
            id=payload.get("tasteoff_id"),
        ).values_list("status", flat=True).first()
        if status != TasteOff.STATUS_SCORING:
            return None
        line = t(loc, "club.notify.tasteoff_open", title=title)
    else:
        line = t(loc, "club.notify.tasteoff_closed", title=title)
    return (line, html.unescape(line), 0, None)


def build_summary(rows, loc, threads=True, quiet=True, quiet_line=""):
    """Compose one summary message from held rows (already claimed).

    Returns (text, markup | None, shown, hidden). Stays under
    SUMMARY_MAX_CHARS; whatever does not fit becomes '+N more, open /club'.
    ``quiet_line`` (first summary a member ever gets) is added under the
    heading with a [Change] button; every summary has a [Notifications] one.
    """
    by_club: dict = {}
    for notice in rows:
        built = _line_for(notice, loc)
        if built is None:
            continue
        by_club.setdefault(notice.club_id, (notice.club, []))[1].append(
            (notice, built),
        )
    head = t(loc, "club.notify.head_quiet" if quiet else "club.notify.head_recap")
    if quiet_line:
        head += "\n" + quiet_line
    total = sum(len(v[1]) for v in by_club.values())
    if not total:
        return "", None, 0, 0
    reserve = len(t(loc, "club.notify.more", n=9999)) + 4
    budget = SUMMARY_MAX_CHARS - len(head) - reserve
    blocks, used, shown = [], 0, 0
    buttons = []  # (weight, label, callback_data)
    first_club_id = None
    stop = False
    for club, items in sorted(by_club.values(), key=lambda v: v[0].name.lower()):
        title = f"<b>{html.escape(club.name)}</b>"
        lines = []
        for notice, (line, _plain, n, event) in items:
            cost = len(line) + 1 + (len(title) + 2 if not lines else 0)
            if used + cost > budget:
                stop = True
                break
            if not lines:
                used += len(title) + 2
                if first_club_id is None:
                    first_club_id = club.id
            lines.append(line)
            used += len(line) + 1
            shown += 1
            if notice.kind == ClubNotice.KIND_TASTEOFF:
                if (notice.payload or {}).get("phase") == "open":
                    buttons.append((
                        1, f"{t('en', 'club.notify.btn_score')} "
                        f"{_short((notice.payload or {}).get('title'))}".strip(),
                        f"cl:to:{club.id}",
                    ))
            elif notice.kind == ClubNotice.KIND_CROSSING:
                for key, action in (("btn_keep", "nk"), ("btn_recap", "nr")):
                    buttons.append(
                        (9, t("en", f"club.notify.{key}"), f"cl:{action}:{club.id}"),
                    )
            elif threads and event is not None:
                name = _short(
                    event.wine_name or (event.payload or {}).get("text") or "",
                )
                if notice.kind == ClubNotice.KIND_COMMENT and n:
                    buttons.append((
                        n, f"{t(loc, 'club.thread.btn_view', n=n)} {name}".strip(),
                        f"cl:th:{club.id}:{event.id}:0",
                    ))
                elif notice.kind == ClubNotice.KIND_ASK:
                    buttons.append((
                        0, f"{t(loc, 'club.thread.btn_reply')} {name}".strip(),
                        f"cl:rp:{event.id}",
                    ))
        if lines:
            blocks.append(title + "\n" + "\n".join(lines))
        if stop:
            break
    hidden = total - shown
    text = head + "\n\n" + "\n\n".join(blocks)
    if hidden:
        text += "\n\n" + t(loc, "club.notify.more", n=hidden)
    rows_kb, seen = [], set()
    buttons.sort(key=lambda b: -b[0])
    for _weight, label, cb in buttons:
        if cb in seen or len(seen) >= SUMMARY_MAX_BUTTONS:
            continue
        seen.add(cb)
        rows_kb.append([{"text": label[:60], "callback_data": cb}])
    if first_club_id is not None:
        settings_cb = f"cl:ns:{first_club_id}"
        if quiet_line:
            rows_kb.append([{
                "text": t("en", "club.notify.btn_change"), "callback_data": settings_cb,
            }])
        rows_kb.append([{
            "text": t("en", "club.notify.btn"), "callback_data": settings_cb,
        }])
    markup = {"inline_keyboard": rows_kb} if rows_kb else None
    return text, markup, shown, hidden


def _eligible(rows, now):
    return [
        r for r in rows
        if r.hold == ClubNotice.HOLD_QUIET or r.due_at <= now
    ]


def send_user_summary(user_id, token, now) -> int:
    """Claim and send one member's due rows as one message. Returns rows
    stamped. Telegram errors are recorded on the rows, never raised."""
    from wine_cellar.apps.club.threads import _post, threads_enabled
    from wine_cellar.apps.user.models import UserSettings

    prefs = prefs_for(user_id)
    if in_quiet(prefs, now):
        return 0
    # Delivery is at-most-once on purpose: rows are stamped sent before the
    # post, so a crash or a second beat run never sends a summary twice. A
    # failed post is recorded on the rows (error), not retried.
    with transaction.atomic():
        pending = list(
            ClubNotice.objects.select_for_update(of=("self",))
            .select_related("recipient", "club", "event", "event__club", "event__actor")
            .filter(recipient_id=user_id, sent_at__isnull=True)
            .exclude(hold="")
            .order_by("created", "id")
        )
        rows = _eligible(pending, now)
        if not rows:
            return 0
        ClubNotice.objects.filter(id__in=[r.id for r in rows]).update(sent_at=now)
    user = rows[0].recipient
    loc = locale_for_user(user)
    live = []
    for row in rows:
        membership = ClubMembership.objects.filter(
            user_id=user_id, club_id=row.club_id,
        ).first()
        if membership is None or (
            effective_mode(membership) == ClubMembership.NOTIFY_MUTED
        ):
            ClubNotice.objects.filter(id=row.id).update(error="skipped:club_muted")
            continue
        live.append(row)
    quiet = any(r.hold == ClubNotice.HOLD_QUIET for r in live)
    settings_row, _ = UserSettings.objects.get_or_create(user_id=user_id)
    quiet_line = ""
    if settings_row.last_summary_at is None and prefs.enabled:
        # First summary this member ever gets: say which quiet hours and zone
        # applied, since nobody asked them.
        quiet_line = t(
            loc, "club.notify.quiet_line",
            span=f"{prefs.start.strftime('%H:%M')}-{prefs.end.strftime('%H:%M')}",
            city=html.escape(tz_city(prefs.tz)),
        )
    text, markup, shown, hidden = build_summary(
        live, loc, threads=threads_enabled(), quiet=quiet, quiet_line=quiet_line,
    )
    if not text:
        return len(rows)
    UserSettings.objects.filter(user_id=user_id).update(last_summary_at=now)
    chats = list(
        TelegramUser.objects.filter(user_id=user_id, is_active=True)
        .values_list("chat_id", flat=True).distinct()
    )
    errors = []
    for chat_id in chats:
        try:
            mid, error = _post(token, chat_id, text, markup)
        except Exception as exc:  # never raise out of the beat
            mid, error = None, f"error:{type(exc).__name__}"
        if error:
            errors.append(f"{chat_id}:{error}")
        elif mid:
            # So a swipe-reply to the summary is recognised (and answered
            # with a hint) instead of falling into the chatbot.
            ClubEventDelivery.objects.create(
                event=None, kind=ClubEventDelivery.KIND_SUMMARY,
                chat_id=str(chat_id), message_id=mid,
            )
    if not chats:
        errors.append("skipped:no_chat")
    if errors:
        ClubNotice.objects.filter(id__in=[r.id for r in live]).update(
            error="; ".join(errors)[:200],
        )
    return len(rows)


def send_due_summaries(now=None) -> int:
    """Beat entry: send every member's due summary. Idempotent: rows are
    stamped sent before posting, so a restart or a second run sends nothing
    twice. Returns members summarised."""
    if not notify_enabled():
        return 0
    from wine_cellar.apps.wine.tasks import _bot_token

    token = _bot_token()
    if not token:
        logger.warning("send_club_summaries: bot_token missing")
        return 0
    now = now or timezone.now()
    users = list(
        ClubNotice.objects.filter(sent_at__isnull=True)
        .exclude(hold="")
        .filter(due_at__lte=now)
        .values_list("recipient_id", flat=True)
        .distinct()[:500]
    )
    quiet_users = list(
        ClubNotice.objects.filter(
            sent_at__isnull=True, hold=ClubNotice.HOLD_QUIET,
        ).values_list("recipient_id", flat=True).distinct()[:500]
    )
    done = 0
    for uid in dict.fromkeys(users + quiet_users):
        try:
            if send_user_summary(uid, token, now):
                done += 1
        except Exception:
            logger.warning("club summary for %s failed", uid, exc_info=True)
    return done


# -- crossing DIGEST_MEMBERS -----------------------------------------------------
def announce_crossing(club_id) -> int:
    """Once per club, when a join takes it to DIGEST_MEMBERS or more: tell
    members who never chose a mode that they now get a daily recap. The stamp
    is sticky: the club's default stays digest even if it later shrinks. In a
    member's quiet hours the message is held for their summary."""
    if not notify_enabled():
        return 0
    from wine_cellar.apps.club.threads import _post
    from wine_cellar.apps.wine.tasks import _bot_token

    club = Club.objects.filter(id=club_id).first()
    if club is None or club.notify_crossing_sent_at is not None:
        return 0
    if club.memberships.count() < DIGEST_MEMBERS:
        return 0
    token = _bot_token()
    if not token:
        return 0
    now = timezone.now()
    claimed = Club.objects.filter(
        id=club_id, notify_crossing_sent_at__isnull=True,
    ).update(notify_crossing_sent_at=now)
    if not claimed:
        return 0
    sent = 0
    members = ClubMembership.objects.filter(
        club=club, notify_mode__isnull=True,
    ).select_related("user")
    for membership in members:
        prefs = prefs_for(membership.user_id)
        if in_quiet(prefs, now):
            hold_notice(
                membership.user_id, club, None, ClubNotice.KIND_CROSSING,
                Decision(HOLD, next_quiet_end(prefs, now), "quiet"),
                f"crossing:{club.id}:{membership.user_id}",
            )
            continue
        loc = locale_for_user(membership.user)
        text = t(
            loc, "club.notify.crossing",
            club=html.escape(club.name), n=DIGEST_MEMBERS,
        )
        markup = {"inline_keyboard": [[
            {"text": t("en", "club.notify.btn_keep"),
             "callback_data": f"cl:nk:{club.id}"},
            {"text": t("en", "club.notify.btn_recap"),
             "callback_data": f"cl:nr:{club.id}"},
        ]]}
        for chat_id in TelegramUser.objects.filter(
            user=membership.user, is_active=True,
        ).values_list("chat_id", flat=True).distinct():
            _mid, error = _post(token, chat_id, text, markup)
            if not error:
                sent += 1
    return sent


# -- "what did I miss" (ask.py) ---------------------------------------------------
def missed_items(user, club, limit=12) -> list[dict]:
    """Plain-text lines for the member's held items in this club, or, when
    nothing is waiting, what the last recap or summary held."""
    loc = locale_for_user(user)
    base = ClubNotice.objects.filter(recipient=user, club=club).exclude(hold="")
    rows = list(
        base.filter(sent_at__isnull=True)
        .select_related("recipient", "event", "event__club", "event__actor", "club")
        .order_by("-created")[:50]
    )
    state = "held"
    if not rows:
        since = timezone.now() - datetime.timedelta(hours=24)
        rows = list(
            base.filter(sent_at__gte=since)
            .select_related("recipient", "event", "event__club", "event__actor", "club")
            .order_by("-created")[:50]
        )
        state = "recap"
    if not rows:
        return _recent_feed(user, club, loc, limit)
    out = []
    for row in rows:
        built = _line_for(row, loc)
        if built is None:
            continue
        out.append({"kind": row.kind, "text": built[1]})
        if len(out) >= limit:
            break
    return [dict(item, state=state) for item in out]


def _recent_feed(user, club, loc, limit) -> list[dict]:
    """Nothing held or recapped: the club's last 24 h of feed activity (other
    people's bottles, asks and notes), so 'what did I miss' still answers."""
    from wine_cellar.apps.club.threads import _event_line

    since = timezone.now() - datetime.timedelta(hours=24)
    kinds = [ClubEvent.KIND_OPENED, ClubEvent.KIND_STAFF_NOTE]
    if flag_on("feature_club_relay"):
        kinds.append(ClubEvent.KIND_ASK)
    events = (
        ClubEvent.objects.filter(
            club=club, kind__in=kinds, created__gte=since, retracted_at__isnull=True,
        )
        .exclude(actor_id=user.id)
        .select_related("actor", "club")
        .order_by("-created")[:limit]
    )
    out = []
    for event in events:
        if event.kind == ClubEvent.KIND_STAFF_NOTE:
            text = (event.payload or {}).get("text") or ""
        else:
            text = html.unescape(_event_line(event, loc))
        out.append({"kind": "feed", "text": text, "state": "feed"})
    return out
