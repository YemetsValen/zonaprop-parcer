"""FastAPI route tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session
from app.db.models import SeenListing
from app.main import app


@pytest.mark.asyncio
async def test_health_reports_clean_state(fresh_db) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_checks"] >= 0
    assert body["listings_in_db"] == 0


@pytest.mark.asyncio
async def test_listings_endpoint_returns_rows(fresh_db) -> None:
    async with get_session() as session:
        session.add(
            SeenListing(
                listing_id="x1",
                url="https://example.com/x1",
                title="Sample",
                price=500000,
                currency="ARS",
                neighborhood="Palermo",
                scraped_at=datetime.now(tz=UTC),
            )
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/listings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["listing_id"] == "x1"


@pytest.mark.asyncio
async def test_check_requires_bearer_token(fresh_db) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/check")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_check_rejects_wrong_token(fresh_db) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/check", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_check_runs_with_valid_token(fresh_db) -> None:
    # Patch the scheduler so we don't hit the network.
    fake = AsyncMock()
    fake.return_value.started_at = datetime.now(tz=UTC)
    fake.return_value.finished_at = datetime.now(tz=UTC)
    fake.return_value.fetched = 0
    fake.return_value.new = 0
    fake.return_value.sent = 0
    fake.return_value.error = None

    with patch("app.api.routes.check_new_listings", fake):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/check", headers={"Authorization": "Bearer test-api-key"})
    assert resp.status_code == 200
    fake.assert_awaited_once()


@pytest.mark.asyncio
async def test_filters_endpoint_returns_search_url(fresh_db) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/filters")
    assert resp.status_code == 200
    body = resp.json()
    assert body["operation_type"] == "alquiler"
    assert "departamentos" in body["property_types"]
    assert body["search_url"].startswith("https://www.zonaprop.com.ar/")


@pytest.mark.asyncio
async def test_filters_update_in_memory(fresh_db) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/filters",
            headers={"Authorization": "Bearer test-api-key"},
            json={"price_min": 500000, "price_max": 1000000},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["price_min"] == 500000
    assert body["price_max"] == 1000000


@pytest.mark.asyncio
async def test_filters_update_rejects_invalid_range(fresh_db) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/filters",
            headers={"Authorization": "Bearer test-api-key"},
            json={"price_min": 1000000, "price_max": 500000},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_export_csv(fresh_db) -> None:
    async with get_session() as session:
        session.add(
            SeenListing(
                listing_id="x1",
                url="https://example.com/x1",
                title="Sample",
                price=500000,
                currency="ARS",
                neighborhood="Palermo",
                scraped_at=datetime.now(tz=UTC),
            )
        )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    text = resp.text
    assert "listing_id" in text
    assert "x1" in text
