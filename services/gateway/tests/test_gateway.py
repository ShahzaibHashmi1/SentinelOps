"""Gateway: routing, request-id forwarding and the 504 / 502 error contract."""
import httpx
import pytest

pytestmark = pytest.mark.anyio


async def test_get_is_routed_to_orders_and_request_id_is_forwarded(client, install):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("x-request-id")))
        return httpx.Response(200, json={"sku": "SKU-001", "stock": 10})

    install(handler)
    r = await client.get("/api/products/SKU-001", headers={"X-Request-ID": "trace-7"})
    assert r.status_code == 200
    assert r.json() == {"sku": "SKU-001", "stock": 10}
    assert seen == [("http://orders.test/products/SKU-001", "trace-7")]


async def test_post_order_forwards_the_raw_body(client, install):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), request.content
        return httpx.Response(201, json={"status": "PAID"})

    install(handler)
    body = b'{"sku":"SKU-001","quantity":1,"customer_id":"c"}'
    r = await client.post("/api/orders", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 201
    assert (seen["url"], seen["body"]) == ("http://orders.test/orders", body)


async def test_payment_lookup_goes_to_payments(client, install):
    urls = []
    install(lambda request: (urls.append(str(request.url)), httpx.Response(200, json={}))[1])
    await client.get("/api/payments/abc")
    assert urls == ["http://payments.test/payments/abc"]


async def test_downstream_status_and_body_pass_through_unchanged(client, install):
    install(lambda request: httpx.Response(409, json={"error": "insufficient_stock", "sku": "SKU-001"}))
    r = await client.post("/api/orders", json={"sku": "SKU-001", "quantity": 9999, "customer_id": "c"})
    assert r.status_code == 409
    assert r.json() == {"error": "insufficient_stock", "sku": "SKU-001"}


async def test_downstream_timeout_returns_504_with_error_downstream_and_request_id(client, install):
    def handler(request):
        raise httpx.ReadTimeout("orders too slow")

    install(handler)
    r = await client.get("/api/products", headers={"X-Request-ID": "trace-9"})
    assert r.status_code == 504
    assert r.json() == {"error": "downstream_timeout", "request_id": "trace-9", "downstream": "orders"}


async def test_connection_error_returns_502_with_error_downstream_and_request_id(client, install):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    install(handler)
    r = await client.get("/api/payments/abc", headers={"X-Request-ID": "trace-10"})
    assert r.status_code == 502
    assert r.json() == {"error": "downstream_unreachable", "request_id": "trace-10", "downstream": "payments"}
