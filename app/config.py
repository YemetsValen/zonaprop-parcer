"""Application settings, loaded from environment variables / .env.

Uses ``pydantic-settings`` so a single :class:`Settings` instance gives the
rest of the code typed access to configuration with one source of truth.

Validation rules live close to the fields (Pydantic validators) so an
invalid ``.env`` fails fast at startup with a clear error, not five minutes
later inside the scheduler loop.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class OperationType(StrEnum):
    ALQUILER = "alquiler"
    VENTA = "venta"


class Currency(StrEnum):
    ARS = "ARS"
    USD = "USD"


# ZonaProp URL slugs the scraper knows how to assemble. Adding a new property
# type = appending one entry here and bumping the regex in tests.
ALLOWED_PROPERTY_TYPES: frozenset[str] = frozenset({"departamentos", "casas", "ph"})


def _split_csv(value: str | list[str]) -> list[str]:
    """Parse ``"a, b ,c"`` / ``["a","b"]`` env values into a clean list."""
    if isinstance(value, list):
        return [v.strip() for v in value if str(v).strip()]
    if not value:
        return []
    return [chunk.strip() for chunk in str(value).split(",") if chunk.strip()]


class Settings(BaseSettings):
    """All runtime configuration. Instantiate via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- Telegram -----
    telegram_bot_token: str = Field(..., min_length=1)
    telegram_chat_id: str = Field(..., min_length=1)

    # ----- Search filters -----
    operation_type: OperationType = OperationType.ALQUILER
    # ``NoDecode`` tells pydantic-settings to hand us the raw env string;
    # our field_validator below converts CSV → list (it also accepts list
    # input for programmatic construction in tests).
    property_types: Annotated[
        list[str], NoDecode, Field(default_factory=lambda: ["departamentos"])
    ]
    neighborhoods: Annotated[list[str], NoDecode, Field(default_factory=list)]
    price_min: int = Field(0, ge=0)
    price_max: int = Field(0, ge=0)
    currency: Currency = Currency.ARS
    rooms_min: int = Field(0, ge=0)
    rooms_max: int = Field(0, ge=0)
    area_min: int = Field(0, ge=0)

    # ----- Scheduler -----
    check_interval_minutes: int = Field(15, ge=1)
    # Watchdog: 0 disables.
    watchdog_timeout_minutes: int = Field(30, ge=0)

    # ----- Security -----
    api_key: str = Field("", description="Bearer token for write endpoints.")

    # ----- Scraper -----
    use_playwright: bool = False
    request_timeout: int = Field(30, ge=1)

    # ----- Storage / app -----
    database_url: str = "sqlite+aiosqlite:///./data/zonaprop.db"
    log_level: str = "INFO"

    @field_validator("property_types", "neighborhoods", mode="before")
    @classmethod
    def _csv_lists(cls, value: object) -> list[str]:
        return _split_csv(value)  # type: ignore[arg-type]

    @field_validator("property_types")
    @classmethod
    def _validate_property_types(cls, value: list[str]) -> list[str]:
        # Lower-case for ZonaProp slugs; reject unknown values up front so the
        # URL builder doesn't quietly produce a 404 page.
        normalised = [v.lower() for v in value]
        unknown = [v for v in normalised if v not in ALLOWED_PROPERTY_TYPES]
        if unknown:
            allowed = ", ".join(sorted(ALLOWED_PROPERTY_TYPES))
            raise ValueError(
                f"property_types contains unknown value(s) {unknown!r}; allowed: {allowed}"
            )
        return normalised

    @field_validator("neighborhoods")
    @classmethod
    def _normalise_neighborhoods(cls, value: list[str]) -> list[str]:
        return [v.lower() for v in value]

    @field_validator("telegram_chat_id")
    @classmethod
    def _strip_chat_id(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_ranges(self) -> Settings:
        if self.price_max and self.price_min > self.price_max:
            raise ValueError("price_min > price_max")
        if self.rooms_max and self.rooms_min > self.rooms_max:
            raise ValueError("rooms_min > rooms_max")
        return self

    # ----- helpers -----
    @property
    def chat_ids(self) -> list[str]:
        """Telegram fan-out targets. Each entry stays a string because chat ids
        for groups/channels are negative (``-100…``) and easier as text."""
        return _split_csv(self.telegram_chat_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Tests call :func:`reload_settings` to reset."""
    return Settings()  # type: ignore[call-arg]


def reload_settings() -> Settings:
    """Drop the cached :class:`Settings`; used by tests via monkeypatch."""
    get_settings.cache_clear()
    return get_settings()
