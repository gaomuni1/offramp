"""P3: manually report OSL fills, then close the corresponding OKX short.

This assistant does not connect to your OSL account; explicit inputs are its only fill signal.
Target short = ETH arrived on OSL - ETH you reported sold. Corrections (fix / undo) just move the target.
"""
from __future__ import annotations

import re
import time
from decimal import Decimal

from .config import Config
from .db import Db
from .hedger import Hedger
from .ledger import bought_eth
from .models import AlignResult, OkxError, OkxTimeout
from .util import ZERO, Console, dec, floor_step, fmt

_QTY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?|\.\d+)\s*(?:@\s*(\d+(?:\.\d+)?))?\s*$")

HELP = """命令:
  回车           按屏幕上「本笔」的数量记为已卖,并平掉同样数量的空单
  0.01 @2480.5   记卖出 0.01 ETH(@均价可省略),并平空
  fix 0.02       把「已卖」总数改成 0.02(输错时用)
  undo           撤销上一次输入
  m              刷新行情
  q              退出(空单保留,随时可以再运行 offramp sell 继续)"""

MAKER_HELP = """OSL 手动 maker 模式(只平空):
  sold 0.2       本轮累计实际卖出 0.2 ETH(跨所有订单累计,重复输入不会重复平空)
                 本次新增超过 0.1 ETH 时会显示要平的数量,输入 y 确认
  fix 0.1 / undo 更正记录;不会重新开空,少了的对冲需另行处理
  m              刷新行情,重试尚未完成的平空
  q              退出;实盘空单保留,模拟状态丢弃
挂单未成交不要报 sold;部分成交只报已成交数。空回车不会记录成交。"""


async def _stdin_ask(prompt: str) -> str:
    # Blocking on purpose: nothing else runs while we wait, and Ctrl+C then exits immediately
    # (a worker thread stuck in input() would keep the process alive).
    try:
        return input(prompt)
    except EOFError:
        return "q"


class SellAssistant:
    def __init__(self, api, osl, db: Db, cfg: Config, round_id: int, hedger: Hedger, out: Console,
                 ask=_stdin_ask, clock=time.time, maker: bool = False):
        self.api = api
        self.osl = osl
        self.db = db
        self.cfg = cfg
        self.round_id = round_id
        self.hedger = hedger
        self.out = out
        self.ask = ask
        self.clock = clock
        self.maker = maker

    # ---- numbers ------------------------------------------------------------------------------

    def arrived(self) -> Decimal:
        return self.db.get_round(self.round_id).arrived_eth or ZERO

    def sold(self) -> Decimal:
        return self.db.sold_total(self.round_id)

    def remaining(self) -> Decimal:
        return self.arrived() - self.sold()

    def chunk(self, remaining: Decimal) -> Decimal:
        c = min(remaining, self.cfg.sell.chunk_eth)
        if remaining - c < self.cfg.osl.min_qty:
            c = remaining  # don't leave an unsellable tail
        return floor_step(c, self.cfg.osl.qty_step)

    # ---- flow ---------------------------------------------------------------------------------

    async def run(self, close_first: bool = False) -> str:
        """Returns 'done', 'stopped' or 'aborted'."""
        if self.maker and close_first:
            raise ValueError("OSL maker 模式必须成交后平空,不能 close-first")
        rnd = self.db.get_round(self.round_id)
        if rnd.status not in ("bought", "selling"):
            self.out.error(f"第 {self.round_id} 轮现在是「{rnd.status}」状态,买入完成后才能卖")
            return "aborted"
        if rnd.arrived_eth is None and not await self._ask_arrived():
            return "aborted"
        await self._align(initial=True)
        self.out.plain(MAKER_HELP if self.maker else HELP)
        while True:
            remaining = self.remaining()
            if remaining < self.cfg.osl.min_qty:
                if await self._finish(remaining):
                    return "done"
                line = (await self.ask("平空未完成:输入 m 重试,q 退出保留未平仓位> ")).strip().lower()
                if line != "m":
                    return "stopped"
                continue
            chunk = self.chunk(remaining)
            await self._show(remaining, chunk, close_first)
            line = (await self.ask("> ")).strip()
            low = line.lower()
            if low in ("q", "quit", "exit"):
                if getattr(self.api, "simulated", False):
                    self.out.warn(f"退出模拟。还剩 {fmt(remaining)} ETH 未模拟卖出;本地状态将丢弃")
                else:
                    command = "offramp sell --maker" if self.maker else "offramp sell"
                    self.out.warn(f"退出。还剩 {fmt(remaining)} ETH 没卖,请核对实际空单。继续卖:{command}")
                return "stopped"
            if low in ("m", "r", "refresh"):
                continue
            if low in ("h", "help", "?"):
                self.out.plain(MAKER_HELP if self.maker else HELP)
                continue
            try:
                if low.startswith("sold "):
                    await self._record_total(line[5:].strip())
                elif low == "undo":
                    await self._undo()
                elif low.startswith("fix"):
                    await self._fix(line[3:].strip())
                elif self.maker:
                    self.out.warn("请用 sold <本轮累计实际成交 ETH>;挂单数量和空回车不算成交")
                else:
                    await self._record_sale(line, chunk, remaining, close_first)
            except (OkxError, OkxTimeout) as e:
                # The sale is already recorded; the next prompt retries the close (target-based, no double close).
                self.out.error(f"OKX 请求失败:{e}。先别在 OSL 继续卖,按 m 重试对齐")

    async def _ask_arrived(self) -> bool:
        held = bought_eth(self.db.orders(self.round_id, "maker_buy"))
        pos = await self.hedger.position()
        self.out.plain(f"本轮买到(扣手续费后){fmt(held, 6)} ETH;OKX 空单 {fmt(self.hedger.ct_to_eth(pos.short_ct))} ETH")
        raw = (await self.ask("OSL 交易账户实际到账多少 ETH?(照 OSL App 显示的数字,全部到账、划到交易账户后再填)> ")).strip()
        try:
            qty = dec(raw)
        except ValueError:
            self.out.error("不是有效数字,已退出。重新运行 offramp sell")
            return False
        if not qty.is_finite() or qty <= 0:
            self.out.error("数量必须大于 0,已退出")
            return False
        if qty > held:
            ans = await self.ask(f"到账 {fmt(qty)} 比本轮买到的 {fmt(held, 6)} 还多(可能 OSL 里有别的 ETH)。"
                                 f"本轮只按 {fmt(held, 6)} ETH 处理,确认?(y/n)> ")
            if ans.strip().lower() != "y":
                return False
            qty = held
        elif held > 0 and qty < held * Decimal("0.97"):
            ans = await self.ask(f"到账比买到的少了 {fmt((held - qty) / held * 100, 1)}%,正常的提币费和 gas 不会这么多。"
                                 f"确认已经全部到账了吗?还在路上的 ETH 会失去对冲(y/n)> ")
            if ans.strip().lower() != "y":
                return False
        self.db.update_round(self.round_id, arrived_eth=qty, status="selling", sell_started_at=self.clock())
        self.db.log(self.round_id, "arrived", f"OSL 到账 {fmt(qty)} ETH(买到 {fmt(held)})")
        self.out.ok(f"已记录到账 {fmt(qty)} ETH。转账损耗 {fmt(held - qty, 6)} ETH,多出来的空单现在平掉")
        return True

    async def _record_total(self, arg: str) -> None:
        try:
            total = dec(arg)
        except ValueError:
            self.out.error("用法:sold 0.2(本轮累计实际卖出 ETH)")
            return
        if not total.is_finite() or not self.sold() <= total <= self.arrived():
            self.out.error("累计成交必须在已记录成交与到账数量之间;更正输错的数字用 fix")
            return
        delta = total - self.sold()
        if delta > self.cfg.sell.confirm_big_input_eth:
            # The maker helper never reopens a short, so a mistyped total cannot be undone on OKX.
            ans = await self.ask(f"累计 {fmt(total)} ETH,即本次新增 {fmt(delta)} ETH,将在 OKX 平掉 {fmt(delta)} ETH 空单。"
                                 f"确认输入 y,其他取消> ")
            if ans.strip().lower() != "y":
                self.out.warn("已取消,没有记录,也没有平空")
                return
        if delta:
            self.db.add_sell(self.round_id, "sell", delta, None, self.clock(), note=f"累计成交 {fmt(total)}")
            self.out.info(f"累计卖出 {fmt(total)} ETH,本次新增 {fmt(delta)} ETH")
        else:
            self.out.info("累计成交没有变化,只检查未完成的平空")
        await self._align()

    async def _record_sale(self, line: str, chunk: Decimal, remaining: Decimal, close_first: bool) -> None:
        if line == "":
            qty, px = chunk, None
        else:
            m = _QTY_RE.match(line)
            if not m:
                self.out.error("看不懂这个输入。输入数量(可加 @均价),或者 h 看帮助")
                return
            qty = dec(m.group(1))
            px = dec(m.group(2)) if m.group(2) else None
            if qty <= 0:
                self.out.error("数量必须大于 0")
                return
            if qty > remaining:
                self.out.error(f"输入 {fmt(qty)} 比剩余待卖 {fmt(remaining)} 还多,没有记录。请重新输入")
                return
            if qty > self.cfg.sell.confirm_big_input_eth and abs(qty - chunk) > remaining / 2:
                ans = await self.ask(f"确认你在 OSL 卖了 {fmt(qty)} ETH?(回车确认,n 取消)> ")
                if ans.strip().lower() == "n":
                    self.out.warn("已取消,没有记录")
                    return
        self.db.add_sell(self.round_id, "sell", qty, px, self.clock())
        self.out.info(f"记录卖出 {fmt(qty)} ETH" + (f" @ {fmt(px)}" if px else ""))
        res = await self._align()
        if close_first and res.ok:
            self.out.ok(f"已平空。现在去 OSL 市价卖出 {fmt(qty)} ETH(卖的数量不同就用 fix 改)")
        close_px = res.avg_px()
        if px is not None and close_px:
            rate = px * (1 - self.cfg.osl.taker_fee) / close_px
            self.out.info(f"本笔兑换率 {fmt(rate, 5)} USD/USDT(OSL {fmt(px)} 扣 taker 费 ÷ OKX 平仓 {fmt(close_px, 2)})")

    async def _undo(self) -> None:
        last = self.db.last_undoable(self.round_id)
        if last is None:
            self.out.warn("没有可以撤销的记录")
            return
        self.db.undo_sell(last, self.clock())
        self.out.info(f"已撤销:{last.kind} {fmt(last.qty)} ETH。已卖现在是 {fmt(self.sold())} ETH")
        await self._align_after_correction()

    async def _fix(self, arg: str) -> None:
        try:
            total = dec(arg)
        except ValueError:
            self.out.error("用法:fix 0.02(把已卖总数改成 0.02)")
            return
        arrived = self.arrived()
        if not total.is_finite() or total < 0 or total > arrived:
            self.out.error(f"已卖必须在 0 到 {fmt(arrived)} 之间")
            return
        delta = total - self.sold()
        if delta == 0:
            self.out.info("已卖本来就是这个数,没有改动")
            return
        self.db.add_sell(self.round_id, "fix", delta, None, self.clock(), note=f"已卖改为 {fmt(total)}")
        self.out.info(f"已卖改为 {fmt(total)} ETH,剩余 {fmt(self.remaining())} ETH")
        await self._align_after_correction()

    async def _align_after_correction(self) -> None:
        target = self.hedger.eth_to_ct(self.remaining())
        pos = await self.hedger.position()
        if target - pos.short_ct >= self.hedger.swap.min_sz:
            gap = self.hedger.ct_to_eth(target - pos.short_ct)
            if self.maker:
                self.out.warn(f"记录已更正,空单少了 {fmt(gap)} ETH;maker 助手只平仓,不会补开空")
                return
            ans = await self.ask(f"改完后剩余 {fmt(self.remaining())} ETH,空单少了 {fmt(gap)} ETH,"
                                 f"要重新开空补上吗?(y/n)> ")
            if ans.strip().lower() != "y":
                self.out.warn("没有开空。这部分 ETH 现在没有对冲;可以再用 fix / undo 改回去")
                return
            res = await self.hedger.align(target, allow_open=True, allow_close=False)
        else:
            res = await self.hedger.align(target, allow_open=False, allow_close=True)
        self._print_alignment(res)

    async def _align(self, initial: bool = False) -> AlignResult:
        target = self.hedger.eth_to_ct(self.remaining())
        res = await self.hedger.align(target, allow_open=False, allow_close=True)
        if res.anomaly:
            gap = self.hedger.ct_to_eth(target - res.pos.short_ct)
            self.out.warn(f"OKX 空单比剩余 ETH 少 {fmt(gap)} ETH,这部分没有对冲")
            if self.maker:
                self.out.warn("maker 助手只平仓,不会补开空")
                self._print_alignment(res)
                return res
            ans = await self.ask("要现在补开空吗?(y/n)> ")
            if ans.strip().lower() == "y":
                res = await self.hedger.align(target, allow_open=True, allow_close=True)
        self._print_alignment(res)
        return res

    def _print_alignment(self, res: AlignResult) -> None:
        short_eth = self.hedger.ct_to_eth(res.pos.short_ct)
        if res.ok:
            self.out.ok(f"对齐 空单 {fmt(short_eth)} ETH ≈ 剩余 {fmt(self.remaining())} ETH ✓")
        else:
            self.out.error(f"没对齐:空单 {fmt(short_eth)} ETH,剩余 {fmt(self.remaining())} ETH。"
                           f"输入 m 重试尚未完成的平空;不要重复报增量成交")

    async def _finish(self, dust: Decimal) -> bool:
        try:
            res = await self.hedger.align(ZERO, allow_open=False, allow_close=True)
        except (OkxError, OkxTimeout) as e:
            self.out.error(f"平空未完成:{e}")
            return False
        self._print_alignment(res)
        if not res.ok:
            return False
        if dust > 0:
            self.out.info(f"OSL 剩 {fmt(dust)} ETH 尾数,低于最小下单量卖不了,报告里会单列")
        if getattr(self.api, "simulated", False):
            self.out.ok("模拟卖出结束,空单已平;不需要运行 round close")
        else:
            self.out.ok("本轮卖完。下一步:offramp round close,然后 offramp report --osl-usd <OSL 实际收到的 USD>")
        return True

    async def _show(self, remaining: Decimal, chunk: Decimal, close_first: bool) -> None:
        if not await self._realign_quietly():
            return
        lines = []
        osl_bid = None
        try:
            osl_bid, osl_ask = await self.osl.ticker()
            lines.append(f" OSL {self.cfg.osl.symbol}   买一 {fmt(osl_bid)}   卖一 {fmt(osl_ask)}")
        except Exception as e:  # public data only; selling must not depend on it
            lines.append(f" OSL 行情读取失败:{e}")
        try:
            book = await self.api.books(self.cfg.okx.swap_inst)
            lines.append(f" OKX 永续       卖一 {fmt(book.best_ask)}")
            if osl_bid is not None and book.best_ask and not self.maker:
                rate = osl_bid * (1 - self.cfg.osl.taker_fee) / book.best_ask
                lines.append(f" 吃买一卖出的净兑换率 {fmt(rate, 5)}(已扣 OSL taker {fmt(self.cfg.osl.taker_fee * 100)}%)")
            usd = f" ≈ {fmt(remaining * osl_bid, 2)} USD" if osl_bid else ""
        except (OkxError, OkxTimeout) as e:
            lines.append(f" OKX 行情读取失败:{e}")
            usd = ""
        lines.append(f" 剩余待卖 {fmt(remaining)} ETH{usd}   已卖 {fmt(self.sold())} / 到账 {fmt(self.arrived())}")
        self.out.plain("")
        for line in lines:
            self.out.plain(line)
        if self.maker:
            self.out.head(" ▶ OSL 挂限价单后等待实际成交,输入 sold <本轮累计成交 ETH>;OKX 用 IOC 买入平空")
        elif close_first:
            self.out.head(f" ▶ 本笔 {fmt(chunk)} ETH:按回车,程序先在 OKX 平空,然后你去 OSL 市价卖出")
        else:
            self.out.head(f" ▶ 本笔:在 OSL 市价卖出 {fmt(chunk)} ETH,卖完直接按回车(数量不同就输入实际数量)")

    async def _realign_quietly(self) -> bool:
        """Before each prompt make sure an earlier failed close gets retried (no-op when aligned)."""
        try:
            target = self.hedger.eth_to_ct(self.remaining())
            pos = await self.hedger.position()
            if pos.short_ct - target >= self.hedger.swap.min_sz:
                res = await self.hedger.align(target, allow_open=False, allow_close=True)
                self._print_alignment(res)
                return res.ok
        except (OkxError, OkxTimeout) as e:
            self.out.error(f"连不上 OKX:{e}。先别在 OSL 继续卖,按 m 重试")
            return False
        return True
