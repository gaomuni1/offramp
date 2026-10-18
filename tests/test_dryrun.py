import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from offramp.buy_engine import BuyEngine
from offramp.cli import build_parser, cmd_buy, cmd_sell
from offramp.dryrun import DryRunOkx
from offramp.ledger import bought_eth, spent_usdt
from offramp.models import Book, Level, OkxError
from tests.fakes import D, SPOT, SWAP, SPOT_SPEC, SWAP_SPEC, FakeOsl, book, make_env


@pytest.mark.parametrize("mode", ["net_mode", "long_short_mode"])
def test_standalone_sell_simulation_only_closes_local_short(monkeypatch, mode):
    env = make_env(status="new")
    ctx = context(env)
    ctx.osl = FakeOsl()
    ctx.pos_mode.return_value = mode
    before = env.db.get_round(env.rid)
    for name in ("balances", "place_order", "cancel_order", "set_leverage", "transfer", "position"):
        setattr(env.api, name, AsyncMock(side_effect=AssertionError("real account call")))
    answers = iter(["", "sold 0.2", "y", "sold 0.2", "sold 0.5", "y", "sold 1", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    args = build_parser().parse_args(["sell", "--maker", "--dry-run", "--paper-eth", "1"])
    asyncio.run(cmd_sell(ctx, args.close_first, args.maker, args.dry_run, args.paper_eth))
    assert env.db.get_round(env.rid) == before
    assert not env.db.orders(env.rid)
    closes = [line for line in env.out.lines if "OKX 买入平空" in line]
    assert len(closes) == 3
    assert any("剩余模拟空单 0 ETH" in line for line in env.out.lines)
    assert any("冻结/保证金 0.0000 USDT" in line for line in env.out.lines)
    assert not any("卖出开空" in line for line in env.out.lines)
    for name in ("balances", "place_order", "cancel_order", "set_leverage", "transfer", "position"):
        getattr(env.api, name).assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {"paper_eth": "1"}, {"dry_run": True}, {"dry_run": True, "paper_eth": "NaN"},
    {"dry_run": True, "paper_eth": "0"}, {"dry_run": True, "paper_eth": "Infinity"},
    {"maker": True, "close_first": True},
])
def test_sell_simulation_invalid_flags_fail_before_account_access(kwargs):
    args = {"close_first": False, **kwargs}
    with pytest.raises(SystemExit):
        asyncio.run(cmd_sell(None, **args))


def context(env):
    env.api.trade_fee = AsyncMock(return_value=(D("-0.0008"), D("-0.0005")))
    return SimpleNamespace(cfg=env.cfg, out=env.out, api=env.api, db=env.db,
                           pos_mode=AsyncMock(return_value="net_mode"),
                           specs=AsyncMock(return_value=(SPOT_SPEC, SWAP_SPEC)))


def test_zero_real_balance_can_quote_and_cancel_with_paper_money(monkeypatch):
    env = make_env(status="new")
    env.api.usdt_eq = D(0)
    ctx = context(env)
    before = env.db.get_round(env.rid)
    for name in ("balances", "place_order", "cancel_order", "set_leverage", "transfer", "position"):
        setattr(env.api, name, AsyncMock(side_effect=AssertionError("unexpected real account call")))

    async def one_quote_then_stop(engine):
        assert await engine.step() is None
        orders = engine.db.open_orders(engine.round_id, "maker_buy")
        assert len(orders) == 1
        assert orders[0].sz * orders[0].px <= D(20)
        await engine.finish(stopped=True)
        assert not engine.db.open_orders(engine.round_id)
        return "stopped"

    monkeypatch.setattr(BuyEngine, "run", one_quote_then_stop)
    args = build_parser().parse_args(["buy", "--usdt", "20", "--dry-run", "--paper-usdt", "50"])
    asyncio.run(cmd_buy(ctx, args.action, args.usdt, args.dry_run, args.paper_usdt))
    assert env.db.get_round(env.rid) == before
    assert not env.db.orders(env.rid)
    assert any("[dry-run] 下单" in line for line in env.out.lines)
    assert any("[dry-run] 撤单" in line for line in env.out.lines)


def test_paper_money_is_rejected_without_dry_run_before_account_access():
    with pytest.raises(SystemExit, match="仅可与 --dry-run"):
        asyncio.run(cmd_buy(None, None, "20", False, "50"))


@pytest.mark.parametrize("balance", ["0", "-50", "NaN", "Infinity"])
def test_invalid_paper_balance_rejected_before_account_access(balance):
    with pytest.raises(SystemExit):
        asyncio.run(cmd_buy(None, None, "20", True, balance))


def test_ordinary_dry_run_still_checks_real_balance():
    env = make_env()
    env.api.usdt_eq = D(0)
    with pytest.raises(SystemExit, match="不足"):
        asyncio.run(cmd_buy(context(env), None, "20", True))
    assert not env.api.placed


def test_paper_budget_still_enforces_margin_requirement():
    env = make_env()
    with pytest.raises(SystemExit, match="不足"):
        asyncio.run(cmd_buy(context(env), None, "20", True, "25"))
    assert not env.api.placed


def test_paper_balances_do_not_mutate_real_balances():
    env = make_env()
    env.api.usdt_eq = D(0)
    dry = DryRunOkx(env.api, env.out, paper_usdt=D(50))
    balances = asyncio.run(dry.balances(["USDT", "ETH"]))
    assert balances["USDT"].eq == D(50)
    assert balances["ETH"].eq == 0
    assert env.api.usdt_eq == 0


async def simulator(cash="5000"):
    env = make_env(budget="2300")
    context(env)  # supplies read-only fee data
    env.api.usdt_eq = D(0)
    for name in ("place_order", "cancel_order", "set_leverage", "transfer", "position"):
        setattr(env.api, name, AsyncMock(side_effect=AssertionError("real account mutation/access")))
    dry = DryRunOkx(env.api, env.out, paper_usdt=D(cash), cfg=env.cfg)
    await dry.initialize(SPOT_SPEC, SWAP_SPEC)
    env.engine.api = dry
    env.hedger.api = dry
    return env, dry


@pytest.mark.parametrize("slice_eth, max_orders", [("0.01", 93), ("0.1", 10), ("0.2", 5)])
def test_full_5000_simulation_buys_2300_and_hedges_without_real_writes(slice_eth, max_orders):
    async def scenario():
        env, dry = await simulator()
        env.cfg.buy.max_slice_eth = D(slice_eth)
        for _ in range(400):
            env.clock.t += 1
            result = await env.engine.step()
            orders = env.db.orders(env.rid)
            spot_eth = bought_eth(orders)
            pos = await dry.position(SWAP, "net_mode")
            balances = await dry.balances(["USDT", "ETH"])
            assert spot_eth == balances["ETH"].cash_bal
            assert abs(spot_eth - pos.short_ct * SWAP_SPEC.ct_val) <= D("0.0005")
            assert spent_usdt(orders) <= D(2300)
            assert balances["USDT"].avail_bal > 0
            if result == "done":
                break
        else:
            pytest.fail("paper simulation did not finish")
        await env.engine.finish(stopped=False)
        await dry.print_summary()
        orders = env.db.orders(env.rid)
        spent = spent_usdt(orders)
        assert D(2300) - spent < SPOT_SPEC.min_sz * D(2480)
        assert env.db.get_round(env.rid).status == "bought"
        assert not env.db.open_orders(env.rid)
        assert len([o for o in orders if o.purpose == "maker_buy"]) <= max_orders
        assert dry._short_ct > 0
        hedge_fees = sum((o.fee for o in orders if o.purpose == "hedge_open"), D(0))
        assert dry._cash == D(5000) - spent + hedge_fees
        assert dry._eth == bought_eth(orders)
        assert any("模拟现货成交" in s for s in env.out.lines)
        assert any("模拟永续成交" in s for s in env.out.lines)
        for name in ("place_order", "cancel_order", "set_leverage", "transfer", "position"):
            getattr(env.api, name).assert_not_called()
    asyncio.run(scenario())


def test_partial_maker_cancel_preserves_fill_and_releases_unfilled_cash():
    async def scenario():
        env, dry = await simulator("50")
        await dry.place_order(SPOT, "cash", "buy", "post_only", D("0.01"), D(2480), "m1")
        before = (await dry.balances(["USDT"]))["USDT"]
        assert before.frozen_bal == D("24.8")
        first = await dry.get_order(SPOT, "m1")
        assert first.state == "partially_filled" and first.acc_fill_sz == D("0.005")
        assert first.fee == -D("0.005") * D("0.0008")
        await dry.cancel_order(SPOT, "m1")
        after = await dry.get_order(SPOT, "m1")
        assert after.acc_fill_sz == first.acc_fill_sz
        assert after.state == "canceled"
        balance = (await dry.balances(["USDT"]))["USDT"]
        assert balance.cash_bal == D("37.6") and balance.frozen_bal == 0
    asyncio.run(scenario())


def test_terminal_maker_reads_do_not_duplicate_fills_or_fees():
    async def scenario():
        _, dry = await simulator("50")
        await dry.place_order(SPOT, "cash", "buy", "post_only", D("0.001"), D(2480), "m1")
        await dry.get_order(SPOT, "m1")
        final = await dry.get_order(SPOT, "m1")
        balances = await dry.balances(["USDT", "ETH"])
        for _ in range(4):
            assert await dry.get_order(SPOT, "m1") == final
        assert await dry.balances(["USDT", "ETH"]) == balances
    asyncio.run(scenario())


def test_crossing_postonly_cancels_without_filling():
    async def scenario():
        _, dry = await simulator("50")
        await dry.place_order(SPOT, "cash", "buy", "post_only", D("0.001"), D(2481), "m1")
        order = await dry.get_order(SPOT, "m1")
        assert order.state == "canceled" and order.acc_fill_sz == 0
        assert dry._cash == D(50) and dry._eth == 0
    asyncio.run(scenario())


def test_ioc_respects_limit_depth_and_contract_units():
    async def scenario():
        env, dry = await simulator("50")
        env.api.books_[SWAP] = Book(
            [Level(D(2481), D("0.01")), Level(D(2480), D("0.02")), Level(D(2479), D("1"))],
            [Level(D(2482), D(10))])
        await dry.place_order(SWAP, "cross", "sell", "ioc", D("0.1"), D(2480), "h1")
        order = await dry.get_order(SWAP, "h1")
        assert order.state == "canceled" and order.acc_fill_sz == D("0.03")
        assert order.avg_px == (D(2481) + 2 * D(2480)) / 3
        assert order.fee == -(D(2481) * D("0.01") + D(2480) * D("0.02")) * D("0.1") * D("0.0005")
        assert dry._short_ct == D("0.03")
    asyncio.run(scenario())


def test_ioc_unmarketable_does_not_fill():
    async def scenario():
        _, dry = await simulator("50")
        await dry.place_order(SWAP, "cross", "sell", "ioc", D("0.1"), D(2500), "h1")
        order = await dry.get_order(SWAP, "h1")
        assert order.state == "canceled" and order.acc_fill_sz == 0
        assert dry._short_ct == 0 and dry._cash == D(50)
    asyncio.run(scenario())


def test_insufficient_margin_rejects_paper_short_without_side_effects():
    async def scenario():
        _, dry = await simulator("2")
        with pytest.raises(OkxError, match="保证金"):
            await dry.place_order(SWAP, "cross", "sell", "ioc", D("0.1"), D(2480), "h1")
        assert dry._short_ct == 0 and dry._cash == D(2)
        assert await dry.get_order(SWAP, "h1") is None
    asyncio.run(scenario())


@pytest.mark.parametrize("pos_mode", ["net_mode", "long_short_mode"])
def test_simulated_close_is_reduce_only_and_realizes_pnl(pos_mode):
    async def scenario():
        env, dry = await simulator("50")
        env.hedger.pos_mode = pos_mode
        opened = await env.hedger.align(D("0.1"), allow_open=True, allow_close=False)
        assert opened.ok
        env.api.books_[SWAP] = book("2400", "2400.01")
        closed = await env.hedger.align(D(0), allow_open=False, allow_close=True)
        assert closed.ok and dry._short_ct == 0
        assert dry._cash > D(50)
        with pytest.raises(OkxError, match="平空"):
            await dry.place_order(SWAP, "cross", "buy", "ioc", D("0.01"), D(2401), "bad", reduce_only=True)
    asyncio.run(scenario())


def test_real_write_methods_cannot_be_forwarded():
    env = make_env()
    dry = DryRunOkx(env.api, env.out, paper_usdt=D(50))
    assert not hasattr(dry, "transfer")
    assert not hasattr(dry, "_request")
    with pytest.raises(RuntimeError, match="不调用账户设置"):
        asyncio.run(dry.set_leverage(SWAP, 1, "cross"))


@pytest.mark.parametrize("budget, expected_size", [("2300", "0.1"), ("20", "0.008064")])
def test_paper_slice_override_is_local_and_budget_capped(monkeypatch, budget, expected_size):
    env = make_env(status="new")
    ctx = context(env)

    async def one_order(engine):
        assert engine.cfg.buy.max_slice_eth == D("0.1")
        assert ctx.cfg.buy.max_slice_eth == D("0.01")
        await engine.step()
        orders = engine.db.open_orders(engine.round_id, "maker_buy")
        assert len(orders) == 1 and orders[0].sz == D(expected_size)
        assert orders[0].px * orders[0].sz <= D(budget)
        await engine.finish(stopped=True)
        return "stopped"

    monkeypatch.setattr(BuyEngine, "run", one_order)
    args = build_parser().parse_args(["buy", "--usdt", budget, "--dry-run", "--paper-usdt", "5000",
                                      "--paper-slice-eth", "0.1"])
    asyncio.run(cmd_buy(ctx, args.action, args.usdt, args.dry_run, args.paper_usdt, args.paper_slice_eth))
    assert ctx.cfg.buy.max_slice_eth == D("0.01")
    assert env.api.placed == []
    assert not env.db.orders(env.rid)


def test_paper_slice_cannot_be_used_in_live_mode():
    with pytest.raises(SystemExit, match="--paper-slice-eth 仅可"):
        asyncio.run(cmd_buy(None, None, "20", False, paper_slice_eth="0.1"))


@pytest.mark.parametrize("size", ["0", "-1", "NaN", "Infinity", "abc"])
def test_invalid_paper_slice_fails_before_account_access(size):
    with pytest.raises(SystemExit):
        asyncio.run(cmd_buy(None, None, "20", True, paper_slice_eth=size))


def test_paper_slice_below_spot_minimum_is_rejected():
    with pytest.raises(SystemExit, match="低于现货最小"):
        asyncio.run(cmd_buy(context(make_env()), None, "20", True, "50", "0.000001"))
