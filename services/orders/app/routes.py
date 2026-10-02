import logging
from uuid import UUID

import psycopg
from fastapi import APIRouter, Request

from app import clients, repository
from app.models import CreateOrder, Order, Product
from common.errors import AppError, describe_exception
from common.logging_setup import log_event

logger = logging.getLogger("orders")
router = APIRouter()


@router.get("/products", response_model=list[Product])
async def list_products(request: Request):
    return await clients.get_inventory(request.app.state.http, request.app.state.settings, "/items")


@router.get("/products/{sku}", response_model=Product)
async def get_product(sku: str, request: Request):
    return await clients.get_inventory(request.app.state.http, request.app.state.settings, f"/items/{sku}")


@router.post("/orders", response_model=Order, status_code=201)
async def create_order(body: CreateOrder, request: Request):
    """Order workflow:
    1) reserve stock (inventory)  2) insert order PENDING (postgres)
    3) charge payment (payments)  4) mark order PAID.
    If anything after step 1 fails, the stock is released (best effort)."""
    db, http, settings = request.app.state.db, request.app.state.http, request.app.state.settings

    # 1) Reserve stock. Raises 404 (unknown sku), 409 (not enough stock) or 502/504 (inventory problem).
    reservation = await clients.reserve_stock(http, settings, body.sku, body.quantity)
    amount = round(reservation["price"] * body.quantity, 2)

    # 2) Insert the order as PENDING. If postgres is unavailable, give the stock back first.
    try:
        order = await repository.insert_pending(db, body.sku, body.quantity, body.customer_id, amount)
    except psycopg.OperationalError:
        await clients.release_stock(http, settings, body.sku, body.quantity)
        raise  # becomes a fast 503 through the database error handler
    order_id = str(order["id"])
    log_event(logger, logging.INFO, "order_created", order_id=order_id, sku=body.sku,
              quantity=body.quantity, amount=amount)

    # 3) Charge the payment. On failure: release stock, mark the order FAILED, return 502/504.
    try:
        payment = await clients.charge_payment(http, settings, order_id, amount)
    except AppError as exc:
        await _compensate(db, http, settings, order)
        exc.extra["order_id"] = order_id  # tell the caller which order failed
        raise

    # 4) Mark the order PAID and return it.
    paid = await repository.mark_paid(db, order["id"], UUID(payment["id"]))
    log_event(logger, logging.INFO, "order_paid", order_id=order_id, payment_id=payment["id"], amount=amount)
    return paid


async def _compensate(db, http, settings, order: dict) -> None:
    """Payment failed: give the stock back and mark the order FAILED. Both best effort."""
    await clients.release_stock(http, settings, order["sku"], order["quantity"])
    try:
        await repository.mark_failed(db, order["id"])
    except psycopg.OperationalError as exc:
        log_event(logger, logging.ERROR, "order_mark_failed_error",
                  order_id=str(order["id"]), error=describe_exception(exc))
        return
    log_event(logger, logging.WARNING, "order_failed", order_id=str(order["id"]), reason="payment_failed")


@router.get("/orders/{order_id}", response_model=Order)
async def get_order(order_id: UUID, request: Request):
    row = await repository.get_order(request.app.state.db, order_id)
    if row is None:
        raise AppError(404, "order_not_found", order_id=str(order_id))
    return row
