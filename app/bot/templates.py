"""Telegram message templates.

Kept separate from :mod:`app.bot.telegram` so they're trivial to unit-test
and so designers / PMs can edit copy without touching the bot client.
"""

from __future__ import annotations

import html
from datetime import datetime

from app.scraper.models import Listing

_TELEGRAM_CAPTION_LIMIT = 1024
_TELEGRAM_MESSAGE_LIMIT = 4096


def _fmt_price(value: float | None, currency: str | None) -> str:
    if value is None:
        return "—"
    return f"{currency or 'ARS'} {int(value):,}".replace(",", ".")


def _fmt_per_m2(value: float | None, currency: str | None) -> str:
    if value is None:
        return "—"
    return f"{currency or 'ARS'} {int(value):,}".replace(",", ".") + "/m²"


def _fmt_date(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _escape(value: str | None) -> str:
    return html.escape(value, quote=False) if value else "—"


def format_listing(listing: Listing) -> str:
    """Render a single listing as Telegram HTML.

    Output is truncated to fit the smaller caption limit (1024 chars) so the
    same string is reusable as a media-group caption or as a standalone text
    message.
    """
    rooms = "—" if listing.rooms is None else str(listing.rooms)
    bathrooms = "—" if listing.bathrooms is None else str(listing.bathrooms)
    area = "—" if listing.area_m2 is None else f"{int(listing.area_m2)} m²"

    lines = [
        f"🏠 <b>{_escape(listing.title)}</b>",
        "",
        f"💰 <b>Precio:</b> {_escape(_fmt_price(listing.price, listing.currency))}",
        (
            f"📐 <b>Superficie:</b> {area}  |  "
            f"💲 {_escape(_fmt_per_m2(listing.price_per_m2, listing.currency))}"
        ),
        f"🛏 <b>Ambientes:</b> {rooms}  |  🚿 <b>Baños:</b> {bathrooms}",
    ]
    if listing.neighborhood:
        lines.append(f"📍 <b>Barrio:</b> {_escape(listing.neighborhood)}")
    if listing.address:
        lines.append(f"🗺 <b>Dirección:</b> {_escape(listing.address)}")
    lines.append(f"📅 <b>Publicado:</b> {_escape(_fmt_date(listing.published_at))}")
    lines.append("")
    lines.append(f'<a href="{html.escape(str(listing.url), quote=True)}">Ver publicación →</a>')

    text = "\n".join(lines)
    return _truncate_html(text, limit=_TELEGRAM_CAPTION_LIMIT)


def _truncate_html(text: str, *, limit: int) -> str:
    """Trim to ``limit`` characters without cutting in the middle of a tag.

    Telegram's HTML parser is unforgiving of dangling ``<b>`` etc.; we drop
    the trailing partial line entirely and append a single ellipsis.
    """
    if len(text) <= limit:
        return text
    # Conservative: cut at the last newline before the limit.
    truncated = text[: limit - 3].rsplit("\n", 1)[0]
    return truncated + "\n…"


__all__ = ["format_listing"]
