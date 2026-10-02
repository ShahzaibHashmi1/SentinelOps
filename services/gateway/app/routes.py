"""Gateway routes: pure forwarding. Business rules and validation stay in the downstream services."""
from urllib.parse import quote

from fastapi import APIRouter, Request, Response

from common.http import request_downstream

router = APIRouter(prefix="/api")


async def forward(request: Request, downstream: str, method: str, path: str, body: bytes | None = None) -> Response:
    """Call a downstream service and hand its answer (status, body) back unchanged.

    If the downstream times out or cannot be reached, request_downstream raises an AppError:
    504 downstream_timeout / 502 downstream_unreachable with {error, downstream, request_id}.
    X-Request-ID is forwarded automatically by the http client (common/http.py).
    """
    settings = request.app.state.settings
    base_url = {"orders": settings.orders_url, "payments": settings.payments_url}[downstream]
    kwargs = {}
    if body is not None:
        kwargs = {"content": body, "headers": {"Content-Type": "application/json"}}
    r = await request_downstream(request.app.state.http, downstream, method, f"{base_url}{path}", **kwargs)
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
    )


@router.get("/products")
async def list_products(request: Request):
    return await forward(request, "orders", "GET", "/products")


@router.get("/products/{sku}")
async def get_product(sku: str, request: Request):
    return await forward(request, "orders", "GET", f"/products/{quote(sku, safe='')}")


@router.post("/orders")
async def create_order(request: Request):
    # The body is forwarded as-is; the orders service validates it (a bad body comes back as its 422).
    return await forward(request, "orders", "POST", "/orders", body=await request.body())


@router.get("/orders/{order_id}")
async def get_order(order_id: str, request: Request):
    return await forward(request, "orders", "GET", f"/orders/{quote(order_id, safe='')}")


@router.get("/payments/{payment_id}")
async def get_payment(payment_id: str, request: Request):
    return await forward(request, "payments", "GET", f"/payments/{quote(payment_id, safe='')}")
