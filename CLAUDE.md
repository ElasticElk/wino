# Wine Fridge Scanner System

## Overview

Multi-service wine cellar management system with barcode scanning, AI label recognition, and Telegram bot integration. Two main services: **wine-cellar** (Django web app) and **scanner-service** (FastAPI barcode bridge).

## Architecture

```
wine-cellar/          # Django 5.2 backend + React 19 frontend
scanner-service/      # FastAPI 0.115 barcode scanner bridge
start.sh / stop.sh    # Service orchestration
```

**Stack:** Django 5.2 + DRF 3.16 | React 19 + Webpack 5 | PostgreSQL 16 | Redis + Celery 5.6 | FastAPI | Caddy (prod proxy) | Docker Compose

## Wine Cellar Service (`wine-cellar/`)

### Django Apps
- `wine_cellar.apps.wine` — Wine models, forms, views, celery tasks
- `wine_cellar.apps.storage` — Storage/fridge management (StorageItem tracking)
- `wine_cellar.apps.user` — User settings, authentication
- `wine_cellar.apps.scanner` — Household, scanner device registration, Telegram users
- `wine_cellar.apps.api` — REST API for scanner service integration

### Key Models
- `Wine` — Core entity (name, barcode, vintage, wine_type, category, country, abv, etc.) scoped to household
- `Storage` — Physical storage locations with row/column grid
- `StorageItem` — Individual bottle placement (soft-delete via `deleted` flag)
- `Household` / `HouseholdMembership` — Multi-tenant system (owner/member roles)
- `Scanner` — Registered barcode devices with API keys + notify_users M2M
- `TelegramUser` — Links Django users to Telegram chat IDs
- `ServiceConfig` — Key-value config store (bot_token, grok_api_key, web_url, `feature_lists` / `feature_clubs` / …)
- `WineList` / `WineListItem` — Personal To-drink list (**user-scoped**, not household). Club lists come in a later phase.

### REST API (`/api/`)
Auth: `Authorization: Token <token>` header. Optional `X-Household: <id>` header.

- `GET /api/wines/by-barcode/<ean>/` — Barcode lookup
- `GET /api/wines/in-stock/` — Stock list (filter by `?storage=Name`). Each wine also carries `vintage`, `region`, `grapes`, `comment`, purchase `price`, `drink_by`, `rating`, `critic_score`, `vivino_rating`, `attributes`, `food_pairings`, drunk-only `votes`, and `items[].price`. `name` still includes the vintage and `grape` is still the first grape.
- `GET /api/wines/search/?q=<query>` — Search wines
- `GET /api/wines/all/` — List all wines
- `POST /api/wines/` — Create wine
- `PATCH /api/wines/<pk>/` — Update wine
- `POST /api/wines/<pk>/images/` — Upload image
- `POST /api/stock/add/` — Add stock (wine_id, storage_name, price, row)
- `POST /api/stock/remove/` — Remove stock. Optional `item_id` targets that bottle (fridge `-` still uses wine_id)
- `POST /api/stock/bulk/` — Catch-up apply: `{ops: [{op: remove|move, item_id, expected_storage, expected_row, storage_name, row}]}` in one transaction. Moves first, then removes. `409 stale_item` / `duplicate_item` / `storage_not_found`. Returns `{batch_id, ops}`
- `POST /api/stock/bulk/<uuid>/undo/` — Reverse that batch (restore removes and clear their vote, PATCH moves back). 10-minute window, same as a single-bottle undo — does not stretch `RESTORE_WINDOW`. A bottle moved again since is `409 stale_item`
- `POST /api/stock/set/` — Set exact quantity
- `GET /api/stock/history/` — Recently drank/removed bottles (`?limit=5&offset=0`)
- `PATCH /api/stock/<pk>/move/` — Move bottle
- `GET /api/wines/<pk>/stock/` — Stock details for a wine
- `POST /api/wines/<pk>/refresh-references/` — **Queues** a Grok reference lookup, returns `202 {task_id}`
- `GET /api/wines/<pk>/refresh-references/?task_id=<id>` — Poll that task (`PENDING`/`SUCCESS`/`FAILURE`); without `task_id`, returns the wine's stored reference values
- `POST /api/references/lookup/` — **Queues** a Grok reference lookup for a wine that has *no* row in the cellar (body: `name` + optional `producer`, `vintage`, `country`, `region`, `appellation`, `grape`, `vineyard`), returns `202 {task_id}`. Nothing is persisted
- `GET /api/references/lookup/?task_id=<id>` — Poll that task (`task_id` is required — there is no stored wine to fall back on)
- `GET /api/images/<pk>/file/` — Serve wine image file
- `GET /api/storages/` — List storages
- `GET /api/scanner/config/` — Scanner bootstrap config (admin-only, returns telegram_users, scanners, service config)
- `GET /api/household/` — Current household + members
- `GET /api/households/` — All user's households
- `POST /api/household/add-member/` — Add member to household
- `POST /api/household/remove-member/` — Remove member from household
- `POST /api/scanner/invites/` — Mint a one-time invite (`kind=join` any member, `kind=new` staff). 30-day expiry. Staff join requires `household_id`; the bot sends it even when there is only one cellar
- `GET /api/scanner/invites/<code>/` — Preview a code without burning it. **Staff only** — the bot calls it with its admin client (`handler.api`)
- `POST /api/scanner/invites/redeem/` — Provision from a code (join=member, new=owner + storage named Cellar). Returns the token of the user for the posted `chat_id`, so **staff only**. A taken name is `409` with `error` "That name's taken — pick another" and a free `suggested_name` (never the taken one; names are global, so the taken name is not echoed)
- `POST /api/scanner/invites/cancel/` — Drop the caller's newest **unexpired** unused invite. Optional `household_id` prefers that cellar, then a staff `new` invite. Returns `code`, `kind`, `household_name`
- `POST /api/scanner/taste/` — Record a taste intent (`look`, `winelist`, or `ask`). `ask` requires a JSON object `payload` and does not rebuild the household snapshot. The response includes `intent_id`
- `PATCH /api/scanner/taste/<id>/` — Merge `outcome`, `shown`, `rejected`, `place`, `model`, `latency_ms` and `fallback` into an `ask` intent. Another household, or a `look` / `winelist` row, is `404`
- `GET /api/scanner/chat-stats/?days=30` — **Staff only.** How chat recommendations landed: taps, whether the suggested bottle was removed from stock within 48h and its vote, model mix, latency, and the five worst asks (`wine/chat_stats.py`)
- `GET /api/lists/` — Caller's personal To-drink lists (creates the default list). **User-keyed**, not `X-Household`. Flag `feature_lists`; off → 404 `not_enabled`
- `GET /api/lists/<id>/` — Open items (`?limit=5&offset=0`). Another user's list is 404
- `POST /api/lists/<id>/items/` — Snapshot a bottle onto the list. Duplicate `wine_id` is 200 (idempotent)
- `DELETE /api/lists/<id>/items/<item_id>/` — Remove. 409 `already_moved`
- `POST /api/lists/<id>/items/<item_id>/to-cellar/` — Create/find `Wine` in the **current** household and add stock. Sets `moved_at`. Undo of that stock add does not restore the list row

### Frontend
- **Templates:** `wine_cellar/templates/` — Django templates with `base.html` layout
- **React components:** `wine_cellar/react/` — Barcode scanner (`react_bar_code.tsx`), maps (`WineMaps.jsx`, Leaflet + MapLibre)
- **JS/TS utilities:** `wine_cellar/assets/js/` — Tom Select dropdowns, image preview, stock forms, carousel
- **Build:** Webpack 5 with Babel, ESLint, SASS

### Web Routes
- `/` — Dashboard | `/wines/` — Wine list | `/wine/<pk>` — Detail
- `/wine/scan/` — Barcode scanner UI | `/wine/scan/<code>` — Handle scanned barcode
- `/wines/map` — Interactive wine origin map
- `/wine/add/`, `/wine/edit/<pk>/`, `/wine/delete/<pk>/` — Wine CRUD
- `/storages/`, `/storage/<pk>/`, `/storage/add/`, etc. — Storage CRUD
- `/stock/add/<pk>/`, `/stock/edit/<pk>/`, `/stock/delete/<pk>/` — Stock management
- `/user/settings/` — User preferences
- `/accounts/` — allauth login/signup

## Scanner Service (`scanner-service/`)

### Purpose
Bridges physical barcode scanners to the Wine Cellar API. Runs as FastAPI server (`server.py`) or standalone (`scanner_service.py`).

### Scan Flow
1. Barcode received → lookup in Wine Cellar DB
2. Unknown → Open Food Facts API lookup
3. Optionally → Grok Vision API analyzes label photo
4. Ambiguous matches → Telegram disambiguation UI
5. Result → add/remove stock via Wine Cellar API

### API
- `GET /health` — Health check
- `POST /scan` — `{barcode, mode: "in"|"out", api_key}` → `{status, beep, message, wine_name, stock_count}`

### Integrations
- `integrations/wine_cellar_api.py` — HTTP client for Wine Cellar REST API
- `integrations/telegram_bot.py` — Telegram bot (notifications, photos, disambiguation, household switching)
- `integrations/telegram_lists.py` — `/list` To-drink flow and label-card `td` button
- `integrations/copy.py` — EN/AF dialogue catalog (menus stay English)
- `integrations/telegram_catchup.py` — `/fridge` Catch up drawer walk (staged Drunk/Moved, bulk apply)
- `integrations/grok_vision.py` — xAI Grok Vision for label analysis
- `integrations/open_food_facts.py` — Barcode → wine data lookup
- `hardware/barcode_reader.py` — Scanner abstraction (mock/real)
- `hardware/gpio_controls.py` — Raspberry Pi GPIO (buzzer, LEDs)

### Config
`config.yaml` for bootstrap; most config fetched from Wine Cellar DB at runtime via `/api/scanner/config/`.

## Docker Setup

### Development (`wine-cellar/docker-compose.yml`)
Services: `web` (Django dev server :8000), `db` (PostgreSQL :5434), `redis`, `celery`, `celery-beat`

### Production (`wine-cellar/docker-compose.prod.yml`)
Services: `web` (Gunicorn :8000), `db` (PostgreSQL), `redis`, `caddy` (proxy :8085), `celery`, `celery-beat`
Images from: `ghcr.io/the-broke-sommeliers/wine-cellar-web:latest`

### Scanner (`scanner-service/docker-compose.yml`)
Service: `scanner-server` (FastAPI :8080). Connects to wine-cellar network for internal API access.

## Inter-Service Communication
Scanner → Wine Cellar via REST API over shared Docker network (`http://web:8000` dev, `wine-cellar-web:8000` prod). Token auth from config.yaml.

## Background Work (Celery)

`refresh_wine_references` looks up critic score, Vivino rating and market price via Grok's live web search. It runs **as a Celery task, never inline**: the search budgets 120s per attempt and retries once, while gunicorn's default timeout is 30s with a single sync worker — so a synchronous call both died and blocked every other request (bot traffic included) until it did. Prod gunicorn is now pinned to `--workers 3 --timeout 120`.

Callers POST to queue, then poll the same URL with `?task_id=`. The Telegram client (`WineCellarAPI.refresh_references`) does this polling on its own background thread; the web detail page does it in JS.

`drink_by_reminder` (beat, 02:30 daily) is household-scoped, like everything else.

**The dev celery workers bind-mount the source** (`.:/usr/src/app`), same as `web`. Without that they ran whatever was baked into the image, so task changes silently never took effect — a `restart` could not pick them up, only a rebuild. If a task appears unregistered (`Received unregistered task of type ...`), check that mount first.

## Auth
- Web: Django built-in + django-allauth (OAuth/OIDC)
- API: `rest_framework.authtoken` (Token header)
- Multi-tenant: Household membership with owner/member roles

## Key Config Files
- `wine-cellar/wine_cellar/conf/settings.py` — Base Django settings
- `wine-cellar/wine_cellar/conf/docker_settings.py` — Docker/prod overrides (env vars)
- `wine-cellar/wine_cellar/conf/celery.py` — Celery + beat schedule (drink_by_reminder at 2:30 AM daily)
- `scanner-service/config.yaml` — Scanner bootstrap config
- `wine-cellar/pyproject.toml` — Project metadata, deps, test config (AGPL-3.0 license)

## Adding a New User / Household

When adding a new user who needs Telegram bot access, **all** of these must be in place:

1. **Django User** — `User.objects.create()` with a password
2. **DRF Auth Token** — `Token.objects.get_or_create(user=user)` — **CRITICAL: without this, the scanner service silently skips the user and their chat_id won't be in the Telegram bot's allow list**
3. **HouseholdMembership** — link user to household with role (`owner`/`member`)
4. **TelegramUser** — link user to their Telegram `chat_id` + `display_name`
5. **Scanner** — household needs a Scanner with `notify_users` M2M pointing to relevant TelegramUsers
6. **Storage** locations — create any needed Storage objects for the household
7. **Restart scanner service** — `cd scanner-service && docker compose restart` — config is loaded from DB at startup only

### Telegram Bot Allow List

The bot uses `filters.Chat(chat_id=[...])` built from users returned by `/api/scanner/config/`. That endpoint only returns TelegramUsers who have a valid DRF token.

**Unfiltered** (work for anyone): `/myid`, `/start`, and the catch-all fallbacks — see *Onboarding* below. Everything else (`/status`, `/stock`, `/fridge`, `/search`, `/help`, …) requires the chat_id to be in the allow list. The unfiltered fallbacks are registered **last** so provisioned users always match their own handlers first (python-telegram-bot stops at the first match within a handler group).

## Telegram Bot Commands

Available to authorized users via the bot:
- `/search <name>` — Find a wine by name. Plain text of 3+ characters goes through natural-language chat when a Grok key is configured, and falls through to this search when chat does not claim it (no key, a one-word wine name, or the parser says it is a search)
- `/status` — Scanner system status
- `/stock` — View current stock (interactive, per-storage)
- `/fridge` — Browse fridge contents. **Catch up** walks one drawer: tap Drunk / Moved (1 or both), then Apply. Empty here means those bottles moved, not drunk. One Undo. Not a new slash command. Buttons carry `wine_id` / `item_id`, never a list position (the list shrinks per tap, so an index would hit another wine). Bottles with no drawer (fridge `+` adds them so) get a *No drawer* bucket. A move may be *Not sure which drawer* (row null). Undo carries its batch id, so it still works after a new Catch up. Stale Apply keeps the changes that are still valid and drops the rest.
- `/recent` — Recent bottles (notes + label photo, pages of 5)
- `/list` — Personal To-drink list (bottles you don't own yet). **User-scoped** — a household partner does not see it. Gated on `ServiceConfig.feature_lists`. Move to cellar writes stock on the current household. Not created on new-cellar redeem; first `/list` or first add creates the default list.
- `/help` — List available commands
- `/pending` — Show barcodes awaiting label photos
- `/flush` — Clear pending barcodes
- `/switch` — Switch active household
- `/household` — Show current household + members
- `/invite` — Mint a one-time join link for the current cellar (any member), with a *Share invite* button (`t.me/share/url`). `/invite cancel` drops the newest unexpired unused invite for this cellar and names it. `/newcellar` (staff only; same as `/invite new`, old alias `/new`) mints a new-cellar link
- `/adduser <chat_id> <name> [household]` — Add user to household (legacy fallback)
- `/removeuser <chat_id> [household]` — Remove user from household

### Photo modes

Triage (`GrokVisionAPI.triage_photo`) classifies every photo as `barcode`, `label`, `winelist` or `other`, so there is no command to discover — sending a photo is the whole interface.

- **barcode / label** → the add-to-cellar flow. A label with no match offers *Add to cellar* or *Just looking*, so a shop lookup costs nothing. *Just looking* **keeps the card** — it only drops the buttons; the reading is the whole point of having asked.
- A label card fires a background `POST /api/references/lookup/` and edits the critic score, Vivino rating and price into itself when they land. The triage vision call answers from the model's own memory and under the reference rubric nearly always returns nulls, so the numbers only exist after a live web search — far too long to hold the user on a spinner. A wine already in the cellar shows its **stored** references instead, for free.
- **winelist** → `analyse_wine_list()` reads the whole list in one vision call and recommends from it, with the user's cellar profile (`build_cellar_summary`) in the prompt. Deliberately **no per-wine web search** — 20 live lookups would take minutes, and this gets used standing in a shop. Accurate pricing stays reserved for wines actually added.
- Wines already owned are matched **locally** (`mark_owned_wines`), not by the model: only a slice of the cellar fits in a prompt, and grape words are excluded from matching so two unrelated Chenins don't collide.
- Read lists are cached per message for `WINELIST_TTL` (30 min) to back the *Show all* / pagination buttons.

Users can also send **photos** to the bot — it will attempt barcode detection (pyzbar) or wine label analysis (Grok Vision).

### Natural language chat

Plain text keeps the slash commands. When `ChatModels` is wired (a Grok key at scanner startup), `_unknown_text` asks `ChatRouter` before it searches. With no key, that branch is today's search, so tests and a keyless bot are unchanged.

`grok-4.20-0309-non-reasoning` parses the sentence (about 4s). Shelf questions — a drawer, a grape, a country, "list everything" — are answered in code from the in-stock rows (`cellar_facts.py`). Shiraz matches Syrah, and a blend is named as a blend. A grape the cellar does not have says so and offers a button; it does not recommend on its own.

A recommendation is one `grok-4.20-0309-non-reasoning` call over the whole cellar (capped at 300 bottles), about 1.5s. **Not a reasoning model**: on 2026-10-21 every xAI reasoning model took 16–32s on a 31-wine cellar (3,500 thinking tokens) and timed out in production, while the non-reasoning model scored the same on the eval. Re-run the eval before switching back. The placeholder says "Looking through your cellar…", then edits itself: a matching line, "down to a few", then the pick. The message is never rewritten into a different bottle. A tie names both bottles in that one message. The reasoning call can ask one question; the option tap recommends without asking again. Each recommendation is a `TasteIntent` of kind `ask`. Buttons are `cf` `cc` `cq` `ck` `cn` `co` `cx` `cr` `cw`. Each ask records its `model` (`quick` for the code fallback) and `latency_ms`; staff see the rollup with `/chatstats [days]`.

*That's the one* leaves a *Take it out now* button (one per fridge holding it, `co`), which removes the bottle through the normal stock-out path with Undo and the thumbs vote, so it never needs scanning out. That removal is also what `/chatstats` counts as the pick being drunk.

Wine questions that aren't about picking from the cellar (ordering at a restaurant, a shop, a region, serving, decanting) are `wine_chat`: one short non-reasoning answer using the taste profile, nothing recorded. How to use Whiney itself (add a person, start a cellar, "does my wife need Telegram?", how photos work) is `help`: one short non-reasoning answer from a fixed fact sheet of how the bot actually works, in the language they asked in. It must not invent commands. If the sheet does not cover it, it says so and points at `/help` or a screenshot to whoever set Whiney up. If that call fails, a canned line for the known topics (`/invite`, `/newcellar` for staff, `/switch`) is the fallback. Anything not about wine, drink, food or Whiney is `offtopic` and gets a one-line decline. There is no keyword list; the parser decides. An `unsure` full sentence goes to `wine_chat`, which declines on its own if it isn't wine. The parser classifies by meaning in any language, and `wine_chat` replies in the language it was asked in.

Finding somewhere to go (a restaurant, wine bar, farm or shop with good wine) is `place`. A named area searches straight away. "Near me" replies with a one-tap *Send my location* button (or the user types a suburb). The pending request is held under `place:<chat_id>`, apart from the open ask, and any other message or command drops it. `find_places` is the only live web search in chat: `grok-4.3` on `POST /v1/responses` with the `web_search` tool (`/chat/completions` rejects that tool with a 422), about 10s behind a progress message, up to 3 places with links. The shared location is used for that one search and is not stored.

A price or a correction patches through `update_wine`. A price naming a wine goes to that wine. Otherwise the bottle is the open suggestion, else the newest unsaved label card (the card's merged reading, so Add still creates the right wine), else the wine named in the sentence. Market price is never written.

A GPS pin is stored on the open ask only when a location arrives while that ask has no place yet. Nothing asks for a location, and a later live-location update is ignored.

If the parser or the cellar API is down, a short phrase still searches and a full sentence says chat is resting. A recommendation that cannot reach the model, times out, or returns junk twice gets a code quick pick with a *Try properly* button, so a sentence never ends without a bottle. Three failures open a two-minute circuit. An auth failure alerts staff once an hour; the user never sees the words key or credit. `/status` includes `Chat: ready` or `Chat: resting`.

Label vision stays on `grok-4.3`. A typed correction after a misread (`identify_wine_from_hint`) is the same live web search as places: `POST /v1/responses` with `web_search`. `/chat/completions` rejects that tool with a 422, so the old path never searched; it now falls back to `/chat/completions` without the tool only if the search call itself fails. The live eval is `scanner-service/tests/eval/run.py` (needs `XAI_API_KEY`; pytest does not collect it).

### Onboarding (unprovisioned users)

`/start` and a catch-all fallback are **unfiltered**. A stranger with no code is pointed at asking a friend for an invite (chat ID is still on `/myid`). `/start <code>` is the invite onboarding: join is one tap, new cellar is a name step (type / suburb pin / Skip) then one photo pitch. Codes are burned only after provision succeeds. The user-facing name is **Whiney** (`/start`, `/help`, invite copy).

Invite state (`InviteFlow`, `telegram_invites.py`) is in memory with a **30-minute sliding TTL**. Buttons carry the code (`inv:<action>:<code>[:<name>]`), so Join and the naming buttons survive a restart by re-previewing; only a half-typed name is lost. Network/5xx failures say "couldn't reach the cellar", never "expired".

Strangers are provisioned as soon as they type a name. **Someone who already has a cellar** must confirm a typed name via a button (they're usually searching), any command drops them out of naming (group `-1` handler), and in the *Stay / Start a new cellar* step text and photos go to their existing cellar — none of these ever create a second cellar silently.

A duplicate name offers the API's `suggested_name` as a button whose label is exactly what it commits. User names inside HTML messages go through `esc()`.

`/adduser` now messages the new member directly with a welcome. **A bot cannot open a conversation with a user** — the person must have messaged the bot at least once first. When delivery fails, `/adduser` tells the adder so, rather than reporting a false success.

Empty states (`/stock`, `/fridge`, `/storages` with no wines) all render one shared card inviting a label photo, via `empty_cellar()` in `telegram_bot.py`.

### Bot presence (description, command menu, icon)

The bot's description, short description and `/` command menu live on Telegram's
servers, not in this repo. `python manage.py set_bot_presence` pushes them from
`scanner/management/commands/set_bot_presence.py`, which is the only record of
what they say — edit there, then run it (`--dry-run` to preview).

The **description** is the text shown on the empty chat screen *before* someone
taps Start, so it's the first thing an unprovisioned person ever reads.

The **profile picture has no Bot API method** — it can only be set via @BotFather
with `/setuserpic`.

## Scanner Service Config Bootstrap

At startup (`server.py`):
1. Reads `config.yaml` for `base_url` and `admin_token`
2. Fetches `/api/scanner/config/` from Wine Cellar DB (telegram_users, scanners, service config)
3. Builds per-user API clients keyed by `chat_id → WineCellarAPI(token, household_id)`
4. Builds `filters.Chat` allow list from all chat_ids
5. If no DB config available, falls back to `config.yaml` values

**Config changes require a scanner service restart** — there is no hot-reload of DB config (except for `/adduser` and `/removeuser` bot commands which hot-add/remove chat_ids at runtime).

## Commands

```bash
# Start all services
./start.sh

# Stop all services
./stop.sh

# Dev (wine-cellar)
cd wine-cellar && docker compose up

# Prod (wine-cellar)
cd wine-cellar && docker compose -f docker-compose.prod.yml up -d

# Scanner service
cd scanner-service && docker compose up

# Restart scanner service (after DB config changes)
cd scanner-service && docker compose restart

# Restart celery workers (after model/task changes)
cd wine-cellar && docker compose restart celery celery-beat

# Run tests (wine-cellar) — the container's env var overrides pyproject, so pass --ds
cd wine-cellar && docker compose exec web python -m pytest --ds=wine_cellar.conf.test

# Run tests (scanner-service)
cd scanner-service && docker run --rm -v "$PWD":/app -w /app wine-scanner-scanner-server \
  sh -c "pip install -q pytest pytest-asyncio; python -m pytest -q"

# Django management (inside container)
cd wine-cellar && docker compose exec web python manage.py migrate
cd wine-cellar && docker compose exec web python manage.py createsuperuser
cd wine-cellar && docker compose exec web python manage.py shell
```
