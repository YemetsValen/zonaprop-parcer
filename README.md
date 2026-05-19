# ZonaProp Telegram Bot

A production-ready FastAPI service that scrapes [ZonaProp.com.ar](https://www.zonaprop.com.ar)
on a schedule and notifies one or more Telegram chats about new property
listings that match your filters.

- **Scraper** — `httpx` + BeautifulSoup4, with an optional Playwright (headless
  Chromium) fallback when ZonaProp serves a JS challenge. Reads the
  `__NEXT_DATA__` JSON the Next.js front-end embeds in every page.
- **Scheduler** — APScheduler `AsyncIOScheduler` (default every 15 min,
  `max_instances=1`).
- **Storage** — SQLite via SQLAlchemy 2.0 async. One row per already-notified
  listing → no duplicate pings on the next tick.
- **Bot** — `python-telegram-bot` v20+, formatted HTML messages with a media
  group of up to 3 photos.
- **API** — FastAPI: `/health`, `/api/listings`, `/api/check`, `/api/filters`,
  `/api/export`. OpenAPI docs at `/docs`.
- **Watchdog** — sends a Telegram alert if no successful scrape has happened
  in the last N minutes.
- **Ops** — Docker / docker-compose, healthcheck, graceful shutdown, all
  configuration in `.env`.

## Quick start (5 steps)

```bash
# 1. Clone
git clone https://github.com/YemetsValen/zonaprop-parcer.git
cd zonaprop-parcer

# 2. Configure
cp .env.example .env
$EDITOR .env          # set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, filters

# 3. Run
docker compose up -d --build

# 4. Verify
curl -fsS http://localhost:8000/health | jq

# 5. Trigger a scrape immediately (optional)
curl -X POST http://localhost:8000/api/check \
     -H "Authorization: Bearer $(grep ^API_KEY .env | cut -d= -f2)"
```

The first scheduled tick fires after `CHECK_INTERVAL_MINUTES` minutes; until
then `/health` reports `total_checks: 0`. Use the manual `POST /api/check`
above to force an immediate run.

## Local dev (without Docker)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env  # edit as above
uvicorn app.main:app --reload --port 8000
```

Run tests and lints:

```bash
ruff check .
pytest
```

## Configuration

All configuration lives in `.env`. See [`.env.example`](.env.example) for the
full schema. Highlights:

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Bot token from `@BotFather`. |
| `TELEGRAM_CHAT_ID` | — | Single id or comma-separated list. |
| `OPERATION_TYPE` | `alquiler` | `alquiler` (rent) or `venta` (sale). |
| `PROPERTY_TYPES` | `departamentos` | CSV: `departamentos`, `ph`, `casas`. |
| `NEIGHBORHOODS` | _empty_ | Either a single city slug (`capital-federal`, `gran-buenos-aires`) or CSV of barrios (`palermo,belgrano`). |
| `PRICE_MIN` / `PRICE_MAX` | `0` / `0` | Inclusive band. `0` disables. |
| `CURRENCY` | `ARS` | `ARS` (pesos) or `USD` (dólares). |
| `ROOMS_MIN` / `ROOMS_MAX` | `0` / `0` | Bedroom count. |
| `AREA_MIN` | `0` | Minimum square metres. |
| `PUBLISHED_WITHIN_DAYS` | `0` | Only listings posted in the last N days. `0` disables. |
| `DAILY_CHECK_TIME` | _empty_ | `HH:MM` (24h). When set, scrape once per day at this time in `SCHEDULE_TIMEZONE`. Overrides `CHECK_INTERVAL_MINUTES`. |
| `SCHEDULE_TIMEZONE` | `America/Argentina/Buenos_Aires` | IANA TZ for `DAILY_CHECK_TIME`. |
| `CHECK_INTERVAL_MINUTES` | `15` | Scrape every N minutes. Ignored when `DAILY_CHECK_TIME` is set. |
| `WATCHDOG_TIMEOUT_MINUTES` | `30` | Alert if no successful scrape for N minutes. `0` disables. |
| `API_KEY` | — | Bearer token for `POST /api/check` and `PUT /api/filters`. |
| `USE_PLAYWRIGHT` | `false` | Enable headless Chromium fallback. |
| `REQUEST_TIMEOUT` | `30` | HTTP timeout in seconds. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/zonaprop.db` | Any SQLAlchemy async URL. |

The data directory is mounted as a Docker volume at `/app/data`, so SQLite
state survives `docker compose down`.

## API

All endpoints are documented at `http://localhost:8000/docs` (Swagger UI)
and `http://localhost:8000/redoc`.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `GET`  | `/health` | — | Liveness, last-tick info, listings count. |
| `GET`  | `/api/listings?limit=50&offset=0` | — | Paginated seen-listings. |
| `POST` | `/api/check` | Bearer `API_KEY` | Run one scrape now. |
| `GET`  | `/api/filters` | — | Active filters + built search URL. |
| `PUT`  | `/api/filters` | Bearer `API_KEY` | Patch filters in-memory. |
| `GET`  | `/api/export` | — | CSV dump of `seen_listings`. |

## Architecture

```
[APScheduler tick]
       ↓
[ZonaPropScraper] ─httpx→ zonaprop.com.ar
       │                   (Playwright fallback if blocked)
       ↓
[__NEXT_DATA__ JSON → Listing models]
       ↓
[Server-side filter recheck]
       ↓
[Diff vs seen_listings (SQLite)]
       ↓
[TelegramNotifier] → fan-out to every TELEGRAM_CHAT_ID
       │
       ↓
[seen_listings ← UPSERT (id, url, scraped_at, notified_at)]
```

A separate watchdog job runs once a minute; if `last_success` is older than
`WATCHDOG_TIMEOUT_MINUTES`, it posts a one-shot alert to Telegram.

## Production notes

- **Blocking.** Run with `USE_PLAYWRIGHT=true` and rebuild
  (`docker compose build --build-arg USE_PLAYWRIGHT=true`) if you start to
  see "block / challenge" warnings in the logs.
- **Multiple chats.** Set `TELEGRAM_CHAT_ID=123,456,-100789` to fan out to
  multiple users / channels. Per-chat failures are isolated.
- **Graceful shutdown.** `docker compose down` (or SIGTERM in general) waits
  for the current scrape to finish before stopping. The scheduler's
  `max_instances=1` guarantees no overlap.
- **Secrets.** Only `.env.example` is in git; the real `.env` is gitignored.

## License

MIT — see `LICENSE`.
