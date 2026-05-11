"""ZonaProp scraper.

Public entry-point is :class:`ZonaPropScraper`. The scraper:

1. Builds a search URL from the active :class:`~app.config.Settings`.
2. Tries ``httpx`` + BeautifulSoup4 first (fast, cheap).
3. Falls back to Playwright when ``USE_PLAYWRIGHT=true`` *and* the plain
   request looks blocked / JS-only.
4. Extracts the listings array out of ``window.__NEXT_DATA__`` (Next.js puts
   the SSR state in a single ``<script id="__NEXT_DATA__">`` JSON blob).
5. Maps each raw posting onto a typed :class:`~app.scraper.models.Listing`.

The mapping is intentionally tolerant of missing fields: ZonaProp's payload
shape varies slightly between listing types, and we never want a single bad
field to drop a whole listing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Currency, OperationType, Settings, get_settings
from app.scraper.models import Listing

log = logging.getLogger(__name__)

ZONAPROP_BASE = "https://www.zonaprop.com.ar"

# When the scraper trips a CAPTCHA / WAF page, the JSON blob is missing and
# the body is small + has these strings. We use this to decide whether to
# fall back to Playwright.
_BLOCK_HINTS = ("captcha", "Just a moment", "Access denied", "Pardon Our Interruption")

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(?P<json>.+?)</script>',
    re.DOTALL,
)


# --- helpers -----------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    """Best-effort numeric coercion that returns ``None`` for junk."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d,.\-]", "", value).replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    f = _as_float(value)
    return int(f) if f is not None else None


def _parse_datetime(value: Any) -> datetime | None:
    """ZonaProp formats vary; handle the common ones, return None otherwise."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


# --- URL builder -------------------------------------------------------------


def build_search_url(settings: Settings) -> str:
    """Assemble a ZonaProp search URL from filters.

    ZonaProp URLs are SEO-style slugs, not query parameters, e.g.::

        /departamentos-alquiler-palermo-belgrano-2-ambientes-orden-publicado-descendente.html

    We compose:
        <property-types>-<operation>[-<neighborhoods>][-<rooms>-ambientes]
        [-mas-<area>-m2][-<price-min>-<price-max>-pesos|dolares]-orden-publicado-descendente.html

    Unknown / empty filters are skipped — ZonaProp tolerates that.
    """
    # Multiple property types: "departamentos-y-ph".
    if len(settings.property_types) == 1:
        prop_slug = settings.property_types[0]
    else:
        prop_slug = "-y-".join(settings.property_types)

    parts: list[str] = [prop_slug, settings.operation_type.value]
    parts.extend(settings.neighborhoods)

    if settings.rooms_min and settings.rooms_max and settings.rooms_min == settings.rooms_max:
        parts.append(f"{settings.rooms_min}-ambientes")
    elif settings.rooms_min and settings.rooms_max:
        parts.append(f"{settings.rooms_min}-{settings.rooms_max}-ambientes")
    elif settings.rooms_min:
        parts.append(f"mas-{settings.rooms_min}-ambientes")

    if settings.area_min:
        parts.append(f"mas-{settings.area_min}-m2")

    if settings.price_min and settings.price_max:
        money = "pesos" if settings.currency == Currency.ARS else "dolares"
        parts.append(f"{settings.price_min}-{settings.price_max}-{money}")

    parts.append("orden-publicado-descendente")
    return f"{ZONAPROP_BASE}/{'-'.join(parts)}.html"


# --- HTML / JSON extraction --------------------------------------------------


def extract_next_data(html: str) -> dict[str, Any] | None:
    """Return parsed ``__NEXT_DATA__`` JSON or ``None`` if missing."""
    # Try a cheap regex first (works on minified HTML); fall back to a
    # BeautifulSoup pass for hand-edited fixtures with weird whitespace.
    match = _NEXT_DATA_RE.search(html)
    raw: str | None = match.group("json") if match else None
    if raw is None:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if tag is None:
            return None
        raw = tag.get_text() or ""

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("__NEXT_DATA__ found but JSON did not parse")
        return None


def _looks_blocked(html: str) -> bool:
    if not html or len(html) < 1024:
        return True
    sample = html[:4096]
    return any(hint.lower() in sample.lower() for hint in _BLOCK_HINTS)


def _iter_postings(next_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk into ``props.pageProps.listPostings`` defensively."""
    try:
        return list(next_data["props"]["pageProps"]["listPostings"])
    except (KeyError, TypeError):
        # Some pages put it under "listings" / "postings"; try a few names.
        page_props = next_data.get("props", {}).get("pageProps", {})
        for key in ("postings", "listings", "results"):
            data = page_props.get(key)
            if isinstance(data, list):
                return data
        return []


def _pick_image_urls(raw: dict[str, Any], limit: int = 3) -> list[str]:
    """Pull at most ``limit`` photo URLs from the various shapes ZonaProp uses."""
    out: list[str] = []
    seen: set[str] = set()
    candidates = (
        raw.get("multimedia", {}) or {},
        raw,
    )
    for source in candidates:
        images = source.get("images") or source.get("photos") or []
        if isinstance(images, dict):
            images = list(images.values())
        for img in images:
            url = None
            if isinstance(img, str):
                url = img
            elif isinstance(img, dict):
                url = img.get("image") or img.get("url") or img.get("src")
            if url and url not in seen:
                # Normalise protocol-relative URLs.
                if url.startswith("//"):
                    url = "https:" + url
                if url.startswith("http"):
                    out.append(url)
                    seen.add(url)
            if len(out) >= limit:
                return out
    return out


def _map_raw_to_listing(raw: dict[str, Any], now: datetime) -> Listing | None:
    """Translate a single raw ZonaProp posting to our :class:`Listing`."""
    pid = str(raw.get("postingId") or raw.get("id") or "").strip()
    if not pid:
        return None

    href = raw.get("url") or raw.get("link") or raw.get("publicationLink")
    if href and href.startswith("/"):
        href = ZONAPROP_BASE + href
    if not href:
        return None

    title = (raw.get("title") or raw.get("publicationTitle") or "ZonaProp listing").strip()

    # ZonaProp may put the operation/price in nested "priceOperationTypes".
    price: float | None = None
    currency = "ARS"
    for op in raw.get("priceOperationTypes") or []:
        prices = op.get("prices") or []
        if prices:
            price = _as_float(prices[0].get("amount"))
            currency = (prices[0].get("currency") or "ARS").upper()
            break
    if price is None:
        # Some search responses flatten price onto the posting.
        price = _as_float(raw.get("price") or raw.get("priceValue"))
        currency = (raw.get("priceCurrency") or currency).upper()

    area_m2 = _as_float(raw.get("totalArea") or raw.get("area") or raw.get("surfaceTotal"))
    rooms = _as_int(raw.get("rooms") or raw.get("bedrooms"))
    bathrooms = _as_int(raw.get("bathrooms"))

    price_per_m2: float | None = None
    if price and area_m2 and area_m2 > 0:
        price_per_m2 = round(price / area_m2, 2)

    address = raw.get("address") or (raw.get("postingLocation") or {}).get("address") or None
    if isinstance(address, dict):
        address = address.get("text") or address.get("street") or None

    neighborhood = None
    loc = raw.get("postingLocation") or {}
    if isinstance(loc, dict):
        neighborhood = (
            (loc.get("location") or {}).get("name")
            if isinstance(loc.get("location"), dict)
            else None
        )
        neighborhood = neighborhood or loc.get("neighborhood")
    neighborhood = neighborhood or raw.get("neighborhood")

    description = raw.get("description") or raw.get("publicationDescription")
    if isinstance(description, str):
        description = description.strip()[:500] or None

    images = _pick_image_urls(raw, limit=3)
    published_at = _parse_datetime(
        raw.get("publicationDate") or raw.get("publishedAt") or raw.get("createdAt")
    )

    try:
        return Listing(
            id=pid,
            url=href,
            title=title,
            price=price,
            currency=currency or "ARS",
            price_per_m2=price_per_m2,
            area_m2=area_m2,
            rooms=rooms,
            bathrooms=bathrooms,
            address=address,
            neighborhood=neighborhood,
            description=description,
            images=images,  # type: ignore[arg-type]
            published_at=published_at,
            scraped_at=now,
        )
    except Exception as exc:  # noqa: BLE001 - one bad posting must not abort the run
        log.warning("dropping malformed posting %s: %s", pid, exc)
        return None


def parse_listings_from_html(html: str, now: datetime | None = None) -> list[Listing]:
    """Public: extract every listing we can recognise out of one HTML page."""
    now = now or datetime.now(tz=UTC)
    data = extract_next_data(html)
    if data is None:
        return []
    out: list[Listing] = []
    for raw in _iter_postings(data):
        listing = _map_raw_to_listing(raw, now=now)
        if listing is not None:
            out.append(listing)
    return out


# --- ZonaPropScraper ---------------------------------------------------------


class ZonaPropScraper:
    """Async ZonaProp scraper. Re-use across calls — the httpx client is
    created lazily and closed via :meth:`aclose`."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        ua: UserAgent | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client_override = http_client
        self._client: httpx.AsyncClient | None = http_client
        try:
            self._ua = ua or UserAgent()
        except Exception:  # noqa: BLE001 - offline / cached-data failures
            self._ua = None

    async def __aenter__(self) -> ZonaPropScraper:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client and self._client is not self._client_override:
            await self._client.aclose()
        self._client = None

    # --- HTTP --------------------------------------------------------------

    def _user_agent(self) -> str:
        if self._ua is not None:
            try:
                return str(self._ua.random)
            except Exception:  # noqa: BLE001
                pass
        return (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.request_timeout,
                follow_redirects=True,
                headers={
                    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
        return self._client

    async def _fetch_with_httpx(self, url: str) -> str | None:
        client = self._ensure_client()

        async def _do() -> str:
            # New UA per attempt — looks more like a real user, especially
            # after a 429.
            await asyncio.sleep(random.uniform(1.0, 3.0))
            resp = await client.get(url, headers={"User-Agent": self._user_agent()})
            if resp.status_code in (429, 503):
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.text

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=1.5, min=2, max=30),
                retry=retry_if_exception_type(httpx.HTTPError),
                reraise=True,
            ):
                with attempt:
                    return await _do()
        except (httpx.HTTPError, RetryError) as exc:
            log.warning("httpx fetch failed for %s: %s", url, exc)
            return None
        return None

    async def _fetch_with_playwright(self, url: str) -> str | None:
        """Headless Chromium fallback. Lazy-imported so tests / minimal
        deployments don't need Playwright installed."""
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError:
            log.error("USE_PLAYWRIGHT=true but playwright is not installed")
            return None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        user_agent=self._user_agent(),
                        locale="es-AR",
                    )
                    page = await context.new_page()
                    await page.goto(
                        url,
                        wait_until="networkidle",
                        timeout=self._settings.request_timeout * 1000,
                    )
                    return await page.content()
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001 - many ways a headless browser can die
            log.warning("playwright fetch failed for %s: %s", url, exc)
            return None

    async def fetch_html(self, url: str | None = None) -> str | None:
        """Fetch raw HTML once, including Playwright fallback if enabled.

        Returns ``None`` if every strategy failed — the caller logs and waits
        for the next scheduler tick rather than crashing the service.
        """
        target = url or build_search_url(self._settings)
        html = await self._fetch_with_httpx(target)
        if html and not _looks_blocked(html):
            return html

        if self._settings.use_playwright:
            log.info("falling back to Playwright for %s", target)
            html = await self._fetch_with_playwright(target)
            if html and not _looks_blocked(html):
                return html

        if html is None:
            log.error("could not fetch ZonaProp page for %s", target)
        else:
            log.warning(
                "ZonaProp returned a likely block / challenge page (%d bytes) for %s",
                len(html),
                target,
            )
        return None

    async def fetch_listings(self) -> list[Listing]:
        """Top-level call used by the scheduler — returns parsed listings.

        Never raises; on any failure returns an empty list and logs.
        """
        url = build_search_url(self._settings)
        log.info("scraping ZonaProp: %s", url)
        html = await self.fetch_html(url)
        if not html:
            return []
        listings = parse_listings_from_html(html)
        log.info("parsed %d listings from %s", len(listings), url)
        return listings


__all__ = [
    "OperationType",
    "ZonaPropScraper",
    "build_search_url",
    "extract_next_data",
    "parse_listings_from_html",
]
