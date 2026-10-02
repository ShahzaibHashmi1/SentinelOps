import asyncio
import logging
import random
from uuid import UUID

from fastapi import APIRouter, Request, Response

from app import idempotency, repository
from app.models import ChargeRequest, Payment
from common.errors import AppError
from common.logging_setup import log_event

logger = logging.getLogger("payments")
router = APIRouter()


@router.post("/payments/charge", response_model=Payment, status_code=201)
async def charge(body: ChargeRequest, request: Request, response: Response):
    """Simulated payment. 201 = new payment, 200 = replay of an earlier charge with the same key."""
    db, redis, settings = request.app.state.db, request.app.state.redis, request.app.state.settings

    # 1) Fast path: this idempotency key was already processed.
    cache_state, cached = await idempotency.lookup(redis, body.idempotency_key)
    if cached is not None:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
        return cached

    # 2) Simulated processing delay (random between PAYMENT_DELAY_MS_MIN and _MAX).
    delay_ms = random.uniform(settings.payment_delay_ms_min, settings.payment_delay_ms_max)
    await asyncio.sleep(delay_ms / 1000)

    # 3) Store the payment. If the key already exists (Redis missed it, or two requests raced),
    #    the INSERT does nothing and we return the stored payment instead of charging twice.
    row = await repository.insert_payment(db, body.order_id, body.amount, body.idempotency_key)
    replay = row is None
    if replay:
        row = await repository.get_by_key(db, body.idempotency_key)
    payment = Payment.model_validate(row)

    if cache_state == "MISS":  # only write to Redis when it is reachable
        await idempotency.remember(redis, payment)

    if replay:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    else:
        log_event(
            logger, logging.INFO, "payment_succeeded",
            payment_id=str(payment.id), order_id=str(payment.order_id),
            amount=payment.amount, delay_ms=round(delay_ms, 1),
        )
    return payment


@router.get("/payments/{payment_id}", response_model=Payment)
async def get_payment(payment_id: UUID, request: Request):
    row = await repository.get_payment(request.app.state.db, payment_id)
    if row is None:
        raise AppError(404, "payment_not_found", payment_id=str(payment_id))
    return row
