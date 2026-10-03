import os

os.environ.update(
    SERVICE_NAME="gateway", APP_VERSION="9.9.9-test", LOG_LEVEL="INFO",
    ORDERS_URL="http://orders.test", PAYMENTS_URL="http://payments.test",
)

import httpx  # noqa: E402
import pytest  # noqa: E402

from common.http import create_http_client  # noqa: E402
from common.testing import make_client  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def install():
    """install(handler): the gateway's outbound calls are answered by `handler` instead of the network."""
    from app.main import app, settings

    app.state.settings = settings

    def _install(handler):
        app.state.http = create_http_client(settings, transport=httpx.MockTransport(handler))

    return _install


@pytest.fixture
async def client(install):
    from app.main import app

    install(lambda request: httpx.Response(200, json={}))
    async with make_client(app) as c:
        yield c
