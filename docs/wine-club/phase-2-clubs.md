# Phase 2 — Private clubs, feed, cellar sharing

**Goal:** a private club with an automatic drink/vote feed, and cellar peek at a **club-wide** policy the owner sets. Join = consent. This is the network adoption loop.

**Depends on:** phase 0 (working `/start` invites). Phase 1 is independent; peek does not need lists.

**Flag:** `ServiceConfig.feature_clubs`. Feed publish **no-ops** when off.

Peek ships **in this phase**. A join card that promises sharing without peek is a broken contract.

## Success metrics

| Signal | How | “It worked” |
| --- | --- | --- |
| Clubs exist | `Club` rows `kind=private` | ≥ 1 club with ≥ 2 members in 2 weeks |
| Invites convert | `ClubInvite` use_count / minted | ≥ 1 redeem (a second person in a club) |
| Feed is the habit | `ClubEvent.kind=opened` per week | Opens in a club > 0, and `last_seen_at` on a **non-actor** member within 24h of an event |
| Peek used | `GET .../members/<id>/cellar/` 200s | ≥ 1 peek (the “bring something else” moment) |
| Consent understood | leave-rate in first 24h after join | Not a hard fail; spike after a widen is a smell |
| Mute vs leave | `feed_muted` vs membership delete | Mute > leave is healthy; leave-all is the product dying |
| Heads-up | `Household.club_share_notice_sent_at` set | Sent once, never includes club/user names (assert in tests) |

Staff: `/stats` lines `clubs: 2 (7 members), feed events 7d: 12`. Admin changelist.

## User journeys

### J2.1 Found a club

Alex `/club` (no clubs) → pitch + `Found a club`.

EN:

```
Wine clubs are for drinking with people who have their own cellars.

Not a shared fridge — you each keep your own.

[Found a club]
```

AF:

```
Wynklubs is om te drink met mense wat hul eie kelders het.

Nie 'n gedeelde yskas nie — elkeen hou sy eie.

[Begin 'n klub]
```

Tap → `What should we call it?` (type, 2–40 chars, same `NAME_MIN`/`NAME_MAX` as household). Then **sharing step** (required):

```
What does Thursday reds share?

Everyone who joins shares this with the club.

[Type and count]     ← default, selected
[Full cellar]
[Drink feed only]

Badges: [On]  [Off]

[Create club]
```

AF: `Wat deel Thursday reds?` / `Tipe en aantal` / `Vol kelder` / `Net die drink-lys` / `Kentekens`.

Create → home card + invite immediately (J2.2).

### J2.2 Invite

`/club invite` or `[Invite]`. One forwardable message. Policy stanza **in the same message**.

EN:

```
Join Thursday reds on Whiney.

Joining shares a type-and-count summary of your cellar
and your badges with the other members. Drink-opens
are posted automatically. Not a shared fridge.

Tap this, then Start:
https://t.me/<bot>?start=K9m2Pq4r

Forward this, or tap Share.

Works until 28 Nov, up to 20 uses.
```

AF: `Sluit by Thursday reds aan op Whiney.` + `Aansluit deel 'n tipe-en-aantal opsomming van jou kelder en jou kentekens met die ander lede.`

Button: `Share invite`.

`/club invite cancel` — newest unused club invite for **this** club.

### J2.3 Join — already has a cellar

Lee `/start K9m2Pq4r` → club preview (staff). Card:

EN:

```
Alex invited you to Thursday reds.

Joining shares a type-and-count summary of Hillside,
including bottles anyone in that cellar added, and
your badges. Drink-opens are posted to the club.
Not a shared fridge.

[Join Thursday reds]  [Not now]
```

Several households: picker **before** Join (`Which cellar should Thursday reds see?`).

Join → `ClubMembership` with `accepted_sharing_version = club.sharing_version`, `household` set. Hot-add if needed. Home card. Other Hillside members: heads-up **if** this is the first club visibility for Hillside (J2.8).

Not now: keep the link; `No problem. That link still works until 28 Nov.`

### J2.4 Join — stranger (compound)

Same preview, then new-cellar name / suburb / Skip (`I1_COPY` / `I2_COPY`). After the cellar exists, **consent card again with the new household name**, then Join. Redeem: household `kind=new` **and** club membership in one staff call (or two in one Django view). Never a second cellar later — household Stay/Start guards still apply.

### J2.5 Automatic feed

Lee drinks a 2019 Merlot (any drank path in the current stock table). For each of Lee’s clubs (not muted **posting** — MVP has no post-mute, only receive mute): insert `ClubEvent(kind=opened)`. Celery fans out to other members with `feed_muted=False`.

EN to Sam:

```
🍷 Lee opened a 2019 Merlot
```

After vote 👎, edit that message:

```
🍷 Lee opened a 2019 Merlot 👎
```

Meh: no extra glyph (or em-dash, matching `VOTE_LABELS`). Undo within 10 min: edit to `Lee put the 2019 Merlot back.` using `ClubEventDelivery.message_id`.

Actor does not get their own ping. Catch-up **Moved**: no event. Catch-up **Drunk**: one event per bottle **on Apply**, not on staging taps.

Join event: `Lee joined` to the others (not to Lee).

### J2.6 Home, members, peek

`/club` with one club → home. Several → picker, remember last-used (`ClubMembership.is_last_used`). Setting `is_last_used=True` must unset the user’s other rows in the same transaction (partial unique `one_last_used_club_per_user`).

```
🍷 Thursday reds · 4 members
Summary · badges on

Lee opened a 2019 Merlot 👎
Sam opened a 2022 Chenin 👍
Pat joined

[Invite] [Members] [Feed] [Mute]
```

Owner also `[Settings]`.

`[Members]`: names, owner crown. Policy line under the title, **not** per-row ticks. Tap Lee → profile: latest open, `[Show cellar]` if visibility is summary/full **and** Lee’s `accepted_sharing_version` matches (or previous level — see widen), `[Badges]` if club badges on and that household’s `gamification_enabled`.

**Summary peek:**

```
Hillside · summary

Red (6) · White (4) · Sparkling (1)
Merlot ×6 · Chenin ×2 · Chardonnay ×2

[Back]
```

**Full peek:** up to 30 lines `Merlot 2019 · Stellenbosch · ×2`, then `and 8 more`. Never prices, drawers, notes, barcodes, critic/Vivino.

Two Hillside members in the club: one Hillside card, not two.

### J2.7 Mute / leave / settings

Mute: `feed_muted=True`. Copy: `Muted Thursday reds. Opens still go to the club; you won't get pings. /club then Unmute.`

Leave: membership deleted. Copy: `You've left Thursday reds.` Historical events keep the denormalised name.

Owner leave with others: must `[Hand over]` first (pick a member → they become owner). Last member leave **deletes** the club and its events (decided default).

Settings (owner): same sharing radios as create. Narrowing applies immediately, auto-acks `accepted_sharing_version`. Widening: J2.9.

### J2.8 Household heads-up (decided)

When a join makes a household visible to a club (`cellar_visibility` in `summary|full`) **and** `Household.club_share_notice_sent_at` is null:

DM every other active `TelegramUser` on that household (not the joiner):

```
Just so you know — this cellar is now shared
inside a wine club.

Only people in the club can see it, and only
what the club allows. Whiney never shares a
cellar any other way.
```

AF:

```
Net gou — hierdie kelder word nou binne 'n
wynklub gedeel.

Net mense in die klub kan dit sien, en net
wat die klub toelaat. Whiney deel nooit 'n
kelder op 'n ander manier nie.
```

No buttons. No club name. No member name. Set `club_share_notice_sent_at=now()`. Do not send again until sharing fully stops (no memberships with visibility ≠ none for this household), then clear the timestamp so a later join can ping once more.

Feed-only clubs (`none`): no heads-up.

### J2.9 Owner widens sharing (recommended)

Summary → full, or badges off → on, or none → summary: increment `sharing_version`, store previous visibility on the club. DM members (not the owner):

EN:

```
Thursday reds now shares the full cellar
(names and vintages, still no prices).

You already share a summary. Stay to share
the full list, or leave.

[Stay] [Leave]
```

Peek at the **new** level only after Stay (`accepted_sharing_version = current`). Unacked members stay visible at the **previous** level. Narrowing: no DM required; apply now.

## Telegram — commands, callbacks, copy

**Command:** `/club` in `PUBLIC_COMMANDS` (`Wine clubs`). Subcommands typed: `/club new`, `/club invite`, `/club invite cancel`, `/club feed`, `/club members`, `/club leave`. Menu stays one item.

Module: `scanner-service/integrations/telegram_clubs.py`. Invite `/start` dispatcher: household preview 404 → club preview (one staff URL, recommended). Prefix `cl:` for callbacks, 64 bytes.

| callback | meaning |
| --- | --- |
| `cl:f` | found / create start |
| `cl:v:<n>` | visibility pick `0=none,1=summary,2=full` while founding (store name in memory TTL 30 min, like `InviteFlow`) |
| `cl:b:<0\|1>` | badges pick |
| `cl:c` | confirm create |
| `cl:h:<club_id>` | home |
| `cl:m:<club_id>` | members page |
| `cl:p:<club_id>:<user_id>` | peek (user_id of member; if household collapsed, user_id of any member of that household) |
| `cl:g:<club_id>:<user_id>` | badges |
| `cl:u:<club_id>` | mute/unmute toggle |
| `cl:l:<club_id>` | leave confirm |
| `cl:s:<club_id>` | owner settings |
| `cl:w:<club_id>:stay` | re-consent stay |
| `cl:fd:<club_id>:<page>` | feed page |
| `cl:x:<club_id>` | picker / switch last-used |

Club ids and user ids are integers; `cl:p:12:34` is well under 64 bytes.

Founding name is **typed**, not in callback_data (same as new-cellar). Memory TTL 30 min; buttons that need the name after restart re-ask.

`/help` adds `/club — Wine clubs`. `/status` adds `Clubs: ready` when flag on.

### Copy catalog (new keys)

Full table: [copy-catalog.md](copy-catalog.md). Phase 2 keys:

| key | EN | AF |
| --- | --- | --- |
| `club.off` | Wine clubs aren't on yet. | Wynklubs is nog nie aan nie. |
| `club.err` | Couldn't reach the club just now. | Kon nie die klub nou bereik nie. |
| `club.not_member` | You're not in that club. | Jy is nie in daai klub nie. |
| `club.forbidden` | Only the owner can change that. | Net die eienaar kan dit verander. |
| `club.name_taken` | You already have a club with that name. | Jy het al 'n klub met daai naam. |
| `club.fresh` | That invite has expired or was already used. Ask them for a fresh link. | Daai uitnodiging het verval of is al gebruik. Vra vir 'n nuwe skakel. |
| `club.unreachable` | Couldn't reach the cellar just now — tap the link again in a minute. | Kon nie die kelder nou bereik nie — tik die skakel oor 'n minuut weer. |
| `club.pitch` | Wine clubs are for drinking with people who have their own cellars.\n\nNot a shared fridge — you each keep your own. | Wynklubs is om te drink met mense wat hul eie kelders het.\n\nNie 'n gedeelde yskas nie — elkeen hou sy eie. |
| `club.found.btn` | Found a club | Begin 'n klub |
| `club.found.name` | What should we call it? | Wat noem ons dit? |
| `club.share.ask` | What does {name} share?\n\nEveryone who joins shares this with the club. | Wat deel {name}?\n\nAlmal wat aansluit deel dit met die klub. |
| `club.share.summary` | Type and count | Tipe en aantal |
| `club.share.full` | Full cellar | Vol kelder |
| `club.share.none` | Drink feed only | Net die drink-lys |
| `club.share.badges_on` | Badges: on | Kentekens: aan |
| `club.share.badges_off` | Badges: off | Kentekens: af |
| `club.create.btn` | Create club | Skep klub |
| `club.invite.body` | Join {name} on Whiney.\n\n{policy}\n\nTap this, then Start:\n{link}\n\nForward this, or tap Share.\n\nWorks until {date}, up to {n} uses. | Sluit by {name} aan op Whiney.\n\n{policy}\n\nTik hier, dan Start:\n{link}\n\nStuur aan, of tik Deel.\n\nWerk tot {date}, tot {n} keer. |
| `club.policy.summary` | Joining shares a type-and-count summary of your cellar and your badges with the other members. Drink-opens are posted automatically. Not a shared fridge. | Aansluit deel 'n tipe-en-aantal opsomming van jou kelder en jou kentekens met die ander lede. Drink-oops word outomaties geplaas. Nie 'n gedeelde yskas nie. |
| `club.policy.full` | Joining shares the names and vintages in your cellar (no prices or drawers) and your badges. Drink-opens are posted automatically. Not a shared fridge. | Aansluit deel die name en oesjare in jou kelder (geen pryse of laaie nie) en jou kentekens. Drink-oops word outomaties geplaas. Nie 'n gedeelde yskas nie. |
| `club.policy.none` | Joining posts drink-opens automatically. Your cellar stays private. | Aansluit plaas drink-oops outomaties. Jou kelder bly privaat. |
| `club.join.known` | {inviter} invited you to {club}.\n\nJoining shares {policy_of} {household}, including bottles anyone in that cellar added. Drink-opens are posted to the club. Not a shared fridge. | {inviter} het jou na {club} genooi.\n\nAansluit deel {policy_of} {household}, insluitend bottels wat enigiemand in daai kelder bygevoeg het. Drink-oops gaan na die klub. Nie 'n gedeelde yskas nie. |
| `club.join.btn` | Join {name} | Sluit by {name} aan |
| `club.join.not_now` | Not now | Nou nie |
| `club.join.pick_hh` | Which cellar should {club} see? | Watter kelder moet {club} sien? |
| `club.not_now.ok` | No problem. That link still works until {date}. | Geen probleem. Daai skakel werk tot {date}. |
| `club.feed.opened` | {name} opened a {vintage} {wine} | {name} het 'n {vintage} {wine} oopgemaak |
| `club.feed.put_back` | {name} put the {vintage} {wine} back. | {name} het die {vintage} {wine} teruggesit. |
| `club.feed.joined` | {name} joined | {name} het aangesluit |
| `club.muted` | Muted {name}. Opens still go to the club; you won't get pings. /club then Unmute. | {name} is stil. Oops gaan nog na die klub; jy kry nie pings nie. /club dan Unmute. |
| `club.left` | You've left {name}. | Jy het {name} verlaat. |
| `club.handover.need` | Hand the club to someone else first. | Gee eers die klub aan iemand anders. |
| `club.hh.notice` | Just so you know — this cellar is now shared\ninside a wine club.\n\nOnly people in the club can see it, and only\nwhat the club allows. Whiney never shares a\ncellar any other way. | Net gou — hierdie kelder word nou binne 'n\nwynklub gedeel.\n\nNet mense in die klub kan dit sien, en net\nwat die klub toelaat. Whiney deel nooit 'n\nkelder op 'n ander manier nie. |
| `club.widen.body` | {club} now shares the {level}.\n\nYou already share a {old}. Stay to share the new level, or leave. | {club} deel nou die {level}.\n\nJy deel al 'n {old}. Bly om die nuwe vlak te deel, of loop. |
| `club.widen.stay` | Stay | Bly |
| `club.widen.leave` | Leave | Loop |
| `club.switch.ask` | Use {household} for {club}? | Gebruik {household} vir {club}? |

Transport 5xx ≠ expired.

## Edge cases

| Case | Behaviour |
| --- | --- |
| Flag off | `/club` one-liner; stock hooks no-op; API 404 `not_enabled` |
| `/invite` vs `/club invite` | First is household; never mint a club code from `/invite` |
| Code collides with `HouseholdInvite` | Mint retries until unique in **both** tables |
| Already member Join | Idempotent `already_member`, hot-add |
| Stranger redeem without name | 400 like household |
| Actor in 3 clubs | 3 events, 3 fan-outs (minus overlapping members once per chat_id per event) |
| Bulk Apply 4 drunks | 4 events after commit |
| Vote then undo | Event retracted; vote ignored |
| Vote after window | Event stays; vote still updates glyph if delivery exists |
| Partner drinks | Feed says **Pat**, not Hillside |
| `/switch` cellar | Ask `Use Riverside for Thursday reds?` `[Yes] [Keep Hillside]` |
| Peek serializer leak | Tests forbid price/drawer/comment/barcode/critic/vivino/market_price |
| Global clubs | Not this phase (`kind` column exists, staff create 403) |
| Owner widens then leaves | Re-consent still on the club; new owner inherits Settings |
| Scanner restart during found-name | Re-ask name (memory TTL); policy buttons after name |
| Gunicorn timeout on peek | Peek is a cheap COUNT/GROUP BY; no Grok |

## Data model

New app `wine_cellar.apps.club`. Add to `INSTALLED_APPS`. Migrations `0001_initial`.

```python
CELLAR_VISIBILITY = [("none", "Feed only"), ("summary", "Type and count"), ("full", "Full cellar")]

class Club(models.Model):
    KIND_PRIVATE = "private"
    KIND_GLOBAL = "global"
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=8, default=KIND_PRIVATE)  # global unused until phase 4
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="clubs_founded",
    )
    created = models.DateTimeField(auto_now_add=True)
    description = models.TextField(blank=True, default="")
    cellar_visibility = models.CharField(max_length=8, choices=CELLAR_VISIBILITY, default="summary")
    badges_visible = models.BooleanField(default=True)
    sharing_version = models.PositiveIntegerField(default=1)
    previous_cellar_visibility = models.CharField(max_length=8, blank=True, default="")
    previous_badges_visible = models.BooleanField(null=True, blank=True)
    frozen = models.BooleanField(default=False)  # phase 4 uses it; land the column now (default false)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["created_by", "name"],
                name="unique_club_name_per_founder",
            ),
        ]

class ClubMembership(models.Model):
    ROLE = [("owner", "Owner"), ("member", "Member")]
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="club_memberships")
    role = models.CharField(max_length=10, choices=ROLE, default="member")
    household = models.ForeignKey(
        "scanner.Household", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="club_memberships",
    )
    feed_muted = models.BooleanField(default=False)
    accepted_sharing_version = models.PositiveIntegerField(default=1)
    is_last_used = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["club", "user"], name="unique_club_member"),
            models.UniqueConstraint(
                fields=["club"],
                condition=models.Q(role="owner"),
                name="one_owner_per_club",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_last_used=True),
                name="one_last_used_club_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "household"], name="clubmem_club_hh_idx"),
            models.Index(fields=["user", "is_last_used"], name="clubmem_user_last_idx"),
        ]

class ClubInvite(models.Model):
    code = models.CharField(max_length=8, unique=True, default=generate_invite_code)
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="invites")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    max_uses = models.PositiveSmallIntegerField(default=20)
    use_count = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)  # first use
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created"]
        indexes = [models.Index(fields=["code"], name="clubinvite_code_idx")]  # unique already

class ClubEvent(models.Model):
    KIND = [
        ("opened", "Opened"),
        ("voted", "Voted"),  # rarely inserted; usually an update on opened
        ("retracted", "Retracted"),
        ("joined", "Joined"),
        ("left", "Left"),
        ("tasteoff", "Taste-off"),  # phase 3
        ("comment", "Comment"),     # phase 3
    ]
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    kind = models.CharField(max_length=12, choices=KIND)
    wine_name = models.CharField(max_length=100, blank=True, default="")
    vintage = models.PositiveIntegerField(null=True, blank=True)
    producer = models.CharField(max_length=100, blank=True, default="")
    vote = models.CharField(max_length=8, blank=True, default="")  # down|meh|up
    wine_id = models.IntegerField(null=True, blank=True)  # NOT an FK
    item_id = models.IntegerField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    retracted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["club", "-created"], name="clubevent_club_created_idx"),
            models.Index(fields=["club", "item_id", "kind"], name="clubevent_item_kind_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["club", "item_id", "kind"],
                condition=models.Q(kind="opened", item_id__isnull=False),
                name="unique_opened_per_club_item",
            ),
        ]

class ClubEventDelivery(models.Model):
    event = models.ForeignKey(ClubEvent, on_delete=models.CASCADE, related_name="deliveries")
    chat_id = models.CharField(max_length=50)
    message_id = models.BigIntegerField(null=True, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    error = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "chat_id"], name="unique_event_chat"),
        ]
```

Also on `Household`:

```python
club_share_notice_sent_at = models.DateTimeField(null=True, blank=True)
```

Migration in `scanner` (`0009_household_club_share_notice.py`).

Invite TTL: `timedelta(days=30)`. Codes: `generate_invite_code()` then `while HouseholdInvite.objects.filter(code=c).exists() or ClubInvite.objects.filter(code=c).exists()`.

Mint lock: `select_for_update` on `ClubInvite` **without** joining `club` (same Postgres trap if you ever nullable the FK — it isn’t, but don’t `select_related` under `FOR UPDATE` anyway).

## API

`/api/clubs/` — token auth. Flag mixin. **Never** use `X-Household` as the club id.

Staff preview/redeem: `IsAdminUser`, like household (token in body).

### Shapes

**`POST /api/clubs/`** body `{ "name": "Thursday reds", "cellar_visibility": "summary", "badges_visible": true }` → 201:

```json
{
  "id": 3,
  "name": "Thursday reds",
  "kind": "private",
  "role": "owner",
  "cellar_visibility": "summary",
  "badges_visible": true,
  "sharing_version": 1,
  "member_count": 1
}
```

409 `{ "error": "You already have a club with that name." }` (founder unique). 400 name too short/long.

**`GET /api/clubs/`** → `{ "clubs": [ {id, name, role, cellar_visibility, badges_visible, member_count, is_last_used, unread optional } ] }`

**`GET /api/clubs/<id>/`** home:

```json
{
  "id": 3,
  "name": "Thursday reds",
  "kind": "private",
  "role": "member",
  "cellar_visibility": "summary",
  "badges_visible": true,
  "sharing_version": 1,
  "accepted_sharing_version": 1,
  "previous_cellar_visibility": "",
  "feed_muted": false,
  "frozen": false,
  "member_count": 4,
  "household": {"id": 4, "name": "Hillside"},
  "events": [
    {
      "id": 88,
      "kind": "opened",
      "actor_name": "Lee",
      "wine_name": "Merlot",
      "vintage": 2019,
      "producer": "",
      "vote": "down",
      "created": "2026-11-05T19:02:00+00:00",
      "retracted": false
    }
  ]
}
```

404 if not a member. 404 `not_enabled` if flag off.

**`PATCH /api/clubs/<id>/`** owner only. Body visibility/badges. If widening: bump `sharing_version`, set previous_* . 403 member.

**`PATCH /api/clubs/<id>/membership/`** `{ "feed_muted": true } | { "household_id": 4 } | { "accepted_sharing_version": 2 }`. Stay must equal **current** club version (no jumping). `household_id` must be a household the user belongs to.

**`POST /api/clubs/<id>/leave/`** 200. Owner with other members: 409 `{ "error": "hand_over_required" }` until `POST /api/clubs/<id>/handover/` `{ "user_id": 9 }`. Last member: 200 and club deleted.

**`POST /api/clubs/<id>/invites/`** 201 `{ code, expires_at, max_uses, use_count, cellar_visibility, badges_visible, club_name, inviter_display_name }`. Any member may mint.

**`POST /api/clubs/<id>/invites/cancel/`** newest unused unexpired, same as household.

**`POST /api/scanner/invites/preview/`** (extended): try `HouseholdInvite` then `ClubInvite`. Club hit:

```json
{
  "kind": "club",
  "code": "K9m2Pq4r",
  "club": {"id": 3, "name": "Thursday reds"},
  "cellar_visibility": "summary",
  "badges_visible": true,
  "sharing_version": 1,
  "expires_at": "...",
  "expired": false,
  "used": false,
  "inviter_display_name": "Alex"
}
```

Household hits keep today’s shape (`kind: join|new`). One round trip for `/start`.

**`POST /api/scanner/club-invites/redeem/`** staff. Body `{ code, chat_id, display_name, household_name?, household_id? }`. If the chat has no user, compound: create cellar like `kind=new` when `household_name` present, else 400. Response includes `token`, `household`, `club`, `role`, `accepted_sharing_version`, `created_by_chat_id` (for “Lee joined” — **club members** get the feed event; the household heads-up is separate and nameless).

**`GET /api/clubs/<id>/feed/?limit=5&offset=0`**

```json
{
  "count": 12,
  "offset": 0,
  "limit": 5,
  "events": [
    {
      "id": 88,
      "kind": "opened",
      "actor_id": 7,
      "actor_name": "Lee",
      "wine_name": "Merlot",
      "vintage": 2019,
      "producer": "",
      "vote": "down",
      "item_id": 9001,
      "created": "2026-11-05T19:02:00+00:00",
      "retracted": false,
      "comment_count": 0
    }
  ]
}
```

Newest first. Include retracted with `"retracted": true` (bot skips them on the home card, shows them grey on `/club feed` if we want an audit — recommended: skip on home, include on feed). No `wine_id` of another household is required by the bot; omit from the public shape even if stored.

**`GET /api/clubs/<id>/members/`** `{ members: [{ user_id, display_name, role, household_id, household_name }], households: [{ household_id, name, user_ids: [...] }] }` — client uses `households` to collapse peek.

**`GET /api/clubs/<id>/members/<user_id>/cellar/`**

Effective visibility: if `accepted_sharing_version == club.sharing_version` → current; else → `previous_cellar_visibility` or `none`. If effective `none` → 404 `{ "error": "not_visible" }`.

Summary:

```json
{
  "household_id": 4,
  "household_name": "Hillside",
  "visibility": "summary",
  "counts_by_type": {"RE": 6, "WH": 4, "SP": 1},
  "counts_by_grape": {"Merlot": 6, "Chenin Blanc": 2},
  "total_bottles": 11
}
```

Full: plus `"wines": [{ "name", "producer", "vintage", "wine_type", "country", "grapes": [], "stock_count" }]` max 30, `"truncated": true`. **Forbidden keys must not be present** (write a test that walks the JSON).

**`GET /api/clubs/<id>/members/<user_id>/badges/`** 404 if club `badges_visible` false or household gamification off. Else `GET /api/scanner/achievements/` shape for **that household** (caller must be club member). Do not send GIF; JSON only.

### Stock hooks (not new routes)

After success of:

- `_soft_delete_item` (remove)
- `StockAddView` when `drank=True`
- each bulk **remove** op (after the transaction)
- `StockVoteView`
- restore / bulk undo

`transaction.on_commit(lambda: publish_club_event.delay(...))`.

Payload to the task: `item_id`, `actor_id`, `kind` (`opened`|`voted`|`retracted`). Task no-ops if flag off or user has no memberships.

## Permissions and privacy

Normative rules (from the overview, applied here):

1. Join = consent to **this club’s current** policy.
2. Peek fields by club visibility; always forbid price, item price, market price, storage, row, comment, drink_by, barcode, rating, critic, vivino.
3. Badges only if club + household flags.
4. Mute ≠ hide peek.
5. Leave stops peek and new events.
6. Widen does not silently expand (J2.9).
7. Shared household: joiner’s card names it; others get nameless heads-up; no veto; sharing only inside clubs.
8. Global: not this phase.
9. Club APIs keyed by user, not `X-Household`.
10. `wine_id` / `item_id` on events are snapshots, not a read grant.

Staff cannot preview a club as a member without membership except via admin.

## Notifications / Celery

```python
@shared_task(name="publish_club_event")
def publish_club_event(item_id, actor_id, kind):
    ...
```

```python
@shared_task(name="deliver_club_event")
def deliver_club_event(event_id):
    # sendMessage / editMessageText per ClubEventDelivery
```

```python
@shared_task(name="club_share_notice")
def club_share_notice(household_id, exclude_user_id):
    # nameless heads-up; sets club_share_notice_sent_at
```

Reuse `_send_telegram`. HTML. `parse_mode=HTML`. Retry once on Telegram 429 (backoff). Do not retry forever. Store `error` on delivery.

Do not send to `actor`’s `chat_id`. Distinct `chat_id`s only.

Gunicorn must not wait on these.

## Web UI

Admin for Club, Membership, Invite, Event. Optional read-only `/clubs/` and `/club/<id>/` (session auth, `ClubMembership` gate — **not** `HouseholdScopedMixin`). Owner settings form = the three radios. No React. Homepage cellar challenges unchanged.

## Test plan

Django:

- founder unique name 409
- member cannot PATCH policy 403
- peek summary has no forbidden keys (parametrize full/summary)
- peek 404 when visibility none
- two members same household → members payload collapses
- invite code not in `HouseholdInvite`
- redeem compound creates household + club
- redeem already_member idempotent
- widen bumps version; unacked peek uses previous
- stay sets accepted; leave deletes
- owner leave 409 without handover; last leave deletes club
- publish no-op when flag off / no memberships
- unique opened per (club, item_id)
- `club_share_notice` copy has no club name, no display_name, not sent to joiner, sent once
- stock remove creates event; move does not; bulk apply creates N; undo retracts

Bot:

- `/club` pitch, create flow, invite card contains policy
- `/start` club code vs household code
- stranger compound does not create two cellars
- mute toggles
- callback budget tests for `cl:p:` with large ids (still < 64)
- EN/AF keys for all new copy
- `/help` + command menu

## Rollout

1. App + migrations, flag **off**. Stock hooks installed but no-op.
2. Bot deployed understanding flag.
3. Flag on for staff chats first (`is_staff` can found a club even if we later add a staff-only gate — not required). Then global on.
4. `set_bot_presence` for `/club`.
5. Watch 48h: 5xx on `/api/clubs/`, Telegram send errors, accidental peek of prices (log response keys in tests, not prod).
6. Rollback: flag off (hooks no-op, `/club` dead). Rows remain.

## Task breakdown (separate PRs)

| PR | Scope | Effort |
| --- | --- | --- |
| **2a** | `club` app, models, admin, `Household.club_share_notice_sent_at`, unique-code helper across both invite tables | M |
| **2b** | Club CRUD API + membership patch + leave/handover; tests | M |
| **2c** | Club invites mint/cancel + extend household preview + redeem (compound); tests including FOR UPDATE without join | M |
| **2d** | Peek + badges GET; redacted serializer tests (forbidden keys) | S |
| **2e** | `publish_club_event` + deliveries + stock/vote/undo hooks; no-op if flag off | M |
| **2f** | `club_share_notice` task + tests (no names) | S |
| **2g** | Widen/re-consent API + previous visibility | S |
| **2h** | `telegram_clubs.py` found/invite/join/home/mute/leave; `/start` dispatcher; copy EN+AF; menu | L — **split if needed:** 2h-join vs 2h-home |
| **2i** | Peek/members/feed bot screens; settings + widen Stay/Leave | M |
| **2j** | `/help`, `/status`, CLAUDE.md, presence, feature flag wiring in `server.py` | S |

Order: **2a → 2b → 2c/2d → 2e/2f/2g → 2h → 2i → 2j**. Do not land 2h before 2c.

If 2h is too large, ship **2h1** found+invite+join (growth) before **2h2** home/feed UI (the join still works via `/start`).

**Phase total: L.**
