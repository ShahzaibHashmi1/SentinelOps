"""Business rules of the payments service (idempotency), with postgres and redis faked."""
import uuid
from datetime import datetime, timezone

import pytest

from common.settings import get_settings
from common.testing import capture_logs

pytestmark = pytest.mark.anyio

ORDER_ID = str(uuid.uuid4())
BODY = {"order_id": ORDER_ID, "amount": 12.5, "idempotency_key": f"order-{ORDER_ID}"}


def payment_row():
    return {
        "id": uuid.uuid4(), "order_id": uuid.UUID(ORDER_ID), "amount": 12.5,
        "idempotency_key": BODY["idempotency_key"], "status": "SUCCEEDED",
        "created_at": datetime.now(timezone.utc),
    }


def insert_ok(sql, params):
    return payment_row() if sql.startswith("INSERT INTO payments") else None


async def test_charge_creates_a_succeeded_payment(client, fakes):
    db, redis = fakes
    db.handler = insert_ok
    r = await client.post("/payments/charge", json=BODY)
    assert r.status_code == 201
    assert r.json()["status"] == "SUCCEEDED"
    assert f"idem:{BODY['idempotency_key']}" in redis.data  # key stored for later replays


async def test_same_idempotency_key_returns_the_first_payment_without_charging_again(client, fakes):
    db, _ = fakes
    db.handler = insert_ok
    first = await client.post("/payments/charge", json=BODY)
    second = await client.post("/payments/charge", json=BODY)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.headers["idempotent-replay"] == "true"
    assert second.json()["id"] == first.json()["id"]
    inserts = [c for c in db.calls if c[0].startswith("INSERT INTO payments")]
    assert len(inserts) == 1  # postgres was written once


async def test_database_unique_key_also_prevents_a_double_charge_when_redis_misses(client, fakes):
    db, _ = fakes
    existing = payment_row()

    def handler(sql, params):
        if sql.startswith("INSERT INTO payments"):
            return None  # ON CONFLICT DO NOTHING: the key already exists
        if "WHERE idempotency_key" in sql:
            return existing

    db.handler = handler
    r = await client.post("/payments/charge", json=BODY)
    assert r.status_code == 200
    assert r.json()["id"] == str(existing["id"])


async def test_charge_still_works_when_redis_is_down_and_logs_a_warning(client, fakes):
    db, redis = fakes
    db.handler = insert_ok
    redis.down = True
    with capture_logs(get_settings()) as lines:
        r = await client.post("/payments/charge", json=BODY)
    assert r.status_code == 201
    warnings = [line for line in lines() if line["level"] == "WARNING"]
    assert any(line["event"] == "redis_unavailable" for line in warnings)


async def test_charge_rejects_a_non_positive_amount(client):
    r = await client.post("/payments/charge", json={**BODY, "amount": 0})
    assert r.status_code == 422
