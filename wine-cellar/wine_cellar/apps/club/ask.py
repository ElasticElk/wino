"""Read-only club answers for typed chat.

Privacy is enforced here, not in the bot. A reply never names a person who
does not share a club with the asker, and cellar payloads follow the same
peek rules as GET .../members/<id>/cellar/.
"""

from __future__ import annotations

import unicodedata
from collections import Counter, defaultdict
from datetime import timedelta

from django.db.models import Avg, Count, Prefetch, Q
from django.utils import timezone

from wine_cellar.apps.club.models import (
    Club,
    ClubComment,
    ClubEvent,
    ClubMembership,
    TasteOff,
    TasteOffBottle,
    TasteOffScore,
)
from wine_cellar.apps.club.peek import (
    FORBIDDEN_PEEK_KEYS,
    collect_keys,
    effective_badges,
    effective_visibility,
    tighter_visibility,
)
from wine_cellar.apps.club.services import display_name, set_last_used
from wine_cellar.apps.scanner.flags import flag_on
from wine_cellar.apps.storage.models import StorageItem
from wine_cellar.apps.wine.achievements import (
    COLLECTION_STEPS,
    COUNT_STEPS,
    _next_step,
    serialize_achievement,
)
from wine_cellar.apps.wine.models import CellarAchievement, Wine, WineList, WineListItem

CLUB_ASK_KINDS = frozenset(
    {
        "members",
        "my_clubs",
        "cellar",
        "who_has",
        "oldest",
        "feed",
        "top_rated",
        "who_drinks",
        "avg_rating",
        "tasteoff",
        "badges",
        "badge_progress",
        "list",
        "comments",
    }
)

VOTE_SCORE = {"up": 3, "meh": 2, "down": 1}

_ALIAS_GROUPS = (
    ("syrah", ("shiraz", "syrah")),
    ("grenache", ("grenache", "garnacha", "cannonau")),
    ("pinot gris", ("pinot gris", "pinot grigio")),
    ("zinfandel", ("zinfandel", "primitivo")),
    ("mourvedre", ("mourvedre", "monastrell", "mataro")),
    ("carignan", ("carignan", "carinena", "mazuelo")),
    ("tempranillo", ("tempranillo", "tinta roriz", "aragonez")),
    ("chenin blanc", ("chenin blanc", "steen")),
    ("sauvignon blanc", ("sauvignon blanc", "fume blanc")),
    ("pinot noir", ("pinot noir", "spatburgunder")),
    ("cabernet sauvignon", ("cabernet sauvignon", "cab sauv")),
    ("pinotage", ("pinotage",)),
    ("merlot", ("merlot",)),
    ("barolo", ("barolo", "nebbiolo")),
)
GRAPE_ALIASES = {
    alias: canonical
    for canonical, aliases in _ALIAS_GROUPS
    for alias in aliases
}

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "maandag": 0, "dinsdag": 1, "woensdag": 2, "donderdag": 3,
    "vrydag": 4, "saterdag": 5, "sondag": 6,
}

CELLAR_KINDS = frozenset({"cellar", "who_has", "oldest"})
TASTEOFF_KINDS = frozenset({"tasteoff"})
LIST_KINDS = frozenset({"list"})
COMMENT_KINDS = frozenset({"comments"})
BADGE_KINDS = frozenset({"badges", "badge_progress"})


def fold(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text or "")
    stripped = "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")
    return stripped.casefold().strip()


def canonical_grape(name: str) -> str:
    folded = fold(name)
    return GRAPE_ALIASES.get(folded, folded)


def answer_club_ask(user, payload) -> dict:
    """Return a structured, privacy-filtered answer.

    HTTP layer maps feature_clubs-off to 404. Every other outcome is a
    200 with a `status` so the bot can pick, explain, or fall back.
    """
    data = payload if isinstance(payload, dict) else {}
    kind = str(data.get("kind") or "").strip()
    if kind not in CLUB_ASK_KINDS:
        return {"status": "fallback"}
    flags = _flags()
    gated = _gated(kind, flags)
    if gated is not None:
        return gated

    memberships = _asker_memberships(user, flags)
    if not memberships:
        return {"status": "no_clubs", "kind": kind}

    if kind == "my_clubs":
        return _answer_my_clubs(memberships)

    club, picker = _resolve_club(user, memberships, data)
    if picker is not None:
        return picker

    set_last_used(user, club["membership"])
    body = _answer_in_club(user, club, kind, data, flags)
    leaked = collect_keys(body) & FORBIDDEN_PEEK_KEYS
    if leaked:
        raise RuntimeError(f"club ask leaked forbidden keys: {sorted(leaked)}")
    return body


def _flags() -> dict:
    return {
        "clubs": flag_on("feature_clubs"),
        "lists": flag_on("feature_lists"),
        "comments": flag_on("feature_club_comments"),
        "tasteoffs": flag_on("feature_tasteoffs"),
        "global_clubs": flag_on("feature_global_clubs"),
    }


def _gated(kind: str, flags: dict) -> dict | None:
    if kind in TASTEOFF_KINDS and not flags["tasteoffs"]:
        return {"status": "fallback"}
    if kind in LIST_KINDS and not flags["lists"]:
        return {"status": "fallback"}
    if kind in COMMENT_KINDS and not flags["comments"]:
        return {"status": "fallback"}
    return None


def _asker_memberships(user, flags: dict) -> list[ClubMembership]:
    qs = (
        ClubMembership.objects.filter(user=user)
        .select_related("club", "household")
        .annotate(_n_members=Count("club__memberships"))
        .order_by("-is_last_used", "club__name")
    )
    if not flags["global_clubs"]:
        qs = qs.exclude(club__kind=Club.KIND_GLOBAL)
    return list(qs)


def _club_choice(membership: ClubMembership) -> dict:
    return {
        "id": membership.club_id,
        "label": membership.club.name,
        "kind": "club",
        "club_kind": membership.club.kind,
    }


def _resolve_club(user, memberships: list[ClubMembership], data: dict):
    raw_id = data.get("club_id")
    if raw_id not in (None, ""):
        try:
            club_id = int(raw_id)
        except (TypeError, ValueError):
            return None, {"status": "not_found", "kind": data.get("kind") or ""}
        for row in memberships:
            if row.club_id == club_id:
                return _club_bundle(row), None
        return None, {"status": "not_found", "kind": data.get("kind") or ""}

    name = str(data.get("club_name") or "").strip()
    if name:
        hits = _name_hits(name, [
            {"id": m.club_id, "name": m.club.name, "membership": m}
            for m in memberships
        ])
        if len(hits) == 1:
            return _club_bundle(hits[0]["membership"]), None
        if len(hits) > 1:
            return None, {
                "status": "need_club",
                "kind": data.get("kind") or "",
                "choices": [_club_choice(h["membership"]) for h in hits],
            }
        return None, {
            "status": "not_found",
            "kind": data.get("kind") or "",
            "target": "club",
            "query": name,
            "choices": [_club_choice(m) for m in memberships],
        }

    if len(memberships) == 1:
        return _club_bundle(memberships[0]), None
    last = next((m for m in memberships if m.is_last_used), None)
    if last is not None:
        return _club_bundle(last), None
    return None, {
        "status": "need_club",
        "kind": data.get("kind") or "",
        "choices": [_club_choice(m) for m in memberships],
    }


def _club_bundle(membership: ClubMembership) -> dict:
    return {
        "membership": membership,
        "club": membership.club,
        "id": membership.club_id,
        "name": membership.club.name,
        "kind": membership.club.kind,
    }


def _club_meta(club: dict) -> dict:
    return {
        "id": club["id"],
        "name": club["name"],
        "kind": club["kind"],
    }


def _answer_my_clubs(memberships: list[ClubMembership]) -> dict:
    return {
        "status": "ok",
        "kind": "my_clubs",
        "clubs": [
            {
                "id": m.club_id,
                "name": m.club.name,
                "role": m.role,
                "kind": m.club.kind,
                "is_last_used": m.is_last_used,
                "member_count": getattr(m, "_n_members", None) or m.club.member_count,
            }
            for m in memberships
        ],
    }


def _answer_in_club(user, club: dict, kind: str, data: dict, flags: dict) -> dict:
    members = _club_members(club["club"])
    if kind == "members":
        return _answer_members(club, members)
    member, picker = _resolve_member(user, club, members, data, kind)
    if picker is not None:
        return picker
    if kind in CELLAR_KINDS:
        return _answer_cellar(user, club, members, member, kind, data)
    if kind in {"feed", "who_drinks", "top_rated", "avg_rating"}:
        return _answer_feed(user, club, members, member, kind, data)
    if kind == "tasteoff":
        return _answer_tasteoff(club, members, data)
    if kind in BADGE_KINDS:
        return _answer_badges(user, club, members, member, kind)
    if kind == "list":
        return _answer_list(user, club, members, member)
    if kind == "comments":
        return _answer_comments(club, data)
    return {"status": "fallback"}


def _club_members(club: Club) -> list[ClubMembership]:
    return list(
        ClubMembership.objects.filter(club=club)
        .select_related("user", "household", "club")
        .prefetch_related("user__telegram_users")
        .order_by("created")
    )


def _member_choice(row: ClubMembership, club: dict) -> dict:
    return {
        "id": row.user_id,
        "label": display_name(row.user),
        "kind": "member",
        "club_id": club["id"],
        "club_name": club["name"],
    }


def _needs_member(kind: str, data: dict) -> bool:
    if kind in {"avg_rating", "badges"} and not (
        data.get("member_id") not in (None, "") or str(data.get("member_name") or "").strip()
    ):
        return True
    return False


def _resolve_member(user, club: dict, members: list[ClubMembership], data: dict, kind: str):
    raw_id = data.get("member_id")
    if raw_id not in (None, ""):
        try:
            member_id = int(raw_id)
        except (TypeError, ValueError):
            return None, {
                "status": "not_found",
                "kind": kind,
                "club": _club_meta(club),
                "target": "member",
                "choices": [_member_choice(m, club) for m in members],
            }
        for row in members:
            if row.user_id == member_id:
                return row, None
        return None, {
            "status": "not_found",
            "kind": kind,
            "club": _club_meta(club),
            "target": "member",
            "choices": [_member_choice(m, club) for m in members],
        }

    name = str(data.get("member_name") or "").strip()
    if not name:
        if _needs_member(kind, data):
            return None, {
                "status": "need_member",
                "kind": kind,
                "club": _club_meta(club),
                "choices": [_member_choice(m, club) for m in members],
            }
        return None, None

    hits = _name_hits(name, [
        {"id": m.user_id, "name": display_name(m.user), "membership": m,
         "username": m.user.get_username()}
        for m in members
    ])
    extra = []
    for row in members:
        username = fold(row.user.get_username())
        q = fold(name)
        if username and (username == q or username.startswith(q)):
            extra.append({"id": row.user_id, "name": display_name(row.user), "membership": row})
    seen = {h["id"] for h in hits}
    for row in extra:
        if row["id"] not in seen:
            hits.append(row)
            seen.add(row["id"])
    if len(hits) == 1:
        return hits[0]["membership"], None
    if len(hits) > 1:
        return None, {
            "status": "need_member",
            "kind": kind,
            "club": _club_meta(club),
            "choices": [_member_choice(h["membership"], club) for h in hits],
        }
    return None, {
        "status": "not_found",
        "kind": kind,
        "club": _club_meta(club),
        "target": "member",
        "query": name,
        "choices": [_member_choice(m, club) for m in members],
    }


def _name_hits(query: str, candidates: list[dict]) -> list[dict]:
    q = fold(query)
    if len(q) < 2:
        return []
    exact, prefix, contains = [], [], []
    for row in candidates:
        names = [row.get("name") or ""]
        if row.get("username"):
            names.append(row["username"])
        folded = [fold(n) for n in names if n]
        tokens = []
        for n in names:
            tokens.extend(fold(n).split())
        if q in folded:
            exact.append(row)
        elif any(n.startswith(q) for n in folded) or any(t.startswith(q) for t in tokens):
            prefix.append(row)
        elif any(q in n for n in folded):
            contains.append(row)
    if exact and prefix:
        return exact + prefix
    return exact or prefix or contains


def _answer_members(club: dict, members: list[ClubMembership]) -> dict:
    return {
        "status": "ok",
        "kind": "members",
        "club": _club_meta(club),
        "members": [
            {
                "display_name": display_name(m.user),
                "role": m.role,
            }
            for m in members
        ],
    }


def _visibility_for(viewer: ClubMembership, target: ClubMembership) -> str:
    club = target.club
    if club.kind == Club.KIND_GLOBAL:
        return "none"
    if viewer.user_id == target.user_id:
        return "full"
    return tighter_visibility(
        effective_visibility(club, viewer),
        effective_visibility(club, target),
    )


def _badges_for(viewer: ClubMembership, target: ClubMembership) -> bool:
    club = target.club
    if club.kind == Club.KIND_GLOBAL:
        return False
    household = target.household
    if household is None or not household.gamification_enabled:
        return False
    return bool(
        effective_badges(club, viewer) and effective_badges(club, target)
    )


def _answer_cellar(user, club: dict, members, member, kind: str, data: dict) -> dict:
    viewer = club["membership"]
    if club["kind"] == Club.KIND_GLOBAL:
        return {
            "status": "not_visible",
            "kind": kind,
            "club": _club_meta(club),
            "reason": "global",
            "choices": [_club_choice(m) for m in _other_private_clubs(user, club["id"])],
        }

    targets = [member] if member is not None else [
        m for m in members if m.household_id is not None
    ]
    vis_map = {m.user_id: _visibility_for(viewer, m) for m in targets}
    query = _wine_query(data)
    named_wine = bool(query["grape"] or query["wine_text"] or query["vintage"])
    if kind == "who_has" or (kind == "cellar" and member is None and named_wine):
        return _answer_who_has(club, members, targets, vis_map, data)
    if kind == "oldest":
        return _answer_oldest(club, targets, vis_map)
    if kind == "cellar" and member is None:
        return _answer_who_has(club, members, targets, vis_map, data)
    if member is None:
        return {
            "status": "need_member",
            "kind": kind,
            "club": _club_meta(club),
            "choices": [_member_choice(m, club) for m in members],
        }
    return _answer_member_cellar(club, member, vis_map, data)


def _other_private_clubs(user, current_id: int) -> list[ClubMembership]:
    return list(
        ClubMembership.objects.filter(user=user, club__kind=Club.KIND_PRIVATE)
        .exclude(club_id=current_id)
        .select_related("club")
        .order_by("club__name")
    )


def _wine_query(data: dict) -> dict:
    grape = str(data.get("grape") or "").strip()
    wine_text = str(data.get("wine_text") or "").strip()
    vintage = data.get("vintage")
    try:
        vintage = int(vintage) if vintage not in (None, "") else None
    except (TypeError, ValueError):
        vintage = None
    return {"grape": grape, "wine_text": wine_text, "vintage": vintage}


def _load_stock(household_ids: list[int]):
    if not household_ids:
        return [], []
    items = list(
        StorageItem.objects.filter(
            wine__household_id__in=household_ids, deleted=False,
        )
        .select_related("wine", "wine__region")
        .prefetch_related("wine__grapes")
    )
    wines = {}
    for item in items:
        wine = item.wine
        row = wines.get(wine.id)
        if row is None:
            row = {
                "id": wine.id,
                "household_id": wine.household_id,
                "name": wine.name,
                "producer": wine.producer or "",
                "vintage": wine.vintage,
                "wine_type": wine.wine_type or "",
                "country": wine.country or "",
                "region": wine.region.name if wine.region_id else "",
                "grapes": [g.name for g in wine.grapes.all()],
                "stock_count": 0,
            }
            wines[wine.id] = row
        row["stock_count"] += 1
    return items, list(wines.values())


def _public_wine(wine: dict) -> dict:
    return {
        "name": wine["name"],
        "producer": wine["producer"],
        "vintage": wine["vintage"],
        "wine_type": wine["wine_type"],
        "country": wine["country"],
        "region": wine["region"],
        "grapes": list(wine["grapes"]),
        "stock_count": wine["stock_count"],
    }


def _wine_matches(wine: dict, query: dict) -> bool:
    if query["vintage"] is not None and wine.get("vintage") != query["vintage"]:
        return False
    grape = canonical_grape(query["grape"]) if query["grape"] else ""
    text = canonical_grape(query["wine_text"]) if query["wine_text"] else grape
    if not text and not grape:
        return True
    needles = [n for n in {grape, text} if n]
    hay = " ".join(
        [
            fold(wine.get("name") or ""),
            fold(wine.get("producer") or ""),
            " ".join(canonical_grape(g) for g in wine.get("grapes") or []),
        ]
    )
    return any(n in hay for n in needles)


def _summary_grape_count(wines: list[dict], grape: str) -> int:
    wanted = canonical_grape(grape)
    total = 0
    for wine in wines:
        if any(canonical_grape(g) == wanted for g in wine.get("grapes") or []):
            total += wine["stock_count"]
    return total


def _answer_member_cellar(club: dict, member: ClubMembership, vis_map: dict, data: dict) -> dict:
    vis = vis_map.get(member.user_id, "none")
    query = _wine_query(data)
    if vis == "none" or member.household_id is None:
        return {
            "status": "not_visible",
            "kind": "cellar",
            "club": _club_meta(club),
            "member": display_name(member.user),
            "reason": "none",
        }
    _items, wines = _load_stock([member.household_id])
    if vis == "summary":
        grape = query["grape"] or query["wine_text"]
        count = _summary_grape_count(wines, grape) if grape else sum(w["stock_count"] for w in wines)
        types = Counter()
        grapes = Counter()
        for wine in wines:
            types[wine["wine_type"] or "RE"] += wine["stock_count"]
            for g in wine["grapes"]:
                grapes[g] += wine["stock_count"]
        return {
            "status": "ok",
            "kind": "cellar",
            "club": _club_meta(club),
            "member": display_name(member.user),
            "visibility": "summary",
            "total_bottles": sum(w["stock_count"] for w in wines),
            "count": count,
            "grape": grape,
            "counts_by_type": dict(types),
            "counts_by_grape": dict(grapes),
        }
    matched = [w for w in wines if _wine_matches(w, query)] if (query["grape"] or query["wine_text"] or query["vintage"]) else wines
    return {
        "status": "ok",
        "kind": "cellar",
        "club": _club_meta(club),
        "member": display_name(member.user),
        "visibility": "full",
        "total_bottles": sum(w["stock_count"] for w in wines),
        "count": sum(w["stock_count"] for w in matched),
        "wines": [_public_wine(w) for w in matched[:20]],
        "truncated": len(matched) > 20,
    }


def _answer_who_has(club, members, targets, vis_map, data) -> dict:
    query = _wine_query(data)
    household_ids = [m.household_id for m in targets if m.household_id and vis_map.get(m.user_id) != "none"]
    _items, wines = _load_stock(household_ids)
    by_hh = defaultdict(list)
    for wine in wines:
        by_hh[wine["household_id"]].append(wine)
    hits = []
    summary_hits = []
    skipped_summary = 0
    seen_full = set()
    seen_summary = set()
    needs_full = bool(query["vintage"] or (query["wine_text"] and not query["grape"]))
    label = query["wine_text"] or query["grape"] or ""
    names_by_hh = defaultdict(list)
    for member in targets:
        if member.household_id:
            names_by_hh[member.household_id].append(display_name(member.user))
    for member in targets:
        vis = vis_map.get(member.user_id, "none")
        hid = member.household_id
        if vis == "none" or hid is None:
            continue
        hh_wines = by_hh.get(hid, [])
        people = ", ".join(names_by_hh.get(hid) or [display_name(member.user)])
        if vis == "summary":
            if hid in seen_summary:
                continue
            seen_summary.add(hid)
            if needs_full:
                skipped_summary += 1
                continue
            grape = query["grape"] or query["wine_text"]
            count = _summary_grape_count(hh_wines, grape) if grape else 0
            if count:
                summary_hits.append({
                    "display_name": people,
                    "count": count,
                    "visibility": "summary",
                })
            continue
        if hid in seen_full:
            continue
        seen_full.add(hid)
        matched = [w for w in hh_wines if _wine_matches(w, query)]
        count = sum(w["stock_count"] for w in matched)
        if count:
            hits.append({
                "display_name": people,
                "count": count,
                "visibility": "full",
                "wines": [_public_wine(w) for w in matched[:8]],
            })
    return {
        "status": "ok",
        "kind": "who_has",
        "club": _club_meta(club),
        "query": label,
        "vintage": query["vintage"],
        "matches": hits,
        "summary_matches": summary_hits,
        "skipped_summary": skipped_summary,
    }


def _answer_oldest(club, targets, vis_map) -> dict:
    household_ids = [
        m.household_id for m in targets
        if m.household_id and vis_map.get(m.user_id) == "full"
    ]
    skipped = sum(
        1 for m in targets
        if vis_map.get(m.user_id) in {"summary", "none"} or m.household_id is None
    )
    if not household_ids:
        return {
            "status": "not_visible",
            "kind": "oldest",
            "club": _club_meta(club),
            "reason": "summary" if skipped else "none",
        }
    _items, wines = _load_stock(household_ids)
    dated = [w for w in wines if w.get("vintage")]
    if not dated:
        return {
            "status": "ok",
            "kind": "oldest",
            "club": _club_meta(club),
            "wines": [],
            "skipped_private": skipped,
        }
    oldest = min(w["vintage"] for w in dated)
    hh_to_name = {
        m.household_id: display_name(m.user)
        for m in targets
        if m.household_id and vis_map.get(m.user_id) == "full"
    }
    rows = [
        {**_public_wine(w), "owner": hh_to_name.get(w["household_id"], "")}
        for w in dated if w["vintage"] == oldest
    ]
    return {
        "status": "ok",
        "kind": "oldest",
        "club": _club_meta(club),
        "vintage": oldest,
        "wines": rows[:8],
        "skipped_private": skipped,
    }


def _period_since(period: str):
    now = timezone.now()
    key = fold(period)
    if key in {"week", "this week", "die week", "hierdie week"}:
        return now - timedelta(days=7)
    if key in {"month", "this month", "die maand"}:
        return now - timedelta(days=30)
    if key in {"lately", "recent", "onlangs", "laas"}:
        return now - timedelta(days=14)
    return None


def _events_qs(club, member, data):
    qs = (
        ClubEvent.objects.filter(club=club["club"], retracted_at__isnull=True)
        .select_related("actor")
        .prefetch_related("actor__telegram_users")
    )
    if member is not None:
        qs = qs.filter(actor_id=member.user_id)
    since = _period_since(str(data.get("period") or ""))
    if since is not None:
        qs = qs.filter(created__gte=since)
    wine_text = str(data.get("wine_text") or data.get("grape") or "").strip()
    if wine_text:
        qs = qs.filter(
            Q(wine_name__icontains=wine_text) | Q(producer__icontains=wine_text)
        )
    vintage = data.get("vintage")
    try:
        if vintage not in (None, ""):
            qs = qs.filter(vintage=int(vintage))
    except (TypeError, ValueError):
        pass
    return qs


def _event_row(event: ClubEvent) -> dict:
    return {
        "kind": event.kind,
        "actor": display_name(event.actor) if event.actor_id else "",
        "wine_name": event.wine_name,
        "vintage": event.vintage,
        "producer": event.producer or "",
        "vote": event.vote or "",
        "created": event.created.isoformat() if event.created else None,
    }


def _answer_feed(user, club, members, member, kind, data) -> dict:
    qs = _events_qs(club, member, data)
    if kind == "feed":
        rows = list(qs.filter(kind=ClubEvent.KIND_OPENED).order_by("-created")[:12])
        return {
            "status": "ok",
            "kind": "feed",
            "club": _club_meta(club),
            "member": display_name(member.user) if member is not None else "",
            "events": [_event_row(e) for e in rows],
        }
    if kind == "who_drinks":
        opened = (
            qs.filter(kind=ClubEvent.KIND_OPENED)
            .values("actor_id")
            .annotate(n=Count("id"))
            .order_by("-n")
        )
        names = {m.user_id: display_name(m.user) for m in members}
        ranking = [
            {"display_name": names.get(row["actor_id"], ""), "count": row["n"]}
            for row in opened
            if row["actor_id"] in names
        ]
        return {
            "status": "ok",
            "kind": "who_drinks",
            "club": _club_meta(club),
            "ranking": ranking,
        }
    voted = list(
        qs.filter(vote__in=VOTE_SCORE)
        .order_by("-created")[:80]
    )
    if kind == "avg_rating":
        if member is None:
            return {
                "status": "need_member",
                "kind": "avg_rating",
                "club": _club_meta(club),
                "choices": [_member_choice(m, club) for m in members],
            }
        scores = [VOTE_SCORE[e.vote] for e in voted if e.actor_id == member.user_id]
        votes = Counter(e.vote for e in voted if e.actor_id == member.user_id)
        taste_avg = None
        if flag_on("feature_tasteoffs"):
            agg = TasteOffScore.objects.filter(
                tasteoff__club=club["club"], user_id=member.user_id,
            ).aggregate(avg=Avg("score"), n=Count("id"))
            if agg["n"]:
                taste_avg = round(float(agg["avg"]), 1)
        return {
            "status": "ok",
            "kind": "avg_rating",
            "club": _club_meta(club),
            "member": display_name(member.user),
            "vote_count": len(scores),
            "votes": dict(votes),
            "vote_average": round(sum(scores) / len(scores), 2) if scores else None,
            "tasteoff_average": taste_avg,
        }
    # top_rated: opened/voted bottles with the strongest club votes.
    by_wine = defaultdict(lambda: {"up": 0, "meh": 0, "down": 0, "name": "", "vintage": None, "producer": ""})
    for event in voted:
        key = (fold(event.wine_name), event.vintage, fold(event.producer or ""))
        bucket = by_wine[key]
        bucket[event.vote] = bucket.get(event.vote, 0) + 1
        bucket["name"] = event.wine_name
        bucket["vintage"] = event.vintage
        bucket["producer"] = event.producer or ""
        bucket["actor"] = display_name(event.actor) if event.actor_id else ""
    ranked = sorted(
        by_wine.values(),
        key=lambda r: (r["up"] * 3 + r["meh"] - r["down"], r["up"]),
        reverse=True,
    )
    wines = [
        {
            "name": r["name"],
            "vintage": r["vintage"],
            "producer": r["producer"],
            "up": r["up"],
            "meh": r["meh"],
            "down": r["down"],
        }
        for r in ranked[:8]
        if r["up"] or r["meh"] or r["down"]
    ]
    return {
        "status": "ok",
        "kind": "top_rated",
        "club": _club_meta(club),
        "wines": wines,
    }


def _weekday(when: str):
    return WEEKDAYS.get(fold(when))


def _answer_tasteoff(club, members, data) -> dict:
    when = fold(str(data.get("when") or ""))
    qs = (
        TasteOff.objects.filter(club=club["club"])
        .select_related("host")
        .prefetch_related(
            Prefetch("bottles", queryset=TasteOffBottle.objects.order_by("sort_order", "id")),
            Prefetch(
                "scores",
                queryset=TasteOffScore.objects.select_related("user", "bottle"),
            ),
        )
        .order_by("-created")
    )
    rows = list(qs[:20])
    if not rows:
        return {
            "status": "ok",
            "kind": "tasteoff",
            "club": _club_meta(club),
            "mode": "none",
            "tasteoffs": [],
        }
    weekday = _weekday(when)
    named = str(data.get("wine_text") or "").strip()
    picked = rows
    if weekday is not None:
        picked = [
            t for t in rows
            if (
                fold(t.title).find(fold(when)) >= 0
                or (t.created and t.created.weekday() == weekday)
                or (t.opened_at and t.opened_at.weekday() == weekday)
            )
        ] or picked
    if named:
        titled = [t for t in picked if fold(named) in fold(t.title)]
        if titled:
            picked = titled
    if when in {"next", "upcoming", "volgende"} or (not when and not named):
        open_ones = [t for t in rows if t.status in (TasteOff.STATUS_SETUP, TasteOff.STATUS_SCORING)]
        if open_ones and when in {"next", "upcoming", "volgende"}:
            return {
                "status": "ok",
                "kind": "tasteoff",
                "club": _club_meta(club),
                "mode": "next",
                "tasteoff": _serialize_tasteoff(open_ones[0], include_scores=False),
            }
        if not when and not named:
            open_ones = [t for t in rows if t.status in (TasteOff.STATUS_SETUP, TasteOff.STATUS_SCORING)]
            if open_ones:
                return {
                    "status": "ok",
                    "kind": "tasteoff",
                    "club": _club_meta(club),
                    "mode": "next",
                    "tasteoff": _serialize_tasteoff(open_ones[0], include_scores=False),
                }
    if when in {"last", "won", "winner", "laas", "gewen"} or (
        when and when not in {"next", "upcoming", "volgende"} and not named
    ):
        closed = [t for t in rows if t.status == TasteOff.STATUS_CLOSED]
        target = closed[0] if closed else (picked[0] if picked else None)
        if target is None:
            return {
                "status": "ok",
                "kind": "tasteoff",
                "club": _club_meta(club),
                "mode": "none",
                "tasteoffs": [],
            }
        return {
            "status": "ok",
            "kind": "tasteoff",
            "club": _club_meta(club),
            "mode": "last" if when in {"last", "won", "winner", "laas", "gewen"} else "scores",
            "tasteoff": _serialize_tasteoff(target, include_scores=True),
        }
    target = picked[0]
    return {
        "status": "ok",
        "kind": "tasteoff",
        "club": _club_meta(club),
        "mode": "scores",
        "tasteoff": _serialize_tasteoff(target, include_scores=True),
    }


def _serialize_tasteoff(tasteoff: TasteOff, *, include_scores: bool) -> dict:
    bottles = list(tasteoff.bottles.all())
    scores = list(tasteoff.scores.all()) if include_scores else []
    by_bottle = defaultdict(list)
    for score in scores:
        by_bottle[score.bottle_id].append(score.score)
    bottle_rows = []
    for bottle in bottles:
        vals = by_bottle.get(bottle.id, [])
        avg = round(sum(vals) / len(vals), 2) if vals else None
        bottle_rows.append({
            "label": bottle.label,
            "average": avg,
            "n": len(vals),
        })
    winner = None
    ranked = [b for b in bottle_rows if b["average"] is not None]
    if ranked:
        best = max(b["average"] for b in ranked)
        tops = [b for b in ranked if b["average"] == best]
        winner = {"average": best, "labels": [b["label"] for b in tops]}
    return {
        "title": tasteoff.title,
        "status": tasteoff.status,
        "host": display_name(tasteoff.host) if tasteoff.host_id else "",
        "bottles": bottle_rows,
        "winner": winner,
    }


def _answer_badges(user, club, members, member, kind) -> dict:
    viewer = club["membership"]
    if club["kind"] == Club.KIND_GLOBAL:
        return {
            "status": "not_visible",
            "kind": kind,
            "club": _club_meta(club),
            "reason": "global",
        }
    if kind == "badges":
        if member is None:
            return {
                "status": "need_member",
                "kind": "badges",
                "club": _club_meta(club),
                "choices": [_member_choice(m, club) for m in members],
            }
        if not _badges_for(viewer, member):
            return {
                "status": "not_visible",
                "kind": "badges",
                "club": _club_meta(club),
                "member": display_name(member.user),
                "reason": "badges",
            }
        unlocks = list(
            CellarAchievement.objects.filter(household=member.household)[:50]
        )
        return {
            "status": "ok",
            "kind": "badges",
            "club": _club_meta(club),
            "member": display_name(member.user),
            "achievements": [serialize_achievement(a) for a in unlocks],
        }
    visible = [m for m in members if _badges_for(viewer, m) and m.household_id]
    if not visible:
        return {
            "status": "not_visible",
            "kind": "badge_progress",
            "club": _club_meta(club),
            "reason": "badges",
        }
    closest = _closest_badge(visible)
    return {
        "status": "ok",
        "kind": "badge_progress",
        "club": _club_meta(club),
        "closest": closest,
    }


def _closest_badge(members: list[ClubMembership]) -> list[dict]:
    household_ids = [m.household_id for m in members]
    bottle_rows = (
        StorageItem.objects.filter(wine__household_id__in=household_ids)
        .values("wine__household_id")
        .annotate(n=Count("id"))
    )
    bottles = {row["wine__household_id"]: row["n"] for row in bottle_rows}
    country_rows = (
        Wine.objects.filter(household_id__in=household_ids)
        .exclude(country="")
        .values("household_id")
        .annotate(n=Count("country", distinct=True))
    )
    countries = {row["household_id"]: row["n"] for row in country_rows}
    grape_rows = (
        Wine.objects.filter(household_id__in=household_ids, grapes__isnull=False)
        .values("household_id")
        .annotate(n=Count("grapes", distinct=True))
    )
    grapes = {row["household_id"]: row["n"] for row in grape_rows}
    results = []
    for member in members:
        hid = member.household_id
        tracks = []
        for have, steps, title in (
            (bottles.get(hid, 0), COUNT_STEPS, "Bottles logged"),
            (countries.get(hid, 0), COLLECTION_STEPS, "Countries"),
            (grapes.get(hid, 0), COLLECTION_STEPS, "Grape varieties"),
        ):
            nxt = _next_step(have, steps)
            if nxt is not None:
                tracks.append({
                    "title": title,
                    "have": have,
                    "next": nxt,
                    "remaining": nxt - have,
                })
        if not tracks:
            continue
        best = min(tracks, key=lambda t: t["remaining"])
        results.append({
            "display_name": display_name(member.user),
            "title": best["title"],
            "have": best["have"],
            "next": best["next"],
            "remaining": best["remaining"],
        })
    results.sort(key=lambda r: (r["remaining"], r["display_name"]))
    return results[:5]


def _answer_list(user, club, members, member) -> dict:
    if member is not None and member.user_id != user.id:
        return {
            "status": "private",
            "kind": "list",
            "club": _club_meta(club),
            "member": display_name(member.user),
            "reason": "personal_list",
        }
    if member is not None and member.user_id == user.id:
        lst = WineList.objects.filter(
            owner=user, kind=WineList.KIND_PERSONAL,
        ).first()
        items = []
        if lst is not None:
            items = list(
                WineListItem.objects.filter(list=lst, moved_at__isnull=True)
                .order_by("created")[:20]
            )
        return {
            "status": "ok",
            "kind": "list",
            "club": _club_meta(club),
            "scope": "personal",
            "member": display_name(member.user),
            "items": [
                {
                    "name": i.name,
                    "vintage": i.vintage,
                    "producer": i.producer or "",
                }
                for i in items
            ],
        }
    lst = WineList.objects.filter(kind=WineList.KIND_CLUB, club=club["club"]).first()
    items = []
    if lst is not None:
        items = list(
            lst.items.filter(moved_at__isnull=True)
            .select_related("added_by")
            .prefetch_related("added_by__telegram_users")
            .order_by("created")[:30]
        )
    return {
        "status": "ok",
        "kind": "list",
        "club": _club_meta(club),
        "scope": "club",
        "items": [
            {
                "name": i.name,
                "vintage": i.vintage,
                "producer": i.producer or "",
                "added_by": display_name(i.added_by) if i.added_by_id else "",
            }
            for i in items
        ],
    }


def _answer_comments(club, data) -> dict:
    wine_text = str(data.get("wine_text") or data.get("grape") or "").strip()
    qs = (
        ClubComment.objects.filter(event__club=club["club"])
        .select_related("event", "user")
        .prefetch_related("user__telegram_users")
        .order_by("-created")
    )
    if wine_text:
        qs = qs.filter(
            Q(event__wine_name__icontains=wine_text)
            | Q(event__producer__icontains=wine_text)
            | Q(text__icontains=wine_text)
        )
    rows = list(qs[:12])
    return {
        "status": "ok",
        "kind": "comments",
        "club": _club_meta(club),
        "query": wine_text,
        "comments": [
            {
                "text": c.text,
                "display_name": display_name(c.user) if c.user_id else "",
                "wine_name": c.event.wine_name,
                "vintage": c.event.vintage,
            }
            for c in rows
        ],
    }
