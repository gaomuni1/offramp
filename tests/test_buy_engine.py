import asyncio
import random
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from offramp.buy_engine import BuyEngine
from offramp.hedger import Hedger
from offramp.ledger import bought_eth, spent_usdt
from offramp.models import HaltError
from tests.fakes import SPOT, SPOT_SPEC, SWAP, SWAP_SPEC, book, make_env


def run(coro):
    return asyncio.run(coro)


def step(env, advance=1.0):
    env.clock.t += advance
    return run(env.engine.step())


def held(env):
    return bought_eth(env.db.orders(env.rid))


def target_ct(env):
    return env.hedger.eth_to_ct(held(env))


def test_first_step_joins_best_bid():
    env = make_env()
    step(env)
    m = env.api.live_maker()
    assert m.px == D("2480.00") and m.sz == D("0.010")


def test_partial_fill_opens_matching_short():
    env = make_env()
    step(env)
    env.api.fill_maker("0.004")
    step(env)
    assert env.api.short_ct == D("0.04") == target_ct(env)
    assert env.api.live_maker() is not None  # same order keeps resting


def test_sub_lot_fills_accumulate_before_hedging():
    env = make_env()
    step(env)
    env.api.fill_maker("0.0004")
    step(env)
    assert env.api.short_ct == 0
    env.api.fill_maker("0.0003")
    step(env)
    assert env.api.short_ct == D("0.01")


def test_perp_drop_cancels_and_fill_during_cancel_is_hedged():
    env = make_env()
    step(env)
    env.api.fill_maker("0.002")
    env.api.books_[SWAP] = book("2470.00", "2470.01")   # cap = 2470 * 1.002 = 2474.94 < 2480
    env.api.fill_on_cancel = D("0.003")
    step(env)                                           # hedges 0.002, then cancels; 0.003 more fills during cancel
    step(env)                                           # sees the final fill, hedges it, quotes again
    assert held(env) == D("0.005") - D("0.000005")
    assert env.api.short_ct == D("0.05") == target_ct(env)
    m = env.api.live_maker()
    assert m.px == D("2474.94")


def test_budget_is_never_exceeded_and_hedge_matches():
    random.seed(7)
    env = make_env(budget="72")
    result = None
    for _ in range(300):
        result = step(env)
        if result == "done":
            break
        m = env.api.live_maker()
        if m and random.random() < 0.6:
            env.api.fill_maker(str(round(random.uniform(0.0002, 0.006), 6)))
        if random.random() < 0.2:
            mid = D("2480") + D(random.randint(-30, 30)) / 10
            env.api.books_[SPOT] = book(mid, mid + D("0.01"))
            env.api.books_[SWAP] = book(mid + 1, mid + D("1.01"))
        assert spent_usdt(env.db.orders(env.rid)) <= D("72")
        assert env.api.short_ct >= 0
    assert result == "done"
    assert env.api.short_ct == target_ct(env)
    assert D("72") - spent_usdt(env.db.orders(env.rid)) < D("2480") * D("0.0001") + 1


def test_position_lag_does_not_double_hedge():
    env = make_env()
    env.api.lag_after_fill = 3
    step(env)
    env.api.fill_maker("0.004")
    step(env)
    step(env)
    assert env.api.short_ct == D("0.04")
    assert len(env.api.hedge_orders()) == 1


def test_position_that_never_updates_halts():
    env = make_env()
    env.api.lag_after_fill = 1000
    step(env)
    env.api.fill_maker("0.004")
    with pytest.raises(HaltError):
        step(env)


def test_unexpected_extra_short_halts():
    env = make_env()
    env.api.short_ct = D("0.10")    # e.g. someone traded the perp by hand
    with pytest.raises(HaltError):
        step(env)


def test_hedge_failures_pause_buying():
    env = make_env()
    step(env)
    env.api.fill_maker("0.004")
    env.api.ioc_script = ["none"] * 9
    for _ in range(3):
        step(env)
    assert "hedge" in env.engine.pause_reasons
    assert env.api.live_maker() is None          # maker cancelled while paused
    step(env)                                      # IOC fills again
    assert env.api.short_ct == D("0.04")
    assert "hedge" not in env.engine.pause_reasons


def test_postonly_rejects_trigger_cooldown():
    env = make_env()
    env.api.postonly_reject_all = True
    for _ in range(8):
        step(env)
    assert len(env.api.placed) == 5


def test_restart_cancels_leftover_maker_and_hedges_once():
    env = make_env()
    step(env)
    env.api.fill_maker("0.004")
    # crash here: a new engine on the same database and exchange
    hedger2 = Hedger(env.api, env.db, env.cfg, env.rid, SWAP_SPEC, "net_mode", env.out, clock=env.clock, sleep=env.clock.sleep)
    engine2 = BuyEngine(env.api, env.db, env.cfg, env.rid, SPOT_SPEC, SWAP_SPEC, hedger2, env.out,
                        clock=env.clock, sleep=env.clock.sleep)
    run(engine2.recover())
    assert env.api.live_maker() is None
    env.clock.t += 1
    run(engine2.step())
    env.clock.t += 1
    run(engine2.step())
    assert env.api.short_ct == D("0.04")
    assert len(env.api.hedge_orders()) == 1


def test_done_marks_round_bought():
    env = make_env(budget="10")
    step(env)
    m = env.api.live_maker()
    env.api.fill_maker(str(m.sz))
    assert step(env) == "done"
    run(env.engine.finish(stopped=False))
    assert env.db.get_round(env.rid).status == "bought"
    assert env.api.short_ct == target_ct(env)


def test_margin_limits_order_size():
    env = make_env()
    env.api.usdt_eq = D("30")
    step(env)
    m = env.api.live_maker()
    per_eth = D("2481.01") * D("1.1")
    assert m.sz <= D("30") / (D("2480.00") + per_eth)
    assert m.sz > D("0.005")


def test_market_data_outage_cancels_maker():
    env = make_env()
    step(env)
    assert env.api.live_maker() is not None
    env.api.fail_books = 100
    step(env, advance=1)
    step(env, advance=10)
    assert env.api.live_maker() is None
    assert "stale" in env.engine.pause_reasons


def test_first_failed_hedge_cancels_remaining_maker():
    env = make_env()
    step(env)
    env.api.fill_maker("0.004")
    env.api.ioc_script = ["none"] * 3
    step(env)
    assert env.api.live_maker() is None
    assert env.api.short_ct == 0


def test_spot_market_outage_does_not_block_hedging_known_fill():
    from offramp.models import OkxTimeout
    env = make_env()
    step(env)
    env.api.fill_maker("0.004")
    original = env.api.books

    async def books(inst_id, sz=5):
        if inst_id == SPOT:
            raise OkxTimeout("spot book unavailable")
        return await original(inst_id, sz)

    env.api.books = books
    step(env)
    assert env.api.short_ct == target_ct(env)
    assert env.api.live_maker() is None
    env.api.books = original
    step(env)
    assert "stale" not in env.engine.pause_reasons
    assert env.api.live_maker() is not None


def test_failed_order_read_cancels_resting_maker_before_exit():
    from offramp.models import OkxTimeout
    env = make_env()
    step(env)

    async def failed_step():
        env.engine.request_stop()
        raise OkxTimeout("order read failed")

    env.engine.step = failed_step
    env.engine.recover = AsyncMock()
    assert run(env.engine.run()) == "stopped"
    assert env.api.live_maker() is None


def test_finish_cannot_succeed_with_failed_hedge():
    env = make_env(budget="20")
    step(env)
    env.api.fill_maker(str(env.api.live_maker().sz))
    env.api.ioc_script = ["none"] * 3
    with pytest.raises(HaltError, match="未对齐"):
        run(env.engine.finish(stopped=False))
    assert env.db.get_round(env.rid).status == "buying"


def test_50_usdt_can_buy_20_and_reserve_rounded_1x_hedge():
    env = make_env(budget="20")
    env.api.usdt_eq = D(50)
    step(env)
    maker = env.api.live_maker()
    assert maker.sz * maker.px <= D(20)
    assert D(20) - maker.sz * maker.px < SPOT_SPEC.lot_sz * maker.px
    env.api.fill_maker(str(maker.sz))
    env.api.usdt_eq -= maker.sz * maker.px
    assert step(env) == "done"
    run(env.engine.finish(stopped=False))
    assert env.api.short_ct > 0
    assert abs(held(env) - env.api.short_ct * SWAP_SPEC.ct_val) <= SWAP_SPEC.lot_sz * SWAP_SPEC.ct_val / 2
    assert env.api.short_ct * SWAP_SPEC.ct_val * D("2481.01") * D("1.1") < env.api.usdt_eq
