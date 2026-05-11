"""Telegram fan-out.

A thin async wrapper around ``python-telegram-bot``. The only public entry
point we use is :class:`TelegramNotifier` — it owns one ``Bot`` instance and
sends one listing at a time, retrying transient ``RetryAfter`` failures.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from telegram import Bot, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError, TimedOut

from app.bot.templates import format_listing
from app.config import Settings, get_settings
from app.scraper.models import Listing

log = logging.getLogger(__name__)


class TelegramNotifier:
    """Fan-out client. One per process (created in the FastAPI lifespan)."""

    def __init__(self, settings: Settings | None = None, bot: Bot | None = None) -> None:
        self._settings = settings or get_settings()
        self._bot = bot or Bot(token=self._settings.telegram_bot_token)

    @property
    def chat_ids(self) -> list[str]:
        return self._settings.chat_ids

    async def send_listing(self, listing: Listing) -> int:
        """Send one listing to every configured chat.

        Returns the number of chats that received the message successfully.
        """
        delivered = 0
        for chat_id in self.chat_ids:
            try:
                await self._send_one(chat_id, listing)
                delivered += 1
            except TelegramError as exc:
                log.warning("telegram delivery failed for chat %s: %s", chat_id, exc)
        return delivered

    async def send_text(self, text: str) -> int:
        """Send a plain text message (used for watchdog alerts)."""
        delivered = 0
        for chat_id in self.chat_ids:
            try:
                await self._call(
                    self._bot.send_message,
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                delivered += 1
            except TelegramError as exc:
                log.warning("telegram text send failed for chat %s: %s", chat_id, exc)
        return delivered

    # ------ internal -------------------------------------------------------

    async def _send_one(self, chat_id: str, listing: Listing) -> None:
        caption = format_listing(listing)
        images = [str(u) for u in listing.images][:3]

        if not images:
            await self._call(
                self._bot.send_message,
                chat_id=chat_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            return

        media: list[InputMediaPhoto] = []
        for i, url in enumerate(images):
            # Telegram allows a caption on at most one item of a media group;
            # we put it on the first photo.
            media.append(
                InputMediaPhoto(
                    media=url,
                    caption=caption if i == 0 else None,
                    parse_mode=ParseMode.HTML if i == 0 else None,
                )
            )
        await self._call(self._bot.send_media_group, chat_id=chat_id, media=media)

    async def _call(self, fn, /, **kwargs):  # type: ignore[no-untyped-def]
        """Call a PTB coroutine with simple ``RetryAfter`` handling.

        PTB raises :class:`telegram.error.RetryAfter` for 429 responses with
        the cool-down in seconds; we sleep and retry once. Other transient
        errors (``TimedOut``) get one retry too. Persistent failures bubble
        up so the caller can log/skip.
        """
        attempts = 0
        max_attempts = 3
        while True:
            attempts += 1
            try:
                return await fn(**kwargs)
            except RetryAfter as exc:
                if attempts >= max_attempts:
                    raise
                wait = max(1, int(getattr(exc, "retry_after", 1)))
                log.info("telegram RetryAfter: sleeping %ds", wait)
                await asyncio.sleep(wait)
            except TimedOut:
                if attempts >= max_attempts:
                    raise
                await asyncio.sleep(2 * attempts)


async def send_listings(
    notifier: TelegramNotifier, listings: Iterable[Listing]
) -> int:
    """Convenience: send a batch, return how many were dispatched at least
    once (i.e. delivered to ≥ 1 chat)."""
    sent = 0
    for listing in listings:
        delivered = await notifier.send_listing(listing)
        if delivered:
            sent += 1
    return sent


__all__ = ["TelegramNotifier", "send_listings"]
