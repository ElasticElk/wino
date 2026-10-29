# Wine Clubs — implementation spec

Planning only. No application code lives in this folder.

Whiney is a self-hosted cellar tracker. Almost all use is Telegram (`scanner-service/integrations/telegram_bot.py`). About 11 people. The web app is secondary. The product gap versus Vivino is **knowing what is actually in the cellar**.

This spec is the build plan for a social layer (clubs) and a To-drink list. It is grounded in the current Django apps (`scanner`, `storage`, `user`, `wine`, `api`) and the bot.

**Out of scope:** Vivino integration (no public API).

## How to read this

| Doc | What it is |
| --- | --- |
| [README.md](README.md) (this file) | Decisions, architecture, language, flags, how phases relate |
| [phase-0-invites.md](phase-0-invites.md) | Harden household invites (gate for growth and clubs) |
| [phase-1-lists.md](phase-1-lists.md) | To-drink lists (standalone adoption) |
| [phase-2-clubs.md](phase-2-clubs.md) | Private clubs, automatic feed, club-level cellar sharing |
| [phase-3-social.md](phase-3-social.md) | Comments, taste-offs, club-tied lists |
| [phase-4-global.md](phase-4-global.md) | Staff-run global clubs |
| [open-questions.md](open-questions.md) | Remaining questions with recommended answers |
| [copy-catalog.md](copy-catalog.md) | Every new EN/AF string keyed for `copy.py` |

Old pointer: [`docs/wine-club-plan.md`](../wine-club-plan.md) redirects here.

Example people in copy: **Riley**, **Sam**, **Lee**, **Pat**. Example cellar: **Hillside**. Example club: **Thursday reds**. Do not put real people’s names in this spec, in tests, or in help examples.

## Why not households?

A household is the **permission boundary for wine data** (`wine_cellar/apps/scanner/households.py`). Everyone in a household sees all of its wine, storages and stock. `Wine`, `Storage`, `StorageItem`, `Scanner`, and `CellarAchievement` are household-scoped. `HouseholdInvite` (`kind=join|new`) provisions a Django user, a DRF token, a `HouseholdMembership`, and a `TelegramUser`. Joining a household means seeing and changing that cellar.

A wine club is the opposite shape: friends who **each have their own cellar**, sharing drinking and a peek at stock at a **club-wide** level the owner chooses. Modelling a club as a household would mix “share my fridge” with “see what I opened”, force one stock ledger, and break `/fridge`, catch-up, scanners, and badges.

```
User ──< HouseholdMembership >── Household (cellar, stock, badges, lists)
  │
  └──< ClubMembership >── Club (feed, sharing policy, taste-offs)
           │
           ├── cellar_visibility / badges_visible  (owner-set)
           └── membership.household = “the cellar this club may see”
```

Lists (`WineList`) live in `wine_cellar.apps.wine`. Clubs live in a new app `wine_cellar.apps.club`.

## Decided (do not reopen in a phase PR)

1. **Clubs are not households.** New app, new membership, new invites. Do not add `kind=club` to `HouseholdInvite`.
2. **Sharing rules live on `Club`**, not on `ClubMembership`. Owner sets `cellar_visibility` (`none` / `summary` / `full`) and `badges_visible`. Joining is consent to that policy. There is no “join but hide my cellar.”
3. **Consent is the join tap.** Invite and join cards name the policy and the household being shared. Settings afterwards are owner-only for policy; members may mute or leave.
4. **Cellar sharing exists only inside clubs.** No friends list, follow, public cellar, or “share with this person.” A household with no club memberships shares nothing.
5. **Shared households, other members:** no veto; not told which club or who joined; one gentle heads-up the first time that cellar becomes visible to any club. If two people from Hillside join the same club, Hillside appears once.
6. **Lists are not stock.** `WineList` / `WineListItem`, not a wishlist `Storage` or a deleted `StorageItem`. Just looking (`TasteIntent.look`) stays ephemeral.
7. **Telegram first.** Web is admin plus optional read-only pages. No new React.
8. **`/invite` stays cellar.** `/club` is social. `/list` is To-drink.
9. **Feed is automatic** from drank / vote / undo. Nobody posts. Catch-up **Moved** is not a feed event. Badge unlocks are not feed events.
10. **Mute is receive-only.** Opens still record. Leave stops new events from and to that user.

Recommended answers for everything still open: [open-questions.md](open-questions.md). Treat those as the build default unless a later review flips them. Phase PRs should not bikeshed them.

## Phases and dependency

```
0 invites ──► 2 clubs (needs a working /start pipe)
                 │
1 lists ─────────┴──► 3 club-tied lists / comments / taste-offs
                         │
                         └──► 4 global clubs
```

Phase 1 does **not** depend on 2. If we pick one adoption bet first, lists are the cheaper solo-user win; clubs are the network win. Phase 2 must include peek: join-time consent is a lie if peek ships months later.

| Phase | Adoption loop | Effort (whole phase) |
| --- | --- | --- |
| 0 | A friend can still tap the link | S |
| 1 | Saw a bottle I don’t own → it’s on To-drink → I come back when I buy it | M |
| 2 | Someone opened a bottle → I open the chat → I invite the next friend | L |
| 3 | Talk about the bottle; host a tasting | M–L |
| 4 | A few themed public rooms | S–M |

Effort is relative surface area, not calendar time. Each phase doc splits that into **PR-sized tasks**.

## Existing code this spec must not rebuild

| Piece | Where |
| --- | --- |
| `Household`, `HouseholdMembership`, `TelegramUser`, `HouseholdInvite` | `wine_cellar/apps/scanner/models.py` |
| Household resolution (`X-Household`, default, first) | `scanner/households.py`, `ScannerAPIView.get_household` |
| Invite mint / preview / redeem / cancel | `scanner/views.py`, `telegram_invites.py` |
| Drank / vote / undo | `POST /api/stock/remove/`, `add/?drank=`, `bulk/`, `PATCH .../vote/`, restore (`RESTORE_WINDOW` = 10 min) |
| Label card | `ADD_TO_CELLAR`, `Drank this`, `Just looking` (`telegram_bot.py`) |
| Badges | `CellarAchievement`, `Household.gamification_enabled` (default on), `/badges` |
| Django → Telegram send | `_send_telegram` in `wine_cellar/apps/wine/tasks.py` (drink-by, empty-cellar nudge) |
| Bot allow list | `filters.Chat` from `/api/scanner/config/`; `_hot_add` after redeem |
| Callback budget | 64 bytes (`CALLBACK_DATA_MAX`); compact `a:vt,i:99,o:up`; invites `inv:` |
| Command menu | `telegram_commands.py` + `set_bot_presence` (keep in sync) |

Do **not** use `TelegramNotifier._send_to_all` for clubs — that is the Pi scanner’s household notify list.

`UserSettings.notifications` is **email drink-by only**. Club mute is `ClubMembership.feed_muted`.

## Language

Django `UserSettings.language` exists (`wine_cellar/apps/user/models.py`) with `LANGUAGES` currently `en-gb`, `de-DE`, `fr-FR` (`wine_cellar/conf/settings.py`). The **bot copy is hardcoded English** (`telegram_invites.py`, `telegram_bot.py`). Chat answers (`wine_chat`) already reply in the language they were asked in; slash-command cards do not.

**Design for any language from day one of new copy; ship English plus Afrikaans for the new strings.**

Rules:

1. New user-visible strings live in a **message catalog** keyed by id (`club.join.consent`, `list.added`, …), not interpolated ad-hoc in handlers. English is the source of truth in this spec.
2. Resolve locale: `UserSettings.language` if set → else Telegram `language_code` for unprovisioned chats → else `en`. Map `en* → en`, `af* → af`. Adding `af` / `af-ZA` to Django `LANGUAGES` is a phase-0 or phase-1 chore so web settings can pick it.
3. Afrikaans examples in this spec are **notes for the catalog**, not a second product. Do not machine-translate the whole bot in a phase PR unless that PR’s job is i18n.
4. `wine_chat` / parser stay meaning-based (already any language). Do not add a keyword list for `/club` or `/list`.
5. Every interpolated name goes through `esc()` when `parse_mode=HTML`.
6. Dates stay as today’s invite formatter (`Works until 19 Oct`) so we don’t invent a new date library.

Catalog shape (bot, not Django gettext — the scanner service has no Django i18n):

```python
# scanner-service/integrations/copy.py  (new, shared)
MESSAGES = {
    "en": {"list.added": "On your To-drink list."},
    "af": {"list.added": "Op jou To-drink-lys."},
}
def t(locale: str, key: str, **kwargs) -> str: ...
```

Fallback: missing key → English. Missing locale → English. Tests lock English; Afrikaans is a fixture that every new key must have (CI fails on missing `af` for keys added in that PR).

## Feature flags

No flag library today. Use `ServiceConfig` (already fetched at scanner bootstrap and readable by Django):

| Key | Default | Turns on |
| --- | --- | --- |
| `feature_lists` | `off` until phase 1 rollout | To-drink API + `/list` + label button |
| `feature_clubs` | `off` until phase 2 rollout | `/club`, feed hooks, peek |
| `feature_club_comments` | `off` | Phase 3 comments |
| `feature_tasteoffs` | `off` | Phase 3 taste-offs |
| `feature_global_clubs` | `off` | Phase 4 |

Behaviour when off: endpoints 404 with `{"error": "not_enabled"}`; bot commands reply with a one-liner and do not register extra menu entries. Stock/drank paths must **no-op** feed publish when `feature_clubs` is off (no exception). Flags are global (11 users, one instance). Household-level flags are unnecessary.

Scanner must read these keys on config bootstrap **and** on a cheap periodic refresh (or `/status` can say Lists/Clubs: off). Do not require a full bot restart to flip a flag if the API already no-ops — but the `/` menu needs `set_bot_presence` / `push_command_menus` after turning a command on.

## Cross-cutting implementation notes

- Django first (models, API, tests `--ds=wine_cellar.conf.test`), bot second (`WineCellarAPI` methods + a module beside `telegram_invites.py`).
- Extract `send_telegram_message(chat_id, html, reply_markup=None)` from `tasks.py` before a third copy of `requests.post`.
- Celery for any fan-out. Never Telegram I/O inside gunicorn (`refresh_wine_references` already taught this).
- `transaction.on_commit` before `.delay(...)`.
- Prod gunicorn: `--workers 3 --timeout 120` — still don’t block it.
- Keep `CLAUDE.md` in sync when a phase ships.
- Presence: `set_bot_presence` and `telegram_commands.PUBLIC_COMMANDS` in the same PR as a new slash command.
- Callback prefixes: household cards `a:`; invites `inv:`; lists `a:td` / `a:lm`; clubs `cl:`. Stay inside 64 bytes; numeric ids only.

## Success, instance-wide

With ~11 people, vanity DAU is noisy. Each phase names a **leading indicator** we can query from Postgres without a warehouse. Staff `/stats` (already exists) can grow a line; do not build a metrics product.

Shared counters we already have: `TelegramUser.last_seen_at`, `HouseholdInvite` rows, `StorageItem` drank rows.

## Permissions matrix (normative)

Applies to every phase. A phase PR that violates a cell is a bug, not a product debate.

| Who | Cellar stock | Personal To-drink | Club feed | Club peek | Club badges | Club list | Taste-off |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Same household member | full | full | — | — | household `/badges` | — | — |
| Club member, visibility `none` | never | never | yes (unmuted) | 404 | 404 | names only (phase 3) | yes |
| Club member, `summary`, accepted | never | never | yes | counts only | if club+hh flags | names only | yes |
| Club member, `full`, accepted | never | never | yes | names/vintages, no prices | if flags | names only | yes |
| Club member, unacked after widen | never | never | yes | **previous** level | previous | names only | yes |
| Household partner, not in club | full (their cellar) | full (household list) | no | no | no | no | no |
| Stranger / other household | never | never | never | never | never | never | never |
| Staff admin | Django admin | Django admin | Django admin | Django admin | Django admin | Django admin | Django admin |
| Global club member | never (forced `none`) | never | yes | 404 always | 404 (recommended) | n/a | n/a |

**Always forbidden on peek JSON:** `price`, `items[].price`, `market_price`, storage name, `row`, `comment`, `drink_by`, barcode, `rating`, `critic_score`, `vivino_rating`. Write a walking test.

Club APIs are keyed by the caller’s `User`, never `X-Household` as the club id. List APIs **are** household-scoped (`ScannerAPIView` + `X-Household`).

## File map (where code goes)

| Piece | Path |
| --- | --- |
| Message catalog | `scanner-service/integrations/copy.py` (new) |
| List bot | `scanner-service/integrations/telegram_lists.py` |
| Club bot | `scanner-service/integrations/telegram_clubs.py` |
| Invite `/start` dispatcher | `telegram_invites.py` (household then club) |
| API client | `scanner-service/integrations/wine_cellar_api.py` |
| Club Django app | `wine_cellar/apps/club/` (`models`, `admin`, `tasks`, `apps.py`) |
| Club API | `wine_cellar/apps/api/club_views.py` + urls under `/api/clubs/` |
| List models | `wine_cellar/apps/wine/models.py` + migration `0030_winelist.py` |
| List API | `wine_cellar/apps/api/list_views.py` + urls under `/api/lists/` |
| Stock hooks | `wine_cellar/apps/api/views.py` (`on_commit` after drank/vote/undo) |
| Telegram send helper | extract from `wine_cellar/apps/wine/tasks.py` |
| Flags | `ServiceConfig` keys; read in mixins + `server.py` bootstrap |
| Presence | `scanner/management/commands/set_bot_presence.py` + `telegram_commands.py` |
| Copy catalog for this spec | [copy-catalog.md](copy-catalog.md) |

Do **not** grow `telegram_bot.py` by another thousand lines. Wire handlers the way invites already do.

## Celery tasks (inventory)

| Task | Phase | Trigger |
| --- | --- | --- |
| `publish_club_event` | 2 | `on_commit` after drank / vote / undo / bulk remove |
| `deliver_club_event` | 2 | from publish, per distinct `chat_id` |
| `club_share_notice` | 2 | first sharing join for a household |
| `club_widen_notice` | 2 | owner PATCH that widens |
| `close_stale_tasteoffs` | 3 | beat, daily |
| `global_club_nudge` | 4 | **skip v1**; staff `/club post` only |

Never Telegram I/O in gunicorn. Retry Telegram 429 once with backoff; store `error` on `ClubEventDelivery`; do not retry forever.

## Natural language chat

Do **not** route “what did Riley open?” or “add this to my list” through `ChatRouter` in these phases. Parser + `cellar_facts` are household-scoped. `/club` and `/list` are the interface so we don’t invent commands in `wine_chat`. A later PR can add `club` / `list` intents; it is not in 0–4.

## Risks

| Risk | Why it’s real here | Mitigation |
| --- | --- | --- |
| Notification fatigue | Drink-by + empty-cellar + badge GIFs already DM people | One line per open, no badge spam, mute one tap, no peek pings. Digest only after 15 members (open-q #7) |
| Privacy leak | Reusing `WineInStockView` would ship prices, drawers, comments | Dedicated peek serializer; forbidden-key tests; join card must match serializer |
| Widening after join | Owner turns summary → full; existing members never tapped that | `sharing_version` + Stay/Leave; unacked stay at previous level |
| Partner / shared cellar | Two memberships, one stock. Partner may not be in the club | Joiner’s card names the household. Others: nameless heads-up, no veto, once |
| To-drink becomes fake stock | Fastest path is a Wishlist `Storage` | Separate models. Tests that list items never appear in `WineInStockView` |
| Club as household | Fastest path is `kind=club` on `HouseholdInvite` | New app; review gate |
| Invite 500 / expiry | Stated current pain. Clubs that reuse a broken `/start` don’t grow | Phase 0 first |
| Gunicorn / I/O | Reference lookup already taught this | `on_commit` + Celery |
| Callback budget | 64 bytes | `cl:` + numeric ids, no names |
| Allow list | Club-only users don’t exist | Compound redeem + `_hot_add` |
| Undo vs feed | Restore window is 10 min from `removed_at` | Retract opened event; no event for bulk **move** |
| Command menu noise | `/help` is already long | `/club` and `/list` in the menu. Subcommands stay typed |
| Web distraction | Building a club site nobody opens | Admin + optional read-only page. Telegram first |

## Effort rollup

Not calendar time — how much of the system moves. Each phase doc splits this into **PR-sized** tasks.

| Phase | Surface | Relative | PRs |
| --- | --- | --- | --- |
| 0 Invite hardening | `scanner/views.py`, tests, TTL + bot copy | S | 0a, 0b, optional 0c |
| 1 Lists (To-drink) | 2 models in `wine`, ~5 endpoints, label-card button, `/list` | M | 1a–1e |
| 2 Clubs + feed + peek | New app, policy, Celery, bot module, onboarding, stock hooks | L | 2a–2j |
| 3 Comments, taste-offs, club lists | 3 models + scoring; list hook is small if 1 shipped | M–L | 3a–3f |
| 4 Global clubs | Kind + staff commands + freeze/kick | S–M | 4a–4c |

Phase 1 is the cheap solo-user habit. Phase 2 is the network product; join-time sharing means peek ships with it. Phase 0 is the gate for anyone new. Club-tied lists wait until both 1 and 2 exist.

## What we do not change

- `Wine`, `Storage`, `StorageItem` stay household-scoped. No `club` FK on stock. Lists do not fake stock.
- `CellarAchievement` stays household-scoped. Club display **reads** it when the **club** has `badges_visible` and the household has `gamification_enabled`.
- `TasteIntent` / `HouseholdTasteSnapshot` stay cellar taste for chat recommendations, not a wishlist.
- `/invite` stays cellar. `/club invite` is the growth link.
- Bot stays **DM-only**. Do not create a Telegram group per club.
