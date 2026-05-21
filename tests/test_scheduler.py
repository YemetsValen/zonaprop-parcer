"""Scheduler core: dedupe + filtering + watchdog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import Settings
from app.db.database import get_session
from app.db.models import SeenListing
from app.scheduler import (
    _check_trigger,
    _passes_filters,
    check_new_listings,
    get_state,
    watchdog_tick,
)
from app.scraper.models import Listing


def _make_listing(**overrides: object) -> Listing:
    base: dict[str, object] = {
        "id": "1",
        "url": "https://www.zonaprop.com.ar/p/1.html",
        "title": "L1",
        "price": 500000.0,
        "currency": "ARS",
        "price_per_m2": 9000.0,
        "area_m2": 55.0,
        "rooms": 2,
        "bathrooms": 1,
        "address": "X",
        "neighborhood": "Palermo",
        "description": None,
        "images": [],
        "published_at": None,
        "scraped_at": datetime.now(tz=UTC),
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


def test_passes_filters_price_band() -> None:
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        price_min=300000,
        price_max=800000,
        rooms_min=2,
        rooms_max=3,
        area_min=45,
        neighborhoods="palermo,belgrano",
    )
    assert _passes_filters(_make_listing(price=500000), s)
    assert not _passes_filters(_make_listing(price=200000), s)
    assert not _passes_filters(_make_listing(price=900000), s)


def test_passes_filters_neighborhood() -> None:
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        neighborhoods="palermo,belgrano",
    )
    assert _passes_filters(_make_listing(neighborhood="Palermo"), s)
    assert not _passes_filters(_make_listing(neighborhood="Recoleta"), s)


def test_passes_filters_currency_mismatch_is_rejected() -> None:
    """Comparing prices across currencies (ARS vs USD) would silently leak
    bogus listings. Reject mismatched-currency listings outright."""
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        currency="USD",
        price_min=0,
        price_max=90000,
        neighborhoods="capital-federal",
    )
    # Listing in ARS, settings in USD -> rejected even though ARS price looks small.
    assert not _passes_filters(_make_listing(price=50000, currency="ARS"), s)
    assert _passes_filters(_make_listing(price=50000, currency="USD"), s)


def test_passes_filters_neighborhood_normalises_spaces_and_accents() -> None:
    """ZonaProp surfaces neighborhoods as free-form labels with spaces and
    accents ("Villa Crespo", "Núñez", "San Cristóbal"), but the configured
    ``NEIGHBORHOODS`` CSV uses ZP's URL slugs (``villa-crespo``, ``nunez``,
    ``san-cristobal``). The local barrio filter must compare them in a
    slug-normalised form."""
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        currency="USD",
        price_min=0,
        price_max=95000,
        rooms_min=2,
        rooms_max=2,
        area_min=37,
        neighborhoods="palermo,villa-crespo,nunez,san-cristobal",
    )
    base = {"price": 88000.0, "currency": "USD", "area_m2": 44.0, "rooms": 2}
    assert _passes_filters(_make_listing(neighborhood="Villa Crespo", **base), s)
    assert _passes_filters(_make_listing(neighborhood="Núñez", **base), s)
    assert _passes_filters(_make_listing(neighborhood="San Cristóbal", **base), s)
    # Listings outside the configured set are still dropped.
    assert not _passes_filters(_make_listing(neighborhood="Caballito", **base), s)


def test_passes_filters_neighborhood_matches_subbarrios() -> None:
    """ZonaProp sometimes returns a child barrio name (``"Belgrano R"``,
    ``"Belgrano C"``) for a listing whose parent slug (``belgrano``) is in
    the configured CSV. We allow the listing through to avoid losing
    coverage of the parent barrio."""
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        currency="USD",
        price_min=0,
        price_max=95000,
        rooms_min=2,
        rooms_max=2,
        area_min=37,
        neighborhoods="belgrano",
    )
    base = {"price": 88000.0, "currency": "USD", "area_m2": 44.0, "rooms": 2}
    assert _passes_filters(_make_listing(neighborhood="Belgrano", **base), s)
    assert _passes_filters(_make_listing(neighborhood="Belgrano R", **base), s)
    assert _passes_filters(_make_listing(neighborhood="Belgrano Chico", **base), s)


def test_passes_filters_rejects_listings_with_no_price_when_band_configured() -> None:
    """``emprendimiento`` (off-plan) rows often publish "Consultar" instead
    of a numeric price. When the user has set a price band we cannot prove
    the listing is in range, so we drop it rather than send a "USD ?"
    surprise into Telegram."""
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        currency="USD",
        price_min=0,
        price_max=95000,
        neighborhoods="palermo",
    )
    # area / rooms are chosen so the listing only ever fails the price gate,
    # never the area/rooms gates contributed by conftest defaults.
    base = {"currency": "ARS", "area_m2": 80.0, "rooms": 2, "neighborhood": "Palermo"}
    assert not _passes_filters(_make_listing(price=None, **base), s)
    # No price band -> ``price=None`` is accepted (different behaviour).
    s_no_band = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        currency="USD",
        price_min=0,
        price_max=0,
        rooms_min=0,
        rooms_max=0,
        area_min=0,
        neighborhoods="palermo",
    )
    assert _passes_filters(_make_listing(price=None, **base), s_no_band)


def test_passes_filters_city_level_skips_barrio_check() -> None:
    """When ``neighborhoods=['capital-federal']`` is a city-level slug,
    listings carry their barrio (Palermo, Belgrano, ...) so the naive
    membership check would reject everything. The filter must skip the
    barrio check in that case."""
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        currency="USD",
        price_min=0,
        price_max=90000,
        neighborhoods="capital-federal",
    )
    assert _passes_filters(_make_listing(price=80000, currency="USD", neighborhood="Palermo"), s)
    assert _passes_filters(_make_listing(price=80000, currency="USD", neighborhood="Caballito"), s)
    # Price still enforced locally:
    assert not _passes_filters(_make_listing(price=120000, currency="USD"), s)


@pytest.mark.asyncio
async def test_check_new_listings_dedupes_against_db(monkeypatch, fresh_db) -> None:
    listing_a = _make_listing(id="a", neighborhood="Palermo", price=500000, rooms=2, area_m2=55)
    listing_b = _make_listing(id="b", neighborhood="Belgrano", price=600000, rooms=3, area_m2=60)

    fake_scraper = AsyncMock()
    fake_scraper.fetch_listings = AsyncMock(return_value=[listing_a, listing_b])
    fake_scraper.aclose = AsyncMock()

    fake_notifier = AsyncMock()
    fake_notifier.send_listing = AsyncMock(return_value=1)

    # First tick: both new.
    result = await check_new_listings(notifier=fake_notifier, scraper=fake_scraper)
    assert result.fetched == 2
    assert result.new == 2
    assert result.sent == 2

    # DB now has both.
    async with get_session() as session:
        rows = (await session.execute(select(SeenListing))).scalars().all()
        assert {row.listing_id for row in rows} == {"a", "b"}

    # Second tick: same listings again — no new sends.
    result = await check_new_listings(notifier=fake_notifier, scraper=fake_scraper)
    assert result.new == 0
    assert result.sent == 0


@pytest.mark.asyncio
async def test_check_new_listings_swallows_scraper_errors(fresh_db) -> None:
    fake_scraper = AsyncMock()
    fake_scraper.fetch_listings = AsyncMock(side_effect=RuntimeError("boom"))
    fake_scraper.aclose = AsyncMock()
    fake_notifier = AsyncMock()

    result = await check_new_listings(notifier=fake_notifier, scraper=fake_scraper)
    assert result.error and "RuntimeError" in result.error
    # State recorded the failure but didn't crash the process.
    assert get_state().last_check is result


@pytest.mark.asyncio
async def test_watchdog_alerts_after_timeout(monkeypatch, fresh_db) -> None:
    state = get_state()
    state.total_checks = 1
    # Pretend the last successful run was 90 minutes ago.
    state.last_success = datetime.now(tz=UTC) - timedelta(minutes=90)
    state.watchdog_alerted_at = None

    fake_notifier = AsyncMock()
    fake_notifier.send_text = AsyncMock(return_value=1)

    await watchdog_tick(notifier=fake_notifier)
    fake_notifier.send_text.assert_called_once()
    text = fake_notifier.send_text.call_args.args[0]
    assert "watchdog" in text.lower()

    # Second call right after should NOT re-alert.
    fake_notifier.send_text.reset_mock()
    await watchdog_tick(notifier=fake_notifier)
    fake_notifier.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_watchdog_silent_before_first_run(fresh_db) -> None:
    state = get_state()
    state.total_checks = 0
    state.last_success = None
    state.watchdog_alerted_at = None
    fake_notifier = AsyncMock()
    fake_notifier.send_text = AsyncMock()
    await watchdog_tick(notifier=fake_notifier)
    fake_notifier.send_text.assert_not_called()


def test_check_trigger_defaults_to_interval() -> None:
    """No ``daily_check_time`` -> classic interval-based trigger."""
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        check_interval_minutes=15,
    )
    trigger = _check_trigger(s)
    assert isinstance(trigger, IntervalTrigger)


def test_check_trigger_uses_cron_when_daily_time_set() -> None:
    """``daily_check_time`` switches the scheduler to a daily cron in the
    configured timezone."""
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        daily_check_time="07:00",
        schedule_timezone="America/Argentina/Buenos_Aires",
    )
    trigger = _check_trigger(s)
    assert isinstance(trigger, CronTrigger)
    # CronTrigger stores fields in a list keyed by name; check hour/minute.
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "7"
    assert fields["minute"] == "0"
