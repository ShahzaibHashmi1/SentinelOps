import os

os.environ.update(
    SERVICE_NAME="orders", APP_VERSION="9.9.9-test", LOG_LEVEL="INFO",
    DATABASE_URL="postgresql://user:pw@localhost:5432/shop",
    INVENTORY_URL="http://inventory.test", PAYMENTS_URL="http://payments.test",
)

import httpx  # noqa: E402
import pytest  # noqa: E402

from common.http import create_http_client  # noqa: E402
from common.testing import FakeDb, make_client  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def wire():
    """Returns install(handler): puts a fake db and a MockTransport-backed http client on the app."""
    from app.main import app, settings

    app.state.settings = settings
    app.state.db = FakeDb()

    def install(http_handler):
        app.state.http = create_http_client(settings, transport=httpx.MockTransport(http_handler))
        return app.state.db

    return install


@pytest.fixture
async def client(wire):
    from app.main import app

    wire(lambda request: httpx.Response(500))  # default: tests that need real answers re-install
    async with make_client(app) as c:
        yield c
