"""Scraper unit tests — URL builder, __NEXT_DATA__ extraction, mapping."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import Settings
from app.scraper.zonaprop import (
    ZONAPROP_BASE,
    ZonaPropScraper,
    _derive_neighborhood,
    _parse_argentine_price,
    build_search_url,
    extract_next_data,
    parse_listings_from_html,
    parse_listings_from_ssr_html,
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
    # Price slug is intentionally omitted whenever any neighborhood is
    # configured: ZonaProp's URL handler 301-strips the last neighborhood
    # when a price slug is present, so we keep the URL location-scoped and
    # enforce the price band locally in ``_passes_filters``.
    assert "300000-800000-pesos" not in url
    assert "pesos" not in url
    # Sort order.
    assert url.endswith("orden-publicado-descendente.html")


def test_build_search_url_usd_uses_dolares() -> None:
    # Price slug (and therefore the currency word) is only emitted when no
    # neighborhood is configured (see ``test_build_search_url_price_dropped_when_neighborhoods``).
    s = _make_settings(neighborhoods="", currency="USD")
    assert "dolares" in build_search_url(s)


def test_build_search_url_single_room_uses_singular_slug() -> None:
    s = _make_settings(rooms_min=2, rooms_max=2)
    assert "2-ambientes" in build_search_url(s)
    # And NOT "2-2-ambientes".
    assert "2-2-ambientes" not in build_search_url(s)


def test_build_search_url_two_property_types_joined() -> None:
    s = _make_settings(property_types="departamentos,ph")
    assert "departamentos-y-ph" in build_search_url(s)


def test_build_search_url_venta_capital_federal_24h() -> None:
    """Venta + capital-federal + 24h: real-world config for the daily digest."""
    s = _make_settings(
        operation_type="venta",
        property_types="departamentos",
        neighborhoods="capital-federal",
        currency="USD",
        price_min=0,
        price_max=90000,
        rooms_min=0,
        rooms_max=0,
        area_min=0,
        published_within_days=1,
    )
    url = build_search_url(s)
    assert "venta" in url
    assert "capital-federal" in url
    assert "publicado-hace-menos-de-1-dia" in url
    # Price slug is intentionally omitted: ZonaProp redirects the
    # ``capital-federal + price`` combo to the global page, so we keep the
    # URL location-scoped and re-check price locally.
    assert "dolares" not in url
    assert "90000" not in url


def test_build_search_url_price_only_max_no_neighborhoods() -> None:
    """Only ``price_max`` set + no neighborhood -> ``0-<max>-{money}``.

    When at least one neighborhood is configured the price slug is dropped
    from the URL (see ``test_build_search_url_price_dropped_when_neighborhoods``)
    because ZonaProp 301-strips the trailing neighborhood from the path
    whenever a price slug is present.
    """
    s = _make_settings(
        neighborhoods="",
        currency="USD",
        price_min=0,
        price_max=150000,
    )
    url = build_search_url(s)
    assert "0-150000-dolares" in url


def test_build_search_url_price_dropped_when_neighborhoods() -> None:
    """With a neighborhood configured the price slug must be omitted, even
    when ``price_max`` is set, to avoid the ZonaProp 301-strip bug."""
    s = _make_settings(
        neighborhoods="palermo,belgrano",
        currency="USD",
        price_min=0,
        price_max=95000,
    )
    url = build_search_url(s)
    assert "95000" not in url
    assert "dolares" not in url
    assert "palermo" in url
    assert "belgrano" in url


def test_build_search_url_published_within_days_plural() -> None:
    s = _make_settings(neighborhoods="palermo", published_within_days=3)
    assert "publicado-hace-menos-de-3-dias" in build_search_url(s)


def test_build_search_url_published_within_days_singular() -> None:
    s = _make_settings(neighborhoods="palermo", published_within_days=1)
    assert "publicado-hace-menos-de-1-dia" in build_search_url(s)
    # And NOT the plural form.
    assert "publicado-hace-menos-de-1-dias" not in build_search_url(s)


def test_build_search_url_long_recency_drops_slug_and_sorts_by_price_asc() -> None:
    """ZonaProp's ``publicado-hace-menos-de-N-dias`` slug only resolves for
    N ∈ {1, …, 6}; anything 7+ is silently 301-stripped. When we can't
    express the recency window in the URL we omit the slug entirely and
    switch sort from newest-first to price-ascending so the cheapest
    matches surface on page 1 (which is what users searching by price
    cap actually want)."""
    s = _make_settings(neighborhoods="palermo", published_within_days=30)
    url = build_search_url(s)
    # Recency slug dropped — would 301-strip server-side anyway.
    assert "publicado-hace-menos-de" not in url
    # Sort flipped to ``precio-ascendente`` so cheap listings reach page 1.
    assert url.endswith("orden-precio-ascendente.html")
    assert "orden-publicado-descendente" not in url


def test_build_search_url_recency_within_supported_range_keeps_default_sort() -> None:
    """For supported recency windows (1-6 days) we keep the URL slug AND
    the default ``publicado-descendente`` sort — there's no need to flip
    to price-asc because the page is already small enough that all matches
    appear on page 1."""
    for n in (1, 2, 3, 4, 5, 6):
        url = build_search_url(_make_settings(neighborhoods="palermo", published_within_days=n))
        assert f"publicado-hace-menos-de-{n}-" in url
        assert url.endswith("orden-publicado-descendente.html")


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


# --- SSR HTML parser tests (WhatsApp-UA path) ---------------------------------


def test_parse_argentine_price_thousands_dot_is_separator() -> None:
    # ZonaProp shows "$ 800.000" meaning 800_000 ARS, not 800.0.
    amount, currency = _parse_argentine_price("$ 800.000")
    assert amount == 800000.0
    assert currency == "ARS"


def test_parse_argentine_price_usd_prefix() -> None:
    amount, currency = _parse_argentine_price("USD 1.500")
    assert amount == 1500.0
    assert currency == "USD"


def test_parse_argentine_price_decimal_comma() -> None:
    # Comma is decimal in es-AR; "$ 1.250,50" = 1250.5.
    amount, currency = _parse_argentine_price("$ 1.250,50")
    assert amount == 1250.5
    assert currency == "ARS"


def test_parse_argentine_price_us_dollar_prefix() -> None:
    amount, currency = _parse_argentine_price("US$ 2.500")
    assert amount == 2500.0
    assert currency == "USD"


def test_parse_argentine_price_returns_none_for_junk() -> None:
    assert _parse_argentine_price("Consultar") == (None, "ARS")
    assert _parse_argentine_price("") == (None, "ARS")


def test_derive_neighborhood_handles_zonaprop_formats() -> None:
    assert _derive_neighborhood("Guise 1686 Palermo, Capital Federal") == "Palermo"
    assert _derive_neighborhood("Cabildo  al 2200 Belgrano, Capital Federal") == "Belgrano"
    assert _derive_neighborhood("Luis María Campos al 300 Las Cañitas, Palermo") == "Las Cañitas"
    # No digits at all — use the head.
    assert _derive_neighborhood("Las Cañitas, Palermo") == "Las Cañitas"
    assert _derive_neighborhood(None) is None
    assert _derive_neighborhood("") is None


def test_parse_listings_from_ssr_html(sample_ssr_html: str) -> None:
    listings = parse_listings_from_ssr_html(sample_ssr_html)
    # 2 valid cards + 1 card with no data-id which must be dropped.
    assert len(listings) == 2

    first = next(listing for listing in listings if listing.id == "58479269")
    assert first.price == 800000.0
    assert first.currency == "ARS"
    assert first.area_m2 == 50.0
    assert first.rooms == 3
    assert first.bathrooms == 1
    assert first.neighborhood == "Palermo"
    assert first.address == "Guise 1686 Palermo, Capital Federal"
    # Tracking query params must be stripped from the URL.
    assert "?n_src" not in str(first.url)
    assert str(first.url).endswith("alquiler-foo-58479269.html")
    # Title derived from description; "$" prefix in price block stripped from
    # any title-candidate text. The first segment of the description sentence.
    assert "Hermoso" in first.title
    assert len(first.images) == 1
    assert "zonapropcdn" in str(first.images[0])
    # price_per_m2 rounded.
    assert first.price_per_m2 == round(800000 / 50, 2)

    second = next(listing for listing in listings if listing.id == "59139567")
    assert second.price == 1500.0
    assert second.currency == "USD"
    assert second.rooms == 1
    assert second.bathrooms is None
    assert second.neighborhood == "Belgrano"
    # Title from description split on "|".
    assert second.title.startswith("Lindo monoambiente")


def test_parse_listings_from_html_falls_back_to_ssr(sample_ssr_html: str) -> None:
    # parse_listings_from_html should detect the missing __NEXT_DATA__ and
    # transparently fall back to the SSR parser.
    listings = parse_listings_from_html(sample_ssr_html)
    assert len(listings) == 2


# --- ZonaPropScraper integration tests ---------------------------------------


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
