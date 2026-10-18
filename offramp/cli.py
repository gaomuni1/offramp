"""Command line entry point: `offramp <command>`. Run `offramp -h` for the list."""
from __future__ import annotations

import argparse
import asyncio
import signal
import time
from datetime import datetime
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from .buy_engine import BuyEngine
from .allocation import Allocation, affordable_qty
from .config import Config, load_config
from .db import Db, Round
from .dryrun import DryRunConsole, DryRunOkx
from .hedger import Hedger
from .ledger import bought_eth, spent_usdt
from .models import HaltError, Instrument, OkxError, OkxTimeout, OrderRow
from .okx import OkxApi
from .osl_public import OslPublic
from .report import print_report
from .sell_assist import SellAssistant
from .util import ZERO, Console, InstanceLock, dec, floor_step, fmt, round_step

ACCT_LEVELS = {"1": "现货模式", "2": "合约模式", "3": "跨币种保证金模式", "4": "组合保证金模式"}
STATUS_ZH = {"new": "新建", "buying": "买入中", "bought": "已买完(转账中)", "selling": "卖出中", "closed": "已收尾"}


class Ctx:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.out = Console(cfg.data_dir / "offramp.log")
        self.db = Db(cfg.data_dir / "offramp.db")
        self.api = OkxApi(cfg)
        self.osl = OslPublic(cfg)

    def lock(self) -> InstanceLock:
        return InstanceLock(self.cfg.data_dir / "offramp.lock")

    async def close(self) -> None:
        await self.api.close()
        await self.osl.close()

    async def specs(self) -> tuple[Instrument, Instrument]:
        spot = await self.api.instrument("SPOT", self.cfg.okx.spot_inst)
        swap = await self.api.instrument("SWAP", self.cfg.okx.swap_inst)
        return spot, swap

    async def pos_mode(self) -> str:
        """Checks the account mode and returns the position mode."""
        conf = await self.api.account_config()
        level = str(conf.get("acctLv", ""))
        if level != "2":
            raise SystemExit(f"OKX 账户模式是「{ACCT_LEVELS.get(level, level)}」,需要切换成「合约模式」"
                             f"(网页:交易 → 设置 → 账户模式)。切换后重新运行。")
        return conf.get("posMode", "net_mode")

    def require_active(self) -> Round:
        rnd = self.db.active_round()
        if rnd is None:
            raise SystemExit("没有进行中的轮次。先运行 offramp round new")
        return rnd


def _ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else "-"


def _dur(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    return f"{h} 小时 {rem // 60} 分" if h else f"{rem // 60} 分"


async def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


async def _wait_transfer(ctx: Ctx, trans_id: str, client_id: str) -> None:
    for _ in range(60):
        state = await ctx.api.transfer_state(trans_id=trans_id or None, client_id=None if trans_id else client_id)
        if state == "success":
            return
        if state == "failed":
            raise SystemExit("划转失败,请到 OKX App 查看")
        await asyncio.sleep(0.5)
    raise SystemExit("划转 30 秒还没完成,请到 OKX App 查看后再继续")


# ---- commands ---------------------------------------------------------------------------------

def positive_amount(value: str) -> Decimal:
    try:
        amount = dec(value)
    except ValueError:
        raise SystemExit("金额必须是大于 0 的有限数字") from None
    if not amount.is_finite() or amount <= 0:
        raise SystemExit("金额必须是大于 0 的有限数字")
    return amount


async def preview_buy(ctx: Ctx, spot: Instrument, swap: Instrument, budget: Decimal, api=None) -> None:
    """Read-only feasibility check, shared by check and a fresh buy."""
    cfg, api, out = ctx.cfg, api if api is not None else ctx.api, ctx.out
    spot_book = await api.books(cfg.okx.spot_inst)
    swap_book = await api.books(cfg.okx.swap_inst)
    px, perp_px = spot_book.best_bid, swap_book.best_ask
    if not px or not perp_px:
        raise SystemExit("盘口为空,暂时不能测试买入")
    qty = floor_step(budget / px, spot.lot_sz)
    contracts = round_step(qty / swap.ct_val, swap.lot_sz)
    if qty < spot.min_sz or contracts < swap.min_sz:
        raise SystemExit("预算太小,无法同时满足现货和永续最低下单数量")
    maker_fee, _ = await api.trade_fee("SPOT", inst_id=cfg.okx.spot_inst)
    _, taker_fee = await api.trade_fee("SWAP", inst_family=cfg.okx.spot_inst)
    margin_per_eth = perp_px / Decimal(cfg.okx.leverage) * (1 + cfg.buy.margin_buffer)
    need = budget + (qty + swap.lot_sz * swap.ct_val / 2) * margin_per_eth
    need += budget * abs(maker_fee) + contracts * swap.ct_val * perp_px * abs(taker_fee)
    balance = (await api.balances(["USDT"]))["USDT"]
    out.info(f"买入预估:现货预算 {fmt(budget)} USDT,约 {fmt(qty)} ETH(未扣手续费);"
             f"空单约 {fmt(contracts)} 张 = {fmt(contracts * swap.ct_val)} ETH")
    out.info(f"永续最小 {fmt(swap.min_sz * swap.ct_val)} ETH,步长 {fmt(swap.lot_sz * swap.ct_val)} ETH;"
             f"按最近步长取整,尾差最多约 {fmt(swap.lot_sz * swap.ct_val / 2)} ETH。最终按实际净成交量对冲")
    out.info(f"含保证金缓冲、取整预留和预计开仓手续费,约需 {fmt(need, 2)} USDT;"
             f"当前权益 {fmt(balance.eq, 2)},可用 {fmt(balance.avail_bal, 2)} USDT")
    if min(balance.eq, balance.avail_bal) < need:
        raise SystemExit("交易账户可用 USDT 不足以完成该预算;请先准备资金或降低现货预算")


def require_total_config(cfg: Config) -> None:
    if cfg.okx.leverage != 1 or cfg.okx.margin_mode != "cross":
        raise SystemExit("总资金模式仅支持全仓 1x,请检查 okx.leverage 和 okx.margin_mode")


async def plan_allocation(ctx: Ctx, spot: Instrument, swap: Instrument, total: Decimal,
                          reserve: Decimal | None = None, *, api=None, cfg=None, out=None):
    api, cfg, out = api or ctx.api, cfg or ctx.cfg, out or ctx.out
    require_total_config(cfg)
    reserve = reserve if reserve is not None else max(Decimal(1), total * Decimal("0.001"))
    if reserve >= total:
        raise SystemExit("预留必须小于总资金")
    bal = (await api.balances(["USDT"]))["USDT"]
    available = min(bal.eq, bal.cash_bal, bal.avail_bal)
    if available < total:
        raise SystemExit(f"本次总资金 {fmt(total)} USDT,交易账户/模拟账户可用只有 {fmt(available, 2)} USDT")
    spot_book = await api.books(cfg.okx.spot_inst)
    swap_book = await api.books(cfg.okx.swap_inst)
    if not all((spot_book.best_bid, spot_book.best_ask, swap_book.best_bid, swap_book.best_ask)):
        raise SystemExit("盘口为空,无法计算总资金分配")
    maker_fee, _ = await api.trade_fee("SPOT", inst_id=cfg.okx.spot_inst)
    _, taker_fee = await api.trade_fee("SWAP", inst_family=cfg.okx.spot_inst)
    allocation = Allocation(total, reserve, maker_fee, taker_fee)
    px = min(spot_book.best_bid, swap_book.best_bid * (1 + cfg.buy.max_entry_basis_bps / 10000))
    px = floor_step(px, spot.tick_sz)
    plan = affordable_qty(max_qty=(total - reserve) / px, equity=total, spot=spot, allocation=allocation,
                          swap=swap, spot_px=px, perp_bid=swap_book.best_bid,
                          perp_ask=swap_book.best_ask, slippage_bps=cfg.okx.hedge_slippage_bps)
    if plan.qty < spot.min_sz or plan.target_ct < swap.min_sz:
        raise SystemExit("总资金扣除预留后不足同时满足现货和永续最低下单量")
    out.info(f"总资金模式:上限 {fmt(total)} USDT,固定预留 {fmt(reserve)} USDT;"
             f"替代配置中的 {fmt(cfg.buy.margin_buffer * 100)}% 比例缓冲")
    out.info(f"当前估算:现货 {fmt(plan.spot_cost, 2)} USDT,净到账约 {fmt(plan.net_eth)} ETH;"
             f"空单约 {fmt(plan.target_ct * swap.ct_val)} ETH,1x 保证金约 {fmt(plan.margin, 2)} USDT")
    out.info(f"现货手续费约 {fmt(-plan.qty * maker_fee)} ETH(负数为返佣,从 ETH 净量计入);"
             f"永续开仓费用预估 {fmt(plan.open_fee, 4)} USDT,开空价差/滑点预留 {fmt(plan.execution_buffer, 4)} USDT")
    out.info("分批执行时按实际成交、费用、浮盈亏和实时价格重算;仅使用本轮资金上限,不保证余额恰好为零")
    return allocation, plan


async def cmd_check(ctx: Ctx, usdt: str | None = None, total_usdt: str | None = None,
                    reserve_usdt: str | None = None) -> None:
    if usdt is not None and total_usdt is not None:
        raise SystemExit("--usdt 和 --total-usdt 不能同时使用")
    if reserve_usdt is not None and total_usdt is None:
        raise SystemExit("--reserve-usdt 需要搭配 --total-usdt")
    cfg, out, api = ctx.cfg, ctx.out, ctx.api
    problems = 0
    out.head(f"OKX 环境:{'模拟盘(demo)' if cfg.creds.demo else '实盘'}")

    # The first request also pays for the TLS handshake, which skews the midpoint estimate on slow
    # links. Warm the connection, then trust the sample with the shortest round trip.
    await api.server_time_ms()
    samples = []
    for _ in range(3):
        t0 = time.time()
        server_ms = await api.server_time_ms()
        t1 = time.time()
        samples.append((t1 - t0, abs(server_ms / 1000 - (t0 + t1) / 2)))
    skew = min(samples)[1]
    (out.ok if skew < 1 else out.error)(f"本机时间和 OKX 相差 {skew:.2f} 秒" + ("" if skew < 1 else ",请打开系统自动校时"))
    problems += skew >= 1

    spot, swap = await ctx.specs()
    out.ok(f"现货 {spot.inst_id}:最小 {fmt(spot.min_sz)} ETH,数量步长 {fmt(spot.lot_sz)},价格步长 {fmt(spot.tick_sz)}")
    out.ok(f"永续 {swap.inst_id}:每张 {fmt(swap.ct_val)} ETH,最小 {fmt(swap.min_sz)} 张 = "
           f"{fmt(swap.min_sz * swap.ct_val)} ETH,价格步长 {fmt(swap.tick_sz)}")

    try:
        status = await api.system_status()
        if status:
            for s in status:
                out.warn(f"OKX 维护公告:{s.get('title', '')} {s.get('begin', '')}-{s.get('end', '')} {s.get('state', '')}")
        else:
            out.ok("OKX 当前没有维护公告")
    except (OkxError, OkxTimeout) as e:
        out.warn(f"维护公告读取失败:{e}")

    try:
        bid, ask = await ctx.osl.ticker()
        out.ok(f"OSL {cfg.osl.symbol} 公开行情:买一 {fmt(bid)} 卖一 {fmt(ask)}")
    except Exception as e:
        out.warn(f"OSL 公开行情读取失败(不影响买入,卖出时只是少了参考价):{e}")

    if not cfg.creds.present:
        out.error("没有 OKX API key:把 .env.example 复制成 .env 并填好,再运行 offramp check")
        raise SystemExit(1)

    conf = await api.account_config()
    level = str(conf.get("acctLv", ""))
    if level == "2":
        out.ok("账户模式:合约模式")
    else:
        out.error(f"账户模式是「{ACCT_LEVELS.get(level, level)}」,需要切换成「合约模式」(网页:交易 → 设置 → 账户模式)")
        problems += 1
    pos_mode = conf.get("posMode", "net_mode")
    out.ok(f"持仓模式:{'单向持仓' if pos_mode == 'net_mode' else '双向持仓'}")
    perm = str(conf.get("perm", ""))
    if "withdraw" in perm:
        out.error("这个 API key 开了「提币」权限。请重建一个只有「读取 + 交易」的 key")
        problems += 1
    elif "trade" not in perm:
        out.error(f"这个 API key 没有「交易」权限(当前:{perm})")
        problems += 1
    else:
        out.ok(f"API key 权限:{perm}(没有提币权限 ✓)")

    lever = await api.leverage(cfg.okx.swap_inst, cfg.okx.margin_mode)
    if lever == cfg.okx.leverage:
        out.ok(f"{cfg.okx.swap_inst} 全仓杠杆 {fmt(lever)}x")
    else:
        out.warn(f"当前杠杆 {fmt(lever)}x;check 不修改账户,正式 buy 时会设置为 {cfg.okx.leverage}x")

    try:
        maker, taker = await api.trade_fee("SPOT", inst_id=cfg.okx.spot_inst)
        out.ok(f"现货费率 maker {fmt(-maker * 100)}% / taker {fmt(-taker * 100)}%(负数表示返佣)")
        maker, taker = await api.trade_fee("SWAP", inst_family=cfg.okx.spot_inst)
        out.ok(f"永续费率 maker {fmt(-maker * 100)}% / taker {fmt(-taker * 100)}%")
    except (OkxError, OkxTimeout) as e:
        out.warn(f"费率读取失败:{e}")

    trading = await api.balances(["USDT", "ETH"])
    funding = await api.funding_balances(["USDT", "ETH"])
    out.ok(f"交易账户 USDT {fmt(trading['USDT'].cash_bal, 2)},ETH {fmt(trading['ETH'].cash_bal, 6)};"
           f"资金账户 USDT {fmt(funding['USDT'], 2)},ETH {fmt(funding['ETH'], 6)}")

    pos = await api.position(cfg.okx.swap_inst, pos_mode)
    active = ctx.db.active_round()
    if pos.short_ct != 0 and active is None:
        out.warn(f"{cfg.okx.swap_inst} 已有仓位 {fmt(pos.short_ct * swap.ct_val)} ETH(空为正)。"
                 f"程序要求开新一轮前这个仓位是 0")
    try:
        for chain in await api.currency_chains("ETH"):
            name = chain.get("chain", "")
            if name.endswith("ERC20") or name.endswith("Ethereum"):
                ok = chain.get("canWd")
                (out.ok if ok else out.warn)(f"ETH 提币({name}):{'可以' if ok else '暂停中'},"
                                             f"手续费 {chain.get('fee', '?')} ETH,最小 {chain.get('minWd', '?')} ETH")
    except (OkxError, OkxTimeout) as e:
        out.warn(f"提币信息读取失败:{e}")

    if active:
        out.info(f"进行中的轮次:第 {active.id} 轮,{STATUS_ZH.get(active.status, active.status)}")
    out.head("")
    if problems:
        out.error(f"预检发现 {problems} 个问题,见上面红字")
        raise SystemExit(1)
    if usdt is not None:
        await preview_buy(ctx, spot, swap, positive_amount(usdt))
    if total_usdt is not None:
        await plan_allocation(ctx, spot, swap, positive_amount(total_usdt),
                              positive_amount(reserve_usdt) if reserve_usdt is not None else None)
    out.ok("预检通过")


async def cmd_market(ctx: Ctx, once: bool) -> None:
    cfg, out, api = ctx.cfg, ctx.out, ctx.api
    out.plain("OKX 现货 买一/卖一 | OKX 永续 买一/卖一 | OSL 买一/卖一 | 买入基差 | 卖出净兑换率")
    while True:
        try:
            spot = await api.books(cfg.okx.spot_inst)
            swap = await api.books(cfg.okx.swap_inst)
            try:
                osl_bid, osl_ask = await ctx.osl.ticker()
            except Exception:
                osl_bid = osl_ask = None
            basis = (spot.best_ask - swap.best_bid) / swap.best_bid * 10000 if spot.best_ask and swap.best_bid else None
            rate = osl_bid * (1 - cfg.osl.taker_fee) / swap.best_ask if osl_bid and swap.best_ask else None
            out.info(f"{fmt(spot.best_bid)}/{fmt(spot.best_ask)} | {fmt(swap.best_bid)}/{fmt(swap.best_ask)} | "
                     f"{fmt(osl_bid)}/{fmt(osl_ask)} | {fmt(basis, 1)} bps | {fmt(rate, 5)}")
        except (OkxError, OkxTimeout) as e:
            out.warn(f"行情读取失败:{e}")
        if once:
            return
        await asyncio.sleep(2)


async def cmd_round_new(ctx: Ctx, usdt: str | None) -> None:
    cfg, out, api, db = ctx.cfg, ctx.out, ctx.api, ctx.db
    active = db.active_round()
    if active:
        raise SystemExit(f"第 {active.id} 轮还没收尾({STATUS_ZH.get(active.status, active.status)})。"
                         f"先完成它,或者 offramp round close")
    pos_mode = await ctx.pos_mode()
    _, swap = await ctx.specs()
    pos = await api.position(cfg.okx.swap_inst, pos_mode)
    if pos.short_ct != 0:
        raise SystemExit(f"{cfg.okx.swap_inst} 现在有仓位 {fmt(pos.short_ct * swap.ct_val)} ETH(空为正)。"
                         f"开新一轮前这个仓位必须是 0,程序不会动你已有的仓位")
    amount = None
    if usdt:
        amount = positive_amount(usdt)
        available = (await api.funding_balances(["USDT"]))["USDT"]
        if available < amount:
            raise SystemExit(f"资金账户只有 {fmt(available, 2)} USDT,不够划转 {fmt(amount)}")
        client_id = f"ofrT{int(time.time())}"
        trans_id = await api.transfer("USDT", amount, "6", "18", client_id)
        await _wait_transfer(ctx, trans_id, client_id)
        out.ok(f"已从资金账户划转 {fmt(amount)} USDT 到交易账户")
    start = (await api.balances(["USDT"]))["USDT"].cash_bal
    rid = db.create_round()
    db.update_round(rid, transfer_usdt=amount, start_usdt=start)
    db.log(rid, "round", f"新建,交易账户 USDT {start}")
    out.ok(f"第 {rid} 轮已建立,交易账户 USDT {fmt(start, 2)}")
    per_eth = Decimal(1) + Decimal(1) / Decimal(cfg.okx.leverage) * (1 + cfg.buy.margin_buffer)
    out.info(f"按 {cfg.okx.leverage}x 对冲,这些 USDT 最多大约能买 {fmt(start / per_eth, 2)} USDT 的 ETH"
             f"(其余留作空单保证金)。下一步:offramp buy --usdt <金额>(第一次建议先加 --dry-run)")


async def cmd_buy(ctx: Ctx, action: str | None, usdt: str | None, dry_run: bool,
                  paper_usdt: str | None = None, paper_slice_eth: str | None = None,
                  total_usdt: str | None = None, reserve_usdt: str | None = None) -> None:
    if usdt is not None and total_usdt is not None:
        raise SystemExit("--usdt 和 --total-usdt 不能同时使用")
    if reserve_usdt is not None and total_usdt is None:
        raise SystemExit("--reserve-usdt 需要搭配 --total-usdt")
    if paper_usdt is not None and not dry_run:
        raise SystemExit("--paper-usdt 仅可与 --dry-run 一起使用,不能用于实盘")
    if paper_slice_eth is not None and not dry_run:
        raise SystemExit("--paper-slice-eth 仅可与 --dry-run 一起使用,不能用于实盘")
    if dry_run and not (usdt or total_usdt):
        raise SystemExit("--dry-run 需要 --usdt 或 --total-usdt")
    if dry_run and action == "resume":
        raise SystemExit("dry-run 状态不保存,不支持 resume;请重新运行原演练命令")
    if action == "resume" and (usdt is not None or total_usdt is not None or reserve_usdt is not None):
        raise SystemExit("resume 使用本轮保存的预算和预留,不能重新指定金额")
    total = positive_amount(total_usdt) if total_usdt is not None else None
    reserve = positive_amount(reserve_usdt) if reserve_usdt is not None else None
    if total is not None and reserve is not None and reserve >= total:
        raise SystemExit("预留必须小于总资金")
    paper_balance = positive_amount(paper_usdt) if paper_usdt is not None else None
    if dry_run and total is not None and paper_balance is None:
        paper_balance = total
    paper_slice = positive_amount(paper_slice_eth) if paper_slice_eth is not None else None
    cfg, out, api, db = ctx.cfg, ctx.out, ctx.api, ctx.db
    if total is not None:
        require_total_config(cfg)
    if paper_slice is not None:
        cfg = replace(cfg, buy=replace(cfg.buy, max_slice_eth=paper_slice))
    if dry_run:
        api = DryRunOkx(api, out, paper_usdt=paper_balance, cfg=cfg)
        out = DryRunConsole(out)
        out.warn("本地模拟:现货每次查询按半单合成成交,永续按盘口模拟 IOC;不发送真实订单")
        if paper_balance is not None:
            out.warn(f"使用虚拟余额 {fmt(paper_balance)} USDT,不是交易账户的真实余额")
        if paper_slice is not None:
            out.info(f"本次演练每单最多 {fmt(paper_slice)} ETH,不修改 config.yaml")
    pos_mode = await ctx.pos_mode()
    spot, swap = await ctx.specs()
    if paper_slice is not None and floor_step(paper_slice, spot.lot_sz) < spot.min_sz:
        raise SystemExit("模拟分单数量低于现货最小下单量")
    if dry_run:
        await api.initialize(spot, swap)
    budget = positive_amount(usdt) if usdt is not None else None
    allocation = None
    if total is not None:
        allocation, plan = await plan_allocation(ctx, spot, swap, total, reserve, api=api, cfg=cfg, out=out)
        budget = plan.spot_cost  # estimate only; total mode resizes on every quote
    elif budget is not None:
        await preview_buy(ctx, spot, swap, budget, api=api)

    if dry_run:
        db = Db(":memory:")  # nothing from a dry run touches the real records
        rid = db.create_round()
        db.save_allocation(rid, allocation)
        db.update_round(rid, buy_budget=budget, status="buying",
                        start_usdt=(await api.balances(["USDT"]))["USDT"].cash_bal)
    else:
        rnd = ctx.require_active()
        rid = rnd.id
        if action == "resume":
            if rnd.status != "buying":
                raise SystemExit(f"第 {rid} 轮是「{STATUS_ZH.get(rnd.status)}」,没有可以继续的买入")
            allocation = db.get_allocation(rid)
            if allocation:
                require_total_config(cfg)
                maker_fee, _ = await api.trade_fee("SPOT", inst_id=cfg.okx.spot_inst)
                _, taker_fee = await api.trade_fee("SWAP", inst_family=cfg.okx.spot_inst)
                allocation = replace(allocation, maker_fee=maker_fee, taker_fee=taker_fee)
                db.save_allocation(rid, allocation)
        else:
            if rnd.status == "buying":
                raise SystemExit("本轮买入已经开始过。继续用:offramp buy resume")
            if rnd.status != "new":
                raise SystemExit(f"第 {rid} 轮已经是「{STATUS_ZH.get(rnd.status)}」,不能再买")
            if budget is None:
                raise SystemExit("需要指定现货预算 --usdt 或总资金 --total-usdt")
            pos = await api.position(cfg.okx.swap_inst, pos_mode)
            if pos.short_ct != 0:
                raise SystemExit("新买入开始前 ETH 永续必须没有仓位,请先核对 OKX")
            bal = (await api.balances(["USDT"]))["USDT"]
            need = budget * (1 + (1 + cfg.buy.margin_buffer) / Decimal(cfg.okx.leverage))
            if allocation is None and bal.eq < need:
                out.warn(f"买 {fmt(budget)} USDT 的 ETH 再留 {cfg.okx.leverage}x 保证金,大约需要 {fmt(need, 2)} USDT,"
                         f"交易账户只有 {fmt(bal.eq, 2)}。程序会在保证金不够时提前停止买入")
            db.save_allocation(rid, allocation)  # persist before marking buying, including for resume
            if allocation:
                db.update_round(rid, start_usdt=bal.cash_bal)
            db.update_round(rid, buy_budget=budget, status="buying", buy_started_at=time.time())
        lever = await api.leverage(cfg.okx.swap_inst, cfg.okx.margin_mode)
        if lever != cfg.okx.leverage:
            await api.set_leverage(cfg.okx.swap_inst, cfg.okx.leverage, cfg.okx.margin_mode)
            out.ok(f"杠杆设为 {cfg.okx.leverage}x")

    hedger = Hedger(api, db, cfg, rid, swap, pos_mode, out)
    engine = BuyEngine(api, db, cfg, rid, spot, swap, hedger, out)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, engine.request_stop)
    label = (f"总资金 {fmt(allocation.total_usdt)},预留 {fmt(allocation.reserve_usdt)} USDT,现货预算动态计算"
             if allocation else f"现货预算 {fmt(engine.budget)} USDT")
    out.info(f"开始买入:{label},每单最多 {fmt(cfg.buy.max_slice_eth)} ETH。Ctrl+C 安全退出")
    try:
        if await engine.run() == "halted":
            raise SystemExit(2)
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        if dry_run:
            try:
                await api.print_summary(reserve_usdt=allocation.reserve_usdt if allocation else None)
            except (OkxError, OkxTimeout) as e:
                out.warn(f"无法刷新模拟账户小结:{e}")
            finally:
                db.conn.close()


async def cmd_status(ctx: Ctx) -> None:
    cfg, out, api, db = ctx.cfg, ctx.out, ctx.api, ctx.db
    pos_mode = await ctx.pos_mode()
    _, swap = await ctx.specs()
    trading = await api.balances(["USDT", "ETH"])
    funding = await api.funding_balances(["USDT", "ETH"])
    pos = await api.position(cfg.okx.swap_inst, pos_mode)
    short_eth = pos.short_ct * swap.ct_val
    out.head("==== OKX ====")
    out.plain(f"交易账户 USDT {fmt(trading['USDT'].cash_bal, 2)}(可用 {fmt(trading['USDT'].avail_bal, 2)}),"
              f"ETH {fmt(trading['ETH'].cash_bal, 6)}")
    out.plain(f"资金账户 USDT {fmt(funding['USDT'], 2)},ETH {fmt(funding['ETH'], 6)}")
    out.plain(f"空单 {fmt(short_eth)} ETH  均价 {fmt(pos.avg_px, 2)}  未实现盈亏 {fmt(pos.upl, 4)} USDT  "
              f"保证金率 {fmt(pos.mgn_ratio, 2)}  强平价 {fmt(pos.liq_px, 2)}")

    rnd = db.active_round() or db.latest_round()
    if rnd is None:
        out.plain("还没有轮次")
        return
    orders = db.orders(rnd.id)
    allocation = db.get_allocation(rnd.id)
    held = bought_eth(orders)
    sold = db.sold_total(rnd.id)
    out.head(f"==== 第 {rnd.id} 轮:{STATUS_ZH.get(rnd.status, rnd.status)} ====")
    out.plain(f"建立 {_ts(rnd.created_at)}  开始时交易账户 USDT {fmt(rnd.start_usdt, 2)}  "
              f"{'初始现货预算估算' if allocation else '买入预算'} {fmt(rnd.buy_budget)}")
    if allocation:
        out.plain(f"总资金模式:上限 {fmt(allocation.total_usdt)} USDT,固定预留 {fmt(allocation.reserve_usdt)} USDT")
    out.plain(f"已买到 {fmt(held, 6)} ETH(花费 {fmt(spent_usdt(orders), 2)} USDT)")
    if rnd.status in ("new", "buying", "bought"):
        target = held
    else:
        target = (rnd.arrived_eth or ZERO) - sold
        out.plain(f"OSL 到账 {fmt(rnd.arrived_eth)} ETH,已卖 {fmt(sold)},剩余 {fmt(target)}")
    if rnd.status != "closed":
        gap = short_eth - target
        flag = "✓ 步长内(仍可能有尾差)" if abs(gap) <= swap.lot_sz * swap.ct_val / 2 else "⚠ 没对齐"
        out.plain(f"应有空单 {fmt(target)} ETH,实际 {fmt(short_eth)} ETH,偏差 {fmt(gap)} {flag}")
    if rnd.status == "bought" and rnd.buy_done_at:
        out.plain(f"买完到现在(在途){_dur(time.time() - rnd.buy_done_at)}")
    try:
        end = rnd.closed_at or time.time()
        funding_fee = await api.funding_fee_sum(cfg.okx.swap_inst, int(rnd.created_at * 1000), int(end * 1000))
        out.plain(f"本轮累计资金费 {fmt(funding_fee, 4)} USDT(正数 = 收到)")
    except (OkxError, OkxTimeout) as e:
        out.warn(f"资金费读取失败:{e}")


async def cmd_to_funding(ctx: Ctx, eth: str | None) -> None:
    out, api, db = ctx.out, ctx.api, ctx.db
    rnd = ctx.require_active()
    if rnd.status != "bought":
        raise SystemExit(f"第 {rnd.id} 轮是「{STATUS_ZH.get(rnd.status)}」,买完以后才需要转 ETH")
    held = bought_eth(db.orders(rnd.id))
    available = (await api.balances(["ETH"]))["ETH"].avail_bal
    amount = dec(eth) if eth else min(held, available)
    amount = floor_step(amount, Decimal("0.00000001"))
    if amount <= 0 or amount > available:
        raise SystemExit(f"交易账户可用 ETH {fmt(available)},不能转 {fmt(amount)}")
    client_id = f"ofrE{int(time.time())}"
    trans_id = await api.transfer("ETH", amount, "18", "6", client_id)
    await _wait_transfer(ctx, trans_id, client_id)
    db.log(rnd.id, "transfer", f"ETH {amount} 交易账户 → 资金账户")
    out.ok(f"已把 {fmt(amount)} ETH 转到资金账户")
    out.plain("接下来在 OKX App 手动操作:")
    out.plain("  1. 提币到 hot wallet,网络选「以太坊主网 / ERC20」(OSL 不收 L2)")
    out.plain("  2. hot wallet 转到 OSL 的 ETH 充值地址(hot wallet 必须已在 OSL 做过白名单验证)")
    out.plain("  3. OSL 到账后,在 OSL 把 ETH 从资金账户划到交易账户")
    out.plain("  4. 运行 offramp sell")
    out.plain("期间空单一直保留,ETH 价格风险是对冲着的。")


async def cmd_sell(ctx: Ctx, close_first: bool, maker: bool = False, dry_run: bool = False,
                   paper_eth: str | None = None) -> None:
    if paper_eth is not None and not dry_run:
        raise SystemExit("--paper-eth 仅可与 --dry-run 一起使用")
    if dry_run and paper_eth is None:
        raise SystemExit("卖出模拟需要 --paper-eth <假设已到 OSL 的 ETH>")
    if (maker or dry_run) and close_first:
        raise SystemExit("maker / 卖出模拟必须成交后平空,不能 --close-first")
    qty = positive_amount(paper_eth) if paper_eth is not None else None
    cfg = ctx.cfg
    if dry_run:
        spot, swap = await ctx.specs()
        pos_mode = await ctx.pos_mode()
        out = DryRunConsole(ctx.out)
        api = DryRunOkx(ctx.api, ctx.out, paper_usdt=ZERO, cfg=cfg)
        await api.initialize(spot, swap)
        short_ct = round_step(qty / swap.ct_val, swap.lot_sz)
        if short_ct < swap.min_sz or qty < cfg.osl.min_qty:
            raise SystemExit("模拟 ETH 数量低于最小交易量")
        book = await api.books(cfg.okx.swap_inst)
        if not book.best_bid:
            raise SystemExit("永续盘口为空,无法初始化模拟仓位")
        api.seed_short(short_ct, book.best_bid)
        db = Db(":memory:")
        try:
            rid = db.create_round()
            db.update_round(rid, status="selling", arrived_eth=qty)
            # Seed the ledger baseline as well, so pending-close recovery can reconcile position.
            db.insert_order(OrderRow("paperSeed", rid, "hedge_open", swap.inst_id, "sell", "seed",
                                     book.best_bid, short_ct, state="filled", acc_fill_sz=short_ct,
                                     avg_px=book.best_bid))
            out.warn(f"独立平仓演练:假设 OSL 到账 {fmt(qty)} ETH,本地已有空单 {fmt(short_ct * swap.ct_val)} ETH")
            initial = (await api.balances(["USDT"]))["USDT"]
            out.info(f"虚拟开仓均价 {fmt(book.best_bid)},虚拟现金 {fmt(initial.cash_bal, 2)} USDT;不读取真实持仓、不发送开仓单")
            out.info("OSL 成交由你输入模拟;不连接 OSL 账户。不会恢复上一次 buy dry-run 的仓位")
            hedger = Hedger(api, db, cfg, rid, swap, pos_mode, out)
            assistant = SellAssistant(api, ctx.osl, db, cfg, rid, hedger, out, maker=True)
            await assistant.run()
            pos = await hedger.position()
            bal = (await api.balances(["USDT"]))["USDT"]
            out.head("==== 平仓模拟小结 ====")
            out.plain(f"OSL 模拟已卖 {fmt(assistant.sold())} ETH,未卖 {fmt(assistant.remaining())} ETH;"
                      f"OKX 剩余模拟空单 {fmt(pos.short_ct * swap.ct_val)} ETH")
            fees = sum((o.fee for o in db.orders(rid, "hedge_close")), ZERO)
            pnl = sum((o.pnl for o in db.orders(rid, "hedge_close")), ZERO)
            out.plain(f"平仓已实现盈亏 {fmt(pnl)} USDT,手续费 {fmt(fees)} USDT;"
                      f"可用 {fmt(bal.avail_bal, 4)},冻结/保证金 {fmt(bal.frozen_bal, 4)} USDT")
            out.warn("模拟状态退出即丢弃;无需 round close。未模拟 OSL 排队、费用、资金费或强平")
        finally:
            db.conn.close()
        return
    rnd = ctx.require_active()
    pos_mode = await ctx.pos_mode()
    _, swap = await ctx.specs()
    hedger = Hedger(ctx.api, ctx.db, cfg, rnd.id, swap, pos_mode, ctx.out)
    assistant = SellAssistant(ctx.api, ctx.osl, ctx.db, cfg, rnd.id, hedger, ctx.out, maker=maker)
    await assistant.run(close_first=False if maker else close_first or cfg.sell.order == "close_first")


async def cmd_round_close(ctx: Ctx, force: bool) -> None:
    cfg, out, api, db = ctx.cfg, ctx.out, ctx.api, ctx.db
    rnd = ctx.require_active()
    pos_mode = await ctx.pos_mode()
    _, swap = await ctx.specs()
    if db.open_orders(rnd.id):
        raise SystemExit("还有没结束的订单。先运行 offramp buy resume 让程序处理完,再收尾")
    if rnd.status == "selling":
        remaining = (rnd.arrived_eth or ZERO) - db.sold_total(rnd.id)
    elif rnd.status == "new":
        remaining = ZERO
    else:
        remaining = bought_eth(db.orders(rnd.id))
    if remaining >= cfg.osl.min_qty:
        if not force:
            raise SystemExit(f"还有 {fmt(remaining)} ETH 没卖。收尾会平掉它们的空单,这部分 ETH 就不再对冲了。"
                             f"确定要这样做,加 --force")
        if (await _ask(f"确认平掉 {fmt(remaining)} ETH 的对冲?(y/n)> ")).strip().lower() != "y":
            raise SystemExit("已取消")
    hedger = Hedger(api, db, cfg, rnd.id, swap, pos_mode, out)
    res = await hedger.align(ZERO, allow_open=False, allow_close=True)
    if not res.ok:
        raise SystemExit(f"空单还剩 {fmt(res.pos.short_ct * swap.ct_val)} ETH 没平掉,请重试 offramp round close")
    end = (await api.balances(["USDT"]))["USDT"].cash_bal
    db.update_round(rnd.id, status="closed", end_usdt=end, closed_at=time.time())
    db.log(rnd.id, "round", f"收尾,交易账户 USDT {end}")
    out.ok(f"第 {rnd.id} 轮已收尾,交易账户 USDT {fmt(end, 2)}。"
           f"对账:offramp report --osl-usd <OSL 本轮实际收到的 USD>")


async def cmd_report(ctx: Ctx, round_id: int | None, all_rounds: bool, osl_usd: str | None, gas_eth: str | None) -> None:
    _, swap = await ctx.specs()
    if all_rounds:
        rounds = ctx.db.all_rounds()
        if osl_usd:
            raise SystemExit("--osl-usd 只能配合单个轮次使用")
    else:
        rnd = ctx.db.get_round(round_id) if round_id else ctx.db.latest_round()
        if rnd is None:
            raise SystemExit("找不到这个轮次")
        rounds = [rnd]
    totals = []
    for rnd in rounds:
        totals.append(await print_report(ctx.api, ctx.db, ctx.cfg, rnd, swap, ctx.out,
                                         osl_usd=dec(osl_usd) if osl_usd else None,
                                         gas_eth=dec(gas_eth) if gas_eth else None))
    if all_rounds:
        done = [t for t in totals if t.consumed_usdt and t.osl_usd is not None]
        consumed = sum((t.consumed_usdt for t in done), ZERO)
        received = sum((t.osl_usd for t in done), ZERO)
        ctx.out.head("")
        ctx.out.head(f"==== 合计({len(done)} 轮有完整数据)====")
        ctx.out.plain(f"消耗 USDT {fmt(consumed, 2)},收到 USD {fmt(received, 2)},"
                      f"兑换率 {fmt(received / consumed, 5) if consumed else '-'}")


# ---- argument parsing -------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="offramp", description="OKX 买 ETH + 1x 空单对冲,OSL 手动卖出时同步平空")
    sub = p.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="只读预检:API、账户模式、杠杆、规格、费率、余额")
    check_amount = check.add_mutually_exclusive_group()
    check_amount.add_argument("--usdt", help="同时预估现货预算所需总资金和对冲步长")
    check_amount.add_argument("--total-usdt", help="按总资金计算最大现货和 1x 空单")
    check.add_argument("--reserve-usdt", help="总资金模式固定预留,默认总额 0.1%%、最低 1 USDT")
    m = sub.add_parser("market", help="实时行情和兑换率(只读)")
    m.add_argument("--once", action="store_true")

    r = sub.add_parser("round", help="轮次:new / close")
    rs = r.add_subparsers(dest="action", required=True)
    rn = rs.add_parser("new", help="新建一轮(可选从资金账户划转 USDT 到交易账户)")
    rn.add_argument("--usdt", help="从资金账户划转多少 USDT 到交易账户")
    rc = rs.add_parser("close", help="收尾:平掉剩余空单尾数,记录结束余额")
    rc.add_argument("--force", action="store_true", help="还有 ETH 没卖也收尾")

    b = sub.add_parser("buy", help="P1:maker 买 ETH + 同步开空")
    b.add_argument("action", nargs="?", choices=["resume"], help="resume = 中断后继续")
    amount = b.add_mutually_exclusive_group()
    amount.add_argument("--usdt", help="本轮用多少 USDT 买 ETH")
    amount.add_argument("--total-usdt", help="本轮现货、1x 空单保证金及费用的总资金上限")
    b.add_argument("--reserve-usdt", help="总资金模式固定预留,默认总额 0.1%%、最低 1 USDT")
    b.add_argument("--dry-run", action="store_true", help="本地模拟现货分批成交和永续开空,不发送真实订单")
    b.add_argument("--paper-usdt", help="仅 dry-run:使用指定虚拟 USDT 余额,无需提前转入资金")
    b.add_argument("--paper-slice-eth", help="仅 dry-run:覆盖本次每单 ETH 上限,不修改实盘配置")

    sub.add_parser("status", help="余额、空单、本轮状态")
    o = sub.add_parser("okx", help="OKX 资金操作")
    os_ = o.add_subparsers(dest="action", required=True)
    tf = os_.add_parser("to-funding", help="把本轮 ETH 从交易账户转到资金账户(之后你手动提币)")
    tf.add_argument("--eth", help="数量,默认本轮买到的全部")

    s = sub.add_parser("sell", help="P3:你在 OSL 卖,回车后程序平掉同样数量的空单")
    s.add_argument("--close-first", action="store_true", help="先平空,再去 OSL 卖")
    s.add_argument("--maker", action="store_true", help="OSL 手动 maker,输入 sold 累计成交;只平空,禁用回车记成交")
    s.add_argument("--dry-run", action="store_true", help="本地模拟已有空单的分批平仓,不发送真实订单")
    s.add_argument("--paper-eth", help="仅 dry-run:假设 OSL 到账数量,初始化相应模拟空单")

    rp = sub.add_parser("report", help="PnL 对账")
    rp.add_argument("--round", type=int, dest="round_id")
    rp.add_argument("--all", action="store_true", dest="all_rounds")
    rp.add_argument("--osl-usd", help="OSL 本轮实际收到的 USD")
    rp.add_argument("--gas-eth", help="hot wallet 另外付的 gas(ETH)")
    return p


async def dispatch(args, cfg: Config) -> None:
    ctx = Ctx(cfg)
    try:
        if args.cmd == "check":
            await cmd_check(ctx, args.usdt, args.total_usdt, args.reserve_usdt)
        elif args.cmd == "market":
            await cmd_market(ctx, args.once)
        elif args.cmd == "status":
            await cmd_status(ctx)
        elif args.cmd == "report":
            await cmd_report(ctx, args.round_id, args.all_rounds, args.osl_usd, args.gas_eth)
        else:
            with ctx.lock():
                if args.cmd == "round" and args.action == "new":
                    await cmd_round_new(ctx, args.usdt)
                elif args.cmd == "round" and args.action == "close":
                    await cmd_round_close(ctx, args.force)
                elif args.cmd == "buy":
                    await cmd_buy(ctx, args.action, args.usdt, args.dry_run, args.paper_usdt, args.paper_slice_eth,
                                  args.total_usdt, args.reserve_usdt)
                elif args.cmd == "okx" and args.action == "to-funding":
                    await cmd_to_funding(ctx, args.eth)
                elif args.cmd == "sell":
                    await cmd_sell(ctx, args.close_first, args.maker, args.dry_run, args.paper_eth)
    except HaltError as e:
        ctx.out.error(f"程序停止:{e}")
        raise SystemExit(2)
    except (OkxError, OkxTimeout) as e:
        ctx.out.error(f"OKX 请求失败:{e}")
        raise SystemExit(1)
    finally:
        await ctx.close()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    cfg = load_config(Path.cwd())
    try:
        asyncio.run(dispatch(args, cfg))
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
