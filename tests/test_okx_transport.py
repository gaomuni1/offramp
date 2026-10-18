import asyncio

import httpx
import pytest

from offramp.config import Config
from offramp.models import OkxTimeout
from offramp.okx import OkxApi


def test_invalid_write_response_is_unknown_not_rejected():
    async def scenario():
        client = httpx.AsyncClient(base_url="https://www.okx.com", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="upstream response lost")))
        api = OkxApi(Config(), client)
        try:
            with pytest.raises(OkxTimeout):
                await api._request("POST", "/api/v5/trade/order", body={}, auth=False)
        finally:
            await api.close()
    asyncio.run(scenario())
