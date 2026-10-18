from decimal import Decimal as D

from offramp.models import Book, Level, OrderInfo, OrderRow
from offramp.util import ceil_step, floor_step, fmt, round_step


def test_step_rounding():
    assert floor_step(D("0.0287"), D("0.01")) == D("0.02")
    assert ceil_step(D("0.0201"), D("0.01")) == D("0.03")
    assert round_step(D("0.285"), D("0.01")) == D("0.29")
    assert round_step(D("0.2849"), D("0.01")) == D("0.28")


def test_fmt():
    assert fmt(D("0.0300")) == "0.03"
    assert fmt(D("100")) == "100"
    assert fmt(D("0")) == "0"
    assert fmt(None) == "-"
    assert fmt(D("2480.456"), 2) == "2480.46"


def test_best_bid_excludes_own_order():
    b = Book([Level(D("100.02"), D("0.01"), 1), Level(D("100.01"), D("3"), 4)], [Level(D("100.05"), D("1"), 2)])
    assert b.best_bid_excluding(D("100.02"), D("0.01")) == D("100.01")  # only us at the top
    assert b.best_bid_excluding(None) == D("100.02")
    shared = Book([Level(D("100.02"), D("2"), 3)], [])
    assert shared.best_bid_excluding(D("100.02"), D("0.01")) == D("100.02")  # others are there too


def _info(state, acc, fee="0"):
    return OrderInfo("ETH-USDT", "c1", "o1", "buy", "post_only", D("100"), D("1"), state, D(acc), D("100"), D(fee), "ETH")


def test_order_apply_is_monotonic():
    row = OrderRow("c1", 1, "maker_buy", "ETH-USDT", "buy", "post_only", D("100"), D("1"))
    assert row.apply(_info("partially_filled", "0.4", "-0.0004"))
    assert row.acc_fill_sz == D("0.4")
    # a stale snapshot arriving late cannot shrink the fill
    row.apply(_info("live", "0.1"))
    assert row.acc_fill_sz == D("0.4")
    row.apply(_info("canceled", "0.5", "-0.0005"))
    assert row.state == "canceled" and row.acc_fill_sz == D("0.5")
    # a final state never reverts
    row.apply(_info("live", "0.5"))
    assert row.state == "canceled"
    # the same snapshot twice changes nothing
    assert not row.apply(_info("canceled", "0.5", "-0.0005"))
