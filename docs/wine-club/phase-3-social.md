# Phase 3 — Comments, taste-offs, club-tied lists

**Goal:** talk about a bottle in the club, host a scored tasting, and save a bottle from the feed onto To-drink. Optional shared club list for the next night.

**Depends on:** phase 2 (clubs + feed). Club-tied lists also need phase 1. If phase 1 slipped, the “add from feed” PR includes the list models (do not fake it with `StorageItem`).

**Flags:** `feature_club_comments`, `feature_tasteoffs` (separate). Club lists reuse `feature_lists` **and** `feature_clubs`.

## Success metrics

| Signal | How | “It worked” |
| --- | --- | --- |
| Comments | `ClubComment` rows / `ClubEvent` opened | ≥ 1 comment from a **non-actor** (not talking to yourself) |
| Taste-off completes | `TasteOff.status=closed` with ≥ 2 scorers | One closed flight with two people is the loop |
| Feed → list | `WineListItem.source=club_feed` | ≥ 1 add-from-feed |
| Club list used | `WineList.kind=club` with ≥ 2 items | Nice-to-have; don’t block on it |
| Scoring drop-off | taste-offs opened vs closed | If none close in 2 weeks, the flow is too long |

## User journeys

### J3.1 Comment on a feed card

Sam’s DM of `Lee opened a 2019 Merlot 👎` gains a button `[Say something]` when `feature_club_comments` is on.

Tap → `Reply to this message with a short note.` Next text from Sam (not a command) is the comment, max 280 chars. Bot posts:

EN (edit original if we still have `message_id`, else a follow-up in the club feed view):

```
🍷 Lee opened a 2019 Merlot 👎

Sam: we had that in Stellenbosch
```

AF button: `Sê iets`. Prompt: `Antwoord op hierdie boodskap met 'n kort nota.`

Cap 5 comments shown on the card, then `and 3 more` → `/club` feed detail.

Empty / too long: `Keep it under 280 characters.` Commands (`/fridge`) abandon the pending comment (group `-1` handler, same as invite naming).

Off-topic: still stored — we are not a moderator in private clubs.

### J3.2 Add to my To-drink from a feed item

Button `[To-drink list]` on the opened event. Copies snapshot (`wine_name`, `vintage`, `producer`, …) onto the actor’s household default list, `source=club_feed`. **Not** a live FK to Lee’s `Wine`. Copy: `On your To-drink list.` Idempotent per `(list, name, vintage, producer)` best-effort, or always insert if no `wine_id`.

### J3.3 Club list for the next tasting

Owner `/club list` → get_or_create `WineList(kind=club, club_id=..., name="Thursday reds")`. Members can `Add` from a peek wine (full visibility) or type a label. Anyone in the club can view. Visibility of **items** is names/vintages only (never prices). Removing: owner or the adder.

This is **not** peek of someone’s fridge. It is an explicit shared checklist.

### J3.4 Taste-off

Host (any member in MVP; recommended: any member, owner can close):

1. `/club tasteoff` → `Name this tasting?` (type, default `Thursday reds · 5 Nov`).
2. Add bottles: from club list, from a member’s **full** peek (404 if summary/none — copy: `This club only shares a summary — type a name instead.`), or type `Producer Name 2019`.
3. Min 2, max 8 bottles. `[Open scoring]`.
4. Club gets a DM: `Alex is running a taste-off: Thursday reds · 5 Nov. Score 1–5.` Buttons: one card per wine, `1 2 3 4 5`. Changeable until close.
5. Host `[Close]`. Results event:

```
🏆 Thursday reds · 5 Nov

1. Chenin 2022 — 4.3 (3 scores)
2. Merlot 2019 — 3.7 (3 scores)
```

Do **not** write `StorageItem.vote` or `Wine.rating` (decided default). Feed thumbs stay 👎/—/👍; taste-offs are 1–5.

Abandon: `/club` or `/fridge` cancels setup (not an open scoring). Open scoring stays until Close or 7 days then auto-close.

## Telegram

Callbacks (numeric, `cl:`):

| data | meaning |
| --- | --- |
| `cl:cm:<event_id>` | start comment (pending in memory TTL 15 min) |
| `cl:td:<event_id>` | add snapshot to To-drink |
| `cl:to` | start taste-off |
| `cl:ta:<tasteoff_id>:<bottle_id>` | add bottle (host) |
| `cl:ts:<tasteoff_id>:<bottle_id>:<1-5>` | score |
| `cl:tc:<tasteoff_id>` | close |

`cl:ts:12:34:5` is fine under 64 bytes.

Pending comment state is **in memory** like `InviteFlow` (lost on restart → `Tap Say something again.`).

### Copy (EN / AF)

| key | EN | AF |
| --- | --- | --- |
| `club.comment.btn` | Say something | Sê iets |
| `club.comment.prompt` | Reply to this message with a short note. | Antwoord op hierdie boodskap met 'n kort nota. |
| `club.comment.saved` | Noted. | Genoteer. |
| `club.td.btn` | To-drink list | To-drink-lys |
| `club.to.title` | Name this tasting? | Wat noem ons die proe? |
| `club.to.score` | How was it? 1 (low) – 5 (high) | Hoe was dit? 1 (swak) – 5 (sterk) |
| `club.to.closed` | Tasting closed. | Proe is afgesluit. |
| `club.to.need_two` | Add at least two wines. | Voeg ten minste twee wyne by. |
| `club.comment.too_long` | Keep it under 280 characters. | Hou dit onder 280 karakters. |
| `club.comment.again` | Tap Say something again. | Tik Sê iets weer. |
| `club.comment.more` | and {n} more | en nog {n} |
| `club.td.added` | On your To-drink list. | Op jou To-drink-lys. |
| `club.list.title` | Club list | Klublys |
| `club.list.empty` | Nothing on the club list yet. | Niks op die klublys nie. |
| `club.to.open` | Open scoring | Begin telling |
| `club.to.close` | Close tasting | Sluit die proe |
| `club.to.running` | {host} is running a taste-off: {title}. Score 1–5. | {host} hou 'n proe: {title}. Tel 1–5. |
| `club.to.summary_only` | This club only shares a summary — type a name instead. | Hierdie klub deel net 'n opsomming — tik liewer 'n naam. |
| `club.to.already` | There's already a tasting open. | Daar is al 'n proe oop. |

## Edge cases

| Case | Behaviour |
| --- | --- |
| Comment on retracted event | 409 `retracted` |
| Comment as non-member | 404 |
| Taste-off in summary-only club | typed names only, no peek pick |
| Two taste-offs at once | one `status=scoring` per club; 409 `already_open` |
| Host leaves mid-flight | owner can Close; scores stand |
| Score 0 or 6 | 400 |
| Flag off | hide buttons; API 404 `not_enabled` |
| List flag off, tap To-drink on feed | `To-drink lists aren't on yet.` |
| Restart during comment prompt | prompt lost; tell them to tap again |
| HTML in comment | `esc()` always |

## Data model

```python
class ClubComment(models.Model):
    event = models.ForeignKey(ClubEvent, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    text = models.CharField(max_length=280)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created"]
        indexes = [models.Index(fields=["event", "created"], name="clubcomment_event_idx")]

class TasteOff(models.Model):
    STATUS = [("setup", "Setup"), ("scoring", "Scoring"), ("closed", "Closed")]
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="tasteoffs")
    host = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    title = models.CharField(max_length=80)
    status = models.CharField(max_length=8, choices=STATUS, default="setup")
    created = models.DateTimeField(auto_now_add=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

class TasteOffBottle(models.Model):
    tasteoff = models.ForeignKey(TasteOff, on_delete=models.CASCADE, related_name="bottles")
    label = models.CharField(max_length=160)  # snapshot
    wine_id = models.IntegerField(null=True, blank=True)  # not FK
    sort_order = models.PositiveSmallIntegerField(default=0)

class TasteOffScore(models.Model):
    tasteoff = models.ForeignKey(TasteOff, on_delete=models.CASCADE, related_name="scores")
    bottle = models.ForeignKey(TasteOffBottle, on_delete=models.CASCADE, related_name="scores")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    score = models.PositiveSmallIntegerField()  # 1–5
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["bottle", "user"], name="unique_tasteoff_score"),
            models.CheckConstraint(check=models.Q(score__gte=1, score__lte=5), name="tasteoff_score_1_5"),
        ]
```

`WineList.kind=club`: set `club_id` (phase 1 column) and, in this phase, add a real `ForeignKey("club.Club", null=True)` replacing the integer if we want integrity. Migration: add FK, backfill, drop integer if unused.

Partial unique: one `scoring` taste-off per club:

```python
UniqueConstraint(fields=["club"], condition=Q(status="scoring"), name="one_open_tasteoff_per_club")
```

On Close, insert `ClubEvent(kind=tasteoff, payload={title, rankings})`.

## API

All club-member auth (`ClubMembership` for this club). Flags per resource (`feature_club_comments`, `feature_tasteoffs`, `feature_lists` ∧ `feature_clubs` for list routes). 404 `{ "error": "not_enabled" }` when the flag is off.

### Comments

**`POST /api/clubs/<id>/events/<event_id>/comments/`**

Body: `{ "text": "we had that in Stellenbosch" }`

201:

```json
{
  "id": 12,
  "event_id": 88,
  "text": "we had that in Stellenbosch",
  "display_name": "Sam",
  "created": "2026-11-05T19:10:00+00:00"
}
```

400 `{ "error": "blank" }` or `{ "error": "too_long", "max": 280 }`. 409 `{ "error": "retracted" }`. 404 if event not in this club or caller not a member.

**`GET /api/clubs/<id>/events/<event_id>/comments/`** → `{ "comments": [ ... ], "count": 4 }`. Oldest first.

### Feed → personal To-drink

**`POST /api/clubs/<id>/events/<event_id>/to-list/`**

No body. Copies `wine_name` / `vintage` / `producer` onto the caller’s default household To-drink list (`source=club_feed`). 201 list-item shape from phase 1. 200 if an open row with the same snapshot already exists. 409 if event retracted. 404 if lists flag off.

Does **not** create `StorageItem`. Does **not** set `wine_id` to the actor’s wine (other household).

### Club list

**`GET /api/clubs/<id>/list/`** — get_or_create `WineList(kind=club, club_id=id, name=club.name)` owned by the founder’s household for the unique-name constraint (or `household=null` — **recommended:** `household` of the club owner, `kind=club`, unique on `club_id` instead of household+name for club rows). Phase 3 migration:

```python
UniqueConstraint(fields=["club_id"], condition=Q(kind="club", club_id__isnull=False),
                 name="unique_club_list_per_club")
```

Response:

```json
{
  "id": 9,
  "name": "Thursday reds",
  "kind": "club",
  "club_id": 3,
  "items": [
    {"id": 70, "name": "Chenin Blanc", "vintage": 2022, "producer": "Coastal Cellars", "added_by_name": "Alex"}
  ]
}
```

**`POST /api/clubs/<id>/list/items/`** `{ "name", "vintage", "producer", "wine_id"? }` — `wine_id` only if it came from a **full** peek of a member who accepted current version; otherwise ignore `wine_id` and store the snapshot. 201 item. Never copy price.

**`DELETE /api/clubs/<id>/list/items/<item_id>/`** — 200 if caller is club owner **or** `added_by`. 403 otherwise.

### Taste-offs

**`POST /api/clubs/<id>/tasteoffs/`** `{ "title": "Thursday reds · 5 Nov" }` → 201:

```json
{
  "id": 4,
  "title": "Thursday reds · 5 Nov",
  "status": "setup",
  "host_id": 2,
  "bottles": []
}
```

409 `{ "error": "already_open" }` if another `setup` or `scoring` exists for this club.

**`POST /api/clubs/<id>/tasteoffs/<tid>/bottles/`** `{ "label": "Chenin Blanc 2022", "wine_id": null }` → 201 bottle `{ id, label, sort_order }`. 400 if already 8 bottles. 409 if status ≠ setup.

**`DELETE /api/clubs/<id>/tasteoffs/<tid>/bottles/<bid>/`** — host only, status=setup.

**`POST /api/clubs/<id>/tasteoffs/<tid>/open/`** setup → scoring. 409 `{ "error": "need_two" }` if `< 2` bottles. Sets `opened_at`. Triggers fan-out.

**`GET /api/clubs/<id>/tasteoffs/<tid>/`** — members. Scores of others hidden until `closed` (**recommended:** hide others; show own). After close, include averages.

**`PATCH /api/clubs/<id>/tasteoffs/<tid>/scores/`** `{ "bottle_id": 1, "score": 4 }` — upsert. 400 if score not 1–5. 409 if not `scoring`.

**`POST /api/clubs/<id>/tasteoffs/<tid>/close/`** — host or club owner. Writes `ClubEvent(kind=tasteoff, payload={title, rankings: [{label, average, n}]})`. 409 if already closed.

400/403/404 as usual.

## Permissions and privacy

Comments are club-visible, not public. Club list is not a cellar dump. Taste-off labels are snapshots. Peek-pick of bottles only if club `cellar_visibility=full` **and** that member accepted current version. Summary clubs type names.

Do not copy scores onto household stock.

## Notifications / Celery

- New comment: optional edit of original feed DMs (deliveries) **or** skip edits (11 users can `/club feed`). Recommended: edit if `len(comments) <= 3` and `message_id` known; else don’t spam.
- Taste-off open: `deliver_club_event` style DM to unmuted members (not host).
- Auto-close: beat task `close_stale_tasteoffs` daily, 7 days after `opened_at`.

Extracted `send_telegram_message` from phase 2.

## Web UI

Admin for comments and taste-offs. Optional club page section. No React. Not required.

## Test plan

- comment 280 cap, esc in HTML
- non-member 404
- retracted event 409
- to-list does not create `StorageItem`
- to-list 404 if lists flag off
- taste-off 1–5 check constraint
- one scoring per club
- close writes rankings in payload, no `StorageItem.vote` change
- auto-close beat
- callback_data length for `cl:ts:`
- EN/AF keys

## Rollout

Flags off by default after deploy. Turn on **comments** first (small). Taste-offs second. Club lists third (needs phase 1). Rollback = flag off; hide buttons.

## Task breakdown (separate PRs)

| PR | Scope | Effort |
| --- | --- | --- |
| **3a** | `ClubComment` + API + bot Say something + pending text + copy EN+AF | M |
| **3b** | Feed → To-drink (`source=club_feed`); depends on phase 1 | S |
| **3c** | Club `WineList.kind=club` + API + `/club list` | S–M |
| **3d** | TasteOff models + API (no bot) | M |
| **3e** | Taste-off bot flow + DMs + close card | M |
| **3f** | Beat auto-close; flags; CLAUDE.md; `/help` lines | S |

**3a** can ship alone. **3b/3c** after lists. **3d→3e** together in sequence.

**Phase total: M–L.**
