# Wine Clubs

Design for a social layer on Whiney, grounded in the current Django apps and Telegram bot. Planning only — this document is not a commitment to ship every piece.

**Goal:** more people using Whiney, and the ~11 people already on it coming back more often. The lever is *community around what is actually in the cellar*, which Vivino cannot do.

**Constraint:** nearly all use is the Telegram bot (`scanner-service/integrations/telegram_bot.py`). The Django web app is a secondary surface. Clubs must work in Telegram first.

**Out of scope:** Vivino integration (no public API).

**Owner decisions:** cellar sharing is a **club-level** policy set by the owner, not a per-member opt-in — joining means you share at that club’s level, and the invite/join card must say so. **Lists** (a “To drink” wishlist, not stock) are in scope; they can ship as an early standalone adoption win and later hook into clubs. Cellar sharing exists **only inside clubs** — there is no standalone sharing or friends feature. When a household member joins a club that peeks the cellar, other people in that household are **not** given a veto and are **not** told which club (or who joined); they get one gentle heads-up that the cellar is now shared with a club.

---

## 1. Why not households?

A household is the **permission boundary for wine data**. From `wine_cellar/apps/scanner/households.py`:

> Access control is by **household**, not by user: everyone in a household sees all of its wine, storages and stock.

That is the shared fridge: `Wine`, `Storage`, `StorageItem`, `Scanner`, and `CellarAchievement` are all `household`-scoped. `HouseholdInvite` (`kind=join|new`) provisions a Django user, a DRF token, a `HouseholdMembership`, and a `TelegramUser`. Joining a household means seeing (and changing) that cellar.

A wine club is the opposite social shape: **friends who each have their own cellar**, sharing drinking and a peek at stock at a **club-wide** level the owner chooses. Modelling a club as a household would:

- mix “share my fridge” with “see what I opened”
- force every club member into one stock ledger
- break `/fridge`, catch-up, scanners, and badges, which all assume one cellar

**Decision:** clubs are a new overlay on `User` / `TelegramUser`. Each membership points at *which household* that person is representing (their default cellar, switchable). Households stay the cellar. Clubs stay the social graph. **Sharing rules live on `Club`, not on `ClubMembership`.** Joining is consent to that club’s current policy.

**No sharing outside clubs.** There is no friends list, follow, public cellar, or “share with this person” toggle. The only way anyone outside a household sees stock (or badges) is by being in a club whose policy says so. A household with no club memberships shares nothing.

```
User ──< HouseholdMembership >── Household (cellar, stock, badges, lists)
  │
  └──< ClubMembership >── Club (feed, sharing policy, taste-offs)
           │
           ├── cellar_visibility / badges_visible  (owner-set)
           └── membership.household = “the cellar this club may see”
```

---

## 2. User stories

### Founding and inviting

- **Alex founds a club** in Telegram without staff help. Names it “Thursday reds”. He is owner. Before the invite card, he sets **what this club shares**: cellar peek (`none` / type-and-count summary / full cellar) and whether badges are visible. That policy is the club’s, not each member’s.
- **Alex invites friends.** He gets a `t.me` link he can forward, same shape as `/invite` today. The card states the sharing policy in plain language. Dewald taps it.
- **Dewald already has a cellar.** The join card repeats the policy (“Joining shares a type-and-count summary of **Hillside** and your badges with Thursday reds”). One tap joins. He is not added to Alex’s household. He does not get a second cellar. He cannot join “but hide my cellar.”
- **Sam has never used Whiney.** The same link onboards her: name a cellar (existing new-cellar flow), then the same consent card, then she lands in the club. Growth and club join are one gesture.
- **Alex cancels an unused link** before it leaks.

### The automatic feed

- **Alex opens a 2019 Merlot** (fridge scan out, “Drank this” on a label card, `/fridge` Catch up Drunk, or chat “Take it out now”). Dewald and Sam each get a DM: `Alex opened a 2019 Merlot`. Nobody typed a post.
- **Alex then taps 👎.** The same card updates: `Alex opened a 2019 Merlot 👎`.
- **Alex Undo** within the existing 10-minute restore window. The club cards retract (or show a short “put back” line). A bottle moved between fridges does **not** appear.
- **Alex mutes Thursday reds** so opens still record but he is not pinged. Leaving the club stops new events about him.

### Cellar peek (club policy)

- **Thursday reds is set to “summary”.** Alex opens Dewald’s card and sees six Merlots, two Chenins — counts by type and grape. No purchase price, no drawer, no notes. If the owner had picked **full cellar**, he would see the wine list (still no prices or locations). If **none**, members are names and a drink feed only.
- **Alex is bringing wine.** He can see Dewald already has Merlot covered and bring something else.
- **Dewald’s partner shares that household.** Peek is the household’s in-stock list, labelled as that cellar, because Dewald joined. The partner did not tap Join and has **no veto**. They are **not** told the club’s name or that it was Dewald. They get one gentle DM the first time this cellar becomes visible to any club (wording in §4). If two people from Hillside join the same club, Hillside still appears once.

### Badges in the club

- **The club has badges on** and Alex’s household has cellar challenges on (`Household.gamification_enabled`, default on). Sam can see “First Croatia”, “10 bottles”, the monthly tally for Alex’s cellar.
- **A household with challenges off** shows nothing for that member, even if the club asks for badges.
- There is no per-member “hide my badges” switch. Leave the club, or ask the owner to turn badges off for everyone.

### To-drink list (not stock)

- **Sam photographs a bottle at a mate’s / in a shop.** The label card offers **Add to cellar**, **Add to To-drink list**, **Drank this**, and **Just looking**. To-drink remembers it without a `StorageItem`. Just looking still only keeps the card (`TasteIntent.look`) — it is not a list.
- **Sam opens `/list`.** The To-drink list is there. She buys the bottle, taps it, **Move to cellar** — that is today’s add-stock path, then the list row goes.
- **From a club feed** (later): “Add to my To-drink list.” A club can also keep a **shared list** for the next tasting.

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

**Not a list:** `TasteIntent.LOOK` (`record_intent(..., "look")` on the Just looking button). That is a taste-profile signal (`HouseholdTasteSnapshot` “Recently looked at (not bought)”). It is not browsable, not a wishlist, and must not be overloaded into To-drink.

### Label card (where Lists plug in)

An unmatched label photo already offers four-ish actions (`telegram_bot.py` around the `ADD_TO_CELLAR` keyboard):

- **Add to cellar** (`a:grok`) → `POST /api/wines/` + `POST /api/stock/add/`
- **Drank this** (`a:dk`) → drank-without-stock add
- **Just looking** (`a:look`) → keep the card, drop buttons, `JUST_LOOKING_NOTE`
- Wrong wine / cancel

There is no third persistence path today. To-drink is that path: same Grok payload, **no** `StorageItem`. Do not create a fake “Wishlist” `Storage` — `/fridge`, catch-up, slot counts, and `award_for_add` all assume physical bottles.

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
- Command menu is a **flat** list (`telegram_commands.py`, `set_bot_presence`). Adding `/club` and `/list` is justified; don’t add `/clubinvite`, `/tasteoff`, `/clubfeed`, `/todrink` as top-level commands.
- A bot cannot message someone who has never pressed Start.

---

## 4. Telegram first (commands and flows)

Two new commands, not a zoo: **`/club`** (social) and **`/list`** (To-drink). Subcommands and inline buttons do the rest. Add both to `PUBLIC_COMMANDS` and `/help`. Keep `/invite` as **cellar** invite so “join my fridge” and “join my club” cannot be confused.

### No club yet

`/club` → short pitch + `Found a club` + (if they have an unused club invite in `/start` state) Join.

`/club new Thursday reds` (or tap Found, then type a name) → create, Alex is owner, then a **sharing** step (required, before any invite goes out):

```
What does Thursday reds share?

Everyone who joins shares this with the club.

○ Type and count (six Merlots, two Chenins)   ← default
○ Full cellar (names and vintages, no prices)
○ Drink feed only (no cellar peek)

Badges: [on] / off

[Create club]
```

Empty name is rejected (`NAME_MIN`/`NAME_MAX` from household invites, 2–40). Default proposed: summary + badges on. The invite card is generated only after this is set, so a forwarded link never undersells the privacy deal.

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

Owner also gets `Settings` (the same sharing choices as at create). Members see the policy as read-only text, not toggles.

### Invite

`/club invite` or the button. Message copy mirrors `JOIN_COPY` in `telegram_invites.py`: link, Share button (`t.me/share/url`), expiry line, **plus the sharing policy in the same message** so a forward is honest. Forward this message — no private follow-up (household invites already learned that people forward the wrong one).

Example extra stanza:

```
Joining shares a type-and-count summary of your cellar
and your badges with the other members. Drink-opens
are posted automatically. Not a shared fridge.
```

`/start <code>` dispatcher:

1. Preview as `HouseholdInvite` (today).
2. Else preview as `ClubInvite`.
3. Else `FRESH_LINK`.

Club preview cards — **consent is the join tap**, so the policy is on this card, not in Settings afterwards:

- **Stranger, no cellar:** “Alex invited you to Thursday reds on Whiney. You’ll get your own cellar, then you’re in the club.” Sharing stanza as above. Then the existing new-cellar name / suburb / Skip pitch (`I1_COPY` / `I2_COPY`). After the cellar exists, show the consent card once more with the **household name** filled in before Join commits. Redeem is compound: `kind=new` household **and** club membership, one staff call (or household redeem then club redeem in one view). Never create a second cellar later by accident — the household “Stay / Start a new cellar” guards apply.
- **Known user:** “Join Thursday reds?” plus sharing stanza naming **their default household** (“This shares a summary of **Hillside**, including bottles anyone in that cellar added.”). Join / Not now. Redeem only inserts `ClubMembership` with `accepted_sharing_version = club.sharing_version`. Confirm-if-already-in-a-cellar is **not** needed (we are not creating a household). If they have several households, pick which cellar this club sees (same picker as `/invite` when `list_households()` length > 1) **before** Join.
- **Already a member:** idempotent, like `already_member` on household redeem.

`/club invite cancel` — drop the newest unused club invite for this club (copy `HouseholdInviteCancelView`).

### Household heads-up (not a veto)

When someone joins a club with `cellar_visibility` of `summary` or `full`, other active `TelegramUser`s on that household (everyone except the joiner) get a DM **if this household was not already visible to a club**. No buttons. No club name, no member name, no “Thursday reds”, no “Dewald”. Not sent when the club is feed-only (`none`). Not sent again when a second club is joined or a second household member joins — that would leak “another one.” If every sharing membership later drops away, the next new one may ping once more (same idea as `empty_nudge_sent_at` on `Household`).

Suggested copy (warm, short; HTML via `esc()` only if a household name is interpolated — this draft uses none):

```
Just so you know — this cellar is now shared
inside a wine club.

Only people in the club can see it, and only
what the club allows. Whiney never shares a
cellar any other way.
```

That is the whole message. No `/club` (they may not be in one). `/household` is optional if we want a single next step; default is no button so it doesn’t feel like an action they missed. The joiner does not get this; they already saw the consent card.

### Feed (automatic)

No `/club post`. On a qualifying stock mutation the API inserts `ClubEvent` rows for each of the actor’s clubs (unless they muted *posting*, which MVP does not offer — mute is receive-only). A Celery task fans out DMs to other members with `feed_muted=False`.

Copy:

- open, no vote: `{name} opened a {vintage} {wine}`
- open, then vote: same line plus `👎` / `👍` / nothing for meh (match `VOTE_LABELS`: down / up / em-dash)
- join: `{name} joined`
- retract: edit the sent messages to `{name} put the {wine} back` when `ClubEventDelivery.message_id` is known; else leave the original (11 users — store deliveries from day one)

Actor is `request.user` via `TelegramUser.display_name`, **not** the household name. Shared cellar: if Alex’s partner drinks using their own Telegram, the feed says the partner’s name.

Buttons on a feed card (MVP): none required. Phase 3 can add `Add to To-drink` (`cl:td,<event_id>` — numeric, fits 64 bytes) and comments.

`/club feed` pages like `/recent` (5 at a time, Newer/Older). Data from `ClubEvent`, not from another household’s `StockHistoryView`.

### Members and peek

`/club members` — list display names, owner crown. A one-line policy under the title (“Summary · badges on”), not per-row ticks.

Tap a member → profile card: name, optional latest open. `Show cellar` if the **club** `cellar_visibility` is `summary` or `full` (and this membership has accepted the current `sharing_version`). `Badges` if the club has `badges_visible` and that household has `gamification_enabled`.

Show cellar:

- `summary` — counts by type and grape (“six Merlots, two Chenins”), then at most a short top list. This is the “bring something else” moment.
- `full` — wines as `name vintage · grape · country · ×N`, cap ~30, “and N more”. Still **not** a `/stock` clone: no prices, drawers, notes, barcodes.
- `none` — no button.

Owner Settings can change the policy. Members cannot. Changing it is the “widen later” problem (open question; suggested call in §12).

### Mute / leave

- `Mute` — `feed_muted=True`, still a member, still in peek at the club’s level.
- `Leave` — delete membership; historical events keep the denormalised name.
- Owner `Leave` with other members: must `Hand over` first, or the club dissolves if they are the last member.

### Lists — To-drink (Telegram)

Standalone of clubs. One default household list named **To drink**. Later, named lists (`Holiday`, `Next tasting`) are extra rows of the same model, not a new product.

**Label / scan card.** Unmatched bottle (shop or mate’s) adds one button next to Add to cellar:

```
[✅ Add to cellar]
[📝 To-drink list]
[🍷 Drank this]
[👀 Just looking]
```

Callback needs a short action (`a:td` + existing `b:<temp_key>` / wine_id). Same 64-byte budget as `a:grok`. Persist the Grok (or OFF) payload on a `WineListItem` snapshot. Do **not** `POST /api/stock/add/`. Do **not** invent a Storage. Keep the card, like Just looking, with a note `On your To-drink list` instead of `JUST_LOOKING_NOTE`. If they already have this wine in stock, the button can say `Already in the cellar` and skip.

Matched existing wine: still allow To-drink (they might want another bottle) — or hide it if `stock_count > 0`. Proposed: allow, unique on `(list, wine_id)` so it doesn’t duplicate.

**`/list`** (add to `PUBLIC_COMMANDS` and `/help`; one command, not `/todrink` + `/wishlist`). Empty: “Photograph a bottle you don’t own yet.” Otherwise pages like `/recent`. Each row: name, vintage, producer, `Move to cellar` / `Remove`.

**Move to cellar:** `POST /api/wines/` if there is no household `Wine` yet (same serializer as Add to cellar, from the stored payload), then `POST /api/stock/add/`, then drop or mark the list item `moved_at`. Fridge picker if they have more than one storage — reuse the existing add-stock UI. Badges fire because this is a real add.

**Just looking** stays. It means “I’m done, don’t remember this.” To-drink means “remember, don’t stock.”

**Named lists later:** `/list new Holiday`, picker when adding if more than one list exists. MVP is a single To-drink list auto-created per household (like Storage “Cellar” on new-cellar redeem).

### Taste-offs (phase 3, command still `/club`)

`/club tasteoff` (owner/host): name it, add bottles (pick from a shared cellar, or type a label). `Open scoring` DMs the club. Each member gets one card per wine with 1–5 (or reuse 👎/—/👍 if we decide thumbs are enough — open question). Host `Close`. Results card is a `ClubEvent` of kind `tasteoff` plus rows in `TasteOffScore`.

### Global clubs (phase 4)

Staff `/club global …` (or admin). Members `/club join New World Cab` from a list. Themed updates are Celery beat posts (same send path as drink-by), not members posting.

### Natural-language chat

Do **not** route “what did Alex open?” or “add this to my list” through `ChatRouter` in MVP. Parser + cellar_facts are household-scoped (`chat_models.py`, `cellar_facts.py`). A later phase can add `club` / `list` intents; until then `/club` and `/list` are the interface so we don’t invent commands in `wine_chat`.

---

## 5. Web second

No new React. Existing templates are Django + Pure CSS (`homepage.html`, `settings.html`).

| Phase | Web |
| --- | --- |
| 0 | none |
| 1 Lists | Optional `/list/` page; admin. Telegram is enough. |
| 2 Clubs | Django admin for `Club`, membership, invites, events. Optional read-only `/clubs/` and `/club/<id>/` feed. `HouseholdScopedMixin` is **wrong** here — gate on `ClubMembership`. Peek uses the club policy, same redacted serializer as Telegram. Owner-only sharing form (not a per-user settings checkbox). |
| 3 | Comments + taste-off results + club list. |
| 4 | Global club pages. |

Do not put club badges on the cellar dashboard. That page is household challenges.

---

## 6. Data model

New Django app: **`wine_cellar.apps.club`**. Do not hang these models off `scanner` just because `Household` lives there — scanner is devices, households, invites, Telegram identity. A new app keeps the cellar/club boundary obvious in `INSTALLED_APPS` (`wine_cellar/conf/settings.py`).

**Lists are not in that app.** `WineList` / `WineListItem` belong in `wine_cellar.apps.wine` (household catalogue, not social graph). A club list is a `kind=club` row with a FK, not a reason to store wishlists next to `HouseholdInvite`.

### `Club`

- `name` (100, like `Household.name`)
- `kind` — `private` \| `global`
- `created_by` FK User
- `created`
- `description` (blank, for global themed copy)
- **Sharing policy (owner-set, applies to every member):**
  - `cellar_visibility` — `none` \| `summary` \| `full` (default `summary`)
  - `badges_visible` — bool (default True)
  - `sharing_version` — small integer, starts at 1, **increments only when the owner widens** (none→summary, summary→full, or badges off→on). Narrowing does not increment.
- Names need **not** be globally unique (households are; clubs are small and personal). Unique together `(created_by, name)` is enough to stop Alex founding two “Thursday reds”.

There are **no** per-member share flags. A member who disagrees leaves.

### `ClubMembership`

- `club`, `user` — unique together
- `role` — `owner` \| `member` (same strings as `HouseholdMembership.ROLE_CHOICES`)
- `household` FK — cellar this membership exposes at the **club’s** visibility level; set at join (default household, or picked); update on `/switch` only if they confirm “use this cellar for Thursday reds”
- `feed_muted` default False
- `accepted_sharing_version` — set to `club.sharing_version` on join and on re-consent. Peek of this member is allowed only when it equals the club’s current version (so a widen can wait for ack without bringing back per-member opt-in).
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

### `WineList` and `WineListItem` (phase 1 — lives in `wine`, not `club`)

Household-scoped, like `Storage`. Not a `StorageItem`. Not a `TasteIntent`.

`WineList`:

- `household` FK
- `name` (default **To drink**; unique per household)
- `kind` — `personal` \| `club` (`club` used in phase 3)
- `club` FK null (only when `kind=club`)
- `created_by` FK User
- Auto-create “To drink” on first use or on new-cellar redeem (same idea as Storage named `Cellar`)

`WineListItem`:

- `list` FK
- Snapshot: `name`, `producer`, `vintage`, `wine_type`, `country`, `grape`, `barcode` (all nullable as on a label read)
- `payload` JSON — the merged Grok/OFF reading so Move to cellar can call `WineCreateSerializer` without another photo
- `wine` FK null — set if the household already has this `Wine`, or after move
- `source` — `label` \| `barcode` \| `club_feed` \| `typed`
- `added_by` FK User (`UserContentModel` provenance)
- `created`, `moved_at` null
- Unique: `(list, wine)` when `wine` is set; otherwise don’t try to unique on free text

Moving to cellar creates stock; it does not keep a ghost bottle in the fridge. Soft-delete the list row or set `moved_at`.

A **club list** (phase 3) is `kind=club`, visible to members at the club’s cellar-visibility level (names on a tasting list are not a full cellar dump). Adding from a feed item copies the event snapshot into the actor’s personal To-drink list (`source=club_feed`).

### What we do **not** change

- `Wine`, `Storage`, `StorageItem` stay household-scoped. No `club` FK on stock. Lists do not fake stock.
- `CellarAchievement` stays household-scoped. Club display **reads** it when the **club** has `badges_visible` and the household has `gamification_enabled`.
- `TasteIntent` / `HouseholdTasteSnapshot` stay cellar taste for chat recommendations, not a wishlist.

### Privacy rules (normative)

1. **Join = consent to this club’s current policy**, named on the invite and join cards: automatic drink/vote feed (`TelegramUser.display_name`) **and** cellar/badges at `Club.cellar_visibility` / `badges_visible`. There is no “join but hide my cellar.”
2. **Cellar peek** is live `StorageItem(deleted=False)` for `membership.household`. Which fields depend on the **club** setting:
   - `none` — no peek endpoint.
   - `summary` — counts by `wine_type` and grape (and maybe country). Individual bottles optional as a short “top” list; still no prices.
   - `full` — name, producer, vintage, wine_type, country, grapes, stock_count.
   - **Always forbidden:** `price`, `items[].price`, `market_price`, storage name, drawer/row, `comment`, `drink_by`, barcode, `rating`, critic/Vivino.
3. **Badges** only if the club has `badges_visible` and that household’s `gamification_enabled`. Show title/emoji/unlocked_at, not `subject` ids.
4. **Mute** stops feed delivery, not peek and not recording.
5. **Leave** stops peek, new events **from** and **to** that user. Historical feed lines keep the denormalised name.
6. **Widening the policy** (none→summary, summary→full, badges off→on) increments `sharing_version` and must not silently expand what already-joined people share. Suggested: DM every member; peek at the *new* level only after they tap Stay (writes `accepted_sharing_version`). Until then they stay visible at the **previous** level (encode previous on the club or treat unacked as `none` for peek — pick one, see §11). Narrowing applies immediately and auto-acks.
7. **Shared households.** Peek is the whole cellar of `membership.household`, including bottles a partner added. The **joiner’s** consent card names that household. Other people in the household are not asked, have no veto, and are not told which club or who joined. They get the one-line heads-up in §4 the first time this cellar is visible to any club. Cellar sharing does not exist outside clubs.
8. **Staff/global clubs** do not bypass the club policy. Proposed: `kind=global` forces `cellar_visibility=none` (feed and maybe badges only).
9. API: every club endpoint uses the caller’s `User`, never `X-Household` as the club id. Peek of another member checks the **club** policy and that member’s `accepted_sharing_version`, not a personal share flag.
10. Cross-household `wine_id` in events is a snapshot aid for the actor’s own undo, not a permission to read that wine.
11. **Lists.** Personal To-drink is household-private. A club list is visible to club members; it is not an excuse to dump someone’s fridge. Adding from a feed copies a snapshot, not a live `Wine` row from another household.

---

## 7. API sketch (Django, consumed by the bot)

Mount under `/api/clubs/` (new urls module), `TokenAuthentication`, `IsAuthenticated`. Staff redeem/preview stay `IsAdminUser` like household invites (token in the body).

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/clubs/` | Found `{name, cellar_visibility, badges_visible}` |
| GET | `/api/clubs/` | Clubs for this user (includes policy) |
| GET | `/api/clubs/<id>/` | Home: members count, last events, policy, `accepted_sharing_version` |
| POST | `/api/clubs/<id>/leave/` | |
| PATCH | `/api/clubs/<id>/` | Owner: `cellar_visibility`, `badges_visible`. Widens bump `sharing_version`. |
| PATCH | `/api/clubs/<id>/membership/` | Member: `feed_muted`, `household_id`, `accepted_sharing_version` (re-consent only, must equal current) |
| POST | `/api/clubs/<id>/invites/` | Mint; preview payload includes policy copy |
| POST | `/api/clubs/<id>/invites/cancel/` | |
| POST | `/api/scanner/club-invites/preview/` | Staff; or a single dispatcher `POST /api/scanner/invites/preview/` that tries household then club. **Must return the sharing policy** so the join card can render it. |
| POST | `/api/scanner/club-invites/redeem/` | Staff; body `code, chat_id, display_name, household_name?, household_id?`. Sets `accepted_sharing_version`. |
| GET | `/api/clubs/<id>/feed/?limit=5&offset=0` | |
| GET | `/api/clubs/<id>/members/` | |
| GET | `/api/clubs/<id>/members/<user_id>/cellar/` | 404 if club `cellar_visibility=none` or member hasn’t accepted current version (per §6.6) |
| GET | `/api/clubs/<id>/members/<user_id>/badges/` | 404 if club badges off / household gamification off |

Lists (household-scoped, `ScannerAPIView` + `X-Household`):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/lists/` | Household lists (create default To-drink if missing) |
| GET | `/api/lists/<id>/` | Items, paginated |
| POST | `/api/lists/<id>/items/` | Body: snapshot fields + `payload` (from the label card) |
| DELETE | `/api/lists/<id>/items/<item_id>/` | Remove |
| POST | `/api/lists/<id>/items/<item_id>/to-cellar/` | Create/find `Wine`, `stock/add`, set `moved_at` |

**Hooks (not new routes):** after successful `_soft_delete_item`, `StockAddView` when `drank=True`, each bulk **remove** op, `StockVoteView`, and restore/undo. Keep the request fast: `transaction.on_commit` → `publish_club_event.delay(item_id, actor_id, kind)`. Never inline Telegram I/O in gunicorn (the reference-lookup saga: search is a Celery task because a sync call blocked the bot).

Idempotency: unique `(club, item_id, kind=opened)` so a double remove or replay doesn’t double-post. Vote updates the opened event’s `vote` and edits deliveries.

Scanner client: add methods on `WineCellarAPI` next to `mint_invite` / `vote_item` / `list_achievements`. Bot module `integrations/telegram_clubs.py` beside `telegram_invites.py` so `telegram_bot.py` doesn’t grow another 1k lines.

`/api/scanner/config/` does **not** need club lists at bootstrap. Feed send is Django→Telegram; interactive `/club` uses the user’s existing token.

---

## 8. How this reuses badges, invites, household

| Existing | Club use |
| --- | --- |
| `Household` | Cellar of record on `ClubMembership.household`. Never the club itself. Peek uses this household at the **club’s** visibility. |
| `HouseholdInvite` + `InviteFlow` | Pattern only. Shared `/start` dispatcher. Compound redeem for strangers (new cellar + membership). Hot-add, `esc()`, Share button, 30‑min in-memory flow TTL. Join card carries club policy the way `JOIN_COPY` carries expiry. |
| `Household.gamification_enabled` + `CellarAchievement` | Unchanged writers (`award_for_add`, `backfill`). Club reads when **club** `badges_visible`. No new badge slugs for “joined a club” in MVP (noise). |
| `StorageItem.vote` / drank cards | Feed copy and vote updates. |
| Label card `ADD_TO_CELLAR` / `look` | Add a To-drink button beside them. Just looking stays non-persistent. |
| `WineCreateSerializer` + `StockAddView` | Move-to-cellar reuses these; list item is not stock. |
| `TelegramUser.display_name` | Actor name. Fall back to `user.username` like `month_tally`. |
| `drink_by_reminder` send helper | Extract `_send_telegram` (already a function) for club fan-out and sharing-widen DMs. |
| `/switch` | May change which cellar the club peeks, with an explicit confirm (policy already consented; this is *which* fridge). |
| `/invite` | Stays “invite to **this cellar**”. `/club invite` is the social link — that’s the growth link we put in people’s hands. |

---

## 9. Phased MVP

Ship for **adoption**, not completeness. Two loops, both valid:

1. *I saw a bottle I don’t own → it’s on my To-drink list → I come back when I buy it.* Works with the 11 people already here, no social graph required.
2. *Someone I know opened a bottle → I open the chat → I invite the next friend.* Needs working invites and a club.

Lists can ship first and standalone. Clubs should not ship a join tap that promises peek and then omit peek — consent-at-join means visibility is in the same phase as founding.

### Phase 0 — Invite reliability (unblocks growth and clubs)

Household invites are how people get onto Whiney at all. Clubs will share that `/start` pipe.

- Catch `_mint_invite` IntegrityError as 503 with the existing “couldn’t reach” copy.
- Harden `_resolve_join_household` `int(header_id)` (copy `ScannerAPIView`).
- Confirm redeem lock never joins (test already there).
- Decide household TTL: keep 7 days but make reminting obvious, **or** extend to 30 days. Document in `/invite` copy.
- Regression tests for create-500 paths.

**Effort:** small. Django views + tests, tiny bot copy. No new models.

### Phase 1 — Lists (To-drink)  ★ early standalone adoption win

Useful the next time someone is in a shop or at a mate’s. No club, no fan-out, no new privacy graph.

- `WineList` / `WineListItem` in the **wine** app, household-scoped.
- Default “To drink” list.
- Label/scan card: **To-drink list** beside Add to cellar; Just looking unchanged.
- `/list`: view, remove, **Move to cellar** (reuse wine create + stock add).
- API as in §7.
- Tests: item is not a `StorageItem`; `/fridge` ignores it; unique-on-wine; move-to-cellar awards badges like a normal add; payload round-trips producer/vintage.

**Effort: medium (M).** Two models, ~5 endpoints, one bot command, one extra button on the existing label keyboard, pytest on both services. Smaller than clubs: no Celery, no invites, no cross-household authz. Main risk is the 64-byte callback and not accidentally creating stock.

**Not in this phase:** named extra lists, club lists, add-from-feed.

Can ship in parallel with phase 2; it does not depend on clubs. If we have to pick one adoption bet before the other, **Lists is the cheaper, solo-user win**; clubs are the network win.

### Phase 2 — Private clubs + feed + club-level sharing

The social product, including peek, because the join card is a consent contract.

- App `club`, models `Club` (with policy + `sharing_version`), `ClubMembership` (with `accepted_sharing_version`, **no** share flags), `ClubInvite`, `ClubEvent`, `ClubEventDelivery`.
- Found → sharing step → invite. Join/invite copy names the policy and the household.
- Stranger compound onboard.
- Hooks on drank + vote + undo.
- Celery fan-out DMs.
- `/club` home, feed, members, peek at `summary`/`full`/`none`, badges if the club says so.
- Owner Settings; widen → re-consent DMs.
- Admin.
- Tests: no household leak, forbidden peek keys, join without hiding, unacked member after a widen, invite dispatcher vs household codes, idempotent opened events, mute, undo edit.

**Effort:** large (L), a bit larger than the previous “feed-only” club slice because peek and join-time consent ride along. New app, migrations, API, Celery, a bot module, onboarding branch in `telegram_invites.py`. Comparable to household-invite work (`1911372`) plus drink-by fan-out plus a redacted in-stock serializer.

**Not in phase 2:** comments, taste-offs, club lists, global clubs, chat-router intent, badge unlock pings to the club, per-member share toggles (won’t come later either).

### Phase 3 — Comments, taste-offs, club-tied lists

- Comment prompt on a feed card (`cl:c:<event_id>` inside 64 bytes).
- Taste-off setup / score / results.
- Optional: copy a taste-off score onto `StorageItem.vote` if it was that member’s bottle — default **no**.
- Feed button **Add to my To-drink list** (copies snapshot onto the personal list).
- Optional **club list** for the next tasting (`WineList.kind=club`). Named personal lists if people actually used To-drink.

**Effort:** medium-large (M–L). Taste-off is the bulk of it. Club-tied lists are small **if phase 1 shipped** (one `kind`, one button). If phase 1 slipped, this phase inherits that M.

### Phase 4 — Global clubs

- `kind=global`, staff create, browse/join.
- Beat task for themed notes (reuse drink-by schedule style in `celery.py`).
- `cellar_visibility` forced to `none` unless we explicitly decide otherwise.
- Moderation (mute user, freeze feed) — don’t skip this if a club is public.

**Effort:** small–medium on top of phase 2.

---

## 10. Risks

| Risk | Why it’s real here | Mitigation |
| --- | --- | --- |
| **Notification fatigue** | Drink-by + empty-cellar + badge GIFs already DM people. A chatty club will train them to mute the bot. | Phase 1: one line per open, no badge spam, mute one tap, no peek pings. Later: daily digest if any member is in >2 clubs. |
| **Privacy leak** | Household is the hard wall. A club peek that reuses `WineInStockView` would ship prices, drawers, comments, critic scores. Join-time consent makes a leak worse (people think they know what they agreed to). | Dedicated serializer; tests that forbidden keys are absent. Policy on the join card must match the serializer. |
| **Widening after join** | Owner turns summary → full; existing members never tapped that. | `sharing_version` + re-consent DMs. Do not silently expand. |
| **Partner / shared cellar** | Two `HouseholdMembership`s, one stock. Peek is the whole fridge. Feed is the Telegram actor. Partner may not be in the club. | Joiner’s consent card names the household. Other members: one anonymous heads-up, no veto, no club/member names. Ping at most once until sharing fully stops. |
| **To-drink becomes fake stock** | Fastest implementation is a “Wishlist” `Storage` or `StorageItem(deleted=True)`. That pollutes `/fridge`, catch-up, badges, drink-by. | Separate models. Tests that list items never appear in `WineInStockView`. |
| **Conflating club with household** | Fastest implementation is `kind=club` on `HouseholdInvite` or a household named “Thursday reds”. | New app; code review gate. |
| **Invite 500 / expiry** | Stated current pain. Clubs that reuse a broken `/start` don’t grow. | Phase 0 first. |
| **Gunicorn / I/O** | Reference lookup already taught us not to send slow work on the request. Telegram fan-out to N members is the same class. | `on_commit` + Celery. Prod gunicorn is `--workers 3 --timeout 120` — still don’t block it. |
| **Callback budget** | 64 bytes, already tight with wine_id + item_id + barcode. | Short `cl:` prefix, numeric ids, no names in `callback_data`. |
| **Allow list** | Club-only users don’t exist — they must be provisioned. | Compound redeem + `_hot_add`. |
| **Undo vs feed** | Restore window is 10 minutes from `removed_at`, not `modified`. | Retract opened event; don’t emit `opened` for bulk **move**. |
| **Command menu noise** | `/help` is already long; `/badges` and `/taste` aren’t even in `PUBLIC_COMMANDS`. | `/club` and `/list` in the menu. Subcommands stay typed. |
| **Web distraction** | Building a club site nobody opens. | Admin + maybe a read-only page. Telegram first. |

---

## 11. Open questions

**Decided.** Households with several people: no veto; no club or member names in the heads-up; cellar sharing exists only inside clubs (no friends/public-share feature). Copy is in §4. If two members join the same club, show that household once.

1. **Household invite TTL** — extend to 30 days, or keep 7 and rely on remint? Club default proposed 30 days / multi-use cap 20.
2. **Thumbs vs 1–5 for taste-offs.** Feed should stay 👎/—/👍 because that’s what people already tap on drank cards. Scoring a flight may want 1–5. Don’t invent a third scale (`Wine.rating` 0–10) without a reason.
3. **Catch-up Drunk** — emit one event per bottle when Apply succeeds (consistent with bulk transaction), not per tap in the staging UI.
4. **Should `/switch` silently move which cellar the club peeks?** Proposed: no, ask. The policy is already consented; this only changes *which* fridge.
5. **Owner widens sharing later.** Re-consent, notify-only (Leave if you hate it), or freeze peek until ack? Unacked members: hide from peek vs keep previous level?
6. *(decided — see above)*
7. **Digest vs per-open** after the first club has more than ~15 members.
8. **Badge unlocks in the feed** — proposed off. They’re about *adding* bottles, not drinking, and they already GIF the household.
9. **Global club cellar peek** — joining a public club would publish inventory. Proposed: force `cellar_visibility=none` on `kind=global`.
10. **Single `/start` preview endpoint** vs parallel club preview — less bot branching if household preview 404s then club preview, but two round trips on every Start. Preview **must** include sharing policy.
11. **Destroying a club** — owner-only, or last-member-leaves? Events retained?
12. **Name collisions with household `/start` codes** — same 8-char alphabet; unique across **both** tables (check both on mint) so a code never changes meaning.
13. **Lists vs Just looking.** Keep both, or replace Just looking with To-drink? Proposed: keep both (ephemeral vs remember).
14. **To-drink if the wine is already in stock.** Allow a second-bottle reminder, or hide the button?
15. **Lists household- vs user-scoped.** Everything else is household. A partner would see Sam’s shop finds. Proposed: household-scoped, same as stock, with `added_by` provenance.
16. **Default club policy.** Summary + badges on (adoption), or drink-feed-only until the owner picks? Proposed: required step at create, defaulting to summary + badges.

---

## 12. Suggested call on the open questions (for review)

These are defaults to argue with, not shipped behaviour:

| # | Default |
| --- | --- |
| 1 | Household TTL 30 days, still one-use. Club 30 days, `max_uses=20`. |
| 2 | Feed = existing thumbs. Taste-off = 1–5. |
| 3 | Events on Apply, not on staging taps. |
| 4 | Confirm before peek household follows `/switch`. |
| 5 | Widen → DM + Stay/Leave. Peek at the **new** level only after Stay (`accepted_sharing_version`). Unacked members stay visible at the **previous** level (store `previous_cellar_visibility` on the club, or treat unacked as `none` if that’s simpler — prefer previous level so a widen isn’t a hide). Narrowing applies now. |
| 6 | **Decided.** No veto. Heads-up only, no club/member names, once per household until sharing stops. Show a household once in a club even if two members joined. Sharing exists only inside clubs. |
| 7 | Per-open until a club has 15 members, then offer digest. |
| 8 | No badge events in the feed. |
| 9 | No cellar peek on global clubs. |
| 10 | One staff preview URL that tries household then club; body includes policy. |
| 11 | Last member leave deletes the club; events go with it (small instance, not a museum). |
| 12 | Unique codes across `HouseholdInvite` and `ClubInvite`. |
| 13 | Keep Just looking and To-drink as different buttons. |
| 14 | Allow To-drink even if in stock; unique on `(list, wine_id)`. |
| 15 | Lists are household-scoped. |
| 16 | Create flow requires a sharing pick; UI default is summary + badges on. |

---

## 13. Implementation notes for whoever picks this up

- Start in `wine-cellar` (models, API, Celery, tests with `--ds=wine_cellar.conf.test`). Bot module and tests in `scanner-service` second, against the API client.
- Extract a tiny `send_telegram_message(chat_id, html, reply_markup=None)` from `tasks.py` rather than copying `requests.post` a third time.
- `publish_club_event` must no-op when the actor has no memberships (cheap filter) so stock tests don’t need a club fixture.
- Do not send club DMs to `request.user`’s own `chat_id`.
- Use `esc()` for every display name in HTML, same as invites.
- Keep `CLAUDE.md` in sync when this ships (invite POST vs GET, new `/api/clubs/`, `/api/lists/`, `/club`, `/list`).
- Presence: `set_bot_presence` + `telegram_commands.PUBLIC_COMMANDS` together or the `/` menu lies.
- Label-card tests in `test_label_card.py` already lock Just looking and Add to cellar — extend them rather than a new photo path.
- Household club-share heads-up: a timestamp on `Household` (same once-pattern as `empty_nudge_sent_at`). Never interpolate club or user names into that message.

---

## 14. Effort rollup

Not calendar time — how much of the system moves:

| Phase | Surface area | Relative effort |
| --- | --- | --- |
| 0 Invite hardening | `scanner/views.py`, tests, maybe TTL + bot copy | S |
| 1 Lists (To-drink) | 2 models in `wine`, ~5 endpoints, label-card button, `/list`, move-to-cellar | **M** |
| 2 Clubs + feed + club-level peek | New app, policy on `Club`, Celery, bot module, onboarding, stock hooks, redacted peek | L |
| 3 Comments, taste-offs, club lists | 3 models + scoring; list hook is small if phase 1 shipped | M–L |
| 4 Global clubs | Kind + staff commands + beat | S–M |

Phase 1 (Lists) is the cheap solo-user habit. Phase 2 is the network product; join-time sharing means peek ships with it. Phase 0 is the gate for anyone new. Club-tied lists wait until both 1 and 2 exist.
