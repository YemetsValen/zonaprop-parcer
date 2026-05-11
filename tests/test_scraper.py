"""Scraper unit tests — URL builder, __NEXT_DATA__ extraction, mapping."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import Settings
from app.scraper.zonaprop import (
    ZONAPROP_BASE,
    ZonaPropScraper,
    build_search_url,
    extract_next_data,
    parse_listings_from_html,
)


def _make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "telegram_bot_token": "t",
        "telegram_chat_id": "1",
        "operation_type": "alquiler",
        "property_types": "departamentos",
        "neighborhoods": "palermo,belgrano",
        "price_min": 300000,
        "price_max": 800000,
        "currency": "ARS",
        "rooms_min": 2,
        "rooms_max": 3,
        "area_min": 45,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_build_search_url_basic() -> None:
    s = _make_settings()
    url = build_search_url(s)
    assert url.startswith(ZONAPROP_BASE + "/")
    # Property type and operation always appear.
    assert "departamentos" in url
    assert "alquiler" in url
    # Both neighborhoods.
    assert "palermo" in url
    assert "belgrano" in url
    # Rooms range.
    assert "2-3-ambientes" in url
    # Area.
    assert "mas-45-m2" in url
    # Price range with currency suffix.
    assert "300000-800000-pesos" in url
    # Sort order.
    assert url.endswith("orden-publicado-descendente.html")


def test_build_search_url_usd_uses_dolares() -> None:
    s = _make_settings(currency="USD")
    assert "dolares" in build_search_url(s)


def test_build_search_url_single_room_uses_singular_slug() -> None:
    s = _make_settings(rooms_min=2, rooms_max=2)
    assert "2-ambientes" in build_search_url(s)
    # And NOT "2-2-ambientes".
    assert "2-2-ambientes" not in build_search_url(s)


def test_build_search_url_two_property_types_joined() -> None:
    s = _make_settings(property_types="departamentos,ph")
    assert "departamentos-y-ph" in build_search_url(s)


def test_extract_next_data_handles_minified_html(sample_html: str) -> None:
    data = extract_next_data(sample_html)
    assert data is not None
    listings = data["props"]["pageProps"]["listPostings"]
    assert len(listings) == 3


def test_extract_next_data_returns_none_when_missing() -> None:
    assert extract_next_data("<html><body>no script</body></html>") is None


def test_parse_listings_from_html(sample_html: str) -> None:
    listings = parse_listings_from_html(sample_html)
    # The third raw row has an empty id and must be dropped.
    assert len(listings) == 2

    first = next(listing for listing in listings if listing.id == "123456")
    assert first.title.startswith("Hermoso 2 ambientes")
    assert first.price == 450000.0
    assert first.currency == "ARS"
    assert first.area_m2 == 55.0
    assert first.rooms == 2
    assert first.bathrooms == 1
    assert first.neighborhood == "Palermo"
    assert first.address == "Av. Santa Fe 1234"
    # Image cap of 3.
    assert len(first.images) == 3
    # price_per_m2 = price / area, rounded.
    assert first.price_per_m2 == round(450000 / 55, 2)
    # Absolute URL.
    assert str(first.url).startswith("https://www.zonaprop.com.ar/")

    second = next(listing for listing in listings if listing.id == "789012")
    assert second.images == []  # tolerates no images
    assert second.rooms == 3


@pytest.mark.asyncio
async def test_scraper_fetch_html_uses_httpx_and_returns_text(sample_html: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://www\.zonaprop\.com\.ar/.*\.html").mock(
            return_value=httpx.Response(200, text=sample_html)
        )
        async with ZonaPropScraper(_make_settings()) as scraper:
            html = await scraper.fetch_html()
    assert html is not None
    assert "__NEXT_DATA__" in html


@pytest.mark.asyncio
async def test_scraper_fetch_listings_end_to_end(sample_html: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://www\.zonaprop\.com\.ar/.*\.html").mock(
            return_value=httpx.Response(200, text=sample_html)
        )
        async with ZonaPropScraper(_make_settings()) as scraper:
            listings = await scraper.fetch_listings()
    assert len(listings) == 2


@pytest.mark.asyncio
async def test_scraper_returns_empty_on_repeated_failures() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://www\.zonaprop\.com\.ar/.*\.html").mock(
            return_value=httpx.Response(503)
        )
        async with ZonaPropScraper(_make_settings()) as scraper:
            listings = await scraper.fetch_listings()
    assert listings == []


@pytest.mark.asyncio
async def test_scraper_detects_block_page() -> None:
    block_html = "<html><body>" + ("Just a moment... " * 20) + "</body></html>"
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://www\.zonaprop\.com\.ar/.*\.html").mock(
            return_value=httpx.Response(200, text=block_html)
        )
        async with ZonaPropScraper(_make_settings()) as scraper:
            html = await scraper.fetch_html()
    # Block page is detected — fetch_html falls back to None when no
    # Playwright is configured.
    assert html is None
