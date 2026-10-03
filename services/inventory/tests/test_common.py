"""Behaviour every service must have: /health, request id, JSON access log, /metrics."""
import pytest

from common import testing
from common.settings import get_settings

pytestmark = pytest.mark.anyio


async def test_health(client):
    await testing.check_health(client, get_settings())


async def test_request_id_is_accepted_or_generated(client):
    await testing.check_request_id(client)


async def test_access_log_is_one_json_line_with_required_fields(client):
    await testing.check_json_access_log(client, get_settings())


async def test_metrics_expose_required_names(client):
    await testing.check_metrics(client)


async def test_ready_returns_503_with_details_when_postgres_is_down(client, fakes):
    db, _ = fakes
    db.down = True
    r = await client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"]["ok"] is False
    assert body["checks"]["redis"]["ok"] is True
