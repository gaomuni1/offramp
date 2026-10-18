"""Local paper exchange: synthetic maker fills and book-based IOC fills.

Only explicitly listed read methods reach the real API. All account state and
orders live in memory. This exercises workflow, not real maker queue priority.
"""
from __future__ import annotations

from copy import copy
from decimal import Decimal

from .config import Config
from .models import Balance, Instrument, OkxError, OrderInfo, Position
from .util import ZERO, Console, floor_step, fmt


class DryRunConsole(Console):
    def __init__(self, real: Console):
        self.real = real

    def _write(self, level: str, msg: str, stamp: bool = True) -> None:
        self.real._write(level, f"[dry-run] {msg}", stamp)


class DryRunOkx:
    simulated = True

    def __init__(self, real, out: Console, paper_usdt: Decimal | None = None, cfg: Config | None = None):
        self._real = real
        self._out = out
        self.cfg = cfg or Config()
        self._orders: dict[str, OrderInfo] = {}
        self._cash = paper_usdt
        self._start_cash = paper_usdt
        self._eth = ZERO
        self._short_ct = ZERO
        self._entry_px = ZERO
        self._spot: Instrument | None = None
        self._swap: Instrument | None = None
        self._maker_fee = ZERO
        self._taker_fee = ZERO
        self._spot_fees = ZERO
        self._swap_fees = ZERO

    # Explicit read-only delegation; no generic forwarding to the real API.
    async def books(self, inst_id: str, sz: int = 5):
        return await self._real.books(inst_id, sz)

    async def trade_fee(self, *args, **kwargs):
        return await self._real.trade_fee(*args, **kwargs)

    async def price_limit(self, inst_id: str):
        return await self._real.price_limit(inst_id)

    async def initialize(self, spot: Instrument, swap: Instrument) -> None:
        self._spot, self._swap = spot, swap
        self._maker_fee, _ = await self.trade_fee("SPOT", inst_id=spot.inst_id)
        _, self._taker_fee = await self.trade_fee("SWAP", inst_family=self.cfg.okx.spot_inst)
        await self._ensure_cash()

    async def _ensure_cash(self) -> None:
        if self._cash is None:
            bal = (await self._real.balances(["USDT"]))["USDT"]
            self._cash = max(ZERO, min(bal.cash_bal, bal.eq, bal.avail_bal))
            self._start_cash = self._cash

    def seed_short(self, short_ct: Decimal, entry_px: Decimal) -> None:
        """Local starting position for a standalone close rehearsal; no opening order or fee."""
        if self._swap is None or self._orders or self._short_ct:
            raise ValueError("只能在初始化后、下单前设置模拟空单")
        if (not short_ct.is_finite() or short_ct < self._swap.min_sz
                or floor_step(short_ct, self._swap.lot_sz) != short_ct
                or not entry_px.is_finite() or entry_px <= 0):
            raise ValueError("模拟空单数量或价格无效")
        self._short_ct, self._entry_px = short_ct, entry_px
        self._cash = short_ct * self._swap.ct_val * entry_px / Decimal(self.cfg.okx.leverage) * Decimal("1.1") + 10
        self._start_cash = self._cash

    def _spot_frozen(self) -> Decimal:
        return sum(((o.sz - o.acc_fill_sz) * o.px for o in self._orders.values()
                    if o.ord_type == "post_only" and not o.terminal), ZERO)

    async def _mark(self) -> Decimal:
        if self._short_ct == 0:
            return ZERO
        book = await self.books(self.cfg.okx.swap_inst)
        px = book.best_ask or book.best_bid
        if not px:
            raise OkxError("book", "[dry-run] 永续盘口为空")
        return px  # conservative proxy, not the exchange's official mark price

    def _upl(self, mark: Decimal) -> Decimal:
        return (self._entry_px - mark) * self._short_ct * self._swap.ct_val if self._swap else ZERO

    async def balances(self, ccys: list[str]) -> dict[str, Balance]:
        await self._ensure_cash()
        mark = await self._mark()
        eq = self._cash + self._upl(mark)
        margin = self._short_ct * self._swap.ct_val * mark / Decimal(self.cfg.okx.leverage) if self._swap else ZERO
        frozen = self._spot_frozen() + margin
        values = {
            "USDT": Balance("USDT", eq, self._cash, max(ZERO, eq - frozen), frozen),
            "ETH": Balance("ETH", self._eth, self._eth, self._eth),
        }
        return {ccy: values.get(ccy, Balance(ccy)) for ccy in ccys}

    async def position(self, inst_id: str, pos_mode: str) -> Position:
        if inst_id != self.cfg.okx.swap_inst:
            raise ValueError("dry-run 仅支持配置中的永续合约")
        return Position(self._short_ct, avg_px=self._entry_px, upl=self._upl(await self._mark()))

    async def place_order(self, inst_id: str, td_mode: str, side: str, ord_type: str, sz: Decimal,
                          px: Decimal | None, cl_ord_id: str, reduce_only: bool = False,
                          pos_side: str | None = None) -> str:
        if self._spot is None or self._swap is None:
            raise RuntimeError("dry-run 必须先初始化规格和费率")
        if cl_ord_id in self._orders:
            raise OkxError("51016", "[dry-run] 重复订单号")
        spot_order = inst_id == self._spot.inst_id and ord_type == "post_only" and side == "buy" and td_mode == "cash"
        swap_order = inst_id == self._swap.inst_id and ord_type == "ioc" and side in ("buy", "sell") and td_mode == self.cfg.okx.margin_mode
        if not (spot_order or swap_order):
            raise ValueError("dry-run 不支持此订单类型,不会转发实盘")
        spec = self._spot if spot_order else self._swap
        if not sz.is_finite() or sz < spec.min_sz or floor_step(sz, spec.lot_sz) != sz:
            raise OkxError("51000", "[dry-run] 数量不符合最小量或步长")
        if px is None or not px.is_finite() or px <= 0 or floor_step(px, spec.tick_sz) != px:
            raise OkxError("51000", "[dry-run] 价格不符合步长")
        row = OrderInfo(inst_id, cl_ord_id, f"dry{cl_ord_id}", side, ord_type, px, sz, "live")
        if spot_order:
            book = await self.books(inst_id)
            if book.best_ask is None:
                raise OkxError("book", "[dry-run] 现货盘口为空")
            if px >= book.best_ask:
                row.state = "canceled"
            elif (await self.balances(["USDT"]))["USDT"].avail_bal < sz * px:
                raise OkxError("51008", "[dry-run] 虚拟现货余额不足")
        else:
            if side == "buy" and (not (reduce_only or pos_side == "short") or sz > self._short_ct):
                raise OkxError("51169", "[dry-run] 平空必须只减仓且不能超过当前空仓")
            if side == "sell" and (reduce_only or pos_side not in (None, "short")):
                raise ValueError("dry-run 只支持卖出开空")
            await self._fill_ioc(row)
        self._orders[cl_ord_id] = row
        self._out.info(f"[dry-run] 下单 {inst_id} {side} {ord_type} {fmt(sz)} @ {fmt(px)} ({row.state})")
        return row.ord_id

    async def _fill_ioc(self, row: OrderInfo) -> None:
        await self._ensure_cash()
        book = await self.books(row.inst_id)
        levels = book.bids if row.side == "sell" else book.asks
        filled, value = ZERO, ZERO
        for level in levels:
            if (row.side == "sell" and level.px < row.px) or (row.side == "buy" and level.px > row.px):
                break
            qty = floor_step(min(row.sz - filled, level.sz), self._swap.lot_sz)
            filled += qty
            value += qty * level.px
            if filled == row.sz:
                break
        row.state = "filled" if filled == row.sz else "canceled"
        if filled == 0:
            return
        avg = value / filled
        fee = value * self._swap.ct_val * self._taker_fee
        if row.side == "sell":
            new_short = self._short_ct + filled
            entry = (self._entry_px * self._short_ct + value) / new_short
            mark = book.best_ask or avg
            equity = self._cash + fee + (entry - mark) * new_short * self._swap.ct_val
            margin = new_short * self._swap.ct_val * mark / Decimal(self.cfg.okx.leverage)
            if equity < margin + self._spot_frozen():
                raise OkxError("51008", "[dry-run] 虚拟余额不足以支付保证金和手续费")
            self._entry_px, self._short_ct = entry, new_short
        else:
            row.pnl = (self._entry_px - avg) * filled * self._swap.ct_val
            self._cash += row.pnl
            self._short_ct -= filled
            if self._short_ct == 0:
                self._entry_px = ZERO
        self._cash += fee
        self._swap_fees += fee
        row.acc_fill_sz, row.avg_px, row.fee, row.fee_ccy = filled, avg, fee, "USDT"
        self._out.info(f"[dry-run] 模拟永续成交 {row.side} {fmt(filled)} 张 @ {fmt(avg)};手续费 {fmt(fee)} USDT")

    async def cancel_order(self, inst_id: str, cl_ord_id: str) -> None:
        order = self._orders.get(cl_ord_id)
        if order is not None and order.inst_id == inst_id and not order.terminal:
            order.state = "canceled"
            self._out.info(f"[dry-run] 撤单 {cl_ord_id}")

    async def get_order(self, inst_id: str, cl_ord_id: str) -> OrderInfo | None:
        order = self._orders.get(cl_ord_id)
        if order is None or order.inst_id != inst_id:
            return None
        if order.ord_type == "post_only" and not order.terminal:
            # Synthetic: each query fills half the ORIGINAL order, rounded to a lot.
            qty = min(order.sz - order.acc_fill_sz,
                      max(self._spot.lot_sz, floor_step(order.sz / 2, self._spot.lot_sz)))
            fee = qty * self._maker_fee
            self._cash -= qty * order.px
            self._eth += qty + fee
            self._spot_fees += fee
            order.acc_fill_sz += qty
            order.avg_px = order.px
            order.fee += fee
            order.fee_ccy = "ETH"
            order.state = "filled" if order.acc_fill_sz == order.sz else "partially_filled"
            self._out.info(f"[dry-run] 模拟现货成交 +{fmt(qty)} ETH @ {fmt(order.px)};手续费 {fmt(fee)} ETH")
        return copy(order)

    async def set_leverage(self, inst_id: str, lever: int, mgn_mode: str) -> None:
        raise RuntimeError("dry-run 使用本地配置杠杆,不调用账户设置")

    async def print_summary(self, reserve_usdt: Decimal | None = None) -> None:
        bal = (await self.balances(["USDT"]))["USDT"]
        short_eth = self._short_ct * self._swap.ct_val
        self._out.head("==== dry-run 模拟账户小结 ====")
        self._out.plain(f"[dry-run] 初始 USDT {fmt(self._start_cash, 2)},剩余现金 {fmt(bal.cash_bal, 4)},"
                        f"USDT 权益(不含现货市值) {fmt(bal.eq, 4)},可用 {fmt(bal.avail_bal, 4)},冻结/保证金 {fmt(bal.frozen_bal, 4)}")
        self._out.plain(f"[dry-run] 净现货 {fmt(self._eth)} ETH,杠杆 {self.cfg.okx.leverage}x,"
                        f"空单 {fmt(short_eth)} ETH,现货减空单尾差 {fmt(self._eth - short_eth)} ETH")
        self._out.plain(f"[dry-run] 现货手续费 {fmt(self._spot_fees)} ETH,永续手续费 {fmt(self._swap_fees)} USDT(负数为支出)")
        if reserve_usdt is not None:
            self._out.plain(f"[dry-run] 预留目标 {fmt(reserve_usdt)} USDT,这是计算目标,不是锁定余额")
            if bal.avail_bal < reserve_usdt:
                self._out.warn("[dry-run] 当前可用余额低于预留目标,价格变化或费用已消耗部分预留")
        self._out.warn("[dry-run] 模拟订单和仓位退出即丢弃,仅保留日志;不代表真实排队成交、成交速度或收益。未模拟资金费和强平")
