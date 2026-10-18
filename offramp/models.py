"""Plain data types shared by the exchange clients, the engines and the database."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .util import ZERO

# "not_placed": the request timed out and the exchange never got the order.
TERMINAL_STATES = frozenset({"filled", "canceled", "mmp_canceled", "rejected", "not_placed"})


class OkxError(Exception):
    """The exchange answered and refused the request."""

    def __init__(self, code: str, msg: str):
        super().__init__(f"OKX {code}: {msg}")
        self.code = code
        self.msg = msg


class OkxTimeout(Exception):
    """No answer: the request may or may not have reached the exchange."""


class HaltError(Exception):
    """A mismatch the program must not fix on its own. Stop and let the user look."""


@dataclass
class Instrument:
    inst_id: str
    ct_val: Decimal  # ETH per contract; 1 for spot
    lot_sz: Decimal
    min_sz: Decimal
    tick_sz: Decimal


@dataclass
class Level:
    px: Decimal
    sz: Decimal
    n: int = 0  # number of orders at this price; 0 = unknown


def _best_excluding(levels: list[Level], own_px: Decimal | None, own_remaining: Decimal) -> Decimal | None:
    for lvl in levels:
        if own_px is not None and lvl.px == own_px and (lvl.n == 1 or lvl.sz - own_remaining <= 0):
            continue  # this level is only our own order
        return lvl.px
    return None


@dataclass
class Book:
    bids: list[Level]
    asks: list[Level]

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].px if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].px if self.asks else None

    def best_bid_excluding(self, own_px: Decimal | None, own_remaining: Decimal = ZERO) -> Decimal | None:
        return _best_excluding(self.bids, own_px, own_remaining)

    def best_ask_excluding(self, own_px: Decimal | None, own_remaining: Decimal = ZERO) -> Decimal | None:
        return _best_excluding(self.asks, own_px, own_remaining)


@dataclass
class OrderInfo:
    """An order as the exchange reports it."""

    inst_id: str
    cl_ord_id: str
    ord_id: str
    side: str
    ord_type: str
    px: Decimal | None
    sz: Decimal
    state: str
    acc_fill_sz: Decimal = ZERO
    avg_px: Decimal = ZERO
    fee: Decimal = ZERO  # OKX sign: negative = charged, positive = rebate
    fee_ccy: str = ""
    pnl: Decimal = ZERO

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass
class OrderRow:
    """An order as we track it locally (written before it is sent)."""

    cl_ord_id: str
    round_id: int
    purpose: str  # maker_buy | hedge_open | hedge_close
    inst_id: str
    side: str
    ord_type: str
    px: Decimal | None
    sz: Decimal
    state: str = "pending"  # pending | unknown | live | partially_filled | filled | canceled | rejected | not_placed
    ord_id: str = ""
    acc_fill_sz: Decimal = ZERO
    avg_px: Decimal = ZERO
    fee: Decimal = ZERO
    fee_ccy: str = ""
    pnl: Decimal = ZERO
    cancel_requested_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def remaining(self) -> Decimal:
        return max(self.sz - self.acc_fill_sz, ZERO)

    def apply(self, info: OrderInfo) -> bool:
        """Merge an exchange snapshot. Filled size only grows and a final state never reverts,
        so duplicate or out-of-order snapshots cannot double count. Returns True if anything changed."""
        before = (self.ord_id, self.state, self.acc_fill_sz, self.avg_px, self.fee, self.fee_ccy, self.pnl)
        if info.ord_id:
            self.ord_id = info.ord_id
        newer = info.acc_fill_sz > self.acc_fill_sz
        same_fill_final = info.acc_fill_sz == self.acc_fill_sz and (info.terminal or (self.fee == 0 and self.avg_px == 0))
        if newer or same_fill_final:
            self.acc_fill_sz = info.acc_fill_sz
            self.avg_px = info.avg_px
            self.fee = info.fee
            self.fee_ccy = info.fee_ccy
            self.pnl = info.pnl
        if not self.terminal:
            self.state = info.state
        return before != (self.ord_id, self.state, self.acc_fill_sz, self.avg_px, self.fee, self.fee_ccy, self.pnl)


@dataclass
class Position:
    short_ct: Decimal  # contracts short; a long position would show as negative
    avg_px: Decimal = ZERO
    upl: Decimal = ZERO
    mgn_ratio: Decimal | None = None
    liq_px: Decimal | None = None


@dataclass
class Balance:
    ccy: str
    eq: Decimal = ZERO
    cash_bal: Decimal = ZERO
    avail_bal: Decimal = ZERO
    frozen_bal: Decimal = ZERO


@dataclass
class AlignResult:
    ok: bool
    pos: Position
    anomaly: str | None = None
    filled_ct: Decimal = ZERO
    fills: list[tuple[Decimal, Decimal]] = field(default_factory=list)  # (contracts, avg px)

    def avg_px(self) -> Decimal | None:
        qty = sum((q for q, _ in self.fills), ZERO)
        if qty == 0:
            return None
        return sum((q * p for q, p in self.fills), ZERO) / qty
