"""Calls from orders to the inventory and payments services."""
import logging

import httpx

from common.errors import AppError
from common.http import downstream_bad_response, request_downstream
from common.logging_setup import log_event
from common.settings import Settings

logger = logging.getLogger("orders.clients")


async def get_inventory(http: httpx.AsyncClient, settings: Settings, path: str):
    """GET {INVENTORY_URL}{path}. 404 is passed on; anything else unexpected becomes 502/504."""
    r = await request_downstream(http, "inventory", "GET", f"{settings.inventory_url}{path}")
    if r.status_code == 404:
        raise AppError(404, "item_not_found")
    if r.status_code != 200:
        raise downstream_bad_response("inventory", r)
    return r.json()


async def reserve_stock(http: httpx.AsyncClient, settings: Settings, sku: str, quantity: int) -> dict:
    """Step 1 of an order. Returns {sku, quantity, stock, price}."""
    r = await request_downstream(
        http, "inventory", "POST", f"{settings.inventory_url}/items/{sku}/reserve", json={"quantity": quantity}
    )
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        raise AppError(404, "item_not_found", sku=sku)
    if r.status_code == 409:
        raise AppError(409, "insufficient_stock", sku=sku, requested=quantity)
    raise downstream_bad_response("inventory", r)


async def release_stock(http: httpx.AsyncClient, settings: Settings, sku: str, quantity: int) -> None:
    """Best effort: undo a reservation. A failure is logged, never raised (the order is already failing)."""
    try:
        r = await request_downstream(
            http, "inventory", "POST", f"{settings.inventory_url}/items/{sku}/release", json={"quantity": quantity}
        )
        if r.status_code != 200:
            raise downstream_bad_response("inventory", r)
    except AppError as exc:
        log_event(
            logger, logging.WARNING, "stock_release_failed",
            sku=sku, quantity=quantity, error=exc.detail or exc.error,
        )


async def charge_payment(
    http: httpx.AsyncClient, settings: Settings, order_id: str, amount: float
) -> dict:
    """Step 3 of an order. The idempotency key is derived from the order id, so a retry
    of the same order can never charge twice. Returns the payment as a dict."""
    r = await request_downstream(
        http, "payments", "POST", f"{settings.payments_url}/payments/charge",
        json={"order_id": order_id, "amount": amount, "idempotency_key": f"order-{order_id}"},
    )
    if r.status_code in (200, 201):
        return r.json()
    raise downstream_bad_response("payments", r)
