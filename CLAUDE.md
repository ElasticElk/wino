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
- `ServiceConfig` — Key-value config store (bot_token, grok_api_key, web_url)

### REST API (`/api/`)
Auth: `Authorization: Token <token>` header. Optional `X-Household: <id>` header.

- `GET /api/wines/by-barcode/<ean>/` — Barcode lookup
- `GET /api/wines/in-stock/` — Stock list (filter by `?storage=Name`)
- `GET /api/wines/search/?q=<query>` — Search wines
- `GET /api/wines/all/` — List all wines
- `POST /api/wines/` — Create wine
- `PATCH /api/wines/<pk>/` — Update wine
- `POST /api/wines/<pk>/images/` — Upload image
- `POST /api/stock/add/` — Add stock (wine_id, storage_name, price, row)
- `POST /api/stock/remove/` — Remove stock
- `POST /api/stock/set/` — Set exact quantity
- `PATCH /api/stock/<pk>/move/` — Move bottle
- `GET /api/wines/<pk>/stock/` — Stock details for a wine
- `GET /api/images/<pk>/file/` — Serve wine image file
- `GET /api/storages/` — List storages
- `GET /api/scanner/config/` — Scanner bootstrap config (admin-only, returns telegram_users, scanners, service config)
- `GET /api/household/` — Current household + members
- `GET /api/households/` — All user's households
- `POST /api/household/add-member/` — Add member to household
- `POST /api/household/remove-member/` — Remove member from household

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
- `/search <name>` — Find a wine by name (plain text also works — any message ≥3 chars is treated as a search)
- `/status` — Scanner system status
- `/stock` — View current stock (interactive, per-storage)
- `/fridge` — Browse fridge contents
- `/recent` — Recently added/removed wines
- `/help` — List available commands
- `/pending` — Show barcodes awaiting label photos
- `/flush` — Clear pending barcodes
- `/switch` — Switch active household
- `/household` — Show current household + members
- `/adduser <chat_id> <name> [household]` — Add user to household
- `/removeuser <chat_id> [household]` — Remove user from household

Users can also send **photos** to the bot — it will attempt barcode detection (pyzbar) or wine label analysis (Grok Vision).

### Onboarding (unprovisioned users)

`/start` and a catch-all fallback are **unfiltered** — an unknown chat always gets a reply containing its own chat ID plus instructions to pass it to a household owner. This matters because Telegram's START button sends `/start`, and leaving it unanswered made the bot look broken (it was the likely cause of six of nine households never adding a wine).

`/adduser` now messages the new member directly with a welcome. **A bot cannot open a conversation with a user** — the person must have messaged the bot at least once first. When delivery fails, `/adduser` tells the adder so, rather than reporting a false success.

Empty states (`/stock`, `/fridge`, `/storages` with no wines) all render one shared card inviting a label photo, via `empty_cellar()` in `telegram_bot.py`.

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
