"""P1: buy ETH on OKX spot with post-only orders, and keep the ETH-USDT-SWAP short equal to the ETH bought.

One loop, one decision at a time: refresh the maker order -> market data -> hedge -> risk -> quote.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal

from .config import Config
from .allocation import affordable_qty
from .db import Db
from .hedger import Hedger
from .ledger import avg_fill_px, bought_eth, spent_usdt
from .models import Book, HaltError, Instrument, OkxError, OkxTimeout, OrderRow
from .util import ZERO, Console, floor_step, fmt


class BuyEngine:
    RESOLVE_S = 10.0
    CANCEL_RESEND_S = 3.0

    def __init__(self, api, db: Db, cfg: Config, round_id: int, spot: Instrument, swap: Instrument,
                 hedger: Hedger, out: Console, clock=time.time, sleep=asyncio.sleep):
        self.api = api
        self.simulated = bool(getattr(api, "simulated", False))
        self.db = db
        self.cfg = cfg
        self.round_id = round_id
        self.spot = spot
        self.swap = swap
        self.hedger = hedger
        self.out = out
        self.clock = clock
        self.sleep = sleep
        rnd = db.get_round(round_id)
        self.budget: Decimal = rnd.buy_budget or ZERO
        self.allocation = db.get_allocation(round_id)
        self.protected_cash = (max(ZERO, (rnd.start_usdt or self.allocation.total_usdt) - self.allocation.total_usdt)
                               if self.allocation else ZERO)
        if self.allocation and (cfg.okx.leverage != 1 or cfg.okx.margin_mode != "cross"):
            raise HaltError("总资金模式仅支持全仓 1x,请恢复配置后再继续")
        self.pause_reasons: dict[str, str] = {}
        self.hedge_failures = 0
        self.postonly_rejects = 0
        self.order_errors = 0
        self.cooldown_until = 0.0
        self.last_requote = float("-inf")
        self.last_market_ok = clock()
        self.unhedged_since: float | None = None
        self._stop = asyncio.Event()

    # ---- ledger -------------------------------------------------------------------------------

    def maker_orders(self) -> list[OrderRow]:
        return self.db.orders(self.round_id, "maker_buy")

    def held_eth(self) -> Decimal:
        return bought_eth(self.maker_orders())

    def spent(self) -> Decimal:
        return spent_usdt(self.maker_orders())

    # ---- lifecycle ----------------------------------------------------------------------------

    def request_stop(self) -> None:
        if not self._stop.is_set():
            self.out.warn("收到停止信号:撤单、对齐模拟空单后退出" if self.simulated else
                          "收到停止信号:撤单、对齐空单后退出(空单保留)")
        self._stop.set()

    async def run(self) -> str:
        """Returns 'done', 'stopped' or 'halted'."""
        try:
            await self.recover()
            while not self._stop.is_set():
                try:
                    result = await self.step()
                except (OkxError, OkxTimeout) as e:
                    # A failed order/position read must not leave a resting buy
                    # accumulating exposure while we cannot hedge it.
                    for row in self.db.open_orders(self.round_id, "maker_buy"):
                        try:
                            await self._cancel(row, "请求失败,暂停增加现货")
                        except (OkxError, OkxTimeout):
                            pass
                    self.out.warn(f"请求失败,下一轮重试:{e}")
                    result = None
                if result == "done":
                    await self.finish(stopped=False)
                    return "done"
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.buy.poll_s)
                except asyncio.TimeoutError:
                    pass
            await self.finish(stopped=True)
            return "stopped"
        except HaltError as e:
            await self.halt(str(e))
            return "halted"

    async def recover(self) -> None:
        """After a crash or Ctrl+C: settle every order we may have left behind."""
        for row in self.db.open_orders(self.round_id):
            if row.purpose == "maker_buy":
                await self._refresh(row)
                if not row.terminal:
                    await self._cancel(row, "恢复:撤掉上次留下的挂单")
                    await self._wait_terminal(row)
        await self.hedger.recover_pending()

    async def finish(self, stopped: bool) -> None:
        for row in self.db.open_orders(self.round_id, "maker_buy"):
            await self._cancel(row, "退出前撤单")
            await self._wait_terminal(row)
        held = self.held_eth()
        res = await self.hedger.align(self.hedger.eth_to_ct(held), allow_open=True, allow_close=False)
        if res.anomaly:
            raise HaltError(res.anomaly)
        if not res.ok:
            raise HaltError("退出时空单未对齐,本轮仍可用 buy resume 恢复")
        orders = self.maker_orders()
        avg = avg_fill_px(orders, "maker_buy")
        hedges = self.db.orders(self.round_id)
        short_avg = avg_fill_px(hedges, "hedge_open")
        self.out.head("")
        self.out.head("==== 买入小结 ====")
        if self.allocation:
            self.out.plain(f"总资金上限 {fmt(self.allocation.total_usdt)} USDT,预留目标 "
                           f"{fmt(self.allocation.reserve_usdt)} USDT;按实时资金能力动态分配")
        self.out.plain(f"买到 ETH(扣手续费后) {fmt(held, 6)}   花费 {fmt(self.spent(), 2)} USDT   均价 {fmt(avg, 2)}")
        self.out.plain(f"OKX 空单 {fmt(self.hedger.ct_to_eth(res.pos.short_ct))} ETH   开空均价 {fmt(short_avg, 2)}"
                       f"   {'✓ 合约步长内对齐(可能有尾差)' if res.ok else '⚠ 没对齐,用 offramp status 检查'}")
        if self.allocation and not self.simulated:
            try:
                available = (await self.api.balances(["USDT"]))["USDT"].avail_bal - self.protected_cash
                if available < self.allocation.reserve_usdt:
                    self.out.warn("当前可用余额扣除预算外保护资金后低于预留目标;价格变化或费用已消耗部分预留")
            except (OkxError, OkxTimeout):
                self.out.warn("未能刷新结束时的可用余额,请用 status 检查")
        if stopped:
            self.out.warn("演练已停止。模拟状态不保存,重新运行原 dry-run 命令可重新演练" if self.simulated else
                          "已停止。空单保留,对应已经买到的 ETH。继续买:offramp buy resume")
        elif res.ok:
            self.db.update_round(self.round_id, status="bought", buy_done_at=self.clock())
            self.out.ok("本轮模拟买入和对冲完成,见模拟账户小结;没有真实持仓" if self.simulated else
                        "本轮买入完成。先用 offramp status 核对现货、空单与尾差;仅测试买入时到这里停止")

    async def halt(self, msg: str) -> None:
        self.out.error(f"程序停止(HALTED):{msg}")
        self.db.log(self.round_id, "halt", msg)
        for row in self.db.open_orders(self.round_id, "maker_buy"):
            try:
                await self._cancel(row, "停止前撤单")
                await self._wait_terminal(row)
            except (OkxError, OkxTimeout, HaltError):
                self.out.error(f"无法确认买单 {row.cl_ord_id} 已撤,请立即到 OKX 检查并撤单")
        self.out.error("模拟流程停止,检查上述错误后重新演练;没有真实持仓" if self.simulated else
                       "空单保持原样,没有自动处理。请用 offramp status 检查;确认没问题后用 offramp buy resume 继续")

    # ---- one iteration ------------------------------------------------------------------------

    async def step(self) -> str | None:
        now = self.clock()
        maker = None
        for row in self.db.open_orders(self.round_id, "maker_buy"):
            await self._refresh(row)
            if not row.terminal:
                maker = row

        # 1. hedge first: short = ETH bought so far
        held = self.held_eth()
        res = await self.hedger.align(self.hedger.eth_to_ct(held), allow_open=True, allow_close=False)
        if res.anomaly:
            raise HaltError(res.anomaly)
        if res.ok:
            self.hedge_failures = 0
            self._resume("hedge")
        else:
            self.hedge_failures += 1
            await self._cancel(maker, "对冲未完成,先撤剩余买单")
            if self.hedge_failures >= self.cfg.risk.max_hedge_failures:
                self._pause("hedge", f"开空连续失败 {self.hedge_failures} 次")

        # 2. risk
        unhedged = abs(held - self.hedger.ct_to_eth(res.pos.short_ct))
        if unhedged > self.cfg.risk.max_unhedged_eth:
            if self.unhedged_since is None:
                self.unhedged_since = now
            elif now - self.unhedged_since > self.cfg.risk.max_unhedged_s:
                self._pause("unhedged", f"未对冲 {fmt(unhedged)} ETH 超过 {self.cfg.risk.max_unhedged_s:g} 秒")
        else:
            self.unhedged_since = None
            self._resume("unhedged")
        ratio = res.pos.mgn_ratio
        if res.pos.short_ct > 0 and ratio is not None and ratio < self.cfg.risk.min_margin_ratio:
            self._pause("margin", f"保证金率 {fmt(ratio, 2)} 低于 {fmt(self.cfg.risk.min_margin_ratio)},请补 USDT")
        else:
            self._resume("margin")

        if any(key != "stale" for key in self.pause_reasons):
            await self._cancel(maker, "暂停买入")
            return None
        if not res.ok:
            return None  # never add more ETH while the hedge is behind

        try:
            spot_book = await self.api.books(self.cfg.okx.spot_inst)
            swap_book = await self.api.books(self.cfg.okx.swap_inst)
        except (OkxError, OkxTimeout):
            self._pause("stale", "行情读取失败")
            await self._cancel(maker, "行情中断")
            return None
        self.last_market_ok = self.clock()
        self._resume("stale")

        # 3. quote
        return await self._quote(maker, spot_book, swap_book, held, now)

    # ---- quoting ------------------------------------------------------------------------------

    def _cap(self, swap_book: Book) -> Decimal | None:
        """Highest spot price we accept: perp bid plus the allowed entry basis."""
        if swap_book.best_bid is None:
            return None
        return floor_step(swap_book.best_bid * (1 + self.cfg.buy.max_entry_basis_bps / Decimal(10000)),
                          self.spot.tick_sz)

    def desired_px(self, spot_book: Book, swap_book: Book, maker: OrderRow | None) -> Decimal | None:
        own_px = maker.px if maker is not None else None
        own_rem = maker.remaining if maker is not None else ZERO
        bid = spot_book.best_bid_excluding(own_px, own_rem)  # others' best bid, not our own order
        ask = spot_book.best_ask
        if bid is None or ask is None:
            return None
        tick = self.spot.tick_sz
        px = bid
        if self.cfg.buy.price_mode == "improve" and ask - bid >= 2 * tick:
            px = bid + tick
        cap = self._cap(swap_book)
        if cap is not None:
            px = min(px, cap)
        px = floor_step(min(px, ask - tick), tick)
        return px if px > 0 else None

    async def _quote(self, maker: OrderRow | None, spot_book: Book, swap_book: Book, held: Decimal,
                     now: float) -> str | None:
        b = self.cfg.buy
        if maker is not None:
            if maker.cancel_requested_at is not None:
                if now - maker.cancel_requested_at > self.CANCEL_RESEND_S:
                    await self._cancel(maker, "重发撤单")
                return None
            if maker.px is None:
                return None
            if self.allocation:
                # The order was sized to the full cap; let it use half the reserve as slack so a large
                # resting order is not cancelled (losing queue priority) on every perp uptick.
                capacity = await self._allocation_capped(maker.remaining, maker.px, held, swap_book,
                                                         own_frozen=maker.remaining * maker.px,
                                                         reserve_usdt=self.allocation.reserve_usdt / 2)
                if capacity < maker.remaining:
                    await self._cancel(maker, "资金或价格变化,重新计算总资金分配")
                    return None
            cap = self._cap(swap_book)
            desired = self.desired_px(spot_book, swap_book, maker)
            reason = None
            if cap is not None and maker.px > cap:
                reason = f"挂单价 {fmt(maker.px)} 高于上限 {fmt(cap)}(永续下跌)"
            elif now - maker.created_at > b.max_order_age_s:
                reason = "挂单时间太长,刷新"
            elif desired is not None and now - self.last_requote >= b.requote_min_interval_s:
                threshold = max(self.spot.tick_sz, maker.px * b.requote_bps / Decimal(10000))
                if abs(desired - maker.px) >= threshold:
                    reason = f"跟价 {fmt(maker.px)} → {fmt(desired)}"
            if reason:
                await self._cancel(maker, reason)
            return None

        if now < self.cooldown_until:
            return None
        budget_left = self.budget - self.spent()
        if self.allocation:
            budget_left = self.allocation.total_usdt - self.allocation.reserve_usdt - self.spent()
        desired = self.desired_px(spot_book, swap_book, None)
        ref_px = desired or spot_book.best_ask
        if ref_px is None:
            return None
        if budget_left <= 0 or floor_step(budget_left / ref_px, self.spot.lot_sz) < self.spot.min_sz:
            return "done"
        if desired is None:
            return None
        qty = floor_step(min(budget_left / desired, b.max_slice_eth), self.spot.lot_sz)
        qty = await self._margin_capped(qty, desired, held, swap_book)
        if qty < self.spot.min_sz:
            self.out.warn("扣除保证金、费用和预留后,剩余资金不足再下一笔最小现货单" if self.allocation else
                          "交易账户 USDT 不够同时买 ETH 和留 1x 空单保证金,本轮买入到此为止")
            return "done"
        await self._place_maker(desired, qty, now)
        return None

    async def _margin_capped(self, qty: Decimal, px: Decimal, held: Decimal, swap_book: Book) -> Decimal:
        """Spot buys freeze USDT, and frozen USDT cannot back the short. Keep enough for the full hedge:
        equity - qty*px >= (held + qty) * perp_px / leverage * (1 + buffer)."""
        if self.allocation:
            return await self._allocation_capped(qty, px, held, swap_book)
        bal = (await self.api.balances(["USDT"]))["USDT"]
        perp_px = swap_book.best_ask or swap_book.best_bid or px
        per_eth = perp_px / Decimal(self.cfg.okx.leverage) * (1 + self.cfg.buy.margin_buffer)
        max_qty = (bal.eq - held * per_eth) / (px + per_eth)
        # Rounding to the nearest contract can require up to half a lot
        # more margin, a material amount for a 20 USDT smoke test.
        rounding_reserve = self.swap.lot_sz * self.swap.ct_val / 2 * per_eth
        max_qty = min(max_qty, (bal.eq - held * per_eth - rounding_reserve) / (px + per_eth),
                      bal.avail_bal / px)
        return max(min(qty, floor_step(max_qty, self.spot.lot_sz)), ZERO)

    async def _allocation_capped(self, qty: Decimal, px: Decimal, held: Decimal, swap_book: Book,
                                 own_frozen: Decimal = ZERO, reserve_usdt: Decimal | None = None) -> Decimal:
        if not swap_book.best_bid or not swap_book.best_ask:
            raise OkxError("book", "永续盘口为空,无法计算总资金分配")
        bal = (await self.api.balances(["USDT"]))["USDT"]
        pos = await self.hedger.position()
        orders = self.db.orders(self.round_id)
        usdt_fees = sum((o.fee for o in orders if o.fee_ccy == "USDT"), ZERO)
        # Never spend other account funds or deposits outside the declared cap.
        allocated_cash = self.allocation.total_usdt - self.spent() + usdt_fees
        margin = pos.short_ct * self.swap.ct_val * swap_book.best_ask
        equity = min(allocated_cash + pos.upl, bal.eq, bal.cash_bal - self.protected_cash + pos.upl,
                     bal.avail_bal + own_frozen + margin)
        allocation = self.allocation
        if reserve_usdt is not None:
            allocation = replace(allocation, reserve_usdt=reserve_usdt)
        need = affordable_qty(max_qty=qty, equity=equity, spot=self.spot, allocation=allocation,
                              swap=self.swap, spot_px=px, perp_bid=swap_book.best_bid,
                              perp_ask=swap_book.best_ask, held=held, short_ct=pos.short_ct,
                              slippage_bps=self.cfg.okx.hedge_slippage_bps)
        return need.qty

    async def _place_maker(self, px: Decimal, qty: Decimal, now: float) -> None:
        row = OrderRow(cl_ord_id=self.db.next_cl_ord_id(self.round_id, "m"), round_id=self.round_id,
                       purpose="maker_buy", inst_id=self.cfg.okx.spot_inst, side="buy", ord_type="post_only",
                       px=px, sz=qty, created_at=now)
        self.db.insert_order(row)
        self.last_requote = now
        try:
            row.ord_id = await self.api.place_order(self.cfg.okx.spot_inst, "cash", "buy", "post_only", qty, px,
                                                    row.cl_ord_id)
            row.state = "live"
            self.order_errors = 0
            detail = (f"现货已花 {fmt(self.spent(), 2)},总资金上限 {fmt(self.allocation.total_usdt)} USDT"
                      if self.allocation else f"已花 {fmt(self.spent(), 2)} / {fmt(self.budget)} USDT")
            self.out.info(f"挂买 {fmt(qty)} ETH @ {fmt(px)}({detail})")
        except OkxError as e:
            row.state = "rejected"
            self.order_errors += 1
            self.out.warn(f"买单被拒:{e}")
            if self.order_errors >= self.cfg.risk.max_order_errors:
                self.cooldown_until = now + self.cfg.buy.postonly_cooldown_s
                self.order_errors = 0
                self.out.warn(f"连续被拒,暂停挂单 {self.cfg.buy.postonly_cooldown_s:g} 秒")
        except OkxTimeout:
            row.state = "unknown"
            self.out.warn("挂单请求超时,下一轮按订单号查")
        self.db.save_order(row)

    # ---- order bookkeeping --------------------------------------------------------------------

    async def _refresh(self, row: OrderRow) -> None:
        before_fill = row.acc_fill_sz
        was_terminal = row.terminal
        info = await self.api.get_order(row.inst_id, row.cl_ord_id)
        if info is None:
            if row.state in ("unknown", "pending") and self.clock() - row.created_at > self.RESOLVE_S:
                row.state = "not_placed"
                self.db.save_order(row)
                self.out.warn(f"订单 {row.cl_ord_id} 查不到,确认没有下出去")
            return
        if row.apply(info):
            self.db.save_order(row)
        if row.acc_fill_sz > before_fill:
            self.postonly_rejects = 0
            self.out.info(f"现货成交 +{fmt(row.acc_fill_sz - before_fill)} ETH @ {fmt(row.avg_px)}"
                          f"(这张单 {fmt(row.acc_fill_sz)}/{fmt(row.sz)})")
        if row.terminal and not was_terminal and row.acc_fill_sz == 0 and row.cancel_requested_at is None \
                and row.state == "canceled":
            # Cancelled by the exchange right away: a post-only order that would have taken liquidity.
            self.postonly_rejects += 1
            if self.postonly_rejects >= self.cfg.risk.max_postonly_rejects:
                self.postonly_rejects = 0
                self.cooldown_until = self.clock() + self.cfg.buy.postonly_cooldown_s
                self.out.warn(f"只挂单(post-only)连续被撤,暂停挂单 {self.cfg.buy.postonly_cooldown_s:g} 秒")

    async def _cancel(self, row: OrderRow | None, reason: str) -> None:
        if row is None or row.terminal:
            return
        if row.cancel_requested_at is None:
            self.out.info(f"撤单:{reason}")
        row.cancel_requested_at = self.clock()
        self.last_requote = self.clock()
        self.db.save_order(row)
        try:
            await self.api.cancel_order(row.inst_id, row.cl_ord_id)
        except OkxError:
            await self._refresh(row)  # already filled / cancelled / unknown: read the final state
        except OkxTimeout:
            pass  # cancel_requested_at makes the next round resend it

    async def _wait_terminal(self, row: OrderRow) -> None:
        deadline = self.clock() + self.RESOLVE_S
        while not row.terminal:
            await self._refresh(row)
            if row.terminal:
                return
            if self.clock() >= deadline:
                raise HaltError(f"买单 {row.cl_ord_id} 撤不掉也查不清,请到 OKX 确认")
            if self.clock() - (row.cancel_requested_at or 0) > self.CANCEL_RESEND_S:
                await self._cancel(row, "重发撤单")
            await self.sleep(0.3)

    # ---- pause bookkeeping --------------------------------------------------------------------

    def _pause(self, key: str, msg: str) -> None:
        if key not in self.pause_reasons:
            self.out.warn(f"暂停买入:{msg}")
            self.db.log(self.round_id, "pause", msg)
        self.pause_reasons[key] = msg

    def _resume(self, key: str) -> None:
        if key in self.pause_reasons:
            del self.pause_reasons[key]
            self.out.ok("暂停条件已解除")
