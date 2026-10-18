"""Numbers derived from the order records (never from balances, which other activity can move)."""
from __future__ import annotations

from decimal import Decimal

from .models import OrderRow
from .util import ZERO


def bought_eth(orders: list[OrderRow]) -> Decimal:
    """ETH actually received from the maker buys: filled size plus the fee when OKX charges it in ETH
    (OKX fee sign: negative = charged, positive = rebate)."""
    total = ZERO
    for o in orders:
        if o.purpose != "maker_buy":
            continue
        total += o.acc_fill_sz
        if o.fee_ccy == "ETH":
            total += o.fee
    return total


def spent_usdt(orders: list[OrderRow]) -> Decimal:
    return sum((o.acc_fill_sz * o.avg_px for o in orders if o.purpose == "maker_buy"), ZERO)


def filled_qty(orders: list[OrderRow], purpose: str) -> Decimal:
    return sum((o.acc_fill_sz for o in orders if o.purpose == purpose), ZERO)


def avg_fill_px(orders: list[OrderRow], purpose: str) -> Decimal | None:
    qty = filled_qty(orders, purpose)
    if qty == 0:
        return None
    return sum((o.acc_fill_sz * o.avg_px for o in orders if o.purpose == purpose), ZERO) / qty


def fee_sum(orders: list[OrderRow], purposes: tuple[str, ...], ccy: str) -> Decimal:
    return sum((o.fee for o in orders if o.purpose in purposes and o.fee_ccy == ccy), ZERO)
