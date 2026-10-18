"""Keeps the OKX ETH-USDT-SWAP short equal to a target, using IOC orders.

The caller always passes the *target* short (= ETH still held), never "close X more". Re-running align()
with the same target is harmless, which is what makes duplicates, restarts and retries safe.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from .config import Config
from .db import Db
from .models import AlignResult, HaltError, Instrument, OkxError, OkxTimeout, OrderRow, Position
from .util import ZERO, Console, ceil_step, floor_step, fmt, round_step


class Hedger:
    RESOLVE_TIMEOUT_S = 10.0   # how long to look for an order whose placement timed out
    POSITION_CONFIRM_S = 5.0   # how long to wait for the position to reflect a fill
    POLL_S = 0.3
    LIMIT_CACHE_S = 10.0

    def __init__(self, api, db: Db, cfg: Config, round_id: int, swap: Instrument, pos_mode: str, out: Console,
                 clock=time.time, sleep=asyncio.sleep):
        self.api = api
        self.db = db
        self.cfg = cfg
        self.round_id = round_id
        self.swap = swap
        self.pos_mode = pos_mode
        self.out = out
        self.clock = clock
        self.sleep = sleep
        self._limits: tuple[Decimal | None, Decimal | None] = (None, None)
        self._limits_at = float("-inf")

    # ---- conversions --------------------------------------------------------------------------

    def eth_to_ct(self, eth: Decimal) -> Decimal:
        """Target contracts for an ETH amount, rounded to the nearest lot (max error half a lot)."""
        if eth <= 0:
            return ZERO
        return round_step(eth / self.swap.ct_val, self.swap.lot_sz)

    def ct_to_eth(self, ct: Decimal) -> Decimal:
        return ct * self.swap.ct_val

    async def position(self) -> Position:
        return await self.api.position(self.cfg.okx.swap_inst, self.pos_mode)

    # ---- main entry ---------------------------------------------------------------------------

    async def recover_pending(self) -> None:
        # A prior request may have reached OKX even if reading its result
        # failed. Resolve it before looking at position and sending another.
        recovered_fill = False
        for row in self.db.open_orders(self.round_id):
            if row.purpose in ("hedge_open", "hedge_close"):
                resolved = await self.resolve(row)
                recovered_fill |= resolved is not None and resolved.acc_fill_sz > 0
        if recovered_fill:
            expected = sum((row.acc_fill_sz if row.purpose == "hedge_open" else -row.acc_fill_sz
                            for row in self.db.orders(self.round_id)
                            if row.purpose in ("hedge_open", "hedge_close")), ZERO)
            if await self._wait_position(expected) is None:
                raise HaltError("恢复对冲订单后仓位仍未确认,停止以避免重复下单")

    async def align(self, target_ct: Decimal, allow_open: bool, allow_close: bool,
                    max_attempts: int = 3) -> AlignResult:
        await self.recover_pending()
        res = AlignResult(False, Position(ZERO))
        for _ in range(max_attempts):
            pos = await self.position()
            res.pos = pos
            delta = pos.short_ct - target_ct
            if abs(delta) < self.swap.min_sz:
                res.ok = True
                return res
            if delta > 0 and not allow_close:
                res.anomaly = (f"OKX 空单 {fmt(self.ct_to_eth(pos.short_ct))} ETH,"
                               f"比应有的 {fmt(self.ct_to_eth(target_ct))} ETH 多")
                return res
            if delta < 0 and not allow_open:
                res.anomaly = (f"OKX 空单 {fmt(self.ct_to_eth(pos.short_ct))} ETH,"
                               f"比应有的 {fmt(self.ct_to_eth(target_ct))} ETH 少")
                return res
            side = "buy" if delta > 0 else "sell"
            sz = floor_step(abs(delta), self.swap.lot_sz)
            if side == "buy":
                # A reduce-only order larger than the position is rejected outright.
                sz = min(sz, floor_step(pos.short_ct, self.swap.lot_sz))
            if sz < self.swap.min_sz:
                res.ok = True
                return res
            row = await self._send_ioc(side, sz)
            if row is None or row.acc_fill_sz <= 0:
                self.out.warn(f"对冲单没有成交({row.state if row else '下单失败'}),用最新价格重试")
                await self.sleep(self.POLL_S)
                continue
            fill = row.acc_fill_sz
            expected = pos.short_ct - fill if side == "buy" else pos.short_ct + fill
            res.filled_ct += fill
            res.fills.append((fill, row.avg_px))
            verb = "买入平空" if side == "buy" else "卖出开空"
            self.out.info(f"OKX {verb} {fmt(self.ct_to_eth(fill))} ETH @ {fmt(row.avg_px)}")
            confirmed = await self._wait_position(expected)
            if confirmed is None:
                res.pos = await self.position()
                res.anomaly = (f"对冲单成交后,OKX 空单应为 {fmt(self.ct_to_eth(expected))} ETH,"
                               f"实际是 {fmt(self.ct_to_eth(res.pos.short_ct))} ETH")
                return res
            res.pos = confirmed
        res.pos = await self.position()
        res.ok = abs(res.pos.short_ct - target_ct) < self.swap.min_sz
        return res

    # ---- internals ----------------------------------------------------------------------------

    async def _price_limits(self) -> tuple[Decimal | None, Decimal | None]:
        if self.clock() - self._limits_at > self.LIMIT_CACHE_S:
            try:
                self._limits = await self.api.price_limit(self.cfg.okx.swap_inst)
                self._limits_at = self.clock()
            except (OkxError, OkxTimeout):
                pass
        return self._limits

    async def _ioc_price(self, side: str) -> Decimal:
        book = await self.api.books(self.cfg.okx.swap_inst)
        slip = self.cfg.okx.hedge_slippage_bps / Decimal(10000)
        buy_limit, sell_limit = await self._price_limits()
        if side == "buy":
            if book.best_ask is None:
                raise OkxError("book", "永续盘口没有卖单")
            px = book.best_ask * (1 + slip)
            if buy_limit is not None:
                px = min(px, buy_limit)
            return floor_step(px, self.swap.tick_sz)
        if book.best_bid is None:
            raise OkxError("book", "永续盘口没有买单")
        px = book.best_bid * (1 - slip)
        if sell_limit is not None:
            px = max(px, sell_limit)
        return ceil_step(px, self.swap.tick_sz)

    async def _send_ioc(self, side: str, sz: Decimal) -> OrderRow | None:
        px = await self._ioc_price(side)
        row = OrderRow(
            cl_ord_id=self.db.next_cl_ord_id(self.round_id, "h"), round_id=self.round_id,
            purpose="hedge_close" if side == "buy" else "hedge_open", inst_id=self.cfg.okx.swap_inst,
            side=side, ord_type="ioc", px=px, sz=sz, created_at=self.clock(),
        )
        self.db.insert_order(row)  # written before sending, so a crash can always find it
        long_short = self.pos_mode == "long_short_mode"
        try:
            row.ord_id = await self.api.place_order(
                self.cfg.okx.swap_inst, self.cfg.okx.margin_mode, side, "ioc", sz, px, row.cl_ord_id,
                reduce_only=(side == "buy" and not long_short), pos_side="short" if long_short else None,
            )
            row.state = "live"
        except OkxError as e:
            row.state = "rejected"
            self.db.save_order(row)
            self.out.error(f"对冲单被拒:{e}")
            return None
        except OkxTimeout:
            row.state = "unknown"
            self.out.warn("对冲下单请求超时,按订单号查结果")
        self.db.save_order(row)
        return await self.resolve(row)

    async def resolve(self, row: OrderRow) -> OrderRow | None:
        """Poll an order until it is final. None if it turns out it was never placed."""
        deadline = self.clock() + self.RESOLVE_TIMEOUT_S
        while True:
            missing = False
            try:
                info = await self.api.get_order(row.inst_id, row.cl_ord_id)
                missing = info is None
            except (OkxError, OkxTimeout):
                info = None
            if info is not None:
                if row.apply(info):
                    self.db.save_order(row)
                if row.terminal:
                    return row
            if self.clock() >= deadline:
                if row.state in ("unknown", "pending") and missing:
                    row.state = "not_placed"
                    self.db.save_order(row)
                    return None
                try:
                    await self.api.cancel_order(row.inst_id, row.cl_ord_id)
                except (OkxError, OkxTimeout):
                    pass
                raise HaltError(f"对冲单 {row.cl_ord_id} 的状态一直查不清,请到 OKX 确认这张单")
            await self.sleep(self.POLL_S)

    async def _wait_position(self, expected_ct: Decimal) -> Position | None:
        deadline = self.clock() + self.POSITION_CONFIRM_S
        while True:
            pos = await self.position()
            if abs(pos.short_ct - expected_ct) < self.swap.lot_sz / 2:
                return pos
            if self.clock() >= deadline:
                return None
            await self.sleep(self.POLL_S)
