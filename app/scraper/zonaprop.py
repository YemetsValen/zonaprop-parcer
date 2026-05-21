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

# ZonaProp's ``publicado-hace-menos-de-N-dia(s)`` URL slug only resolves
# for N ∈ {1, …, 6}; any larger value 301-redirects to the URL with no
# recency filter at all, silently broadening the search.
ZONAPROP_MAX_RECENCY_DAYS = 6

# Cloudflare on www.zonaprop.com.ar runs Managed Challenge against datacenter
# IPs / generic browser UAs, but explicitly whitelists link-preview crawlers
# (WhatsApp, etc.) so their server-side rendered metadata is reachable. We use
# WhatsApp's UA as the primary fetch identity — it gets us a real SSR-rendered
# HTML page that we then parse with BeautifulSoup. fake-useragent / Chrome UAs
# stay available as a fallback for environments where the WhatsApp trick has
# been patched.
_PRIMARY_UA = "WhatsApp/2.0"

# When the scraper trips a CAPTCHA / WAF page, the JSON blob is missing and
# the body is small + has these strings. We use this to decide whether to
# fall back to Playwright.
_BLOCK_HINTS = ("captcha", "Just a moment", "Access denied", "Pardon Our Interruption")

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(?P<json>.+?)</script>',
    re.DOTALL,
)

# Regex helpers for the SSR (server-rendered) HTML format. The numbers come
# from spans like "50 m² tot.", "3 amb.", "2 dorm.", "1 baño" — we keep them
# lenient (allow nbsp / leading punctuation) so a small layout tweak doesn't
# silently drop data.
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²", re.IGNORECASE)
_ROOMS_RE = re.compile(r"(\d+)\s*amb\.", re.IGNORECASE)
_BEDROOMS_RE = re.compile(r"(\d+)\s*dorm\.", re.IGNORECASE)
_BATHS_RE = re.compile(r"(\d+)\s*baño", re.IGNORECASE)
_POSTING_ID_FROM_URL = re.compile(r"-(\d+)\.html(?:[?#]|$)")
_PRICE_TEXT_RE = re.compile(r"(USD|US\$|\$)\s*([\d.,]+)", re.IGNORECASE)


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

    # ZonaProp's URL handler silently drops the LAST neighborhood from the
    # path whenever a price slug is also present — verified empirically with
    # 1, 2, ..., 9 neighborhoods + ``2-ambientes`` + ``mas-37-m2`` +
    # ``publicado-hace-menos-de-1-dia`` + a price range; in every case the
    # response is a 301 to the same URL with the trailing neighborhood
    # removed. For city-level slugs (``capital-federal``) the entire
    # location is wiped and the user is dumped on the global results page.
    # The only safe option is to omit the price slug from the URL whenever
    # any location is configured and to enforce the price band locally in
    # ``app.scheduler._passes_filters`` (which is currency-aware).
    if not settings.neighborhoods:
        if settings.price_min and settings.price_max:
            money = "pesos" if settings.currency == Currency.ARS else "dolares"
            parts.append(f"{settings.price_min}-{settings.price_max}-{money}")
        elif settings.price_max:
            # ``0-{max}-{money}`` is ZonaProp's "up to X" form. We don't use
            # ``hasta-`` because ZP silently rewrites that to the full results page.
            money = "pesos" if settings.currency == Currency.ARS else "dolares"
            parts.append(f"0-{settings.price_max}-{money}")

    # ZonaProp's ``publicado-hace-menos-de-N-dias`` slug only supports
    # N ∈ {1, …, 6}. Anything 7+ is silently 301-stripped to the URL with
    # no recency filter at all — which leaves the page sorted by
    # ``publicado-descendente`` (newest first) on the first page, hiding
    # cheap listings several pages back. When the user asks for a longer
    # window we therefore:
    #   * skip the (broken) slug, and
    #   * switch sort to ``precio-ascendente`` so the cheapest matches
    #     surface on page 1 — much more useful for "find me apartments
    #     under USD X" use cases than the default newest-first sort.
    sort_slug = "orden-publicado-descendente"
    if settings.published_within_days:
        n = settings.published_within_days
        if 1 <= n <= ZONAPROP_MAX_RECENCY_DAYS:
            suffix = "dia" if n == 1 else "dias"
            parts.append(f"publicado-hace-menos-de-{n}-{suffix}")
        else:
            sort_slug = "orden-precio-ascendente"

    parts.append(sort_slug)
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


# --- SSR HTML parser ---------------------------------------------------------


def _collect_ldjson_by_url(soup: BeautifulSoup) -> dict[str, dict[str, Any]]:
    """Walk all ld+json blocks and index RealEstateListing entries by URL.

    ZonaProp emits one ``<script type="application/ld+json">`` per listing
    on a results page, plus a couple of "Organization" boilerplate blocks.
    """
    out: dict[str, dict[str, Any]] = {}
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("@type") != "RealEstateListing":
            continue
        url = data.get("url")
        if isinstance(url, str):
            out[url] = data
    return out


def _ldjson_for_url(ld_index: dict[str, dict[str, Any]], url: str) -> dict[str, Any] | None:
    if url in ld_index:
        return ld_index[url]
    # Match by postingId suffix — listing URLs sometimes carry query strings.
    match = _POSTING_ID_FROM_URL.search(url)
    if not match:
        return None
    suffix = f"-{match.group(1)}.html"
    for key, value in ld_index.items():
        if key.endswith(suffix):
            return value
    return None


def _first_image_url(card: Any) -> str | None:
    for img in card.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-flickity-lazyload")
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        if not src.startswith("http"):
            continue
        if "zonapropcdn" in src or "naventcdn" in src:
            return src
    return None


def _ldjson_main_entity(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Return the ``mainEntity`` listing dict if present.

    On listing pages the structure is ``mainEntity: [Apartment, ...]`` and
    on search pages it's ``mainEntity: [{type: RealEstateListing, ...}]``.
    """
    entities = entry.get("mainEntity")
    if isinstance(entities, list) and entities:
        first = entities[0]
        if isinstance(first, dict):
            return first
    if isinstance(entities, dict):
        return entities
    return None


def _ldjson_address(entry: dict[str, Any]) -> str | None:
    me = _ldjson_main_entity(entry) or {}
    addr = me.get("address")
    if isinstance(addr, dict):
        return addr.get("name") or addr.get("streetAddress") or None
    if isinstance(addr, str):
        return addr
    return None


def _parse_argentine_price(text: str) -> tuple[float | None, str]:
    """Parse a ZonaProp price string.

    Returns ``(amount, currency)``. ZonaProp prefixes ``USD`` or ``US$`` for
    dollar prices and plain ``$`` for ARS, and uses the Argentine numeric
    format (``.`` as thousands separator, ``,`` as decimal). So ``$ 800.000``
    means *800 000*, not *800.0*. We treat a lone ``.`` group as a thousands
    separator and only honour ``,`` as a decimal point.
    """
    if not text:
        return None, "ARS"
    m = _PRICE_TEXT_RE.search(text)
    if not m:
        return None, "ARS"
    prefix, num = m.group(1), m.group(2)
    currency = "USD" if prefix.upper() in ("USD", "US$") else "ARS"
    cleaned = (
        # Thousands sep is ``.``, decimal is ``,``.
        num.replace(".", "").replace(",", ".")
        if "," in num
        # All dots are thousands separators — ZonaProp never shows fractional
        # prices without a comma.
        else num.replace(".", "")
    )
    try:
        return float(cleaned), currency
    except ValueError:
        return None, currency


def _derive_title(description: str | None, features: str | None) -> str:
    """Build a short, human-friendly title.

    ZonaProp cards have no explicit title, so we synthesise one from the
    first segment of the description (split on ``|``, ``.``, newline) and
    fall back to the feature line if the description is empty.
    """
    if description:
        head = re.split(r"[|\n\r]|(?<=\D)\.(?:\s|$)", description, maxsplit=1)[0]
        head = head.strip()
        if 5 < len(head) <= 140:
            return head
        if head:
            return head[:140].rsplit(" ", 1)[0]
    if features:
        return features.strip()
    return "ZonaProp listing"


def _derive_neighborhood(address: str | None) -> str | None:
    """Pick the neighborhood out of a ZonaProp address line.

    Formats observed:
        "Guise 1686 Palermo, Capital Federal"          -> Palermo
        "Cabildo  al 2200 Belgrano, Capital Federal"   -> Belgrano
        "Luis María Campos al 300 Las Cañitas, Palermo" -> Las Cañitas
        "Av. Libertador 4400, Las Cañitas, Palermo"    -> Las Cañitas
    The heuristic: the part before the first ``,`` is ``<street> <number?>
    <neighborhood>``. We strip the street name + house number prefix, leaving
    the neighborhood. "al" is ZonaProp's filler for an approximate house
    number and we drop it.
    """
    if not address:
        return None
    head = address.split(",", 1)[0].strip()
    if not head:
        return None
    tokens = head.split()
    # Find the last digit-only token — everything after it is the neighborhood.
    last_num_idx = -1
    for i, t in enumerate(tokens):
        if t.replace(".", "").isdigit():
            last_num_idx = i
    # If we found a digit-only token, everything after it is the neighborhood;
    # otherwise (no digit at all, e.g. "Las Cañitas, Palermo") use the whole head.
    nb_tokens = tokens[last_num_idx + 1 :] if 0 <= last_num_idx < len(tokens) - 1 else tokens
    # Drop trailing/leading "al" filler.
    nb_tokens = [t for t in nb_tokens if t.lower() != "al"]
    return " ".join(nb_tokens) or None


def _parse_card_to_listing(
    card: Any,
    *,
    ld_index: dict[str, dict[str, Any]],
    now: datetime,
) -> Listing | None:
    """Parse one ``.postingCardLayout-...`` div into a typed Listing."""
    pid = (card.get("data-id") or "").strip()
    if not pid:
        return None

    # URL: first link into /propiedades/.
    href: str | None = None
    for a in card.find_all("a", href=True):
        if "/propiedades/" in a["href"]:
            href = a["href"]
            break
    if not href:
        return None
    if href.startswith("/"):
        href = ZONAPROP_BASE + href
    # Strip tracking query params — keeps the DB tidy and avoids dedup races.
    href = href.split("?", 1)[0]

    # --- price + currency ---
    # Per-card ARS/USD prices live in the visible price block. The ld+json
    # ``offers`` field is *aggregate* across the whole results page (highPrice/
    # lowPrice describe the page, not this listing) so we never trust it for
    # the amount.
    price: float | None = None
    currency = "ARS"
    price_el = card.select_one('[data-qa="POSTING_CARD_PRICE"], .postingPrices-module__price')
    if price_el is not None:
        price, currency = _parse_argentine_price(price_el.get_text(" ", strip=True))

    # --- features (m² / rooms / bathrooms) ---
    feat_text_parts = [
        span.get_text(" ", strip=True)
        for span in card.select(".postingMainFeatures-module__posting-main-features-span")
    ]
    feat_text = " ".join(feat_text_parts)
    area_m2 = _as_float(m.group(1)) if (m := _AREA_RE.search(feat_text)) else None
    rooms = _as_int(m.group(1)) if (m := _ROOMS_RE.search(feat_text)) else None
    bathrooms = _as_int(m.group(1)) if (m := _BATHS_RE.search(feat_text)) else None

    price_per_m2: float | None = None
    if price and area_m2 and area_m2 > 0:
        price_per_m2 = round(price / area_m2, 2)

    # --- address / neighborhood ---
    address: str | None = None
    block = card.select_one(".postingLocations-module__location-block")
    if block is not None:
        address = block.get_text(" ", strip=True) or None
    if not address:
        ld = _ldjson_for_url(ld_index, href)
        if ld is not None:
            address = _ldjson_address(ld)
    neighborhood = _derive_neighborhood(address)

    # --- description / title / images ---
    description: str | None = None
    desc_el = card.select_one('[data-qa="POSTING_CARD_DESCRIPTION"]')
    if desc_el is not None:
        text = desc_el.get_text(" ", strip=True)
        description = (text or "").strip()[:500] or None

    title = _derive_title(description, feat_text)

    image = _first_image_url(card)
    images: list[str] = [image] if image else []

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
            published_at=None,
            scraped_at=now,
        )
    except Exception as exc:  # noqa: BLE001 - one bad posting must not abort the run
        log.warning("dropping malformed posting %s: %s", pid, exc)
        return None


def parse_listings_from_ssr_html(html: str, now: datetime | None = None) -> list[Listing]:
    """Extract listings from ZonaProp's server-rendered HTML.

    This is the path taken when we fetch with a link-preview UA
    (``WhatsApp/2.0``) and Cloudflare lets the request through with full SSR
    content but **no** ``__NEXT_DATA__`` script.
    """
    now = now or datetime.now(tz=UTC)
    soup = BeautifulSoup(html, "html.parser")
    ld_index = _collect_ldjson_by_url(soup)
    cards = soup.select(".postingCardLayout-module__posting-card-layout[data-id]")
    out: list[Listing] = []
    for card in cards:
        listing = _parse_card_to_listing(card, ld_index=ld_index, now=now)
        if listing is not None:
            out.append(listing)
    return out


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
    """Public: extract every listing we can recognise out of one HTML page.

    Strategy:

    1. Try the legacy ``__NEXT_DATA__`` JSON path (kept for fixtures and any
       page where ZonaProp still emits it).
    2. Fall back to the SSR HTML parser. The link-preview UA we use most of
       the time returns the server-rendered page without ``__NEXT_DATA__``,
       so this is the hot path in production.
    """
    now = now or datetime.now(tz=UTC)
    data = extract_next_data(html)
    if data is not None:
        out: list[Listing] = []
        for raw in _iter_postings(data):
            listing = _map_raw_to_listing(raw, now=now)
            if listing is not None:
                out.append(listing)
        if out:
            return out
    return parse_listings_from_ssr_html(html, now=now)


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
        rotate_ua: bool = False,
    ) -> None:
        self._settings = settings or get_settings()
        self._client_override = http_client
        self._client: httpx.AsyncClient | None = http_client
        # ``rotate_ua=True`` opts back in to fake-useragent's Chrome rotation,
        # which can be useful when running this scraper against domains that
        # don't whitelist WhatsApp. The default is the stable WhatsApp UA.
        if rotate_ua:
            try:
                self._ua = ua or UserAgent()
            except Exception:  # noqa: BLE001 - offline / cached-data failures
                self._ua = None
        else:
            self._ua = ua

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
        """Pick a UA per request.

        We **default to the WhatsApp link-preview UA** because that's the
        identity Cloudflare lets through on www.zonaprop.com.ar. Pages served
        to WhatsApp are full server-side-rendered HTML — perfect for our
        SSR parser. If you really need a browser-style UA (e.g. for a future
        domain that blocks WhatsApp), set ``USE_PLAYWRIGHT=true`` or override
        the scraper's ``ua`` constructor argument.
        """
        if self._ua is not None:
            try:
                return str(self._ua.random)
            except Exception:  # noqa: BLE001
                pass
        return _PRIMARY_UA

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
