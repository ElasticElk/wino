# Wine Clubs

Design for a social layer on Whiney, grounded in the current Django apps and Telegram bot. Planning only — this document is not a commitment to ship every piece.

**Goal:** more people using Whiney, and the ~11 people already on it coming back more often. The lever is *community around what is actually in the cellar*, which Vivino cannot do.

**Constraint:** nearly all use is the Telegram bot (`scanner-service/integrations/telegram_bot.py`). The Django web app is a secondary surface. Clubs must work in Telegram first.

**Out of scope:** Vivino integration (no public API).

---

## 1. Why not households?

A household is the **permission boundary for wine data**. From `wine_cellar/apps/scanner/households.py`:

> Access control is by **household**, not by user: everyone in a household sees all of its wine, storages and stock.

That is the shared fridge: `Wine`, `Storage`, `StorageItem`, `Scanner`, and `CellarAchievement` are all `household`-scoped. `HouseholdInvite` (`kind=join|new`) provisions a Django user, a DRF token, a `HouseholdMembership`, and a `TelegramUser`. Joining a household means seeing (and changing) that cellar.

A wine club is the opposite social shape: **friends who each have their own cellar**, sharing drinking and optionally a peek at stock. Modelling a club as a household would:

- mix “share my fridge” with “see what I opened”
- force every club member into one stock ledger
- break `/fridge`, catch-up, scanners, and badges, which all assume one cellar

**Decision:** clubs are a new overlay on `User` / `TelegramUser`. Each membership points at *which household* that person is representing (their default cellar, switchable). Households stay the cellar. Clubs stay the social graph.

```
User ──< HouseholdMembership >── Household (cellar, stock, badges)
  │
  └──< ClubMembership >── Club (feed, peek, taste-offs)
           │
           └── household FK = “the cellar I show this club”
```

---

## 2. User stories

### Founding and inviting

- **Alex founds a club** in Telegram without staff help. Names it “Thursday reds”. He is owner.
- **Alex invites friends.** He gets a `t.me` link he can forward, same shape as `/invite` today. Dewald taps it.
- **Dewald already has a cellar.** One tap joins the club. He is not added to Alex’s household. He does not get a second cellar.
- **Sam has never used Whiney.** The same link onboards her: name a cellar (existing new-cellar flow), then she lands in the club. Growth and club join are one gesture.
- **Alex cancels an unused link** before it leaks.

### The automatic feed

- **Alex opens a 2019 Merlot** (fridge scan out, “Drank this” on a label card, `/fridge` Catch up Drunk, or chat “Take it out now”). Dewald and Sam each get a DM: `Alex opened a 2019 Merlot`. Nobody typed a post.
- **Alex then taps 👎.** The same card updates: `Alex opened a 2019 Merlot 👎`.
- **Alex Undo** within the existing 10-minute restore window. The club cards retract (or show a short “put back” line). A bottle moved between fridges does **not** appear.
- **Alex mutes Thursday reds** so opens still record but he is not pinged. Leaving the club stops new events about him.

### Cellar peek (opt-in)

- **Dewald turns on “show my cellar to this club”.** Alex opens Dewald’s shelf in the club: six Merlots, two Chenins, a vintage, a grape. No purchase price, no drawer, no notes.
- **Alex is bringing wine.** He can see Dewald already has Merlot covered and bring something else.
- **Dewald’s partner shares that household.** Peek is the household’s in-stock list, labelled as Dewald’s cellar-of-record for this club. Partner identity is not listed unless they also joined.

### Badges in the club

- **Alex has cellar challenges on** (`Household.gamification_enabled`, default on). He opts to show badges in Thursday reds. Sam can see “First Croatia”, “10 bottles”, the monthly tally for *his* cellar.
- **A household with challenges off** shows nothing, even if the member opted in.

### Comments and taste-offs (later)

- **Sam taps “Say something”** on Alex’s Merlot card and types “we had that in Stellenbosch”. The comment is stored on the event and appended for the club.
- **Alex hosts a taste-off:** picks three bottles (from shared cellars or typed names). Members score. Results stay on the club.

### Global clubs (later)

- Staff create a few public clubs (“New World Cab”, “Drink-by this month”) with a regular themed note. Private-club rules still apply for cellar peek.

---

## 3. What already exists (do not rebuild)

### Households and identity

| Piece | Where |
| --- | --- |
| `Household`, `HouseholdMembership` (owner/member, `is_default`) | `wine_cellar/apps/scanner/models.py` |
| `TelegramUser` (`chat_id`, `display_name`, `last_seen_at`) | same |
| `UserSettings` (language, currency, country, **email** `notifications`) | `wine_cellar/apps/user/models.py` |
| Household resolution (`X-Household`, default, first) | `scanner/households.py`, `ScannerAPIView.get_household` |
| `/switch`, `/household` | `telegram_bot.py` |
| Config bootstrap: `chat_id → WineCellarAPI(token, household_id)` | `GET /api/scanner/config/`, `server.py` |

A provisioned Telegram user always has a DRF token. The bot allow list is `filters.Chat(chat_id=[...])` built from those users. Unfiltered commands are `/start`, `/myid`, and stranger fallbacks. **Club members who are not yet provisioned cannot use `/club` until redeem hot-adds them** — same as today’s invite.

### Invites (reuse the pattern, not the row)

`HouseholdInvite` is a one-time 8-char code, 7-day TTL (`INVITE_TTL`), kinds `join` / `new`. Flow:

1. Any member `POST /api/scanner/invites/` (`kind=join`). Staff `kind=new`.
2. Bot builds `https://t.me/<bot>?start=<code>` (`telegram_invites.invite_link`).
3. `/start <code>` previews via staff token (`POST .../invites/preview/` — **POST, not GET**; `CLAUDE.md` is stale).
4. Redeem is **staff-only** because the response includes the new user’s token. Burned only after provision succeeds. Replay of the same `chat_id` is idempotent.
5. Bot `_hot_add`s the chat into the allow list without a restart.
6. `/invite cancel` deletes the newest unexpired unused row.

Buttons carry the code (`inv:<action>:<code>[:<name>]`) so Join survives a scanner restart; only a half-typed cellar name is lost (`STATE_TTL_SECONDS = 30 * 60`).

**Known breakage — fix before clubs depend on this path:**

- **Expiry.** Seven days, one use. Copy says “Works until {date}, one use.” Links go stale before a friend taps Start. Cancel leaves expired rows in the table; they still 410 on redeem (`FRESH_LINK`).
- **Redeem 500 (fixed in tests, must stay fixed).** Postgres rejects `SELECT … FOR UPDATE` on a `LEFT JOIN`. A `kind=new` invite has `household_id=NULL`. `lock_invite_qs` now locks the invite row only (`scanner/views.py`); `test_lock_invite_sql_does_not_join` guards it. Do not reintroduce `select_related("household")` on that lock.
- **Create 500 still possible.** `_mint_invite` retries IntegrityError five times then **raises**; `HouseholdInviteCreateView` does not catch it. `_resolve_join_household` does `int(header_id)` on `X-Household` without a try for non-staff — a bad header 500s. ScannerAPIView already guards this for `/api/stock/*`.
- **Staff join mint requires `household_id`** even when there is only one cellar (intentional, so staff don’t mint into their own house). The bot already names it when the user has one household.

Club invites should copy: code alphabet, `t.me` deep link, `/start` dispatcher, staff redeem + token, hot-add, cancel, `esc()` on names, “couldn’t reach the cellar” on 5xx (never “expired” for transport failures). They should **not** share the `HouseholdInvite` table or `kind`.

### Opening a bottle and voting

These are the feed triggers. There is no “post” API today.

| User action | Code path |
| --- | --- |
| Fridge scan out, Catch up **Drunk**, chat “Take it out now” | `POST /api/stock/remove/` → `_soft_delete_item` (`deleted=True`, `removed_at` set) |
| Label card **Drank this** (not in stock) | `POST /api/stock/add/` with `drank=True` (row created already deleted) |
| Catch up Apply of several Drunk/Moved | `POST /api/stock/bulk/` — **moves first, then removes**, one transaction |
| Undo (10 min, `RESTORE_WINDOW`) | `POST /api/stock/<pk>/restore/` or bulk undo. Vote does **not** restart the clock. Undo does **not** revoke badges. |
| Thumbs on the drank card | `PATCH /api/stock/<pk>/vote/` — `StorageItem.vote` in `{down, meh, up}` even if deleted |

`/recent` is `GET /api/stock/history/` — deleted items for **this household only**, with `vote` and `drank_at`. That payload is the closest existing “feed”, but it is not fan-out and not cross-household.

**Not a rating for the feed:** `Wine.rating` (0–10 on the wine row, web form). Club copy like `Alex opened a 2019 Merlot 👎` is `StorageItem.vote`.

**Not a feed event:** Catch up **Moved**, `PATCH /api/stock/<pk>/move/`, stock add, barcode in.

### Badges / cellar challenges

Household-scoped, **on by default** (`gamification_enabled`, migrations `0005`/`0006`). Owners toggle via `/badges on|off` or web settings. Awards are `CellarAchievement` unique on `(household, slug, subject)` — idempotent `get_or_create`.

- Fired from `award_for_add` on **stock add** (API and web `StorageItemAddView`), including `drank=True` adds. **Not** fired on `StockRemoveView`.
- Telegram ping: `send_achievement_card` + family GIFs under `scanner-service/assets/badges/`.
- Listing: `GET /api/scanner/achievements/` → unlocks, `month_tally` (bottles **logged** this month, ranked by `StorageItem.user`), `progress`.
- Off: listing returns `enabled: false` and empty lists; existing rows are kept.
- Dashboard: `homepage.html` “Cellar challenges” block.

Monthly race is **intra-household, bottles added**, not drunk, and never pings on lead changes. That is the wrong grain for a club; don’t reuse `month_tally` as the club leaderboard without renaming what it counts.

### Fan-out that already works

Do **not** use `TelegramNotifier._send_to_all` — that is the Pi scanner’s `notify_chat_ids` (household members watching that fridge).

Do reuse the Django → Telegram pattern in `wine_cellar/apps/wine/tasks.py`: `drink_by_reminder` (02:30) and `empty_cellar_nudge` (10:00) load `ServiceConfig.bot_token`, resolve `TelegramUser.chat_id` from household memberships, `sendMessage` with HTML. Club feed pings are the same shape, plus optional `reply_markup` for later buttons.

`UserSettings.notifications` is **email drink-by only**. Club mute is a new flag on `ClubMembership`, not that field.

### Telegram limits the UX must respect

- Bot is **DM-only** (`filters.Chat` of provisioned chat IDs). It does not run in groups. Don’t create a Telegram group per club for MVP.
- `callback_data` max **64 bytes** (`CALLBACK_DATA_MAX`). Household buttons use compact `a:vt,i:99,o:up`. Invite buttons use `inv:…`. Club callbacks need a short prefix (`cl:`) and numeric ids, not names.
- `/start` payload max 64 characters — an 8-char code is fine; disambiguate household vs club in the dispatcher, not in a long payload.
- Command menu is a **flat** list (`telegram_commands.py`, `set_bot_presence`). Adding `/club` is justified; don’t add `/clubinvite`, `/tasteoff`, `/clubfeed` as top-level commands.
- A bot cannot message someone who has never pressed Start.

---

## 4. Telegram first (commands and flows)

One command: **`/club`**. Subcommands and inline buttons do the rest. Add it to `PUBLIC_COMMANDS` and `/help`. Keep `/invite` as **cellar** invite so “join my fridge” and “join my club” cannot be confused.

### No club yet

`/club` → short pitch + `Found a club` + (if they have an unused club invite in `/start` state) Join.

`/club new Thursday reds` (or tap Found, then type a name) → create, Alex is owner, then the invite card immediately. Empty name is rejected (`NAME_MIN`/`NAME_MAX` from household invites, 2–40).

### In one or more clubs

`/club` with no args, one club: home card.

`/club` with several: picker (same idea as `/invite` household picker when `list_households()` length > 1). Remember last-used club per chat in memory (like `InviteFlow`) with a DB default on `ClubMembership`.

Home card:

```
🍷 Thursday reds · 4 members

Alex opened a 2019 Merlot 👎
Sam opened a 2022 Chenin 👍
Dewald joined

[Invite] [Members] [Feed] [Mute]
```

Owner also gets `Settings`.

### Invite

`/club invite` or the button. Message copy mirrors `JOIN_COPY` in `telegram_invites.py`: link, Share button (`t.me/share/url`), expiry line. Forward this message — no private follow-up (household invites already learned that people forward the wrong one).

`/start <code>` dispatcher:

1. Preview as `HouseholdInvite` (today).
2. Else preview as `ClubInvite`.
3. Else `FRESH_LINK`.

Club preview cards:

- **Stranger, no cellar:** “Alex invited you to Thursday reds on Whiney. You’ll get your own cellar, then you’re in the club.” Then the existing new-cellar name / suburb / Skip pitch (`I1_COPY` / `I2_COPY`). Redeem is compound: `kind=new` household **and** club membership, one staff call (or household redeem then club redeem in one view). Never create a second cellar later by accident — the household “Stay / Start a new cellar” guards apply.
- **Known user:** “Join Thursday reds?” Join / Not now. Redeem only inserts `ClubMembership`. Confirm-if-already-in-a-cellar is **not** needed (we are not creating a household).
- **Already a member:** idempotent, like `already_member` on household redeem.

`/club invite cancel` — drop the newest unused club invite for this club (copy `HouseholdInviteCancelView`).

### Feed (automatic)

No `/club post`. On a qualifying stock mutation the API inserts `ClubEvent` rows for each of the actor’s clubs (unless they muted *posting*, which MVP does not offer — mute is receive-only). A Celery task fans out DMs to other members with `feed_muted=False`.

Copy:

- open, no vote: `{name} opened a {vintage} {wine}`
- open, then vote: same line plus `👎` / `👍` / nothing for meh (match `VOTE_LABELS`: down / up / em-dash)
- join: `{name} joined`
- retract: edit the sent messages to `{name} put the {wine} back` when `ClubEventDelivery.message_id` is known; else leave the original (11 users — store deliveries from day one)

Actor is `request.user` via `TelegramUser.display_name`, **not** the household name. Shared cellar: if Alex’s partner drinks using their own Telegram, the feed says the partner’s name.

Buttons on a feed card (MVP): none, or a single `Open in /club` that is just `/club` home. Comments come in phase 3 so we don’t spend the 64-byte budget yet.

`/club feed` pages like `/recent` (5 at a time, Newer/Older). Data from `ClubEvent`, not from another household’s `StockHistoryView`.

### Members and peek

`/club members` — list display names, owner crown, cellar-share / badge-share ticks.

Tap a member → profile card: name, optional latest open, `Show cellar` if `share_cellar`, `Badges` if `share_badges` and that household has `gamification_enabled`.

Show cellar → a **summary**, not a full `/stock` clone: counts by type and grape, then wines as `name vintage · grape · country · ×N`. Cap the list (e.g. 30) with “and N more”. This is the “Dewald has six Merlots” moment.

On join, two optional toggles on the home Settings card (owner and member):

- `Share cellar with this club` — default **off**
- `Show badges in this club` — default **off**

Default off is deliberate: joining a drinking feed is not the same as publishing inventory. The join card can say so in one line.

### Mute / leave

- `Mute` — `feed_muted=True`, still a member, still in peek if they shared.
- `Leave` — delete membership; historical events keep the denormalised name.
- Owner `Leave` with other members: must `Hand over` first, or the club dissolves if they are the last member.

### Taste-offs (phase 3, command still `/club`)

`/club tasteoff` (owner/host): name it, add bottles (pick from a shared cellar, or type a label). `Open scoring` DMs the club. Each member gets one card per wine with 1–5 (or reuse 👎/—/👍 if we decide thumbs are enough — open question). Host `Close`. Results card is a `ClubEvent` of kind `tasteoff` plus rows in `TasteOffScore`.

### Global clubs (phase 4)

Staff `/club global …` (or admin). Members `/club join New World Cab` from a list. Themed updates are Celery beat posts (same send path as drink-by), not members posting.

### Natural-language chat

Do **not** route “what did Alex open?” through `ChatRouter` in MVP. Parser + cellar_facts are household-scoped (`chat_models.py`, `cellar_facts.py`). A later phase can add a `club` intent; until then `/club` is the interface so we don’t invent commands in `wine_chat`.

---

## 5. Web second

No new React. Existing templates are Django + Pure CSS (`homepage.html`, `settings.html`).

| Phase | Web |
| --- | --- |
| 0–1 | Django admin for `Club`, membership, invites, events. Optional read-only `/clubs/` list and `/club/<id>/` feed for the logged-in user, household-scoped auth via `HouseholdScopedMixin` is **wrong** here — gate on `ClubMembership`. |
| 2 | Member cellar peek page, same redacted serializer as Telegram. |
| 3 | Comments + taste-off results. |
| 4 | Global club pages. |

Settings: club share toggles can appear under `/user/settings/` later; Telegram remains the place people will actually flip them.

Do not put club badges on the cellar dashboard. That page is household challenges.

---

## 6. Data model

New Django app: **`wine_cellar.apps.club`**. Do not hang these models off `scanner` just because `Household` lives there — scanner is devices, households, invites, Telegram identity. A new app keeps the cellar/club boundary obvious in `INSTALLED_APPS` (`wine_cellar/conf/settings.py`).

### `Club`

- `name` (100, like `Household.name`)
- `kind` — `private` \| `global`
- `created_by` FK User
- `created`
- `description` (blank, for global themed copy)
- Names need **not** be globally unique (households are; clubs are small and personal). Unique together `(created_by, name)` is enough to stop Alex founding two “Thursday reds”.

### `ClubMembership`

- `club`, `user` — unique together
- `role` — `owner` \| `member` (same strings as `HouseholdMembership.ROLE_CHOICES`)
- `household` FK — cellar this membership shares from; set to the user’s default household on join; update on `/switch` only if they confirm “use this cellar for Thursday reds”
- `share_cellar` default False
- `share_badges` default False
- `feed_muted` default False
- `created`

### `ClubInvite`

Mirror `HouseholdInvite` fields that matter: `code` (same `generate_invite_code` / alphabet / length so `/start` stays one parser), `club` FK (never null), `created_by`, `created`, `expires_at`, `max_uses` (default 1 for parity, raise for club WhatsApp groups), `use_count`, `used_at` (first use; keep for admin), `revoked_at`.

**Do not** add `kind=club` to `HouseholdInvite`. Redeem semantics differ (may or may not create a household).

TTL: recommend **30 days** and **multi-use with a cap** (e.g. 20) for clubs, because 7-day one-shot is why household invites “have expired”. Phase 0 can also lengthen household TTL independently.

### `ClubEvent`

- `club`, `actor` FK User (SET_NULL)
- `kind` — `opened` \| `voted` \| `retracted` \| `joined` \| `left` \| `badge` (unused in MVP) \| `comment` \| `tasteoff`
- Denormalised snapshot: `wine_name`, `vintage`, `producer`, `vote` (nullable)
- Pointers, nullable, **not** used for authz: `wine_id`, `item_id` (ints, no FK to another household’s `Wine` — deleting a wine must not cascade-wipe the feed, and a club member must not be able to `GET /api/wines/<pk>/` across households)
- `payload` JSON (taste-off summary, etc.)
- `created`, `retracted_at`

### `ClubEventDelivery`

- `event`, `chat_id`, `message_id`, `sent_at`
- Unique `(event, chat_id)`
- Lets Undo **edit** the DM instead of sending a second message. Cheap at this scale.

### `ClubComment` (phase 3)

- `event`, `user`, `text` (keep short; Telegram captions are not essays), `created`

### `TasteOff`, `TasteOffBottle`, `TasteOffScore` (phase 3)

- Host, title, status `setup|scoring|closed`
- Bottles: `label` snapshot + optional `wine_id`/`item_id` + `sort_order`
- Scores: unique `(tasteoff, bottle, user)`, `score` small integer

### What we do **not** change

- `Wine`, `Storage`, `StorageItem` stay household-scoped. No `club` FK on stock.
- `CellarAchievement` stays household-scoped. Club display **reads** it when `share_badges` and `gamification_enabled`.
- `TasteIntent` / `HouseholdTasteSnapshot` stay cellar taste for chat recommendations, not club.

### Privacy rules (normative)

1. **Join = consent to the drink/vote feed** using `TelegramUser.display_name`. Inventory and badges stay private until toggled.
2. **Cellar peek** is live `StorageItem(deleted=False)` for `membership.household`, and only if `share_cellar`. Serializer allows: name, producer, vintage, wine_type, country, grapes, stock_count. Serializer **forbids**: `price`, `items[].price`, `market_price`, storage name, drawer/row, `comment`, `drink_by`, barcode, `rating`, critic/Vivino (those are nice-to-have later and are not needed for “bring something else”).
3. **Badges** only if `share_badges` and the household flag is on. Show title/emoji/unlocked_at, not a dump of `subject` ids.
4. **Mute** stops delivery, not recording.
5. **Leave** stops new events **from** and **to** that user.
6. **Staff/global clubs** do not bypass peek flags.
7. API: every club endpoint uses the caller’s `User`, never `X-Household` as the club id. Peek of another member checks *their* `share_cellar`, not the caller’s.
8. Cross-household `wine_id` in events is a snapshot aid for the actor’s own undo, not a permission to read that wine.

---

## 7. API sketch (Django, consumed by the bot)

Mount under `/api/clubs/` (new urls module), `TokenAuthentication`, `IsAuthenticated`. Staff redeem/preview stay `IsAdminUser` like household invites (token in the body).

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/clubs/` | Found `{name}` |
| GET | `/api/clubs/` | Clubs for this user |
| GET | `/api/clubs/<id>/` | Home: members count, last events, my flags |
| POST | `/api/clubs/<id>/leave/` | |
| PATCH | `/api/clubs/<id>/membership/` | `share_cellar`, `share_badges`, `feed_muted`, `household_id` |
| POST | `/api/clubs/<id>/invites/` | Mint |
| POST | `/api/clubs/<id>/invites/cancel/` | |
| POST | `/api/scanner/club-invites/preview/` | Staff; or a single dispatcher `POST /api/scanner/invites/preview/` that tries household then club |
| POST | `/api/scanner/club-invites/redeem/` | Staff; body `code, chat_id, display_name, household_name?` |
| GET | `/api/clubs/<id>/feed/?limit=5&offset=0` | |
| GET | `/api/clubs/<id>/members/` | |
| GET | `/api/clubs/<id>/members/<user_id>/cellar/` | 404 if not shared |
| GET | `/api/clubs/<id>/members/<user_id>/badges/` | 404 if not shared / gamification off |

**Hooks (not new routes):** after successful `_soft_delete_item`, `StockAddView` when `drank=True`, each bulk **remove** op, `StockVoteView`, and restore/undo. Keep the request fast: `transaction.on_commit` → `publish_club_event.delay(item_id, actor_id, kind)`. Never inline Telegram I/O in gunicorn (the reference-lookup saga: search is a Celery task because a sync call blocked the bot).

Idempotency: unique `(club, item_id, kind=opened)` so a double remove or replay doesn’t double-post. Vote updates the opened event’s `vote` and edits deliveries.

Scanner client: add methods on `WineCellarAPI` next to `mint_invite` / `vote_item` / `list_achievements`. Bot module `integrations/telegram_clubs.py` beside `telegram_invites.py` so `telegram_bot.py` doesn’t grow another 1k lines.

`/api/scanner/config/` does **not** need club lists at bootstrap. Feed send is Django→Telegram; interactive `/club` uses the user’s existing token.

---

## 8. How this reuses badges, invites, household

| Existing | Club use |
| --- | --- |
| `Household` | Cellar of record on `ClubMembership.household`. Never the club itself. |
| `HouseholdInvite` + `InviteFlow` | Pattern only. Shared `/start` dispatcher. Compound redeem for strangers (new cellar + membership). Hot-add, `esc()`, Share button, 30‑min in-memory flow TTL. |
| `Household.gamification_enabled` + `CellarAchievement` | Unchanged writers (`award_for_add`, `backfill`). Club reads when `share_badges`. No new badge slugs for “joined a club” in MVP (noise). |
| `StorageItem.vote` / drank cards | Feed copy and vote updates. |
| `TelegramUser.display_name` | Actor name. Fall back to `user.username` like `month_tally`. |
| `drink_by_reminder` send helper | Extract `_send_telegram` (already a function) for club fan-out. |
| `/switch` | May change which cellar peek uses, with an explicit confirm if `share_cellar`. |
| `/invite` | Stays “invite to **this cellar**”. `/club invite` is the social link — that’s the growth link we put in people’s hands. |

---

## 9. Phased MVP

Ship for **adoption**, not completeness. The loop that grows a self-hosted bot is: *someone I know opened a bottle → I open the chat → I invite the next friend*.

### Phase 0 — Invite reliability (unblocks everything)

Household invites are how people get onto Whiney at all. Clubs will share that `/start` pipe.

- Catch `_mint_invite` IntegrityError as 503 with the existing “couldn’t reach” copy.
- Harden `_resolve_join_household` `int(header_id)` (copy `ScannerAPIView`).
- Confirm redeem lock never joins (test already there).
- Decide household TTL: keep 7 days but make reminting obvious, **or** extend to 30 days. Document in `/invite` copy.
- Regression tests for create-500 paths.

**Effort:** small. Django views + tests, tiny bot copy. No new models.

### Phase 1 — Private clubs + automatic feed  ★ ship this first for adoption

Biggest effect per line of code: a reason to open Telegram when you didn’t scan, and a link worth forwarding.

- App `club`, models `Club`, `ClubMembership`, `ClubInvite`, `ClubEvent`, `ClubEventDelivery`.
- Found / invite / join / leave / mute in Telegram.
- Stranger compound onboard.
- Hooks on drank + vote + undo.
- Celery fan-out DMs.
- `/club` home + feed + members (no peek yet).
- Admin.
- Tests: permissions (no household leak), invite dispatcher vs household codes, idempotent opened events, mute, undo edit.

**Effort:** large. New app, migrations, API, Celery, a bot module, onboarding branch in `telegram_invites.py`, pytest in both services. Comparable to the original household-invite work (`1911372`) plus drink-by fan-out.

**Not in phase 1:** cellar peek, badges in club, comments, taste-offs, web UI beyond admin, global clubs, chat-router intent, badge unlock pings to the club.

### Phase 2 — Cellar peek + badges in clubs

This is the “bring something else” differentiator vs Vivino.

- `share_cellar` / `share_badges` toggles.
- Redacted in-stock serializer.
- Member profile cards.
- Read-only web club page if it stays cheap.

**Effort:** medium. Mostly serializers, one bot screen, privacy tests (prices, drawers, comments, other household).

### Phase 3 — Comments + taste-offs

- Comment prompt on a feed card (`cl:c:<event_id>` inside 64 bytes).
- Taste-off setup / score / results.
- Optional: copy a taste-off score onto `StorageItem.vote` if it was that member’s bottle — default **no**.

**Effort:** medium-large. More bot state (host picking bottles), new models, scoring UX.

### Phase 4 — Global clubs

- `kind=global`, staff create, browse/join.
- Beat task for themed notes (reuse drink-by schedule style in `celery.py`).
- Moderation (mute user, freeze feed) — don’t skip this if a club is public.

**Effort:** small–medium on top of phase 1.

---

## 10. Risks

| Risk | Why it’s real here | Mitigation |
| --- | --- | --- |
| **Notification fatigue** | Drink-by + empty-cellar + badge GIFs already DM people. A chatty club will train them to mute the bot. | Phase 1: one line per open, no badge spam, mute one tap, no peek pings. Later: daily digest if any member is in >2 clubs. |
| **Privacy leak** | Household is the hard wall. A club peek that reuses `WineInStockView` would ship prices, drawers, comments, critic scores. | Dedicated serializer; tests that forbidden keys are absent. Default share off. |
| **Partner / shared cellar** | Two `HouseholdMembership`s, one stock. Peek is the whole fridge. Feed is the Telegram actor. | Document on the share toggle: “This shows everything in {household}.” |
| **Conflating club with household** | Fastest implementation is `kind=club` on `HouseholdInvite` or a household named “Thursday reds”. | New app; code review gate. |
| **Invite 500 / expiry** | Stated current pain. Clubs that reuse a broken `/start` don’t grow. | Phase 0 first. |
| **Gunicorn / I/O** | Reference lookup already taught us not to send slow work on the request. Telegram fan-out to N members is the same class. | `on_commit` + Celery. Prod gunicorn is `--workers 3 --timeout 120` — still don’t block it. |
| **Callback budget** | 64 bytes, already tight with wine_id + item_id + barcode. | Short `cl:` prefix, numeric ids, no names in `callback_data`. |
| **Allow list** | Club-only users don’t exist — they must be provisioned. | Compound redeem + `_hot_add`. |
| **Undo vs feed** | Restore window is 10 minutes from `removed_at`, not `modified`. | Retract opened event; don’t emit `opened` for bulk **move**. |
| **Command menu noise** | `/help` is already long; `/badges` and `/taste` aren’t even in `PUBLIC_COMMANDS`. | One `/club` in the menu. Subcommands stay typed. |
| **Web distraction** | Building a club site nobody opens. | Admin + maybe a read-only page. Telegram first. |

---

## 11. Open questions

1. **Household invite TTL** — extend to 30 days, or keep 7 and rely on remint? Club default proposed 30 days / multi-use cap 20.
2. **Thumbs vs 1–5 for taste-offs.** Feed should stay 👎/—/👍 because that’s what people already tap on drank cards. Scoring a flight may want 1–5. Don’t invent a third scale (`Wine.rating` 0–10) without a reason.
3. **Catch-up Drunk** — emit one event per bottle when Apply succeeds (consistent with bulk transaction), not per tap in the staging UI.
4. **Should `/switch` silently move peek?** Proposed: no, ask if `share_cellar`.
5. **Owner of a shared household opts in** — does the partner’s `/club` see that share, or only the user who toggled? Proposed: flag is per `ClubMembership`, so each person who joined the club opts independently; both would show the same stock if both opt in (duplicate peek). Better UX: share is per household-in-club, one toggle, owner-only. That’s more honest and less duplicate. **Needs a call.**
6. **Digest vs per-open** after the first club has more than ~15 members.
7. **Badge unlocks in the feed** — proposed off. They’re about *adding* bottles, not drinking, and they already GIF the household.
8. **Global club cellar peek** — even with opt-in, a “public” club makes inventory semi-public. Maybe forbid peek on `kind=global`.
9. **Single `/start` preview endpoint** vs parallel club preview — less bot branching if household preview 404s then club preview, but two round trips on every Start.
10. **Destroying a club** — owner-only, or last-member-leaves? Events retained?
11. **Name collisions with household `/start` codes** — same 8-char alphabet; unique across **both** tables (check both on mint) so a code never changes meaning.

---

## 12. Suggested call on the open questions (for review)

These are defaults to argue with, not shipped behaviour:

| # | Default |
| --- | --- |
| 1 | Household TTL 30 days, still one-use. Club 30 days, `max_uses=20`. |
| 2 | Feed = existing thumbs. Taste-off = 1–5. |
| 3 | Events on Apply, not on staging taps. |
| 4 | Confirm before peek household follows `/switch`. |
| 5 | One share flag per (club, household), owner of that household toggles. |
| 6 | Per-open until a club has 15 members, then offer digest. |
| 7 | No badge events in the feed. |
| 8 | No cellar peek on global clubs. |
| 9 | One staff preview URL that tries household then club. |
| 10 | Last member leave deletes the club; events go with it (small instance, not a museum). |
| 11 | Unique codes across `HouseholdInvite` and `ClubInvite`. |

---

## 13. Implementation notes for whoever picks this up

- Start in `wine-cellar` (models, API, Celery, tests with `--ds=wine_cellar.conf.test`). Bot module and tests in `scanner-service` second, against the API client.
- Extract a tiny `send_telegram_message(chat_id, html, reply_markup=None)` from `tasks.py` rather than copying `requests.post` a third time.
- `publish_club_event` must no-op when the actor has no memberships (cheap filter) so stock tests don’t need a club fixture.
- Do not send club DMs to `request.user`’s own `chat_id`.
- Use `esc()` for every display name in HTML, same as invites.
- Keep `CLAUDE.md` in sync when this ships (invite POST vs GET, new `/api/clubs/`, `/club`).
- Presence: `set_bot_presence` + `telegram_commands.PUBLIC_COMMANDS` together or the `/` menu lies.

---

## 14. Effort rollup

Not calendar time — how much of the system moves:

| Phase | Surface area | Relative effort |
| --- | --- | --- |
| 0 Invite hardening | `scanner/views.py`, tests, maybe TTL + bot copy | S |
| 1 Clubs + feed | New app, 5 models, 10 endpoints, Celery, bot module, onboarding, hooks on stock/vote/undo | L |
| 2 Peek + badges | Serializer + two GET endpoints + bot cards + privacy tests | M |
| 3 Comments + taste-offs | 3 models, bot flows, scoring | M–L |
| 4 Global clubs | Kind + staff commands + beat | S–M |

Phase 1 is the product. Phase 0 is the gate. Phase 2 is the cellar-native wedge. Everything else waits until people actually found a club.
