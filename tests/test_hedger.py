import asyncio
from decimal import Decimal as D

import pytest

from offramp.models import HaltError, OrderRow
from tests.fakes import make_env


def run(coro):
    return asyncio.run(coro)


def test_open_to_target():
    env = make_env()
    res = run(env.hedger.align(D("0.29"), allow_open=True, allow_close=False))
    assert res.ok and env.api.short_ct == D("0.29")
    assert len(env.api.hedge_orders()) == 1


def test_partial_ioc_is_completed_on_next_attempt():
    env = make_env()
    env.api.ioc_script = ["half", "fill"]
    res = run(env.hedger.align(D("0.20"), allow_open=True, allow_close=False))
    assert res.ok and env.api.short_ct == D("0.20")
    assert len(env.api.hedge_orders()) == 2


def test_no_fill_three_times_reports_not_ok():
    env = make_env()
    env.api.ioc_script = ["none", "none", "none"]
    res = run(env.hedger.align(D("0.10"), allow_open=True, allow_close=False))
    assert not res.ok and res.anomaly is None and env.api.short_ct == 0


def test_timeout_but_placed_is_found_and_not_sent_twice():
    env = make_env()
    env.api.ioc_script = ["timeout_placed"]
    res = run(env.hedger.align(D("0.10"), allow_open=True, allow_close=False))
    assert res.ok and env.api.short_ct == D("0.10")
    assert len(env.api.hedge_orders()) == 1


def test_timeout_lost_is_marked_not_placed_then_retried():
    env = make_env()
    env.api.ioc_script = ["timeout_lost"]
    res = run(env.hedger.align(D("0.10"), allow_open=True, allow_close=False))
    assert res.ok and env.api.short_ct == D("0.10")
    states = [o.state for o in env.db.orders(env.rid)]
    assert states == ["not_placed", "filled"]


def test_position_lag_does_not_double_hedge():
    env = make_env()
    env.api.lag_after_fill = 3
    res = run(env.hedger.align(D("0.10"), allow_open=True, allow_close=False))
    assert res.ok and env.api.short_ct == D("0.10")
    assert len(env.api.hedge_orders()) == 1


def test_position_never_updates_is_an_anomaly():
    env = make_env()
    env.api.lag_after_fill = 1000
    res = run(env.hedger.align(D("0.10"), allow_open=True, allow_close=False))
    assert res.anomaly is not None
    assert len(env.api.hedge_orders()) == 1  # did not keep sending


def test_close_never_flips_long():
    env = make_env()
    env.api.short_ct = D("0.05")
    res = run(env.hedger.align(D("0"), allow_open=False, allow_close=True))
    assert res.ok and env.api.short_ct == 0
    o = env.api.hedge_orders()[0]
    assert o.side == "buy"


def test_direction_not_allowed_is_reported():
    env = make_env()
    env.api.short_ct = D("0.30")
    res = run(env.hedger.align(D("0.10"), allow_open=True, allow_close=False))
    assert res.anomaly and env.api.short_ct == D("0.30")


def test_eth_to_contracts_rounds_to_nearest_lot():
    env = make_env()
    assert env.hedger.eth_to_ct(D("0.028971")) == D("0.29")
    assert env.hedger.eth_to_ct(D("0.0003996")) == D("0")
    assert env.hedger.eth_to_ct(D("0.0006993")) == D("0.01")


def test_pending_ioc_is_recovered_and_position_confirmed_before_new_order():
    env = make_env()
    env.api.lag_after_fill = 3
    cid = env.db.next_cl_ord_id(env.rid, "h")
    row = OrderRow(cid, env.rid, "hedge_open", "ETH-USDT-SWAP", "sell", "ioc", D(2480), D("0.1"),
                   state="unknown", created_at=env.clock())
    env.db.insert_order(row)
    run(env.api.place_order(row.inst_id, "cross", "sell", "ioc", row.sz, row.px, cid))
    result = run(env.hedger.align(D("0.1"), allow_open=True, allow_close=False))
    assert result.ok
    assert len(env.api.hedge_orders()) == 1
    assert env.api.short_ct == D("0.1")
