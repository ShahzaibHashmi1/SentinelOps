"""Order workflow of the orders service: inventory and payments are mocked at the HTTP level."""
import uuid
from datetime import datetime, timezone

import httpx
import pytest

pytestmark = pytest.mark.anyio

ORDER_ID, PAYMENT_ID = uuid.uuid4(), uuid.uuid4()
NOW = datetime.now(timezone.utc)
NEW_ORDER = {"sku": "SKU-001", "quantity": 2, "customer_id": "cust-1"}


def order_row(status, payment_id=None):
    return {"id": ORDER_ID, "sku": "SKU-001", "quantity": 2, "customer_id": "cust-1",
            "amount": 21.0, "status": status, "payment_id": payment_id, "created_at": NOW}


def db_handler(sql, params):
    if sql.startswith("INSERT INTO orders"):
        return order_row("PENDING")
    if "SET status = 'PAID'" in sql:
        return order_row("PAID", PAYMENT_ID)
    if "SET status = 'FAILED'" in sql:
        return 1  # execute() row count


def make_downstream(payments_status=201):
    """Fake inventory + payments. Records (method, path, X-Request-ID) of every call."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("x-request-id")))
        if request.url.path == "/items/SKU-001/reserve":
            return httpx.Response(200, json={"sku": "SKU-001", "quantity": 2, "stock": 998, "price": 10.5})
        if request.url.path == "/items/SKU-001/release":
            return httpx.Response(200, json={"sku": "SKU-001", "quantity": 2, "stock": 1000, "price": 10.5})
        if request.url.path == "/payments/charge":
            if payments_status >= 500:
                return httpx.Response(payments_status, json={"error": "boom"})
            return httpx.Response(payments_status, json={"id": str(PAYMENT_ID)})
        return httpx.Response(404)

    return handler, seen


async def test_order_is_paid_amount_is_price_times_quantity_and_request_id_is_forwarded(client, wire):
    handler, seen = make_downstream()
    db = wire(handler)
    db.handler = db_handler
    r = await client.post("/orders", json=NEW_ORDER, headers={"X-Request-ID": "trace-42"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "PAID"
    assert body["payment_id"] == str(PAYMENT_ID)
    insert = next(c for c in db.calls if c[0].startswith("INSERT INTO orders"))
    assert insert[1][3] == 21.0  # 2 x 10.50
    assert [(m, p) for m, p, _ in seen] == [
        ("POST", "/items/SKU-001/reserve"), ("POST", "/payments/charge")]
    assert {rid for _, _, rid in seen} == {"trace-42"}  # same id on EVERY outbound call


async def test_failed_payment_releases_stock_marks_order_failed_and_returns_502(client, wire):
    handler, seen = make_downstream(payments_status=500)
    db = wire(handler)
    db.handler = db_handler
    r = await client.post("/orders", json=NEW_ORDER)
    assert r.status_code == 502
    body = r.json()
    assert body["downstream"] == "payments"
    assert body["order_id"] == str(ORDER_ID)
    assert ("POST", "/items/SKU-001/release") in [(m, p) for m, p, _ in seen]  # stock given back
    assert any("SET status = 'FAILED'" in sql for sql, _ in db.calls)


async def test_not_enough_stock_returns_409_and_creates_no_order(client, wire):
    def handler(request):
        return httpx.Response(409, json={"error": "insufficient_stock"})

    db = wire(handler)
    db.handler = db_handler
    r = await client.post("/orders", json=NEW_ORDER)
    assert r.status_code == 409
    assert r.json()["error"] == "insufficient_stock"
    assert not any(sql.startswith("INSERT INTO orders") for sql, _ in db.calls)


async def test_inventory_timeout_returns_504_with_downstream_name(client, wire):
    def handler(request):
        raise httpx.ReadTimeout("inventory too slow")

    wire(handler)
    r = await client.post("/orders", json=NEW_ORDER)
    assert r.status_code == 504
    assert r.json()["error"] == "downstream_timeout"
    assert r.json()["downstream"] == "inventory"


async def test_unknown_order_returns_404(client, wire):
    db = wire(lambda request: httpx.Response(500))
    db.handler = lambda sql, params: None
    r = await client.get(f"/orders/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"] == "order_not_found"
