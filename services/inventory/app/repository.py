"""All SQL for the inventory service. Business rules live in routes.py."""
from common.db import Database

ITEM_COLUMNS = "sku, name, price, stock"


async def list_items(db: Database) -> list[dict]:
    return await db.fetch_all(f"SELECT {ITEM_COLUMNS} FROM items ORDER BY sku")


async def get_item(db: Database, sku: str) -> dict | None:
    return await db.fetch_one(f"SELECT {ITEM_COLUMNS} FROM items WHERE sku = %s", (sku,))


async def reserve(db: Database, sku: str, quantity: int) -> dict | None:
    """Atomic: the stock check and the decrement are ONE statement, so two concurrent
    requests can never both take the last units. Returns None if sku is unknown or stock is too low."""
    return await db.fetch_one(
        f"UPDATE items SET stock = stock - %s, updated_at = now() "
        f"WHERE sku = %s AND stock >= %s RETURNING {ITEM_COLUMNS}",
        (quantity, sku, quantity),
    )


async def release(db: Database, sku: str, quantity: int) -> dict | None:
    return await db.fetch_one(
        f"UPDATE items SET stock = stock + %s, updated_at = now() "
        f"WHERE sku = %s RETURNING {ITEM_COLUMNS}",
        (quantity, sku),
    )


async def item_exists(db: Database, sku: str) -> bool:
    return await db.fetch_one("SELECT 1 AS found FROM items WHERE sku = %s", (sku,)) is not None
