"""Async SQLAlchemy engine + session helpers.

The engine is created lazily on first use (and re-created if the database
URL changes between tests via ``reset_engine``). API handlers and the
scheduler share a single global event-loop-bound engine.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.db.models import Base

log = logging.getLogger(__name__)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _ensure_sqlite_directory(database_url: str) -> None:
    """SQLAlchemy won't create the parent dir for a file-backed SQLite URL,
    so we do it ourselves before opening a connection. No-op for other URLs."""
    if not database_url.startswith("sqlite"):
        return
    # URL format: sqlite+aiosqlite:///path/to.db  →  path/to.db
    _, _, path_part = database_url.partition(":///")
    if not path_part or path_part == ":memory:":
        return
    Path(path_part).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _build_engine(database_url: str) -> AsyncEngine:
    """Per-driver tuning. SQLite + aiosqlite already handles single-loop
    constraints, so kwargs stay minimal."""
    kwargs: dict[str, object] = {"future": True}
    if not database_url.startswith("sqlite"):
        kwargs["pool_pre_ping"] = True
    return create_async_engine(database_url, **kwargs)


def get_engine() -> AsyncEngine:
    """Return (creating if needed) the process-global engine."""
    global _engine, _session_factory
    if _engine is None:
        url = get_settings().database_url
        _ensure_sqlite_directory(url)
        _engine = _build_engine(url)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


async def reset_engine() -> None:
    """Tear down the engine so the next call to :func:`get_engine` re-reads
    settings. Tests use this between fixtures."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def init_db() -> None:
    """Create tables if missing. Idempotent; safe to call on every startup."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("database initialised at %s", get_settings().database_url)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session; commit on success, rollback on exception."""
    # Touching the engine guarantees the session factory is built.
    get_engine()
    assert _session_factory is not None
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
