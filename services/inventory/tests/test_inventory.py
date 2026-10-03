"""Business rules of the inventory service, with postgres and redis faked."""
import json
from decimal import Decimal

import pytest

from common.settings import get_settings
from common.testing import capture_logs

pytestmark = pytest.mark.anyio

ITEM = {"sku": "SKU-001", "name": "Product 001", "price": Decimal("6.99"), "stock": 1000}


def handler(sql, params):
    if sql.startswith("SELECT sku, name, price, stock FROM items WHERE sku"):
        return dict(ITEM)
    if sql.startswith("UPDATE items SET stock = stock - "):
        return None  # stock check failed: the single UPDATE matched no row
    if "SELECT 1 AS found" in sql:
        return {"found": 1}  # the sku exists, so the failure means "not enough stock"


async def test_reserve_more_than_stock_returns_409(client, fakes):
    db, _ = fakes
    db.handler = handler
    r = await client.post("/items/SKU-001/reserve", json={"quantity": 5000})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "insufficient_stock"
    assert body["request_id"] == r.headers["x-request-id"]


async def test_reserve_unknown_sku_returns_404(client, fakes):
    db, _ = fakes
    db.handler = lambda sql, params: None  # UPDATE matched nothing and the sku does not exist
    r = await client.post("/items/NOPE/reserve", json={"quantity": 1})
    assert r.status_code == 404
    assert r.json()["error"] == "item_not_found"


async def test_reserve_rejects_non_positive_quantity(client):
    r = await client.post("/items/SKU-001/reserve", json={"quantity": 0})
    assert r.status_code == 422


async def test_successful_reserve_returns_new_stock_and_clears_the_cache(client, fakes):
    db, redis = fakes
    redis.data["item:SKU-001"] = json.dumps({"stock": 1000})  # a cached copy exists
    db.handler = lambda sql, params: {**ITEM, "stock": 995} if sql.startswith("UPDATE items SET stock = stock - ") else None
    r = await client.post("/items/SKU-001/reserve", json={"quantity": 5})
    assert r.status_code == 200
    assert r.json() == {"sku": "SKU-001", "quantity": 5, "stock": 995, "price": 6.99}
    assert "item:SKU-001" not in redis.data  # invalidated, so the next read shows 995


async def test_get_item_is_cached_after_the_first_read(client, fakes):
    db, _ = fakes
    db.handler = handler
    first = await client.get("/items/SKU-001")
    second = await client.get("/items/SKU-001")
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert second.json()["sku"] == "SKU-001"
    assert len(db.calls) == 1  # the second read never touched postgres


async def test_get_item_falls_back_to_postgres_when_redis_is_down(client, fakes):
    db, redis = fakes
    db.handler = handler
    redis.down = True
    with capture_logs(get_settings()) as lines:
        r = await client.get("/items/SKU-001")
    assert r.status_code == 200
    assert r.headers["x-cache"] == "BYPASS"
    assert any(line["level"] == "WARNING" and line["event"] == "redis_unavailable" for line in lines())
