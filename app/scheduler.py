"""APScheduler glue.

One :class:`AsyncIOScheduler` instance owned by the FastAPI app. Jobs:

* ``zonaprop_check`` — run :func:`check_new_listings` every
  ``CHECK_INTERVAL_MINUTES`` minutes (``max_instances=1`` so an overlap is
  impossible if a previous run is slow).
* ``zonaprop_watchdog`` — once a minute, check the timestamp of the last
  *successful* run and post an alert to Telegram if it's been silent for
  ``WATCHDOG_TIMEOUT_MINUTES``. Set to 0 to disable.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.bot.telegram import TelegramNotifier
from app.config import Settings, get_settings
from app.db.database import get_session
from app.db.models import SeenListing
from app.scraper.models import Listing
from app.scraper.zonaprop import ZonaPropScraper

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Snapshot of one scheduler tick — surfaced over the API."""

    started_at: datetime
    finished_at: datetime
    fetched: int = 0
    new: int = 0
    sent: int = 0
    error: str | None = None


@dataclass
class SchedulerState:
    """Lightweight in-memory health view (no extra DB round-trip per /health)."""

    last_check: CheckResult | None = None
    last_success: datetime | None = None
    total_checks: int = 0
    total_new: int = 0
    watchdog_alerted_at: datetime | None = None
    history: list[CheckResult] = field(default_factory=list)

    def record(self, result: CheckResult, *, history_limit: int = 50) -> None:
        self.last_check = result
        self.total_checks += 1
        self.total_new += result.new
        if result.error is None:
            self.last_success = result.finished_at
            # Recovery — reset watchdog state so a future outage alerts again.
            self.watchdog_alerted_at = None
        self.history.append(result)
        if len(self.history) > history_limit:
            self.history = self.history[-history_limit:]


_state = SchedulerState()
_scheduler: AsyncIOScheduler | None = None


def get_state() -> SchedulerState:
    """Singleton — used by the API layer."""
    return _state


# --- core job ---------------------------------------------------------------


def _slugify_for_compare(value: str) -> str:
    """Normalise a free-form barrio name (``"Villa Crespo"``, ``"Núñez"``,
    ``"Belgrano R"``) into a ZonaProp-style slug (``villa-crespo``,
    ``nunez``, ``belgrano-r``) so the local filter can match listings
    against the configured CSV of slugs."""
    # Strip accents — Spanish neighborhoods commonly include them while ZP
    # slugs don't ("Nuñez" -> "nunez", "San Cristóbal" -> "san-cristobal").
    decomposed = unicodedata.normalize("NFD", value)
    ascii_only = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return ascii_only.lower().strip().replace(" ", "-")


def _passes_filters(listing: Listing, settings: Settings) -> bool:
    """ZonaProp's URL filters do most of the work, but the response often
    contains nearby / similar postings — re-check key constraints here so
    only matches reach Telegram."""
    # Price comparison only makes sense within the same currency — mixing
    # ARS / USD would silently let through a 750 000 ARS listing under a
    # 90 000 USD ceiling (or block a 90 000 USD listing under an 800 000 ARS
    # ceiling). When the listing currency differs from the configured one we
    # drop the listing rather than guess at an FX rate. Same logic when a
    # price band is configured but the listing has no parsed price (e.g.
    # ``emprendimiento`` rows that publish "Consultar"): we can't prove the
    # listing is in-range, so drop it.
    price_band_configured = bool(settings.price_min or settings.price_max)
    if listing.price is None:
        if price_band_configured:
            return False
    elif listing.currency != settings.currency.value:
        return False
    if settings.price_max and listing.price and listing.price > settings.price_max:
        return False
    if settings.price_min and listing.price and listing.price < settings.price_min:
        return False
    if settings.area_min and listing.area_m2 and listing.area_m2 < settings.area_min:
        return False
    if settings.rooms_min and listing.rooms and listing.rooms < settings.rooms_min:
        return False
    if settings.rooms_max and listing.rooms and listing.rooms > settings.rooms_max:
        return False
    # When the user configured a city-level location slug (e.g. ``capital-federal``)
    # we trust ZonaProp's URL-side filter and don't re-check the barrio locally:
    # individual listings carry the barrio (Palermo, Belgrano, …), not the city
    # slug, so a naive ``in`` check would reject everything.
    if settings.has_city_level_location:
        return True
    if settings.neighborhoods and listing.neighborhood:
        # ZP serves barrio names with spaces, capitalisation and accents
        # ("Villa Crespo", "Núñez", "Belgrano R"); the configured neighborhoods
        # are URL slugs ("villa-crespo", "nunez", "belgrano-r"). Slugify both
        # sides before comparing, and allow listings whose barrio _starts with_
        # one of the configured slugs so "Belgrano R" still matches "belgrano"
        # when the user only configured the parent barrio.
        listing_slug = _slugify_for_compare(listing.neighborhood)
        for nb_slug in settings.neighborhoods:
            if listing_slug == nb_slug or listing_slug.startswith(nb_slug + "-"):
                return True
        return False
    return True


async def check_new_listings(
    notifier: TelegramNotifier | None = None,
    scraper: ZonaPropScraper | None = None,
) -> CheckResult:
    """One tick: scrape, diff against ``seen_listings``, notify, record.

    Always returns a :class:`CheckResult`; any exception is captured as
    ``result.error`` so an upstream failure can't kill the scheduler.
    """
    started_at = datetime.now(tz=UTC)
    result = CheckResult(started_at=started_at, finished_at=started_at)

    settings = get_settings()
    owns_scraper = scraper is None
    owns_notifier = notifier is None
    scraper = scraper or ZonaPropScraper(settings)
    notifier = notifier or TelegramNotifier(settings)

    try:
        listings = await scraper.fetch_listings()
        result.fetched = len(listings)

        listings = [listing for listing in listings if _passes_filters(listing, settings)]

        if not listings:
            log.info("no listings matched filters this tick")
            return result

        async with get_session() as session:
            known = await session.execute(
                select(SeenListing.listing_id).where(
                    SeenListing.listing_id.in_([listing.id for listing in listings])
                )
            )
            seen_ids = {row[0] for row in known.all()}
            new_listings = [listing for listing in listings if listing.id not in seen_ids]
            result.new = len(new_listings)

            for listing in new_listings:
                delivered = await notifier.send_listing(listing)
                notified_at = datetime.now(tz=UTC) if delivered else None
                # Insert even on failed delivery so we don't spam later — but
                # leave ``notified_at`` null so an admin can spot it.
                session.add(
                    SeenListing(
                        listing_id=listing.id,
                        url=str(listing.url),
                        title=listing.title,
                        price=listing.price,
                        currency=listing.currency,
                        neighborhood=listing.neighborhood,
                        scraped_at=listing.scraped_at,
                        notified_at=notified_at,
                    )
                )
                if delivered:
                    result.sent += 1
        return result
    except Exception as exc:  # noqa: BLE001 - never kill the scheduler
        log.exception("scheduler tick failed")
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result.finished_at = datetime.now(tz=UTC)
        _state.record(result)
        if owns_scraper:
            await scraper.aclose()
        # No close on notifier — Bot keeps a session we want to reuse.
        _ = owns_notifier


async def watchdog_tick(notifier: TelegramNotifier | None = None) -> None:
    """Send a Telegram alert if the main job hasn't succeeded recently."""
    settings = get_settings()
    if settings.watchdog_timeout_minutes <= 0:
        return

    timeout = timedelta(minutes=settings.watchdog_timeout_minutes)
    now = datetime.now(tz=UTC)
    last_success = _state.last_success

    if last_success is not None and (now - last_success) < timeout:
        return
    if last_success is None and _state.total_checks == 0:
        # Service just started; don't alert before the first scheduled run.
        return
    # Don't alert twice in a row for the same outage.
    if _state.watchdog_alerted_at and (now - _state.watchdog_alerted_at) < timeout:
        return

    notifier = notifier or TelegramNotifier(settings)
    last_seen = last_success.strftime("%Y-%m-%d %H:%M UTC") if last_success else "never"
    minutes = settings.watchdog_timeout_minutes
    await notifier.send_text(
        "⚠️ <b>ZonaProp watchdog</b>\n"
        f"No successful scrape in the last {minutes} minutes. "
        f"Last success: <code>{last_seen}</code>."
    )
    _state.watchdog_alerted_at = now


# --- lifecycle --------------------------------------------------------------


def _check_trigger(settings: Settings) -> IntervalTrigger | CronTrigger:
    """Pick a trigger: daily cron in ``schedule_timezone`` if
    ``daily_check_time`` is set, otherwise the interval trigger."""
    if settings.daily_check_time:
        hh, mm = settings.daily_check_time.split(":", 1)
        return CronTrigger(
            hour=int(hh),
            minute=int(mm),
            timezone=settings.schedule_timezone,
        )
    return IntervalTrigger(minutes=settings.check_interval_minutes)


def build_scheduler() -> AsyncIOScheduler:
    """Construct (but do not start) the scheduler with all jobs registered."""
    global _scheduler
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_new_listings,
        trigger=_check_trigger(settings),
        id="zonaprop_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.watchdog_timeout_minutes > 0:
        scheduler.add_job(
            watchdog_tick,
            trigger=IntervalTrigger(minutes=1),
            id="zonaprop_watchdog",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    _scheduler = scheduler
    return scheduler


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the currently-installed scheduler (or ``None`` outside lifespan)."""
    return _scheduler


__all__ = [
    "CheckResult",
    "SchedulerState",
    "build_scheduler",
    "check_new_listings",
    "get_scheduler",
    "get_state",
    "watchdog_tick",
]
