"""Config loading and validation."""

from __future__ import annotations

import pytest

from app.config import Currency, OperationType, Settings


def test_csv_lists_parse_from_string() -> None:
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1, 2 ,3",
        property_types="departamentos, ph",
        neighborhoods="Palermo,Belgrano",
    )
    assert s.chat_ids == ["1", "2", "3"]
    assert s.property_types == ["departamentos", "ph"]
    assert s.neighborhoods == ["palermo", "belgrano"]


def test_property_types_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown value"):
        Settings(
            telegram_bot_token="t",
            telegram_chat_id="1",
            property_types="houses",
        )


def test_price_ranges_validated() -> None:
    with pytest.raises(ValueError, match="price_min > price_max"):
        Settings(
            telegram_bot_token="t",
            telegram_chat_id="1",
            price_min=1000,
            price_max=500,
        )


def test_rooms_ranges_validated() -> None:
    with pytest.raises(ValueError, match="rooms_min > rooms_max"):
        Settings(
            telegram_bot_token="t",
            telegram_chat_id="1",
            rooms_min=5,
            rooms_max=2,
        )


def test_enums_round_trip() -> None:
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        operation_type="venta",
        currency="USD",
    )
    assert s.operation_type is OperationType.VENTA
    assert s.currency is Currency.USD
