"""How the chat recommendations are landing, for staff.

A tap on Enjoy is cheap. Whether the suggested bottle was actually opened
soon after, and the thumbs it then got, is the signal that matters, so each
ask is matched to the household's bottles removed in the window after it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median

from django.utils import timezone

from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.wine.models import TasteIntent

OPENED_WITHIN = timedelta(hours=48)
WORST_LIMIT = 5


def _ask_rows(since, household_id=None):
    asks = TasteIntent.objects.filter(kind=TasteIntent.ASK, created__gte=since).select_related(
        "household",
    )
    if household_id is not None:
        asks = asks.filter(household_id=household_id)
    return list(asks.order_by("created"))


def _opened_by_household(household_ids, since, until):
    items = (
        StorageItem.objects.filter(
            wine__household_id__in=household_ids,
            deleted=True,
            removed_at__gte=since,
            removed_at__lte=until,
        )
        .values("wine_id", "wine__household_id", "removed_at", "vote")
        .order_by("removed_at")
    )
    grouped = defaultdict(list)
    for item in items:
        grouped[item["wine__household_id"]].append(item)
    return grouped


def _match(intent, opened):
    """The suggested bottle, or any other bottle, opened inside the window."""
    payload = intent.payload or {}
    shown = payload.get("shown")
    end = intent.created + OPENED_WITHIN
    picked = None
    other = None
    for item in opened:
        if item["removed_at"] < intent.created or item["removed_at"] > end:
            continue
        if shown and item["wine_id"] == shown and picked is None:
            picked = item
        elif other is None:
            other = item
    return picked, other


def _percent(part, whole):
    return round(100 * part / whole) if whole else 0


def chat_stats(days: int = 30, household_id: int | None = None, now=None) -> dict:
    now = now or timezone.now()
    since = now - timedelta(days=days)
    asks = _ask_rows(since, household_id)
    opened = _opened_by_household({a.household_id for a in asks}, since, now + OPENED_WITHIN)

    outcomes = Counter()
    models = Counter()
    votes = Counter()
    latencies = []
    something_else = 0
    questions = 0
    shown_count = 0
    picked_opened = 0
    other_opened = 0
    fallback = 0
    worst = []

    for intent in asks:
        payload = intent.payload or {}
        outcomes[payload.get("outcome") or "pending"] += 1
        models[payload.get("model") or "unknown"] += 1
        if payload.get("fallback"):
            fallback += 1
        if isinstance(payload.get("latency_ms"), (int, float)):
            latencies.append(payload["latency_ms"])
        rejected = payload.get("rejected") or []
        something_else += len(rejected)
        if payload.get("question"):
            questions += 1
        if not payload.get("shown"):
            continue
        shown_count += 1
        picked, other = _match(intent, opened.get(intent.household_id, []))
        if picked is not None:
            picked_opened += 1
            votes[picked["vote"] or "none"] += 1
        elif other is not None:
            other_opened += 1
        score = 0
        reasons = []
        if len(rejected) >= 2:
            score += len(rejected)
            reasons.append(f"{len(rejected)}× something else")
        if payload.get("outcome") == "abandoned":
            score += 1
            reasons.append("walked away")
        if picked is not None and picked["vote"] == StorageItem.VOTE_DOWN:
            score += 3
            reasons.append("opened, thumbs down")
        if picked is None and other is not None:
            score += 1
            reasons.append("opened a different bottle")
        if score:
            worst.append((score, intent, reasons))

    worst.sort(key=lambda row: (-row[0], -row[1].created.timestamp()))
    return {
        "days": days,
        "asks": len(asks),
        "shown": shown_count,
        "questions": questions,
        "outcomes": {
            key: outcomes.get(key, 0) for key in ("accepted", "rejected", "abandoned", "pending")
        },
        "something_else_taps": something_else,
        "opened_pick": picked_opened,
        "opened_pick_pct": _percent(picked_opened, shown_count),
        "opened_other": other_opened,
        "pick_votes": {
            key: votes.get(key, 0) for key in ("up", "meh", "down", "none")
        },
        "fallback": fallback,
        "models": dict(models),
        "latency_ms": {
            "p50": round(median(latencies)) if latencies else None,
            "p90": round(sorted(latencies)[int(0.9 * (len(latencies) - 1))]) if latencies else None,
        },
        "worst": [
            {
                "id": intent.id,
                "household": intent.household.name,
                "created": intent.created.isoformat(),
                "sentence": ((intent.payload or {}).get("sentence") or "")[:80],
                "why": reasons,
            }
            for _score, intent, reasons in worst[:WORST_LIMIT]
        ],
    }
