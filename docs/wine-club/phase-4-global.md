# Phase 4 — Global clubs

**Goal:** a few staff-run rooms with a regular themed note (e.g. “New World Cab”, “Drink-by this month”). Same bot, same feed machinery, **no cellar peek**.

**Depends on:** phase 2.

**Flag:** `feature_global_clubs`.

## Success metrics

| Signal | How | “It worked” |
| --- | --- | --- |
| Someone joins | `ClubMembership` on `kind=global` | ≥ 3 members besides staff |
| Themed note lands | beat-created `ClubEvent` or staff post | 1 note/week actually sent, mute-rate < 50% |
| Peek never used | GET cellar on global | **zero** 200s (forced `none`) |

If mutes spike, slow the beat or kill the club.

## User journeys

### J4.1 Browse and join

`/club` home grows `[Discover]` when the flag is on (or `/club discover`). List global clubs:

```
🌍 New World Cab
Drink-by this month

[Join New World Cab]
```

Join card: **feed only**, no cellar stanza. Drink-opens **are** posted if the member drinks (decided, open-q #19) — the card must say so. Mute available. Do **not** auto-post household drink-by into global.

EN:

```
New World Cab is a Whiney club for everyone.

Joining means drink-opens (if you post them — you don't have to)
and a note from us now and then. Your cellar stays private.

[Join] [Not now]
```

AF: `Jou kelder bly privaat.`

No household picker. `ClubMembership.household` may be set for feed attribution but `cellar_visibility` is **forced `none`** (decided). No heads-up to household partners (nothing became visible).

### J4.2 Themed update

Staff `/club post New World Cab` (staff filter) or beat. Members with `feed_muted=False` get:

```
🌍 New World Cab

This week: Cabs that actually drink well now.
If you've got one, open it — the club will see.
```

Not a live web search per member. Staff writes the note. Optional later: beat copies drink-by wines **without names of households** — skip in v1; too easy to leak.

### J4.3 Leave / mute

Same as private clubs. No peek buttons on member cards. Private-club **dismantle votes** (J2.10) do **not** apply here — staff freeze / kick / admin delete.

## Telegram

Staff commands (already a staff menu): `("clubpost", "Post to a global club")` in `ADMIN_COMMANDS` + `set_bot_presence`.

Callbacks: `cl:dj:<club_id>` discover join. Reuse home/mute/leave.

### Copy catalog (new keys)

| key | EN | AF |
| --- | --- | --- |
| `club.discover.title` | Clubs for everyone | Klubs vir almal |
| `club.discover.btn` | Discover | Ontdek |
| `club.global.consent` | {name} is a Whiney club for everyone.\n\nJoining means drink-opens (if you drink a bottle, the club can see that you opened it) and a note from us now and then. Your cellar stays private. | {name} is 'n Whiney-klub vir almal.\n\nAansluit beteken drink-oops (as jy 'n bottel oopmaak, kan die klub dit sien) en nou en dan 'n nota van ons. Jou kelder bly privaat. |
| `club.global.private_cellar` | Your cellar stays private. | Jou kelder bly privaat. |
| `club.global.join` | Join | Sluit aan |
| `club.global.not_now` | Not now | Nou nie |
| `club.global.frozen` | This club is paused. | Hierdie klub is gepouseer. |
| `club.global.kicked` | You've been removed from {name}. | Jy is uit {name} verwyder. |

## Edge cases

| Case | Behaviour |
| --- | --- |
| Owner tries to set visibility on global | 400 `{ "error": "global_clubs_are_feed_only" }` |
| Found-as-global by non-staff | 403 |
| Private club converted to global | **Forbidden** (no PATCH kind) |
| Member of private + global | two homes; picker |
| Drink-by note naming a wine in Hillside | **Do not auto-post household drink-by into global** |
| Staff post empty | 400 |
| Flag off | hide Discover; staff post 404 |

## Data model

No new tables. Use `Club.kind=global`, `Club.description` for the blurb, `created_by` staff user.

Constraint:

```python
CheckConstraint(
    check=~Q(kind="global") | Q(cellar_visibility="none"),
    name="global_club_feed_only",
)
```

`badges_visible` default **false** for global (recommended). Feed of opens still works if a member drinks (their display name goes to a public-ish room — join card must say so). Recommended join stanza: `If you drink a bottle, the club can see that you opened it.` Make that explicit.

Optional `ClubEvent.kind=staff_note`.

## API

Private clubs stay invite-only. Do not add self-join to private.

**`POST /api/clubs/global/`** staff. Body `{ "name": "New World Cab", "description": "Cabs that drink well now." }`. Forces `kind=global`, `cellar_visibility=none`, `badges_visible=false`. 201 club object (phase 2 create shape). 403 non-staff. 409 if this staff user already founded that name.

**`GET /api/clubs/discover/`** any provisioned user:

```json
{
  "clubs": [
    {
      "id": 10,
      "name": "New World Cab",
      "description": "Cabs that drink well now.",
      "member_count": 6,
      "joined": false,
      "frozen": false
    }
  ]
}
```

Empty list if flag off (or 404 `not_enabled` — pick 404 for consistency).

**`POST /api/clubs/<id>/join/`** `{ }` — 201 membership if `kind=global` and not frozen. 403 `{ "error": "invite_only" }` on private. 409 `{ "error": "already_member" }` idempotent-as-200 is also fine (match household redeem: 200 `already_member`). Sets `accepted_sharing_version = club.sharing_version`, `household` = caller’s default (unused for peek). No heads-up.

**`POST /api/clubs/<id>/notes/`** staff `{ "text": "This week: Cabs that actually drink well now." }` → 201 `{ "event_id": 91 }` and `deliver_club_event`. 400 empty. 403 non-staff. 409 if frozen.

**`POST /api/clubs/<id>/members/<user_id>/kick/`** staff. 200 `{ "status": "kicked" }`. 404 if not a member. Does not delete the club. Optional DM using `club.global.kicked`.

**`PATCH /api/clubs/<id>/`** staff may set `{ "frozen": true }` on global (and private). Publish no-ops when frozen. Members still `/club` and can Leave.

## Permissions and privacy

- Global = feed + staff notes. **No peek, no badges dump** (constraint).
- Opens still publish **if** the member drinks — called out on the join card. Mute available.
- No household heads-up (visibility none).
- Moderation: staff `POST /api/clubs/<id>/members/<user_id>/kick/` (phase 4, don’t skip). Frozen feed: `Club.frozen` boolean — staff sets, publish no-ops.

Add `frozen = models.BooleanField(default=False)` on `Club` (useful for private too; can land in 2a as a column default false).

## Notifications / Celery

Reuse `deliver_club_event`. Beat optional:

```python
# celery.py — only if staff want a weekly nudge, not a generated leaky digest
"global_club_nudge": crontab(minute="0", hour="10", day_of_week="thu"),
```

v1: **no beat**. Staff `/club post` is enough. Add beat later with a canned string from `Club.description`, not household data.

## Web UI

Admin: filter `kind=global`, freeze checkbox. No public website required.

## Test plan

- check constraint rejects global + summary
- self-join private 403
- self-join global 201
- peek 404 on global even if someone PATCHes (constraint)
- staff note fans out; muted skipped
- kick removes membership
- frozen publish no-op
- join copy asserts cellar stays private (string test)

## Rollout

Flag off. Staff create 1–2 globals in admin. Flag on. Discover button. Watch mute rate 2 weeks before a second post cadence.

Rollback: flag off; freeze the club.

## Task breakdown (separate PRs)

| PR | Scope | Effort |
| --- | --- | --- |
| **4a** | `kind=global` enforcement (constraint, API staff create, discover, self-join); `frozen` + kick | M |
| **4b** | Staff note API + bot `/club post` + Discover UI + join copy EN+AF | M |
| **4c** | Optional beat canned nudge; `/stats` global line; CLAUDE.md; admin | S |

**4a → 4b**. Skip 4c until a global club actually has members.

**Phase total: S–M.**
