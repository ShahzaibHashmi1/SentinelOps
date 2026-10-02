"""All SQL for the payments service."""
from uuid import UUID

from common.db import Database

PAYMENT_COLUMNS = "id, order_id, amount, idempotency_key, status, created_at"


async def insert_payment(db: Database, order_id: UUID, amount: float, idempotency_key: str) -> dict | None:
    """Insert a SUCCEEDED payment. The UNIQUE idempotency_key makes a duplicate a no-op:
    returns None when a payment with this key already exists."""
    return await db.fetch_one(
        "INSERT INTO payments (order_id, amount, idempotency_key, status) "
        "VALUES (%s, round(%s::numeric, 2), %s, 'SUCCEEDED') "
        f"ON CONFLICT (idempotency_key) DO NOTHING RETURNING {PAYMENT_COLUMNS}",
        (order_id, amount, idempotency_key),
    )


async def get_by_key(db: Database, idempotency_key: str) -> dict | None:
    return await db.fetch_one(
        f"SELECT {PAYMENT_COLUMNS} FROM payments WHERE idempotency_key = %s", (idempotency_key,)
    )


async def get_payment(db: Database, payment_id: UUID) -> dict | None:
    return await db.fetch_one(f"SELECT {PAYMENT_COLUMNS} FROM payments WHERE id = %s", (payment_id,))
