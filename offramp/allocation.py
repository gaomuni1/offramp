"""Total-capital allocation using Decimal arithmetic and exchange quantity steps.

Spot maker fees are charged in ETH (signed OKX fee convention). Perpetual fees
are paid in USDT. Reserve includes neither future funding nor future price moves.
"""
from dataclasses import dataclass
from decimal import Decimal

from .models import Instrument
from .util import ZERO, floor_step, round_step


@dataclass(frozen=True)
class Allocation:
    total_usdt: Decimal
    reserve_usdt: Decimal
    maker_fee: Decimal
    taker_fee: Decimal

    def __post_init__(self):
        if not all(v.is_finite() for v in (self.total_usdt, self.reserve_usdt, self.maker_fee, self.taker_fee)):
            raise ValueError("总资金、预留和费率必须是有限数字")
        if not ZERO < self.reserve_usdt < self.total_usdt:
            raise ValueError("预留必须大于 0 且小于总资金")
        if not -1 < self.maker_fee < 1 or not -1 < self.taker_fee < 1:
            raise ValueError("费率超出有效范围")


@dataclass(frozen=True)
class FundingNeed:
    qty: Decimal
    net_eth: Decimal
    target_ct: Decimal
    spot_cost: Decimal
    margin: Decimal
    open_fee: Decimal
    execution_buffer: Decimal
    required_equity: Decimal


def funding_need(qty: Decimal, *, allocation: Allocation, swap: Instrument,
                 spot_px: Decimal, perp_bid: Decimal, perp_ask: Decimal,
                 held: Decimal = ZERO, short_ct: Decimal = ZERO,
                 slippage_bps: Decimal = Decimal(15)) -> FundingNeed:
    """1x cross margin; existing equity already includes existing position UPL.

Reserve worst allowed execution loss on NEW short contracts, relative to ask.
Existing margin is counted once; ETH spot fees reduce the hedge quantity.
"""
    net_eth = held + qty * (1 + allocation.maker_fee)
    target_ct = round_step(net_eth / swap.ct_val, swap.lot_sz)
    target_eth = target_ct * swap.ct_val
    added_eth = max(ZERO, target_ct - short_ct) * swap.ct_val
    spot_cost = qty * spot_px
    margin = target_eth * perp_ask
    open_fee = added_eth * perp_ask * max(ZERO, -allocation.taker_fee)
    worst_sell = perp_bid * (1 - slippage_bps / 10000)
    execution_buffer = added_eth * max(ZERO, perp_ask - worst_sell)
    need = spot_cost + margin + open_fee + execution_buffer + allocation.reserve_usdt
    return FundingNeed(qty, net_eth, target_ct, spot_cost, margin, open_fee, execution_buffer, need)


def affordable_qty(*, max_qty: Decimal, equity: Decimal, spot: Instrument, **kwargs) -> FundingNeed:
    """Largest spot lot whose monotone funding requirement fits the equity cap."""
    low, high = 0, max(0, int(floor_step(max_qty, spot.lot_sz) / spot.lot_sz))
    while low < high:
        mid = (low + high + 1) // 2
        if funding_need(spot.lot_sz * mid, **kwargs).required_equity <= equity:
            low = mid
        else:
            high = mid - 1
    return funding_need(spot.lot_sz * low, **kwargs)
