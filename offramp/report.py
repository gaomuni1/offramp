"""Per-round PnL: USDT consumed on OKX vs USD received on OSL, with a cost breakdown and a sanity check."""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from .config import Config
from .db import Db, Round
from .ledger import avg_fill_px, bought_eth, fee_sum, filled_qty, spent_usdt
from .models import Instrument, OkxError, OkxTimeout
from .util import ZERO, Console, fmt


@dataclass
class RoundNumbers:
    round_id: int
    consumed_usdt: Decimal | None
    osl_usd: Decimal | None


async def print_report(api, db: Db, cfg: Config, rnd: Round, swap: Instrument, out: Console,
                       osl_usd: Decimal | None = None, gas_eth: Decimal | None = None) -> RoundNumbers:
    orders = db.orders(rnd.id)
    fee_rate = cfg.osl.taker_fee

    spot_qty = filled_qty(orders, "maker_buy")
    spot_avg = avg_fill_px(orders, "maker_buy")
    spot_notional = spent_usdt(orders)
    spot_fee_eth = fee_sum(orders, ("maker_buy",), "ETH")
    spot_fee_usdt = fee_sum(orders, ("maker_buy",), "USDT")
    held = bought_eth(orders)
    open_eth = filled_qty(orders, "hedge_open") * swap.ct_val
    close_eth = filled_qty(orders, "hedge_close") * swap.ct_val
    open_avg = avg_fill_px(orders, "hedge_open")
    close_avg = avg_fill_px(orders, "hedge_close")
    perp_fee = fee_sum(orders, ("hedge_open", "hedge_close"), "USDT")
    perp_pnl = sum((o.pnl for o in orders if o.purpose == "hedge_close"), ZERO)

    funding: Decimal | None = None
    try:
        end_ms = int((rnd.closed_at or time.time()) * 1000)
        funding = await api.funding_fee_sum(cfg.okx.swap_inst, int(rnd.created_at * 1000), end_ms)
    except (OkxError, OkxTimeout) as e:
        out.warn(f"资金费读取失败:{e}")

    sells = db.sells(rnd.id)
    sold = sum((s.qty for s in sells), ZERO)
    active = [s for s in sells if s.kind == "sell" and not s.undone]
    priced = [s for s in active if s.px is not None]
    gross_priced = sum((s.qty * s.px for s in priced), ZERO)
    osl_estimate = None
    if priced and sum((s.qty for s in priced), ZERO) == sold:
        osl_estimate = gross_priced * (1 - fee_rate)

    if osl_usd is not None:
        db.update_round(rnd.id, osl_usd=osl_usd)
    osl_final = osl_usd if osl_usd is not None else (rnd.osl_usd if rnd.osl_usd is not None else osl_estimate)
    arrived = rnd.arrived_eth
    consumed = rnd.start_usdt - rnd.end_usdt if rnd.start_usdt is not None and rnd.end_usdt is not None else None

    out.head("")
    out.head(f"====== 第 {rnd.id} 轮对账({rnd.status})======")
    out.plain(f"本轮开始时交易账户 USDT          {fmt(rnd.start_usdt, 2)}")
    out.plain(f"本轮结束时交易账户 USDT          {fmt(rnd.end_usdt, 2) if rnd.end_usdt is not None else '未收尾(先 offramp round close)'}")
    out.plain(f"→ 消耗 USDT                       {fmt(consumed, 2)}")
    src = "你输入" if osl_usd is not None or rnd.osl_usd is not None else ("按你报的成交价估算" if osl_estimate else "缺")
    out.plain(f"OSL 收到 USD({src})            {fmt(osl_final, 2) if osl_final is not None else '缺:用 --osl-usd 输入'}")
    rate = osl_final / consumed if osl_final is not None and consumed else None
    out.plain(f"→ 实际兑换率                      {fmt(rate, 5)} USD/USDT")
    if gas_eth and consumed and osl_final is not None and spot_avg:
        rate_gas = osl_final / (consumed + gas_eth * spot_avg)
        out.plain(f"→ 含另付 gas 的兑换率             {fmt(rate_gas, 5)}")

    out.plain("")
    out.plain("明细")
    out.plain(f"  现货买入 {fmt(spot_qty, 6)} ETH @ {fmt(spot_avg, 2)} = {fmt(spot_notional, 2)} USDT;"
              f"扣费后到手 {fmt(held, 6)} ETH")
    out.plain(f"  永续开空 {fmt(open_eth)} ETH @ {fmt(open_avg, 2)};平空 {fmt(close_eth)} ETH @ {fmt(close_avg, 2)};"
              f"平仓盈亏 {fmt(perp_pnl, 4)} USDT")
    out.plain(f"  OSL 到账 {fmt(arrived)} ETH;已卖 {fmt(sold)} ETH")

    if consumed is not None:
        known = []
        spot_fee_cost = -spot_fee_usdt + (-spot_fee_eth * spot_avg if spot_avg else ZERO)
        known.append(("现货买入手续费", spot_fee_cost))
        known.append(("永续开平仓手续费", -perp_fee))
        if funding is not None:
            known.append(("资金费(付出为正,收到为负)", -funding))
        if arrived is not None and spot_avg:
            known.append(("转账损耗(提币费 + gas)", (held - arrived) * spot_avg))
            if arrived - sold > 0:
                known.append(("OSL 没卖掉的尾数(还在你账户里)", (arrived - sold) * spot_avg))
        if osl_final is not None:
            known.append(("OSL taker 手续费", osl_final / (1 - fee_rate) * fee_rate))
        out.plain("")
        out.plain("成本分解(按 1 USD ≈ 1 USDT 近似,正数 = 成本)")
        for name, value in known:
            out.plain(f"  {name:<28}{fmt(value, 4)}")
        if osl_final is not None:
            rest = consumed - osl_final - sum((v for _, v in known), ZERO)
            out.plain(f"  {'价格因素(OSL 卖价 vs OKX、基差、时间差)':<28}{fmt(rest, 4)}")

        # OKX cash check: every USDT that left the trading account should be explained by our own orders.
        predicted = spot_notional - spot_fee_usdt - perp_pnl - perp_fee - (funding or ZERO)
        unexplained = consumed - predicted
        out.plain("")
        flag = "✓" if abs(unexplained) < Decimal("0.05") else "⚠ 有费用或成交没被记录,或者这期间交易账户有别的操作"
        out.plain(f"OKX 核对:按订单算应消耗 {fmt(predicted, 2)},实际 {fmt(consumed, 2)},未解释差额 {fmt(unexplained, 4)} {flag}")

    if priced:
        out.plain("")
        out.plain(f"OSL 成交核对:你报了均价的 {len(priced)} 笔合计 {fmt(gross_priced * (1 - fee_rate), 2)} USD(已扣 taker 费)")
    return RoundNumbers(rnd.id, consumed, osl_final)

