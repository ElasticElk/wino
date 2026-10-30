"""Render structured club-ask payloads as Telegram HTML.

Button labels stay English. Body copy follows the asker's locale.
"""

from __future__ import annotations

import html

from integrations.copy import t


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _wine_line(wine: dict) -> str:
    name = _esc(wine.get("name") or "a bottle")
    vintage = wine.get("vintage")
    producer = _esc(wine.get("producer") or "")
    bit = f"{vintage} {name}" if vintage else name
    if producer:
        bit += f" ({producer})"
    count = wine.get("stock_count")
    if count and count > 1:
        bit += f" ×{count}"
    return bit


def render_club(payload: dict, locale: str) -> str:
    status = payload.get("status") or ""
    kind = payload.get("kind") or ""
    club = payload.get("club") or {}
    club_name = _esc(club.get("name") or "")
    if status == "no_clubs":
        return t(locale, "chat.club.no_clubs")
    if status == "need_club":
        return t(locale, "chat.club.pick_club")
    if status == "need_member":
        return t(locale, "chat.club.pick_member", club=club_name or t(locale, "chat.club.this_club"))
    if status == "not_found":
        target = payload.get("target") or ""
        if target == "club":
            return t(locale, "chat.club.not_found_club", name=_esc(payload.get("query") or ""))
        return t(locale, "chat.club.not_found_member", name=_esc(payload.get("query") or ""), club=club_name)
    if status == "private":
        return t(locale, "chat.club.private_list", name=_esc(payload.get("member") or ""))
    if status == "not_visible":
        reason = payload.get("reason") or ""
        if reason == "global":
            return t(locale, "chat.club.global_cellar", club=club_name)
        if reason == "badges":
            return t(locale, "chat.club.badges_hidden", club=club_name)
        if reason == "summary":
            return t(locale, "chat.club.summary_only", club=club_name)
        member = _esc(payload.get("member") or "")
        if member:
            return t(locale, "chat.club.private_cellar_member", name=member, club=club_name)
        return t(locale, "chat.club.private_cellar", club=club_name)
    if status != "ok":
        return t(locale, "chat.club.nothing")
    renderer = {
        "members": _render_members,
        "my_clubs": _render_my_clubs,
        "cellar": _render_cellar,
        "who_has": _render_who_has,
        "oldest": _render_oldest,
        "feed": _render_feed,
        "who_drinks": _render_who_drinks,
        "avg_rating": _render_avg_rating,
        "top_rated": _render_top_rated,
        "tasteoff": _render_tasteoff,
        "badges": _render_badges,
        "badge_progress": _render_badge_progress,
        "list": _render_list,
        "comments": _render_comments,
    }.get(kind)
    if renderer is None:
        return t(locale, "chat.club.nothing")
    return renderer(payload, locale)


def club_pick_buttons(payload: dict) -> list[tuple[str, str, int | None, int | None]]:
    """English button labels: (label, action, wine_id, page)."""
    status = payload.get("status") or ""
    choices = payload.get("choices") or []
    buttons = []
    if status in {"need_club", "not_found"} and choices:
        for choice in choices[:6]:
            if choice.get("kind") == "club":
                buttons.append((choice.get("label") or "Club", "cg", choice.get("id"), None))
            elif choice.get("kind") == "member":
                buttons.append((
                    choice.get("label") or "Member", "cm",
                    choice.get("id"), choice.get("club_id"),
                ))
        if status == "not_found" and (payload.get("target") == "member" or payload.get("kind") not in {"my_clubs"}):
            club_id = (payload.get("club") or {}).get("id")
            if club_id:
                buttons.append(("List members", "ca", club_id, None))
        if status == "not_found" and payload.get("target") == "club":
            buttons.append(("My clubs", "ca", 0, None))
        return buttons
    if status == "need_member":
        for choice in choices[:6]:
            buttons.append((
                choice.get("label") or "Member", "cm",
                choice.get("id"), choice.get("club_id"),
            ))
        return buttons
    if status == "not_visible" and (payload.get("choices") or []):
        for choice in (payload.get("choices") or [])[:4]:
            buttons.append((choice.get("label") or "Club", "cg", choice.get("id"), None))
        return buttons
    if status == "private":
        club_id = (payload.get("club") or {}).get("id")
        if club_id:
            buttons.append(("Club list", "ca", club_id, 1))
        return buttons
    if status == "no_clubs":
        return []
    return buttons


def _render_members(payload, locale):
    names = []
    for row in payload.get("members") or []:
        name = _esc(row.get("display_name") or "")
        role = row.get("role") or ""
        if role == "owner":
            names.append(t(locale, "chat.club.member_owner", name=name))
        else:
            names.append(name)
    club = _esc((payload.get("club") or {}).get("name") or "")
    body = "\n".join(f"• {n}" for n in names) or t(locale, "chat.club.members_empty")
    return t(locale, "chat.club.members", club=club, list=body)


def _render_my_clubs(payload, locale):
    lines = []
    for row in payload.get("clubs") or []:
        mark = " ✓" if row.get("is_last_used") else ""
        lines.append(f"• {_esc(row.get('name') or '')}{mark}")
    return t(locale, "chat.club.my_clubs", list="\n".join(lines) or t(locale, "chat.club.no_clubs"))


def _render_cellar(payload, locale):
    member = _esc(payload.get("member") or "")
    club = _esc((payload.get("club") or {}).get("name") or "")
    if payload.get("visibility") == "summary":
        grape = _esc(payload.get("grape") or "")
        count = payload.get("count")
        if grape:
            return t(
                locale, "chat.club.cellar_summary_grape",
                name=member, club=club, grape=grape, n=count,
            )
        return t(
            locale, "chat.club.cellar_summary",
            name=member, club=club, n=payload.get("total_bottles") or 0,
        )
    wines = payload.get("wines") or []
    if payload.get("count") == 0 and not wines:
        return t(locale, "chat.club.cellar_none", name=member, club=club)
    lines = "\n".join(f"• {_wine_line(w)}" for w in wines[:10])
    return t(
        locale, "chat.club.cellar_full",
        name=member, club=club, n=payload.get("count") or len(wines), list=lines,
    )


def _render_who_has(payload, locale):
    query = _esc(payload.get("query") or "")
    club = _esc((payload.get("club") or {}).get("name") or "")
    matches = payload.get("matches") or []
    summary = payload.get("summary_matches") or []
    if not matches and not summary:
        extra = ""
        if payload.get("skipped_summary"):
            extra = " " + t(locale, "chat.club.who_has_summary_note")
        return t(locale, "chat.club.who_has_none", query=query, club=club) + extra
    lines = []
    for row in matches:
        names = _esc(row.get("display_name") or "")
        wines = ", ".join(_wine_line(w) for w in (row.get("wines") or [])[:3])
        lines.append(f"• {names}: {wines or row.get('count')}")
    for row in summary:
        lines.append(
            t(locale, "chat.club.who_has_summary_line",
              name=_esc(row.get("display_name") or ""), n=row.get("count") or 0)
        )
    return t(locale, "chat.club.who_has", query=query, club=club, list="\n".join(lines))


def _render_oldest(payload, locale):
    club = _esc((payload.get("club") or {}).get("name") or "")
    wines = payload.get("wines") or []
    if not wines:
        return t(locale, "chat.club.oldest_none", club=club)
    lines = [
        f"• {_wine_line(w)} — {_esc(w.get('owner') or '')}"
        for w in wines
    ]
    return t(locale, "chat.club.oldest", year=payload.get("vintage") or "", club=club, list="\n".join(lines))


def _render_feed(payload, locale):
    club = _esc((payload.get("club") or {}).get("name") or "")
    events = payload.get("events") or []
    member = _esc(payload.get("member") or "")
    if not events:
        if member:
            return t(locale, "chat.club.feed_none_member", name=member, club=club)
        return t(locale, "chat.club.feed_none", club=club)
    lines = []
    for event in events:
        wine = _esc(event.get("wine_name") or "a bottle")
        vintage = event.get("vintage") or ""
        actor = _esc(event.get("actor") or "")
        bit = f"{vintage} {wine}".strip()
        lines.append(f"• {actor}: {bit}")
    return t(locale, "chat.club.feed", club=club, list="\n".join(lines))


def _render_who_drinks(payload, locale):
    club = _esc((payload.get("club") or {}).get("name") or "")
    ranking = payload.get("ranking") or []
    if not ranking:
        return t(locale, "chat.club.who_drinks_none", club=club)
    lines = [
        f"• {_esc(r.get('display_name') or '')}: {r.get('count') or 0}"
        for r in ranking[:8]
    ]
    return t(locale, "chat.club.who_drinks", club=club, list="\n".join(lines))


def _render_avg_rating(payload, locale):
    name = _esc(payload.get("member") or "")
    if payload.get("tasteoff_average") is not None:
        return t(
            locale, "chat.club.avg_tasteoff",
            name=name, n=payload["tasteoff_average"],
        )
    votes = payload.get("votes") or {}
    if not payload.get("vote_count"):
        return t(locale, "chat.club.avg_rating_none", name=name)
    return t(
        locale, "chat.club.avg_rating",
        name=name,
        up=votes.get("up") or 0,
        meh=votes.get("meh") or 0,
        down=votes.get("down") or 0,
    )


def _render_top_rated(payload, locale):
    club = _esc((payload.get("club") or {}).get("name") or "")
    wines = payload.get("wines") or []
    if not wines:
        return t(locale, "chat.club.top_rated_none", club=club)
    lines = []
    for wine in wines[:8]:
        name = _wine_line(wine)
        ups = wine.get("up") or 0
        lines.append(f"• {name} 👍{ups}")
    return t(locale, "chat.club.top_rated", club=club, list="\n".join(lines))


def _render_tasteoff(payload, locale):
    club = _esc((payload.get("club") or {}).get("name") or "")
    row = payload.get("tasteoff") or {}
    if payload.get("mode") == "none" or not row:
        return t(locale, "chat.club.tasteoff_none", club=club)
    title = _esc(row.get("title") or "")
    if payload.get("mode") == "next":
        return t(locale, "chat.club.tasteoff_next", title=title, club=club, status=row.get("status") or "")
    winner = row.get("winner") or {}
    if payload.get("mode") == "last" and winner.get("labels"):
        names = ", ".join(_esc(n) for n in winner["labels"])
        return t(locale, "chat.club.tasteoff_won", title=title, names=names, n=winner.get("average") or "")
    lines = []
    for bottle in row.get("bottles") or []:
        avg = bottle.get("average")
        label = _esc(bottle.get("label") or "")
        if avg is None:
            lines.append(f"• {label}")
        else:
            lines.append(f"• {label}: {avg}")
    return t(locale, "chat.club.tasteoff_scores", title=title, list="\n".join(lines) or "—")


def _render_badges(payload, locale):
    name = _esc(payload.get("member") or "")
    rows = payload.get("achievements") or []
    if not rows:
        return t(locale, "chat.club.badges_none", name=name)
    lines = [
        f"• {row.get('emoji') or ''} {_esc(row.get('title') or '')}".strip()
        for row in rows[:12]
    ]
    return t(locale, "chat.club.badges", name=name, list="\n".join(lines))


def _render_badge_progress(payload, locale):
    rows = payload.get("closest") or []
    if not rows:
        return t(locale, "chat.club.badge_progress_none")
    top = rows[0]
    return t(
        locale, "chat.club.badge_progress",
        name=_esc(top.get("display_name") or ""),
        title=_esc(top.get("title") or ""),
        have=top.get("have") or 0,
        nxt=top.get("next") or 0,
    )


def _render_list(payload, locale):
    items = payload.get("items") or []
    club = _esc((payload.get("club") or {}).get("name") or "")
    if not items:
        if payload.get("scope") == "personal":
            return t(locale, "chat.club.list_empty_personal")
        return t(locale, "chat.club.list_empty", club=club)
    lines = []
    for item in items[:15]:
        bit = _wine_line(item)
        added = _esc(item.get("added_by") or "")
        lines.append(f"• {bit}" + (f" — {added}" if added else ""))
    if payload.get("scope") == "personal":
        return t(locale, "chat.club.list_personal", list="\n".join(lines))
    return t(locale, "chat.club.list", club=club, list="\n".join(lines))


def _render_comments(payload, locale):
    rows = payload.get("comments") or []
    query = _esc(payload.get("query") or "")
    if not rows:
        return t(locale, "chat.club.comments_none", query=query)
    lines = [
        f"• {_esc(c.get('display_name') or '')}: {_esc(c.get('text') or '')}"
        for c in rows[:8]
    ]
    return t(locale, "chat.club.comments", query=query, list="\n".join(lines))
