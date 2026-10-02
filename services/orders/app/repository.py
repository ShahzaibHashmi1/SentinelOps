"""All SQL for the orders service."""
from uuid import UUID

from common.db import Database

ORDER_COLUMNS = "id, sku, quantity, customer_id, amount, status, payment_id, created_at"


async def insert_pending(db: Database, sku: str, quantity: int, customer_id: str, amount: float) -> dict:
    row = await db.fetch_one(
        "INSERT INTO orders (sku, quantity, customer_id, amount, status) "
        f"VALUES (%s, %s, %s, round(%s::numeric, 2), 'PENDING') RETURNING {ORDER_COLUMNS}",
        (sku, quantity, customer_id, amount),
    )
    assert row is not None  # INSERT ... RETURNING always returns the new row
    return row


async def mark_paid(db: Database, order_id: UUID, payment_id: UUID) -> dict | None:
    return await db.fetch_one(
        "UPDATE orders SET status = 'PAID', payment_id = %s, updated_at = now() "
        f"WHERE id = %s RETURNING {ORDER_COLUMNS}",
        (payment_id, order_id),
    )


async def mark_failed(db: Database, order_id: UUID) -> int:
    return await db.execute(
        "UPDATE orders SET status = 'FAILED', updated_at = now() WHERE id = %s", (order_id,)
    )


async def get_order(db: Database, order_id: UUID) -> dict | None:
    return await db.fetch_one(f"SELECT {ORDER_COLUMNS} FROM orders WHERE id = %s", (order_id,))
