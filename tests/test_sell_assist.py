import asyncio
from decimal import Decimal as D

from offramp.models import OrderRow
from offramp.report import print_report
from offramp.sell_assist import SellAssistant
from tests.fakes import SWAP_SPEC, FakeOsl, make_env


def run(coro):
    return asyncio.run(coro)


def bought_env(short_ct="0.29"):
    """A round whose buy is done: 0.029 ETH filled, 0.000029 ETH fee -> 0.028971 held, short 0.29 contracts."""
    env = make_env(status="bought")
    env.db.insert_order(OrderRow("m1", env.rid, "maker_buy", "ETH-USDT", "buy", "post_only", D("2480"), D("0.029"),
                                 state="filled", acc_fill_sz=D("0.029"), avg_px=D("2480"), fee=D("-0.000029"),
                                 fee_ccy="ETH", created_at=env.clock()))
    env.api.short_ct = D(short_ct)
    env.api.entry_px = D("2481")
    return env


def assistant(env, inputs):
    answers = list(inputs)

    async def ask(prompt):
        env.out.plain(f"{prompt}{answers[0] if answers else '<EOF>'}")
        return answers.pop(0) if answers else "q"

    return SellAssistant(env.api, FakeOsl(), env.db, env.cfg, env.rid, env.hedger, env.out, ask=ask, clock=env.clock)


def test_enter_sells_everything_and_closes_short():
    env = bought_env()
    result = run(assistant(env, ["0.0287", ""]).run())
    assert result == "done"
    assert env.api.short_ct == 0
    assert env.db.sold_total(env.rid) == D("0.0287")


def test_partial_sales_close_step_by_step():
    env = bought_env()
    run(assistant(env, ["0.0287", "0.01 @2478.5", "0.0187 @2478"]).run())
    closes = [o for o in env.api.hedge_orders() if o.side == "buy"]
    assert [o.acc_fill_sz for o in closes] == [D("0.10"), D("0.19")]
    assert env.api.short_ct == 0


def test_transfer_loss_closes_excess_short_first():
    env = bought_env()
    result = run(assistant(env, ["0.0270", "y", "q"]).run())   # well below what was bought -> asks to confirm
    assert result == "stopped"
    assert env.api.short_ct == D("0.27")


def test_input_larger_than_remaining_is_rejected():
    env = bought_env()
    run(assistant(env, ["0.0287", "0.05", "q"]).run())
    assert env.api.short_ct == D("0.29")
    assert env.db.sold_total(env.rid) == 0


def test_undo_reopens_after_confirmation():
    env = bought_env()
    run(assistant(env, ["0.0287", "0.02", "undo", "y", "q"]).run())
    assert env.db.sold_total(env.rid) == 0
    assert env.api.short_ct == D("0.29")


def test_fix_moves_target():
    env = bought_env()
    run(assistant(env, ["0.0287", "0.02", "fix 0.01", "y", "q"]).run())
    assert env.db.sold_total(env.rid) == D("0.01")
    assert env.api.short_ct == D("0.19")


def test_declined_reopen_leaves_short_alone():
    env = bought_env()
    run(assistant(env, ["0.0287", "0.02", "undo", "n", "q"]).run())
    assert env.api.short_ct == D("0.09")
    assert any("没有对冲" in line for line in env.out.lines)


def test_short_smaller_than_remaining_never_goes_long():
    env = bought_env(short_ct="0.05")       # e.g. part of the short was auto-deleveraged
    run(assistant(env, ["0.0287", "n", ""]).run())
    assert env.api.short_ct == 0


def test_rejected_close_is_retried_without_double_close():
    env = bought_env()
    env.api.ioc_script = ["reject", "reject", "reject"]
    result = run(assistant(env, ["0.0287", ""]).run())
    assert result == "done"
    assert env.api.short_ct == 0
    assert sum(o.acc_fill_sz for o in env.api.hedge_orders()) == D("0.29")


def test_report_reconciles():
    env = bought_env()
    run(assistant(env, ["0.0287", "0.0287 @2478.5"]).run())
    orders = env.db.orders(env.rid)
    # OKX cash: start - spot cost + pnl + fees (fees negative); no funding in the fake
    spot_cost = D("0.029") * D("2480")
    close = [o for o in orders if o.purpose == "hedge_close"]
    end = D(1000) - spot_cost + sum(o.pnl for o in close) + sum(o.fee for o in close)
    env.db.update_round(env.rid, end_usdt=end, status="closed", closed_at=env.clock())
    osl_usd = D("0.0287") * D("2478.5") * (1 - D("0.0005"))
    numbers = run(print_report(env.api, env.db, env.cfg, env.db.get_round(env.rid), SWAP_SPEC, env.out,
                               osl_usd=osl_usd))
    assert numbers.consumed_usdt == D(1000) - end
    assert any("未解释差额" in line and "✓" in line for line in env.out.lines)


def test_maker_cumulative_fills_and_duplicates():
    env = bought_env()
    helper = assistant(env, ["0.0287", "", "0.01", "sold 0.01", "sold 0.01", "sold 0.02", "sold 0.0287"])
    helper.maker = True
    assert run(helper.run()) == "done"
    assert env.db.sold_total(env.rid) == D("0.0287")
    closes = env.api.hedge_orders()
    assert all(o.side == "buy" for o in closes)
    assert [o.acc_fill_sz for o in closes] == [D(".10"), D(".10"), D(".09")]


def test_maker_rejects_invalid_totals_and_never_reopens_after_correction():
    env = bought_env()
    helper = assistant(env, ["0.0287", "sold NaN", "sold Infinity", "sold 0.03", "sold -1",
                             "sold 0.02", "sold 0.01", "fix NaN", "fix 0.01", "q"])
    helper.maker = True
    assert run(helper.run()) == "stopped"
    assert env.db.sold_total(env.rid) == D(".01")
    assert env.api.short_ct == D(".09")
    assert all(o.side == "buy" for o in env.api.hedge_orders())


def test_persistent_close_failure_does_not_claim_done():
    env = bought_env()
    env.api.ioc_script = ["reject"] * 20
    helper = assistant(env, ["0.0287", "sold 0.0287", "m", "q"])
    helper.maker = True
    assert run(helper.run()) == "stopped"
    assert env.api.short_ct == D(".29")
    assert not any("本轮卖完" in line for line in env.out.lines)


def test_maker_large_increment_needs_confirmation():
    env = make_env(status="bought")
    env.db.insert_order(OrderRow("m1", env.rid, "maker_buy", "ETH-USDT", "buy", "post_only", D("2480"), D("2.05"),
                                 state="filled", acc_fill_sz=D("2.05"), avg_px=D("2480"), fee_ccy="ETH",
                                 created_at=env.clock()))
    env.api.short_ct = D("20.5")
    env.api.entry_px = D("2481")
    # "sold 2" typed instead of "sold 0.2" is declined; then 1 ETH confirmed; then 1.05 vs 2.05 shows the small increment
    helper = assistant(env, ["2.05", "sold 2", "", "sold 1", "y", "sold 1.05", "q"])
    helper.maker = True
    assert run(helper.run()) == "stopped"
    assert env.db.sold_total(env.rid) == D("1.05")
    assert env.api.short_ct == D("10")
    assert any("本次新增 2 ETH" in line for line in env.out.lines)
