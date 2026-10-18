"""In-memory OKX / OSL stand-ins with knobs for the awkward cases (lag, timeouts, partial fills...)."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

from offramp.buy_engine import BuyEngine
from offramp.config import Config
from offramp.db import Db
from offramp.hedger import Hedger
from offramp.models import Balance, Book, Instrument, Level, OkxError, OkxTimeout, OrderInfo, Position
from offramp.util import ZERO, Console, floor_step

SPOT = "ETH-USDT"
SWAP = "ETH-USDT-SWAP"
D = Decimal


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def book(bid, ask, size="5", n=3) -> Book:
    return Book([Level(D(str(bid)), D(size), n)], [Level(D(str(ask)), D(size), n)])


class FakeOkx:
    def __init__(self):
        self.books_ = {SPOT: book("2480.00", "2480.01"), SWAP: book("2481.00", "2481.01")}
        self.short_ct = ZERO
        self.entry_px = ZERO
        self.lag_after_fill = 0      # position() keeps returning the old value this many times after a fill
        self._stale_reads = 0
        self._stale_value = ZERO
        self.orders: dict[str, OrderInfo] = {}
        self.placed: list[str] = []
        self.ioc_script: list[str] = []  # fill | none | half | reject | timeout_placed | timeout_lost
        self.usdt_eq = D(1000)
        self.spot_fee_rate = D("0.001")
        self.postonly_reject_all = False
        self.fill_on_cancel: Decimal | None = None
        self.fail_books = 0
        self.funding = ZERO

    # ---- reads ----
    async def books(self, inst_id, sz=5):
        if self.fail_books:
            self.fail_books -= 1
            raise OkxTimeout("books")
        return deepcopy(self.books_[inst_id])

    async def position(self, inst_id, pos_mode):
        if self._stale_reads > 0:
            self._stale_reads -= 1
            return Position(self._stale_value, mgn_ratio=D(100))
        return Position(self.short_ct, avg_px=self.entry_px, mgn_ratio=D(100))

    async def balances(self, ccys):
        return {c: Balance(c, eq=self.usdt_eq if c == "USDT" else ZERO, cash_bal=self.usdt_eq if c == "USDT" else ZERO,
                           avail_bal=self.usdt_eq if c == "USDT" else ZERO) for c in ccys}

    async def price_limit(self, inst_id):
        return (None, None)

    async def funding_fee_sum(self, inst_id, begin_ms, end_ms):
        return self.funding

    async def get_order(self, inst_id, cl_ord_id):
        o = self.orders.get(cl_ord_id)
        return deepcopy(o) if o else None

    # ---- writes ----
    def _set_short(self, new: Decimal) -> None:
        if self.lag_after_fill:
            self._stale_value = self.short_ct
            self._stale_reads = self.lag_after_fill
        self.short_ct = new

    async def place_order(self, inst_id, td_mode, side, ord_type, sz, px, cl_ord_id, reduce_only=False, pos_side=None):
        existing = self.orders.get(cl_ord_id)
        if existing and not existing.terminal:
            raise OkxError("51016", "Duplicated clOrdId")
        oid = f"o{len(self.orders) + 1}"
        if ord_type == "ioc":
            action = self.ioc_script.pop(0) if self.ioc_script else "fill"
            if action == "reject":
                raise OkxError("51008", "Order failed. Insufficient margin")
            if action == "timeout_lost":
                raise OkxTimeout("request lost")
            if reduce_only and side == "buy" and sz > self.short_ct:
                raise OkxError("51169", "reduce-only order size exceeds position")
            frac = {"fill": D(1), "timeout_placed": D(1), "half": D("0.5"), "none": ZERO}[action]
            filled = floor_step(sz * frac, D("0.01"))
            b = self.books_[SWAP]
            fill_px = b.best_ask if side == "buy" else b.best_bid
            pnl = ZERO
            if filled:
                if side == "sell":
                    total = self.short_ct + filled
                    self.entry_px = (self.entry_px * self.short_ct + fill_px * filled) / total
                    self._set_short(total)
                else:
                    pnl = (self.entry_px - fill_px) * filled * D("0.1")
                    self._set_short(self.short_ct - filled)
            fee = -(filled * D("0.1") * fill_px * D("0.0005"))
            self.orders[cl_ord_id] = OrderInfo(inst_id, cl_ord_id, oid, side, "ioc", px, sz,
                                               "filled" if filled == sz else "canceled", filled,
                                               fill_px if filled else ZERO, fee, "USDT", pnl)
            self.placed.append(cl_ord_id)
            if action == "timeout_placed":
                raise OkxTimeout("placed, answer lost")
            return oid
        assert ord_type == "post_only"
        state = "live"
        if self.postonly_reject_all or (side == "buy" and px >= self.books_[inst_id].best_ask):
            state = "canceled"
        self.orders[cl_ord_id] = OrderInfo(inst_id, cl_ord_id, oid, side, ord_type, px, sz, state)
        self.placed.append(cl_ord_id)
        return oid

    async def cancel_order(self, inst_id, cl_ord_id):
        o = self.orders.get(cl_ord_id)
        if o is None or o.terminal:
            raise OkxError("51400", "Cancellation failed as the order has been filled, canceled or does not exist")
        if self.fill_on_cancel:
            self.fill_maker(self.fill_on_cancel, cl_ord_id)
            self.fill_on_cancel = None
        if not o.terminal:
            o.state = "canceled"

    # ---- test helpers ----
    def live_maker(self) -> OrderInfo | None:
        return next((o for o in self.orders.values() if o.ord_type == "post_only" and not o.terminal), None)

    def fill_maker(self, qty, cl_ord_id: str | None = None) -> Decimal:
        o = self.orders[cl_ord_id] if cl_ord_id else self.live_maker()
        qty = min(D(str(qty)), o.sz - o.acc_fill_sz)
        new_acc = o.acc_fill_sz + qty
        o.avg_px = (o.avg_px * o.acc_fill_sz + o.px * qty) / new_acc
        o.acc_fill_sz = new_acc
        o.fee -= qty * self.spot_fee_rate
        o.fee_ccy = "ETH"
        o.state = "filled" if new_acc == o.sz else "partially_filled"
        return qty

    def hedge_orders(self) -> list[OrderInfo]:
        return [self.orders[c] for c in self.placed if self.orders.get(c) and self.orders[c].ord_type == "ioc"]


class FakeOsl:
    def __init__(self, bid="2478.50", ask="2478.60"):
        self.bid, self.ask = D(bid), D(ask)

    async def ticker(self):
        return self.bid, self.ask


SPOT_SPEC = Instrument(SPOT, D(1), D("0.000001"), D("0.0001"), D("0.01"))
SWAP_SPEC = Instrument(SWAP, D("0.1"), D("0.01"), D("0.01"), D("0.01"))


def make_env(budget="72", status="buying") -> SimpleNamespace:
    clock = FakeClock()
    cfg = Config()
    db = Db(":memory:")
    rid = db.create_round(clock())
    db.update_round(rid, buy_budget=D(budget), status=status, start_usdt=D(1000))
    api = FakeOkx()
    out = Console(echo=False)
    hedger = Hedger(api, db, cfg, rid, SWAP_SPEC, "net_mode", out, clock=clock, sleep=clock.sleep)
    engine = BuyEngine(api, db, cfg, rid, SPOT_SPEC, SWAP_SPEC, hedger, out, clock=clock, sleep=clock.sleep)
    return SimpleNamespace(clock=clock, cfg=cfg, db=db, rid=rid, api=api, out=out, hedger=hedger, engine=engine)
