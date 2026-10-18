"""OSL public market data (no login needed)."""
from __future__ import annotations

from decimal import Decimal

import httpx

from .config import Config
from .util import dec


class OslPublic:
    def __init__(self, cfg: Config, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(base_url=cfg.osl.rest_base, timeout=5.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def ticker(self) -> tuple[Decimal, Decimal]:
        """(best bid, best ask) in USD."""
        resp = await self.client.get("/api/v5/book/ticker", params={"symbol": self.cfg.osl.symbol})
        resp.raise_for_status()
        payload = resp.json()
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        x = rows[0] if isinstance(rows, list) else rows
        return dec(x["bid"]), dec(x["ask"])
