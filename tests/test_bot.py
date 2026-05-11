"""Telegram bot tests — formatting + fan-out."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.telegram import TelegramNotifier
from app.bot.templates import format_listing
from app.config import Settings
from app.scraper.models import Listing


def _make_listing(**overrides: object) -> Listing:
    base: dict[str, object] = {
        "id": "42",
        "url": "https://www.zonaprop.com.ar/p/42.html",
        "title": "2 ambientes en <Palermo>",
        "price": 450000.0,
        "currency": "ARS",
        "price_per_m2": 8181.82,
        "area_m2": 55.0,
        "rooms": 2,
        "bathrooms": 1,
        "address": "Av. Santa Fe 1234",
        "neighborhood": "Palermo",
        "description": "Luminoso",
        "images": [],
        "published_at": datetime(2026, 4, 25, 10, 30, tzinfo=UTC),
        "scraped_at": datetime(2026, 4, 25, 11, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


def test_format_listing_escapes_html_in_title() -> None:
    text = format_listing(_make_listing())
    # Bot would crash on a raw "<Palermo>" tag — we must escape it.
    assert "<Palermo>" not in text
    assert "&lt;Palermo&gt;" in text


def test_format_listing_includes_required_fields() -> None:
    text = format_listing(_make_listing())
    assert "ARS 450.000" in text
    assert "55 m²" in text
    assert "Palermo" in text  # neighborhood line
    assert "Ver publicación" in text
    assert text.startswith("🏠")


def test_format_listing_handles_missing_data() -> None:
    text = format_listing(
        _make_listing(price=None, price_per_m2=None, area_m2=None, rooms=None, bathrooms=None,
                     neighborhood=None, address=None, published_at=None)
    )
    # All "—" placeholders shouldn't break HTML — just visually empty fields.
    assert "—" in text
    assert "<b>" in text and "</b>" in text


def test_format_listing_respects_caption_limit() -> None:
    long_title = "x" * 5000
    text = format_listing(_make_listing(title=long_title))
    assert len(text) <= 1024


@pytest.fixture
def _settings() -> Settings:
    return Settings(
        telegram_bot_token="t",
        telegram_chat_id="111, 222",
    )


@pytest.mark.asyncio
async def test_notifier_sends_text_when_no_images(_settings: Settings) -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_media_group = AsyncMock()

    notifier = TelegramNotifier(_settings, bot=bot)
    delivered = await notifier.send_listing(_make_listing())
    assert delivered == 2  # one per chat id
    assert bot.send_message.call_count == 2
    bot.send_media_group.assert_not_called()


@pytest.mark.asyncio
async def test_notifier_sends_media_group_when_images_present(_settings: Settings) -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_media_group = AsyncMock()

    listing = _make_listing(
        images=[
            "https://images.zonaprop.com/avisos/1.jpg",
            "https://images.zonaprop.com/avisos/2.jpg",
            "https://images.zonaprop.com/avisos/3.jpg",
        ],
    )
    notifier = TelegramNotifier(_settings, bot=bot)
    delivered = await notifier.send_listing(listing)
    assert delivered == 2
    bot.send_message.assert_not_called()
    assert bot.send_media_group.call_count == 2
    # First call's media list — caption only on the first photo.
    media = bot.send_media_group.call_args_list[0].kwargs["media"]
    assert len(media) == 3
    assert media[0].caption is not None
    assert media[1].caption is None


@pytest.mark.asyncio
async def test_notifier_continues_on_per_chat_failure(_settings: Settings) -> None:
    from telegram.error import TelegramError

    async def _flaky(*args: object, **kwargs: object) -> None:
        if kwargs.get("chat_id") == "111":
            raise TelegramError("blocked by user")

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=_flaky)
    bot.send_media_group = AsyncMock()

    notifier = TelegramNotifier(_settings, bot=bot)
    delivered = await notifier.send_listing(_make_listing())
    # First chat failed, second still succeeded.
    assert delivered == 1
