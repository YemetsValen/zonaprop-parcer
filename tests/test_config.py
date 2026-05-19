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


def test_daily_check_time_accepts_hh_mm() -> None:
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        daily_check_time="7:5",
    )
    # Normalised to zero-padded form so log lines / API reads stay consistent.
    assert s.daily_check_time == "07:05"


def test_daily_check_time_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        Settings(
            telegram_bot_token="t",
            telegram_chat_id="1",
            daily_check_time="every-morning",
        )


def test_daily_check_time_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="out of range"):
        Settings(
            telegram_bot_token="t",
            telegram_chat_id="1",
            daily_check_time="25:00",
        )


def test_has_city_level_location_true_for_capital_federal() -> None:
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        neighborhoods="capital-federal",
    )
    assert s.has_city_level_location is True


def test_has_city_level_location_false_for_barrios() -> None:
    s = Settings(
        telegram_bot_token="t",
        telegram_chat_id="1",
        neighborhoods="palermo,belgrano",
    )
    assert s.has_city_level_location is False
