import logging

from fastapi import APIRouter, Request, Response

from app import repository
from app.cache import cache_get, cache_invalidate, cache_set
from app.models import Item, QuantityRequest, StockChange
from common.errors import AppError
from common.logging_setup import log_event

logger = logging.getLogger("inventory")
router = APIRouter()


@router.get("/items", response_model=list[Item])
async def list_items(request: Request):
    return await repository.list_items(request.app.state.db)


@router.get("/items/{sku}", response_model=Item)
async def get_item(sku: str, request: Request, response: Response):
    """Read-through cache: Redis (TTL 30 s) first, postgres on a miss or when Redis is down."""
    db, redis = request.app.state.db, request.app.state.redis

    cache_state, item = await cache_get(redis, sku)
    if item is None:
        item = await repository.get_item(db, sku)
        if item is None:
            raise AppError(404, "item_not_found", sku=sku)
        if cache_state == "MISS":  # only try to fill the cache when Redis is reachable
            await cache_set(redis, item)

    # X-Cache is a debugging aid: HIT, MISS, or BYPASS (Redis unreachable, served from postgres).
    response.headers["X-Cache"] = "BYPASS" if cache_state == "ERROR" else cache_state
    return item


@router.post("/items/{sku}/reserve", response_model=StockChange)
async def reserve(sku: str, body: QuantityRequest, request: Request):
    db, redis = request.app.state.db, request.app.state.redis
    row = await repository.reserve(db, sku, body.quantity)
    if row is None:
        # Either the sku does not exist (404) or there is not enough stock (409).
        if not await repository.item_exists(db, sku):
            raise AppError(404, "item_not_found", sku=sku)
        raise AppError(409, "insufficient_stock", sku=sku, requested=body.quantity)
    await cache_invalidate(redis, sku)
    log_event(logger, logging.INFO, "stock_reserved", sku=sku, quantity=body.quantity, stock=row["stock"])
    return {"sku": sku, "quantity": body.quantity, "stock": row["stock"], "price": row["price"]}


@router.post("/items/{sku}/release", response_model=StockChange)
async def release(sku: str, body: QuantityRequest, request: Request):
    db, redis = request.app.state.db, request.app.state.redis
    row = await repository.release(db, sku, body.quantity)
    if row is None:
        raise AppError(404, "item_not_found", sku=sku)
    await cache_invalidate(redis, sku)
    log_event(logger, logging.INFO, "stock_released", sku=sku, quantity=body.quantity, stock=row["stock"])
    return {"sku": sku, "quantity": body.quantity, "stock": row["stock"], "price": row["price"]}
