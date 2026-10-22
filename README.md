# Whiney

A self-hosted wine cellar: Django web app, barcode scanner bridge, and a Telegram bot (Whiney) that reads labels, tracks stock, and answers “what should we open?”.

This is a household fork of [the-broke-sommeliers/wine-cellar](https://github.com/the-broke-sommeliers/wine-cellar), plus a scanner service and bot that are original to this repo.

```
wine-cellar/       Django 5.2 + React 19 (AGPL-3.0 upstream)
scanner-service/   FastAPI barcode bridge + Telegram bot
start.sh / stop.sh Service orchestration
```

**Stack:** Django 5.2 + DRF | React 19 | PostgreSQL 16 | Redis + Celery | FastAPI | Docker Compose

## What it does

- Scan a barcode in or out of a fridge (physical scanner or a photo in Telegram).
- Photograph a label; Grok Vision reads it and can add the wine.
- Photograph a wine list; the bot recommends from it against your cellar.
- Ask in plain language: what’s in a drawer, what to open, where to go.
- Households, invites, and `/fridge` catch-up when bottles have moved.

Config and secrets stay out of git (`scanner-service/config.yaml`, `.env.dev`, `.env.prod`). Copy the `-sample` files.

## Run

```bash
./start.sh
./stop.sh
```

Wine Cellar is at http://localhost:8000 in development. The scanner service is at http://localhost:8080.

See `CLAUDE.md` for the API, bot commands, and how the two services talk.

## Tests

```bash
cd wine-cellar && docker compose exec web python -m pytest --ds=wine_cellar.conf.test
cd scanner-service && docker run --rm -v "$PWD":/app -w /app wine-scanner-scanner-server \
  sh -c "pip install -q pytest pytest-asyncio; python -m pytest -q"
```

## License

[AGPL-3.0](LICENSE). The web app remains a modified version of wine-cellar; the scanner service is licensed the same so the combined system can be shared as one work.
