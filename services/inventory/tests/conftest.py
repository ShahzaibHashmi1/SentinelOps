import os

# Must be set BEFORE app.main is imported (it reads the settings at import time).
os.environ.update(
    SERVICE_NAME="inventory", APP_VERSION="9.9.9-test", LOG_LEVEL="INFO",
    DATABASE_URL="postgresql://user:pw@localhost:5432/shop", REDIS_URL="redis://localhost:6379/0",
)

import pytest  # noqa: E402

from common.testing import FakeDb, FakeRedis, make_client  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"  # run async tests on asyncio only


@pytest.fixture
def fakes():
    from app.main import app

    app.state.db, app.state.redis = FakeDb(), FakeRedis()
    return app.state.db, app.state.redis


@pytest.fixture
async def client(fakes):
    from app.main import app

    async with make_client(app) as c:
        yield c
