import html
import logging
import time
from datetime import timedelta

import requests
from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from wine_cellar.apps.scanner.models import Household, ServiceConfig, TelegramUser
from wine_cellar.apps.wine.models import Wine

logger = logging.getLogger(__name__)


def _drink_by_line(wine, today):
    bits = [html.escape(wine.name)]
    if wine.vintage:
        bits.append(str(wine.vintage))
    label = " ".join(bits)
    if wine.drink_by < today:
        return f"{label} — overdue"
    return f"{label} — drink by {wine.drink_by.isoformat()}"


def _drink_by_message(household, wines, today):
    shown = wines[:15]
    extra = len(wines) - len(shown)
    lines = [
        f"<b>{html.escape(household.name)}</b>",
        "These are ready to drink:",
    ]
    lines.extend(_drink_by_line(wine, today) for wine in shown)
    if extra:
        lines.append(f"and {extra} more")
    return "\n".join(lines)


def _bot_token():
    try:
        token = ServiceConfig.objects.get(key="bot_token").value
    except ServiceConfig.DoesNotExist:
        return None
    return token or None


def _send_telegram(
    token, chat_id, text, purpose="drink_by_reminder",
    reply_markup=None, message_id=None,
):
    """Send or edit a Telegram HTML message. Retry once on 429.

    Returns the message_id (int) when Telegram includes it, True on a
    successful send without an id, False on failure. Existing callers
    only check truthiness.
    """
    method = "editMessageText" if message_id else "sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if message_id:
        payload["message_id"] = message_id
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    url = f"https://api.telegram.org/bot{token}/{method}"
    for attempt in range(2):
        try:
            response = requests.post(url, json=payload, timeout=15)
        except requests.RequestException:
            logger.warning("%s: send failed for chat %s", purpose, chat_id)
            return False
        if response.status_code == 429 and attempt == 0:
            retry_after = 1
            try:
                retry_after = int(
                    (response.json().get("parameters") or {}).get("retry_after") or 1
                )
            except (ValueError, TypeError, AttributeError):
                retry_after = 1
            time.sleep(min(max(retry_after, 1), 5))
            continue
        if response.status_code != 200:
            logger.warning(
                "%s: telegram returned %s for chat %s",
                purpose,
                response.status_code,
                chat_id,
            )
            return False
        try:
            mid = (response.json().get("result") or {}).get("message_id")
        except ValueError:
            mid = None
        return mid if mid is not None else True
    return False


@shared_task(name="drink_by_reminder")
def drink_by_reminder():
    token = _bot_token()
    if not token:
        logger.warning("drink_by_reminder: bot_token missing, skipping")
        return

    today = timezone.localdate()
    horizon = today + timedelta(days=14)
    reping_before = today - timedelta(days=6)

    households = Household.objects.filter(
        memberships__user__telegram_users__is_active=True,
    ).distinct()
    for household in households:
        wines = list(
            Wine.objects.filter(
                household=household,
                drink_by__isnull=False,
                drink_by__lte=horizon,
                storageitem__deleted=False,
            )
            .filter(
                Q(drink_by_reminded_on__isnull=True)
                | Q(drink_by_reminded_on__lt=reping_before)
            )
            .distinct()
            .order_by("drink_by", "id")
        )
        if not wines:
            continue
        text = _drink_by_message(household, wines, today)
        chats = TelegramUser.objects.filter(
            is_active=True,
            user__household_memberships__household=household,
        ).distinct()
        any_ok = False
        for chat in chats:
            if _send_telegram(token, chat.chat_id, text):
                any_ok = True
        if any_ok:
            Wine.objects.filter(pk__in=[wine.pk for wine in wines]).update(
                drink_by_reminded_on=today
            )


@shared_task(name="empty_cellar_nudge")
def empty_cellar_nudge():
    """One message, once, when a cellar is still empty after a few days."""
    token = _bot_token()
    if not token:
        logger.warning("empty_cellar_nudge: bot_token missing, skipping")
        return

    cutoff = timezone.now() - timedelta(days=3)
    households = Household.objects.filter(
        created__lte=cutoff,
        empty_nudge_sent_at__isnull=True,
    )
    for household in households:
        if Wine.objects.filter(household=household).exists():
            continue
        chats = TelegramUser.objects.filter(
            is_active=True,
            user__is_staff=False,
            user__household_memberships__household=household,
        ).distinct()
        if not chats:
            continue
        text = (
            f"<b>{html.escape(household.name)}</b>\n"
            "Your cellar is still empty. Send a photo of a front label "
            "and I'll add the bottle."
        )
        any_ok = False
        for chat in chats:
            if _send_telegram(token, chat.chat_id, text, purpose="empty_cellar_nudge"):
                any_ok = True
        if any_ok:
            Household.objects.filter(pk=household.pk).update(
                empty_nudge_sent_at=timezone.now()
            )


@shared_task(
    name="refresh_wine_references",
    bind=True,
    max_retries=2,
    soft_time_limit=90,
    time_limit=120,
    autoretry_for=(requests.Timeout, requests.ConnectionError),
    retry_backoff=True,
    retry_jitter=True,
    acks_late=True,
)
def refresh_wine_references(self, wine_id, user_id=None, force=False):
    """Look up references for one wine, off the request path.

    Grok's live search can run for minutes; doing it inside a request killed
    the gunicorn worker at its 30s default and, with one sync worker, blocked
    everything else meanwhile.
    """
    from wine_cellar.apps.wine.references import perform_reference_refresh

    return perform_reference_refresh(wine_id, user_id=user_id, force=force)


@shared_task(
    name="lookup_wine_references",
    bind=True,
    max_retries=2,
    soft_time_limit=90,
    time_limit=120,
    autoretry_for=(requests.Timeout, requests.ConnectionError),
    retry_backoff=True,
    retry_jitter=True,
    acks_late=True,
)
def lookup_wine_references(
    self, name, producer="", vintage=None, user_id=None, **hints,
):
    """Look up references for a wine that isn't in the cellar.

    Same reasons as refresh_wine_references for being a task: the live search
    outruns a request by minutes. Used by the bot when a label photo matches
    nothing you own — standing in a shop is when the rating matters most.
    """
    from wine_cellar.apps.wine.references import perform_reference_lookup

    return perform_reference_lookup(
        name, producer=producer, vintage=vintage, user_id=user_id, **hints
    )
