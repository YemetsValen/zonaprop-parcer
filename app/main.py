"""FastAPI entry-point.

Wires the lifespan: init DB → start scheduler → yield → stop scheduler.
The scheduler shutdown waits for jobs already running so SIGTERM doesn't
truncate an in-flight scrape (the spec calls this "graceful shutdown").
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.db.database import init_db, reset_engine
from app.scheduler import build_scheduler


def _configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # APScheduler is chatty at INFO; trim it a notch.
    logging.getLogger("apscheduler.scheduler").setLevel(max(level, logging.WARNING))
    logging.getLogger("apscheduler.executors.default").setLevel(max(level, logging.WARNING))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    log = logging.getLogger("app.main")
    settings = get_settings()
    schedule_desc = (
        f"cron daily @ {settings.daily_check_time} {settings.schedule_timezone}"
        if settings.daily_check_time
        else f"interval={settings.check_interval_minutes}m"
    )
    log.info(
        "starting ZonaProp bot — %s, chats=%d, playwright=%s",
        schedule_desc,
        len(settings.chat_ids),
        settings.use_playwright,
    )

    await init_db()
    scheduler = build_scheduler()
    scheduler.start()
    try:
        yield
    finally:
        log.info("stopping scheduler (waiting for running jobs)")
        scheduler.shutdown(wait=True)
        await reset_engine()


app = FastAPI(
    title="ZonaProp Bot",
    description="Scrape ZonaProp.com.ar and notify Telegram about matching new listings.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "zonaprop-bot", "docs": "/docs", "health": "/health"}
