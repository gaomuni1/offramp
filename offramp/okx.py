"""OKX v5 REST client (only the endpoints this tool needs)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from .config import Config
from .models import Balance, Book, Instrument, Level, OkxError, OkxTimeout, OrderInfo, Position
from .util import ZERO, dec, fmt

ORDER_NOT_FOUND = "51603"


class OkxApi:
    def __init__(self, cfg: Config, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.creds = cfg.creds
        self.client = client or httpx.AsyncClient(base_url=cfg.okx.rest_base, timeout=cfg.okx.timeout_s)
        self._instruments: dict[str, Instrument] = {}

    async def close(self) -> None:
        await self.client.aclose()

    # ---- transport ----------------------------------------------------------------------------

    def _headers(self, method: str, path: str, body: str, auth: bool) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.creds.demo:
            headers["x-simulated-trading"] = "1"
        if auth:
            if not self.creds.present:
                raise SystemExit("没有 OKX API key。把 .env.example 复制成 .env,填好 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE。")
            now = datetime.now(timezone.utc)
            ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
            digest = hmac.new(self.creds.secret_key.encode(), (ts + method + path + body).encode(), hashlib.sha256)
            headers.update({
                "OK-ACCESS-KEY": self.creds.api_key,
                "OK-ACCESS-SIGN": base64.b64encode(digest.digest()).decode(),
                "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": self.creds.passphrase,
            })
        return headers

    async def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                       auth: bool = True) -> list:
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                path = f"{path}?{urlencode(clean)}"
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        # Reads are safe to retry. A write that times out has an unknown outcome: the caller must look it up.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            last = attempt + 1 == attempts
            try:
                resp = await self.client.request(method, path, content=body_str or None,
                                                 headers=self._headers(method, path, body_str, auth))
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if not last:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                raise OkxTimeout(f"{method} {path}: {e!r}") from e
            if resp.status_code >= 500 or resp.status_code == 429:
                if not last:
                    await asyncio.sleep(1.0)
                    continue
                if method != "GET":
                    raise OkxTimeout(f"{method} {path}: HTTP {resp.status_code}")
                raise OkxError(str(resp.status_code), resp.text[:200])
            try:
                payload = resp.json()
            except ValueError:
                if method != "GET":
                    raise OkxTimeout(f"{method} {path}: 无法解析写入结果,必须查询确认") from None
                raise OkxError(str(resp.status_code), resp.text[:200]) from None
            code = str(payload.get("code", ""))
            if code == "50011" and not last:  # rate limited
                await asyncio.sleep(1.0)
                continue
            data = payload.get("data") or []
            if code != "0":
                first = data[0] if data and isinstance(data[0], dict) else {}
                if first.get("sCode") not in (None, "", "0"):
                    raise OkxError(str(first["sCode"]), first.get("sMsg", ""))
                raise OkxError(code, payload.get("msg", ""))
            return data
        raise OkxTimeout(f"{method} {path}")  # not reached

    # ---- public -------------------------------------------------------------------------------

    async def server_time_ms(self) -> int:
        data = await self._request("GET", "/api/v5/public/time", auth=False)
        return int(data[0]["ts"])

    async def instrument(self, inst_type: str, inst_id: str) -> Instrument:
        if inst_id not in self._instruments:
            data = await self._request("GET", "/api/v5/public/instruments",
                                       {"instType": inst_type, "instId": inst_id}, auth=False)
            if not data:
                raise OkxError("instrument", f"找不到 {inst_id}")
            x = data[0]
            ct_val = dec(x.get("ctVal")) if inst_type != "SPOT" else Decimal(1)
            self._instruments[inst_id] = Instrument(inst_id, ct_val or Decimal(1), dec(x["lotSz"]),
                                                    dec(x["minSz"]), dec(x["tickSz"]))
        return self._instruments[inst_id]

    async def books(self, inst_id: str, sz: int = 5) -> Book:
        data = await self._request("GET", "/api/v5/market/books", {"instId": inst_id, "sz": sz}, auth=False)
        x = data[0] if data else {}

        def levels(rows):
            return [Level(dec(r[0]), dec(r[1]), int(r[3]) if len(r) > 3 and r[3] not in ("", None) else 0)
                    for r in rows]

        return Book(levels(x.get("bids", [])), levels(x.get("asks", [])))

    async def price_limit(self, inst_id: str) -> tuple[Decimal | None, Decimal | None]:
        """(highest allowed buy price, lowest allowed sell price)."""
        data = await self._request("GET", "/api/v5/public/price-limit", {"instId": inst_id}, auth=False)
        x = data[0] if data else {}
        return (dec(x["buyLmt"]) if x.get("buyLmt") else None, dec(x["sellLmt"]) if x.get("sellLmt") else None)

    async def system_status(self) -> list[dict]:
        return await self._request("GET", "/api/v5/system/status", auth=False)

    # ---- account ------------------------------------------------------------------------------

    async def account_config(self) -> dict:
        data = await self._request("GET", "/api/v5/account/config")
        return data[0]

    async def leverage(self, inst_id: str, mgn_mode: str) -> Decimal:
        data = await self._request("GET", "/api/v5/account/leverage-info", {"instId": inst_id, "mgnMode": mgn_mode})
        return dec(data[0]["lever"]) if data else ZERO

    async def set_leverage(self, inst_id: str, lever: int, mgn_mode: str) -> None:
        await self._request("POST", "/api/v5/account/set-leverage",
                            body={"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode})

    async def balances(self, ccys: list[str]) -> dict[str, Balance]:
        """Trading account."""
        data = await self._request("GET", "/api/v5/account/balance", {"ccy": ",".join(ccys)})
        out = {c: Balance(c) for c in ccys}
        for d in (data[0].get("details", []) if data else []):
            c = d.get("ccy", "")
            out[c] = Balance(c, dec(d.get("eq")), dec(d.get("cashBal")), dec(d.get("availBal")), dec(d.get("frozenBal")))
        return out

    async def funding_balances(self, ccys: list[str]) -> dict[str, Decimal]:
        """Funding account, available amount."""
        data = await self._request("GET", "/api/v5/asset/balances", {"ccy": ",".join(ccys)})
        out = {c: ZERO for c in ccys}
        for d in data:
            out[d.get("ccy", "")] = dec(d.get("availBal"))
        return out

    async def position(self, inst_id: str, pos_mode: str) -> Position:
        data = await self._request("GET", "/api/v5/account/positions", {"instType": "SWAP", "instId": inst_id})
        pos = Position(ZERO)
        for p in data:
            qty = dec(p.get("pos"))
            if qty == 0:
                continue
            side = p.get("posSide", "net")
            if side == "net":
                pos.short_ct += -qty
            elif side == "short":
                pos.short_ct += qty
            elif side == "long":
                pos.short_ct -= qty
            pos.avg_px = dec(p.get("avgPx"))
            pos.upl += dec(p.get("upl"))
            if p.get("mgnRatio") not in (None, ""):
                pos.mgn_ratio = dec(p["mgnRatio"])
            if p.get("liqPx") not in (None, ""):
                pos.liq_px = dec(p["liqPx"])
        return pos

    async def trade_fee(self, inst_type: str, inst_id: str | None = None,
                        inst_family: str | None = None) -> tuple[Decimal, Decimal]:
        """(maker, taker); OKX sign: negative = you pay."""
        data = await self._request("GET", "/api/v5/account/trade-fee",
                                   {"instType": inst_type, "instId": inst_id, "instFamily": inst_family})
        x = data[0]
        return dec(x.get("makerU") or x.get("maker")), dec(x.get("takerU") or x.get("taker"))

    async def funding_fee_sum(self, inst_id: str, begin_ms: int, end_ms: int) -> Decimal:
        """Sum of funding fees (positive = received) for inst_id between begin and end."""
        path = "/api/v5/account/bills"
        if time.time() * 1000 - begin_ms > 6.5 * 86400 * 1000:
            path = "/api/v5/account/bills-archive"
        total, after = ZERO, None
        for _ in range(50):
            data = await self._request("GET", path, {"instType": "SWAP", "ccy": "USDT", "type": "8",
                                                     "begin": begin_ms, "end": end_ms, "after": after,
                                                     "limit": 100})
            for b in data:
                if b.get("instId") == inst_id:
                    total += dec(b.get("balChg"))
            if len(data) < 100:
                break
            after = data[-1].get("billId")
        return total

    # ---- trading ------------------------------------------------------------------------------

    async def place_order(self, inst_id: str, td_mode: str, side: str, ord_type: str, sz: Decimal,
                          px: Decimal | None, cl_ord_id: str, reduce_only: bool = False,
                          pos_side: str | None = None) -> str:
        body = {"instId": inst_id, "tdMode": td_mode, "side": side, "ordType": ord_type,
                "sz": fmt(sz), "clOrdId": cl_ord_id}
        if px is not None:
            body["px"] = fmt(px)
        if reduce_only:
            body["reduceOnly"] = True
        if pos_side:
            body["posSide"] = pos_side
        data = await self._request("POST", "/api/v5/trade/order", body=body)
        x = data[0]
        if x.get("sCode") not in (None, "", "0"):
            raise OkxError(str(x["sCode"]), x.get("sMsg", ""))
        return x.get("ordId", "")

    async def cancel_order(self, inst_id: str, cl_ord_id: str) -> None:
        await self._request("POST", "/api/v5/trade/cancel-order", body={"instId": inst_id, "clOrdId": cl_ord_id})

    async def get_order(self, inst_id: str, cl_ord_id: str) -> OrderInfo | None:
        try:
            data = await self._request("GET", "/api/v5/trade/order", {"instId": inst_id, "clOrdId": cl_ord_id})
        except OkxError as e:
            if e.code == ORDER_NOT_FOUND:
                return None
            raise
        if not data:
            return None
        x = data[0]
        return OrderInfo(
            inst_id=inst_id, cl_ord_id=x.get("clOrdId") or cl_ord_id, ord_id=x.get("ordId", ""),
            side=x.get("side", ""), ord_type=x.get("ordType", ""),
            px=dec(x["px"]) if x.get("px") else None, sz=dec(x.get("sz")), state=x.get("state", ""),
            acc_fill_sz=dec(x.get("accFillSz")), avg_px=dec(x.get("avgPx")), fee=dec(x.get("fee")),
            fee_ccy=x.get("feeCcy", ""), pnl=dec(x.get("pnl")),
        )

    # ---- funding ------------------------------------------------------------------------------

    async def transfer(self, ccy: str, amt: Decimal, frm: str, to: str, client_id: str) -> str:
        """frm/to: '6' = funding account, '18' = trading account."""
        data = await self._request("POST", "/api/v5/asset/transfer",
                                   body={"ccy": ccy, "amt": fmt(amt), "from": frm, "to": to, "type": "0",
                                         "clientId": client_id})
        return data[0].get("transId", "")

    async def transfer_state(self, trans_id: str | None = None, client_id: str | None = None) -> str:
        data = await self._request("GET", "/api/v5/asset/transfer-state", {"transId": trans_id, "clientId": client_id})
        return data[0].get("state", "") if data else ""

    async def currency_chains(self, ccy: str) -> list[dict]:
        return await self._request("GET", "/api/v5/asset/currencies", {"ccy": ccy})
