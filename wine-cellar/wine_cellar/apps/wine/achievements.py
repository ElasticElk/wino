"""Cellar challenges — firsts, count milestones, collection badges.

Household-scoped and on by default. Owners can turn `gamification_enabled`
off. Awards are idempotent (unique slug+subject per cellar) so enabling the
flag on a cellar that already has wine backfills quietly; only *new* unlocks
on a bottle add are worth a Telegram ping.
"""

from __future__ import annotations

from calendar import monthrange

import pycountry
from django.db.models import Count
from django.utils import timezone

from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.wine.models import CellarAchievement, Wine, WineType

COUNT_STEPS = (1, 5, 10, 25, 50, 100)
COLLECTION_STEPS = (5, 10, 20)
MONTH_STEPS = (5, 10, 25)


def _country_name(code: str) -> str:
    if not code:
        return ""
    try:
        c = pycountry.countries.get(alpha_2=code)
        return c.name if c else code
    except Exception:
        return code


def serialize_achievement(obj: CellarAchievement) -> dict:
    return {
        "slug": obj.slug,
        "subject": obj.subject,
        "emoji": obj.emoji,
        "title": obj.title,
        "detail": obj.detail,
        "unlocked_at": obj.unlocked_at.isoformat() if obj.unlocked_at else None,
    }


def _award(household, user, slug, subject, emoji, title, detail) -> dict | None:
    obj, created = CellarAchievement.objects.get_or_create(
        household=household,
        slug=slug,
        subject=subject or "",
        defaults={
            "user": user,
            "emoji": emoji,
            "title": title,
            "detail": detail,
        },
    )
    if created:
        return serialize_achievement(obj)
    return None


def _count_copy(n: int, noun: str, qualifier: str = "") -> tuple[str, str]:
    """Title + detail for a count milestone."""
    tail = f" {qualifier}" if qualifier else ""
    if n == 1:
        return f"First {noun}{tail}", f"Your first {noun}{tail} is logged."
    return f"{n} {noun}s{tail}", f"That's {n} {noun}s{tail} logged."


def _storage_for_add(wine: Wine, storage=None):
    if storage is not None:
        return storage
    item = (
        StorageItem.objects.filter(wine=wine)
        .select_related("storage")
        .order_by("-id")
        .first()
    )
    return item.storage if item else None


def award_for_add(wine: Wine, user, storage=None, drank=False) -> list[dict]:
    """Unlock whatever this newly logged bottle just earned. Empty if the flag is off.

    Milestones use >= so a skipped step (delete, backfill, first bottle only
    ever drunk) still awards. get_or_create keeps them from firing twice.
    """
    household = getattr(wine, "household", None)
    if household is None or not household.gamification_enabled:
        return []
    unlocked: list[dict] = []

    def take(*args):
        got = _award(household, user, *args)
        if got:
            unlocked.append(got)

    n_bottles = StorageItem.objects.filter(wine__household=household).count()
    for m in COUNT_STEPS:
        if n_bottles >= m:
            title, detail = _count_copy(m, "bottle")
            take("bottles", str(m), "🍾", title, detail)

    if wine.country:
        n_country = Wine.objects.filter(
            household=household, country=wine.country,
        ).count()
        name = _country_name(wine.country)
        if n_country >= 1:
            take(
                "country", wine.country,
                "🌍", f"First wine from {name}",
                f"{wine.name} opens {name} in this cellar.",
            )
        for m in COUNT_STEPS:
            if m > 1 and n_country >= m:
                take(
                    f"country:{m}", wine.country,
                    "🌍", f"{m} wines from {name}",
                    f"{name} is becoming a habit.",
                )
        n_countries = (
            Wine.objects.filter(household=household)
            .exclude(country="")
            .values("country")
            .distinct()
            .count()
        )
        for m in COLLECTION_STEPS:
            if n_countries >= m:
                take(
                    "explorer", str(m),
                    "🗺️", f"{m}-country cellar",
                    "The map on the wall is filling in.",
                )

    if wine.wine_type:
        n_type = Wine.objects.filter(
            household=household, wine_type=wine.wine_type,
        ).count()
        label = WineType(wine.wine_type).label.lower()
        if n_type >= 1:
            take(
                "type", wine.wine_type,
                "🍷", f"First {label}",
                f"{wine.name} is the first {label} on the books.",
            )
        for m in COUNT_STEPS:
            if m > 1 and n_type >= m:
                take(
                    f"type:{m}", wine.wine_type,
                    "🍷", f"{m} {label}s",
                    f"{m} {label} wines in the cellar.",
                )

    producer = (wine.producer or "").strip()
    if producer:
        n_prod = Wine.objects.filter(
            household=household, producer__iexact=producer,
        ).count()
        if n_prod >= 1:
            take(
                "producer", producer[:120],
                "🏡", f"First {producer}",
                f"A new producer lands in the cellar.",
            )
        letter = producer[0].upper()
        if letter.isalpha():
            letters = {
                (p or "")[:1].upper()
                for p in Wine.objects.filter(household=household)
                .exclude(producer="")
                .values_list("producer", flat=True)
                if (p or "")[:1].isalpha()
            }
            n_letters = len(letters)
            for m in COLLECTION_STEPS:
                if n_letters >= m:
                    take(
                        "alphabet", str(m),
                        "🔤", f"Producer alphabet · {m} letters",
                        "Different houses, different first letters.",
                    )

    if wine.vintage:
        decade = (wine.vintage // 10) * 10
        n_decade = Wine.objects.filter(
            household=household,
            vintage__gte=decade,
            vintage__lt=decade + 10,
        ).count()
        if n_decade >= 1:
            take(
                "decade", str(decade),
                "📅", f"First wine from the {decade}s",
                f"{wine.vintage} opens that decade.",
            )
        for m in COUNT_STEPS:
            if m > 1 and n_decade >= m:
                take(
                    f"decade:{m}", str(decade),
                    "📅", f"{m} wines from the {decade}s",
                    f"The {decade}s are piling up.",
                )

    st = _storage_for_add(wine, storage)
    if st is not None and not drank:
        n_ever = StorageItem.objects.filter(storage=st, deleted=False).count()
        if n_ever >= 1:
            take(
                "storage", str(st.pk),
                "🧊", f"First bottle in {st.name}",
                f"{wine.name} opens {st.name}.",
            )
        if st.total_slots:
            n_in = StorageItem.objects.filter(storage=st, deleted=False).count()
            if n_in >= st.total_slots:
                take(
                    "storage-full", str(st.pk),
                    "🧊", f"{st.name} is full",
                    "Every slot has a bottle.",
                )

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    n_month = StorageItem.objects.filter(
        wine__household=household, created__gte=month_start,
    ).count()
    for m in MONTH_STEPS:
        if n_month >= m:
            take(
                "month", f"{now:%Y-%m}:{m}",
                "🏁", f"{m} bottles in {now:%B}",
                f"{m} logged this month — the household race is on.",
            )

    for grape in wine.grapes.all():
        n_grape = (
            Wine.objects.filter(household=household, grapes=grape)
            .distinct()
            .count()
        )
        if n_grape >= 1:
            take(
                "grape", grape.name[:120],
                "🍇", f"First {grape.name}",
                f"{wine.name} is the first {grape.name} here.",
            )
        for m in COUNT_STEPS:
            if m > 1 and n_grape >= m:
                take(
                    f"grape:{m}", grape.name[:120],
                    "🍇", f"{m}× {grape.name}",
                    f"{grape.name} is earning its keep.",
                )
        n_grapes = (
            Wine.objects.filter(household=household)
            .exclude(grapes=None)
            .values("grapes")
            .distinct()
            .count()
        )
        for m in COLLECTION_STEPS:
            if n_grapes >= m:
                take(
                    "grape-explorer", str(m),
                    "🌿", f"{m} grape varieties",
                    "The tasting range is widening.",
                )

    return unlocked


def backfill(household, user) -> int:
    """Award everything the cellar already qualifies for. No notifications.

    Called when a household turns challenges on so /badges isn't empty
    on day one, and so the next bottle only pings *new* milestones.
    """
    if household is None or not household.gamification_enabled:
        return 0
    before = CellarAchievement.objects.filter(household=household).count()
    wines = Wine.objects.filter(household=household)

    n_bottles = StorageItem.objects.filter(wine__household=household).count()
    for m in COUNT_STEPS:
        if n_bottles >= m:
            title, detail = _count_copy(m, "bottle")
            _award(household, user, "bottles", str(m), "🍾", title, detail)

    for row in wines.exclude(country="").values("country").annotate(n=Count("id")):
        code, n = row["country"], row["n"]
        name = _country_name(code)
        if n >= 1:
            _award(
                household, user, "country", code,
                "🌍", f"First wine from {name}",
                f"{name} is on the map.",
            )
        for m in COUNT_STEPS:
            if m > 1 and n >= m:
                _award(
                    household, user, f"country:{m}", code,
                    "🌍", f"{m} wines from {name}",
                    f"{name} is becoming a habit.",
                )

    n_countries = wines.exclude(country="").values("country").distinct().count()
    for m in COLLECTION_STEPS:
        if n_countries >= m:
            _award(
                household, user, "explorer", str(m),
                "🗺️", f"{m}-country cellar",
                "The map on the wall is filling in.",
            )

    for row in wines.exclude(wine_type="").values("wine_type").annotate(n=Count("id")):
        code, n = row["wine_type"], row["n"]
        try:
            label = WineType(code).label.lower()
        except ValueError:
            label = code.lower()
        if n >= 1:
            _award(
                household, user, "type", code,
                "🍷", f"First {label}",
                f"The first {label} on the books.",
            )
        for m in COUNT_STEPS:
            if m > 1 and n >= m:
                _award(
                    household, user, f"type:{m}", code,
                    "🍷", f"{m} {label}s",
                    f"{m} {label} wines in the cellar.",
                )

    for row in wines.exclude(producer="").values("producer").annotate(n=Count("id")):
        if row["n"] >= 1:
            producer = row["producer"]
            _award(
                household, user, "producer", producer[:120],
                "🏡", f"First {producer}",
                "A new producer lands in the cellar.",
            )

    letters = {
        (p or "")[:1].upper()
        for p in wines.exclude(producer="").values_list("producer", flat=True)
        if (p or "")[:1].isalpha()
    }
    for m in COLLECTION_STEPS:
        if len(letters) >= m:
            _award(
                household, user, "alphabet", str(m),
                "🔤", f"Producer alphabet · {m} letters",
                "Different houses, different first letters.",
            )

    for wine in wines.filter(vintage__isnull=False).only("vintage"):
        decade = (wine.vintage // 10) * 10
        n_decade = Wine.objects.filter(
            household=household,
            vintage__gte=decade,
            vintage__lt=decade + 10,
        ).count()
        if n_decade >= 1:
            _award(
                household, user, "decade", str(decade),
                "📅", f"First wine from the {decade}s",
                f"{decade}s are in the cellar.",
            )
        for m in COUNT_STEPS:
            if m > 1 and n_decade >= m:
                _award(
                    household, user, f"decade:{m}", str(decade),
                    "📅", f"{m} wines from the {decade}s",
                    f"The {decade}s are piling up.",
                )

    for st in Storage.objects.filter(household=household):
        if StorageItem.objects.filter(storage=st).exists():
            _award(
                household, user, "storage", str(st.pk),
                "🧊", f"First bottle in {st.name}",
                f"{st.name} is in play.",
            )
        if st.total_slots:
            n_in = StorageItem.objects.filter(storage=st, deleted=False).count()
            if n_in >= st.total_slots:
                _award(
                    household, user, "storage-full", str(st.pk),
                    "🧊", f"{st.name} is full",
                    "Every slot has a bottle.",
                )

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    n_month = StorageItem.objects.filter(
        wine__household=household, created__gte=month_start,
    ).count()
    for m in MONTH_STEPS:
        if n_month >= m:
            _award(
                household, user, "month", f"{now:%Y-%m}:{m}",
                "🏁", f"{m} bottles in {now:%B}",
                f"{m} logged this month — the household race is on.",
            )

    grape_rows = (
        wines.filter(grapes__isnull=False)
        .values("grapes__id", "grapes__name")
        .annotate(n=Count("id", distinct=True))
    )
    n_grapes = 0
    for row in grape_rows:
        n_grapes += 1
        name, n = row["grapes__name"], row["n"]
        if not name:
            continue
        if n >= 1:
            _award(
                household, user, "grape", name[:120],
                "🍇", f"First {name}",
                f"The first {name} here.",
            )
        for m in COUNT_STEPS:
            if m > 1 and n >= m:
                _award(
                    household, user, f"grape:{m}", name[:120],
                    "🍇", f"{m}× {name}",
                    f"{name} is earning its keep.",
                )
    for m in COLLECTION_STEPS:
        if n_grapes >= m:
            _award(
                household, user, "grape-explorer", str(m),
                "🌿", f"{m} grape varieties",
                "The tasting range is widening.",
            )

    after = CellarAchievement.objects.filter(household=household).count()
    return after - before


def _next_step(n: int, steps: tuple[int, ...]) -> int | None:
    for s in steps:
        if n < s:
            return s
    return None


def progress(household) -> dict:
    """Closed-ended 'yet to achieve' plus the nearest in-progress counts.

    Open-ended firsts (every country, every grape, every producer) cannot be
    listed exhaustively, so those only appear here once you've started them.
    """
    empty = {"tracks": [], "missing": [], "close": []}
    if household is None or not household.gamification_enabled:
        return empty

    wines = Wine.objects.filter(household=household)
    tracks: list[dict] = []
    n_bottles = StorageItem.objects.filter(wine__household=household).count()
    nxt = _next_step(n_bottles, COUNT_STEPS)
    if nxt:
        tracks.append({
            "emoji": "🍾", "title": "Bottles logged",
            "have": n_bottles, "next": nxt,
        })

    n_countries = wines.exclude(country="").values("country").distinct().count()
    nxt = _next_step(n_countries, COLLECTION_STEPS)
    if nxt:
        tracks.append({
            "emoji": "🗺️", "title": "Countries",
            "have": n_countries, "next": nxt,
        })

    n_grapes = (
        wines.filter(grapes__isnull=False).values("grapes").distinct().count()
    )
    nxt = _next_step(n_grapes, COLLECTION_STEPS)
    if nxt:
        tracks.append({
            "emoji": "🌿", "title": "Grape varieties",
            "have": n_grapes, "next": nxt,
        })

    letters = {
        (p or "")[:1].upper()
        for p in wines.exclude(producer="").values_list("producer", flat=True)
        if (p or "")[:1].isalpha()
    }
    nxt = _next_step(len(letters), COLLECTION_STEPS)
    if nxt:
        tracks.append({
            "emoji": "🔤", "title": "Producer alphabet",
            "have": len(letters), "next": nxt,
        })

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    n_month = StorageItem.objects.filter(
        wine__household=household, created__gte=month_start,
    ).count()
    nxt = _next_step(n_month, MONTH_STEPS)
    if nxt:
        tracks.append({
            "emoji": "🏁", "title": f"{now:%B} bottles",
            "have": n_month, "next": nxt,
        })

    have_types = set(
        wines.exclude(wine_type="").values_list("wine_type", flat=True)
    )
    missing = [
        {"emoji": "🍷", "title": f"First {label.lower()}"}
        for code, label in WineType.choices
        if code not in have_types
    ]

    close: list[dict] = []
    for row in wines.exclude(country="").values("country").annotate(n=Count("id")):
        nxt = _next_step(row["n"], COUNT_STEPS)
        if nxt:
            close.append({
                "emoji": "🌍",
                "title": f"Wines from {_country_name(row['country'])}",
                "have": row["n"],
                "next": nxt,
            })
    grape_rows = (
        wines.filter(grapes__isnull=False)
        .values("grapes__name")
        .annotate(n=Count("id", distinct=True))
    )
    for row in grape_rows:
        name, n = row["grapes__name"], row["n"]
        if not name:
            continue
        nxt = _next_step(n, COUNT_STEPS)
        if nxt:
            close.append({
                "emoji": "🍇",
                "title": name,
                "have": n,
                "next": nxt,
            })
    for row in wines.exclude(wine_type="").values("wine_type").annotate(n=Count("id")):
        nxt = _next_step(row["n"], COUNT_STEPS)
        if nxt:
            try:
                label = WineType(row["wine_type"]).label
            except ValueError:
                label = row["wine_type"]
            close.append({
                "emoji": "🍷",
                "title": label,
                "have": row["n"],
                "next": nxt,
            })
    decades = {}
    for vintage in wines.filter(vintage__isnull=False).values_list("vintage", flat=True):
        decade = (vintage // 10) * 10
        decades[decade] = decades.get(decade, 0) + 1
    for decade, n in decades.items():
        nxt = _next_step(n, COUNT_STEPS)
        if nxt:
            close.append({
                "emoji": "📅",
                "title": f"The {decade}s",
                "have": n,
                "next": nxt,
            })
    for st in Storage.objects.filter(household=household):
        if not st.total_slots:
            continue
        n_in = StorageItem.objects.filter(storage=st, deleted=False).count()
        if n_in < st.total_slots:
            close.append({
                "emoji": "🧊",
                "title": f"Fill {st.name}",
                "have": n_in,
                "next": st.total_slots,
            })
    close.sort(key=lambda x: x["have"] / x["next"], reverse=True)
    return {"tracks": tracks, "missing": missing, "close": close[:4]}


def _empty_month() -> dict:
    return {
        "year": None, "month": None, "label": "",
        "days_left": 0, "standings": [],
    }


def month_tally(household, when=None) -> dict:
    """Household race for a calendar month: bottles logged, ranked.

    View-only — adding a bottle never pings just because the lead changed.
    """
    empty = _empty_month()
    if household is None:
        return empty
    now = when or timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = monthrange(start.year, start.month)[1]
    today = timezone.localdate()
    if today.year == start.year and today.month == start.month:
        days_left = max(0, last_day - today.day)
    else:
        days_left = 0
    rows = (
        StorageItem.objects.filter(wine__household=household, created__gte=start)
        .values("user_id", "user__username")
        .annotate(bottles=Count("id"))
        .order_by("-bottles", "user__username")
    )
    from wine_cellar.apps.scanner.models import TelegramUser
    user_ids = [r["user_id"] for r in rows if r["user_id"]]
    display = {
        tu.user_id: tu.display_name
        for tu in TelegramUser.objects.filter(user_id__in=user_ids)
        if tu.display_name
    }
    standings = []
    for i, r in enumerate(rows, start=1):
        standings.append({
            "user_id": r["user_id"],
            "name": display.get(r["user_id"]) or r["user__username"] or "Someone",
            "bottles": r["bottles"],
            "rank": i,
            "leading": i == 1,
        })
    return {
        "year": start.year,
        "month": start.month,
        "label": start.strftime("%B"),
        "days_left": days_left,
        "standings": standings,
    }
