"""ORM models — SQLAlchemy 2.0 typed mappings."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Single declarative base; metadata.create_all is called in init_db()."""


class SeenListing(Base):
    """One row per ZonaProp listing we have already notified about.

    ``listing_id`` is the natural key from ZonaProp; we keep ``id`` as a
    surrogate so retries that produce duplicates can be deleted by primary
    key without touching the unique constraint.
    """

    __tablename__ = "seen_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(512), default=None)
    price: Mapped[float | None] = mapped_column(default=None)
    currency: Mapped[str | None] = mapped_column(String(8), default=None)
    neighborhood: Mapped[str | None] = mapped_column(String(128), default=None)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


# Most listings-table queries are "find by listing_id" or "order by scraped_at desc";
# the unique index above covers the first, this one speeds up listing endpoints.
Index("ix_seen_listings_scraped_at_desc", SeenListing.scraped_at.desc())
