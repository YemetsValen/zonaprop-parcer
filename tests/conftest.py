"""Shared test fixtures.

Every test runs against an isolated in-memory SQLite database and a fresh
``Settings`` instance so that ordering can't leak state.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Sensible defaults BEFORE importing app modules so Settings() parses cleanly.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "111,222")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("OPERATION_TYPE", "alquiler")
os.environ.setdefault("PROPERTY_TYPES", "departamentos,ph")
os.environ.setdefault("NEIGHBORHOODS", "palermo,belgrano")
os.environ.setdefault("PRICE_MIN", "300000")
os.environ.setdefault("PRICE_MAX", "800000")
os.environ.setdefault("CURRENCY", "ARS")
os.environ.setdefault("ROOMS_MIN", "2")
os.environ.setdefault("ROOMS_MAX", "3")
os.environ.setdefault("AREA_MIN", "45")
os.environ.setdefault("CHECK_INTERVAL_MINUTES", "15")
os.environ.setdefault("WATCHDOG_TIMEOUT_MINUTES", "30")
os.environ.setdefault("USE_PLAYWRIGHT", "false")


@pytest.fixture
def fixtures_dir() -> Path:
    return ROOT / "tests" / "fixtures"


@pytest.fixture
def sample_html(fixtures_dir: Path) -> str:
    return (fixtures_dir / "zonaprop_sample.html").read_text(encoding="utf-8")


@pytest.fixture
def sample_ssr_html(fixtures_dir: Path) -> str:
    return (fixtures_dir / "zonaprop_ssr_sample.html").read_text(encoding="utf-8")


@pytest.fixture
def tmp_sqlite_url(tmp_path: Path) -> str:
    """Per-test SQLite file URL — keeps tests independent."""
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def settings_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Patch :func:`get_settings` so every test gets a fresh instance."""
    from app import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
async def fresh_db(monkeypatch: pytest.MonkeyPatch, tmp_sqlite_url: str) -> AsyncIterator[None]:
    """Point the engine at a temp database, init schema, tear down after."""
    from app import config
    from app.db import database

    monkeypatch.setenv("DATABASE_URL", tmp_sqlite_url)
    config.get_settings.cache_clear()
    await database.reset_engine()
    await database.init_db()
    try:
        yield
    finally:
        await database.reset_engine()
        config.get_settings.cache_clear()
