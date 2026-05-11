"""Pydantic models the scraper produces.

These are the shape we want internally and over the API; the ZonaProp
JSON-LD / __NEXT_DATA__ payload is mapped onto this in :mod:`app.scraper.zonaprop`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Listing(BaseModel):
    """One ZonaProp listing, normalised."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(..., min_length=1, description="ZonaProp's natural id ('postingId').")
    url: HttpUrl
    title: str
    price: float | None = None
    currency: str = "ARS"
    price_per_m2: float | None = None
    area_m2: float | None = None
    rooms: int | None = None
    bathrooms: int | None = None
    address: str | None = None
    neighborhood: str | None = None
    description: str | None = None
    # Cap at 3 URLs in the scraper (max Telegram media-group size of interest).
    images: list[HttpUrl] = Field(default_factory=list)
    published_at: datetime | None = None
    scraped_at: datetime

    def short_summary(self) -> str:
        """One-line description for log lines."""
        bits = [self.title]
        if self.price is not None:
            bits.append(f"{self.currency} {int(self.price):,}")
        if self.neighborhood:
            bits.append(self.neighborhood)
        return " — ".join(bits)
