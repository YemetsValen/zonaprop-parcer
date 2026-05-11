"""REST API for the ZonaProp bot.

Endpoints:
    GET  /health           — liveness + last-tick snapshot
    GET  /api/listings     — paginated list of seen listings
    POST /api/check        — run a tick immediately (API-key protected)
    GET  /api/filters      — current Settings derived filters
    PUT  /api/filters      — patch filters in-memory (no restart)
    GET  /api/export       — CSV dump of seen listings
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.config import ALLOWED_PROPERTY_TYPES, Currency, OperationType, Settings, get_settings
from app.db.database import get_session
from app.db.models import SeenListing
from app.scheduler import check_new_listings, get_state
from app.scraper.zonaprop import build_search_url

log = logging.getLogger(__name__)

router = APIRouter()
_security = HTTPBearer(auto_error=False)


# --- auth -------------------------------------------------------------------


def require_api_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Require ``Authorization: Bearer <API_KEY>`` on write endpoints.

    If the operator left ``API_KEY`` blank we treat the service as
    locked-down rather than open: every write returns 503. Production
    deployments should set a real key.
    """
    if not settings.api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_KEY is not configured; write endpoints are disabled.",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    if credentials.credentials != settings.api_key:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid api key")


# --- response models --------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    last_check_at: datetime | None
    last_success_at: datetime | None
    last_check_error: str | None
    last_check_new: int
    last_check_sent: int
    total_checks: int
    total_new_listings: int
    listings_in_db: int


class ListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    listing_id: str
    url: str
    title: str | None
    price: float | None
    currency: str | None
    neighborhood: str | None
    scraped_at: datetime
    notified_at: datetime | None


class ListingsPage(BaseModel):
    items: list[ListingOut]
    total: int
    limit: int
    offset: int


class FiltersView(BaseModel):
    operation_type: OperationType
    property_types: list[str]
    neighborhoods: list[str]
    price_min: int
    price_max: int
    currency: Currency
    rooms_min: int
    rooms_max: int
    area_min: int
    check_interval_minutes: int
    search_url: str


class FiltersPatch(BaseModel):
    """All fields optional; only provided ones are updated.

    Updates are in-memory: they survive until the next process restart but
    are not written to ``.env``. (We deliberately don't persist secrets-bearing
    env files from the app.)
    """

    operation_type: OperationType | None = None
    property_types: list[str] | None = None
    neighborhoods: list[str] | None = None
    price_min: int | None = Field(default=None, ge=0)
    price_max: int | None = Field(default=None, ge=0)
    currency: Currency | None = None
    rooms_min: int | None = Field(default=None, ge=0)
    rooms_max: int | None = Field(default=None, ge=0)
    area_min: int | None = Field(default=None, ge=0)


class CheckRunResponse(BaseModel):
    started_at: datetime
    finished_at: datetime
    fetched: int
    new: int
    sent: int
    error: str | None


# --- helpers ----------------------------------------------------------------


def _filters_view(settings: Settings) -> FiltersView:
    return FiltersView(
        operation_type=settings.operation_type,
        property_types=settings.property_types,
        neighborhoods=settings.neighborhoods,
        price_min=settings.price_min,
        price_max=settings.price_max,
        currency=settings.currency,
        rooms_min=settings.rooms_min,
        rooms_max=settings.rooms_max,
        area_min=settings.area_min,
        check_interval_minutes=settings.check_interval_minutes,
        search_url=build_search_url(settings),
    )


# --- endpoints --------------------------------------------------------------


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    state = get_state()
    async with get_session() as session:
        count = await session.scalar(select(func.count(SeenListing.id))) or 0

    last = state.last_check
    return HealthResponse(
        status="ok",
        last_check_at=last.finished_at if last else None,
        last_success_at=state.last_success,
        last_check_error=last.error if last else None,
        last_check_new=last.new if last else 0,
        last_check_sent=last.sent if last else 0,
        total_checks=state.total_checks,
        total_new_listings=state.total_new,
        listings_in_db=int(count),
    )


@router.get("/api/listings", response_model=ListingsPage, tags=["listings"])
async def list_listings(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ListingsPage:
    async with get_session() as session:
        total = await session.scalar(select(func.count(SeenListing.id))) or 0
        rows = await session.execute(
            select(SeenListing)
            .order_by(SeenListing.scraped_at.desc())
            .limit(limit)
            .offset(offset)
        )
        items = [ListingOut.model_validate(row) for row in rows.scalars().all()]
    return ListingsPage(items=items, total=int(total), limit=limit, offset=offset)


@router.post(
    "/api/check",
    response_model=CheckRunResponse,
    tags=["listings"],
    dependencies=[Depends(require_api_key)],
)
async def trigger_check() -> CheckRunResponse:
    """Run one scrape immediately. Use sparingly — ZonaProp rate-limits."""
    result = await check_new_listings()
    return CheckRunResponse(
        started_at=result.started_at,
        finished_at=result.finished_at,
        fetched=result.fetched,
        new=result.new,
        sent=result.sent,
        error=result.error,
    )


@router.get("/api/filters", response_model=FiltersView, tags=["filters"])
async def get_filters(settings: Annotated[Settings, Depends(get_settings)]) -> FiltersView:
    return _filters_view(settings)


@router.put(
    "/api/filters",
    response_model=FiltersView,
    tags=["filters"],
    dependencies=[Depends(require_api_key)],
)
async def update_filters(
    patch: FiltersPatch,
    settings: Annotated[Settings, Depends(get_settings)],
) -> FiltersView:
    data = patch.model_dump(exclude_unset=True)
    if "property_types" in data:
        unknown = [v for v in data["property_types"] if v not in ALLOWED_PROPERTY_TYPES]
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"unknown property_types: {unknown}",
            )
    if {"price_min", "price_max"} <= data.keys() and data["price_min"] > data["price_max"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="price_min > price_max")

    for key, value in data.items():
        setattr(settings, key, value)
    log.info("filters updated in-memory: %s", list(data))
    return _filters_view(settings)


@router.get("/api/export", tags=["listings"])
async def export_csv() -> Response:
    """CSV dump of every row in ``seen_listings``."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "listing_id",
            "url",
            "title",
            "price",
            "currency",
            "neighborhood",
            "scraped_at",
            "notified_at",
        ]
    )
    async with get_session() as session:
        rows = await session.execute(
            select(SeenListing).order_by(SeenListing.scraped_at.desc())
        )
        for row in rows.scalars().all():
            writer.writerow(
                [
                    row.listing_id,
                    row.url,
                    row.title or "",
                    row.price if row.price is not None else "",
                    row.currency or "",
                    row.neighborhood or "",
                    row.scraped_at.isoformat() if row.scraped_at else "",
                    row.notified_at.isoformat() if row.notified_at else "",
                ]
            )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="zonaprop_listings.csv"'},
    )


__all__ = ["router"]
