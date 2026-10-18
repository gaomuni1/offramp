import asyncio
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from offramp.allocation import Allocation, affordable_qty, funding_need
from offramp.buy_engine import BuyEngine
from offramp.cli import build_parser, cmd_buy, plan_allocation
from offramp.db import Db
from offramp.dryrun import DryRunOkx
from offramp.hedger import Hedger
from offramp.ledger import spent_usdt
from tests.fakes import SPOT_SPEC, SWAP_SPEC, SWAP, book, make_env


def inputs():
    return dict(allocation=Allocation(D(10000), D(10), D('-0.0008'), D('-0.0005')),
                swap=SWAP_SPEC, spot_px=D(2480), perp_bid=D(2481), perp_ask=D('2481.01'))


def test_solver_finds_largest_affordable_lot_and_counts_eth_fee_once():
    args = inputs()
    plan = affordable_qty(max_qty=D(10), equity=D(10000), spot=SPOT_SPEC, **args)
    assert plan.required_equity <= D(10000)
    assert funding_need(plan.qty + SPOT_SPEC.lot_sz, **args).required_equity > D(10000)
    assert plan.net_eth == plan.qty * D('0.9992')
    assert plan.required_equity == plan.spot_cost + plan.margin + plan.open_fee + plan.execution_buffer + D(10)
    assert D(4950) < plan.spot_cost < D(5010)


@pytest.mark.parametrize('reserve', [D(1), D(10), D(100)])
def test_larger_reserve_reduces_buy_capacity(reserve):
    args = inputs()
    args['allocation'] = replace(args['allocation'], reserve_usdt=reserve)
    plan = affordable_qty(max_qty=D(10), equity=D(10000), spot=SPOT_SPEC, **args)
    assert plan.required_equity <= D(10000)
    assert plan.required_equity - reserve <= D(10000) - reserve


async def setup(total='10000', reserve='10', paper='10000', path=':memory:'):
    env = make_env(status='new')
    env.cfg.buy.max_slice_eth = D('0.1')
    env.api.usdt_eq = D(0)
    env.api.trade_fee = AsyncMock(return_value=(D('-0.0008'), D('-0.0005')))
    for name in ('place_order', 'cancel_order', 'set_leverage', 'transfer', 'position'):
        setattr(env.api, name, AsyncMock(side_effect=AssertionError('real write/position access')))
    dry = DryRunOkx(env.api, env.out, paper_usdt=D(paper), cfg=env.cfg)
    await dry.initialize(SPOT_SPEC, SWAP_SPEC)
    ctx = SimpleNamespace(api=dry, out=env.out, cfg=env.cfg)
    allocation, plan = await plan_allocation(ctx, SPOT_SPEC, SWAP_SPEC, D(total), D(reserve))
    db = Db(path)
    rid = db.create_round()
    db.save_allocation(rid, allocation)
    db.update_round(rid, status='buying', buy_budget=plan.spot_cost, start_usdt=D(paper))
    hedger = Hedger(dry, db, env.cfg, rid, SWAP_SPEC, 'net_mode', env.out,
                    clock=env.clock, sleep=env.clock.sleep)
    engine = BuyEngine(dry, db, env.cfg, rid, SPOT_SPEC, SWAP_SPEC, hedger, env.out,
                       clock=env.clock, sleep=env.clock.sleep)
    return env, dry, db, rid, engine


async def finish(env, engine, limit=300):
    for _ in range(limit):
        env.clock.t += 1
        if await engine.step() == 'done':
            await engine.finish(stopped=False)
            return
    pytest.fail('total-capital run did not complete')


@pytest.mark.parametrize('paper', ['10000', '20000'])
def test_total_capital_simulation_keeps_only_small_remainder_and_protects_extra_funds(paper):
    async def scenario():
        env, dry, db, rid, engine = await setup(paper=paper)
        await finish(env, engine)
        bal = (await dry.balances(['USDT']))['USDT']
        allocated_available = bal.avail_bal - (D(paper) - D(10000))
        assert D(10) <= allocated_available < D(15)
        assert D(4950) < engine.spent() < D(5010)
        assert abs(engine.held_eth() - dry._short_ct * SWAP_SPEC.ct_val) <= D('0.0005')
        assert db.get_round(rid).status == 'bought'
        assert not db.open_orders(rid)
        for name in ('place_order', 'cancel_order', 'set_leverage', 'transfer', 'position'):
            getattr(env.api, name).assert_not_called()
    asyncio.run(scenario())


def test_total_mode_obeys_real_available_balance_not_just_equity():
    async def scenario():
        env, dry, db, rid, engine = await setup()
        original = dry.balances
        async def restricted(ccys):
            result = await original(ccys)
            result['USDT'].avail_bal = D(11)
            return result
        dry.balances = restricted
        await engine.step()
        maker = db.open_orders(rid, 'maker_buy')[0]
        assert maker.sz * maker.px < D(1)
    asyncio.run(scenario())


def test_falling_available_funds_cancels_resting_order():
    async def scenario():
        env, dry, db, rid, engine = await setup()
        await engine.step()
        dry._cash = D(20)
        # No synthetic fill while checking the existing order's capacity.
        original = dry.get_order
        async def no_fill(inst, cid):
            if inst == SPOT_SPEC.inst_id:
                from copy import copy
                return copy(dry._orders[cid])
            return await original(inst, cid)
        dry.get_order = no_fill
        await engine.step()
        assert all(o.terminal for o in dry._orders.values())
    asyncio.run(scenario())


def test_saved_total_and_reserve_survive_restart(tmp_path):
    async def scenario():
        path = tmp_path / 'round.db'
        env, dry, db, rid, engine = await setup(path=path)
        await engine.step()
        await engine.step()
        await engine.finish(stopped=True)
        before = engine.spent()
        db.conn.close()
        restored = Db(path)
        hedger = Hedger(dry, restored, env.cfg, rid, SWAP_SPEC, 'net_mode', env.out,
                        clock=env.clock, sleep=env.clock.sleep)
        resumed = BuyEngine(dry, restored, env.cfg, rid, SPOT_SPEC, SWAP_SPEC, hedger, env.out,
                            clock=env.clock, sleep=env.clock.sleep)
        assert resumed.allocation.total_usdt == D(10000)
        assert resumed.allocation.reserve_usdt == D(10)
        assert resumed.spent() == before > 0
        await resumed.recover()
        await finish(env, resumed)
        assert D(4950) < resumed.spent() < D(5010)
        assert (await dry.balances(['USDT']))['USDT'].avail_bal >= D(10)
    asyncio.run(scenario())


def test_price_moves_recompute_allocation_and_keep_reserve():
    async def scenario():
        env, dry, db, rid, engine = await setup()
        await engine.step()
        await engine.step()
        env.api.books_[SPOT_SPEC.inst_id] = book('3000', '3000.01')
        env.api.books_[SWAP] = book('3001', '3001.01')
        await finish(env, engine)
        assert (await dry.balances(['USDT']))['USDT'].avail_bal >= D(10)
        assert abs(engine.held_eth() - dry._short_ct * SWAP_SPEC.ct_val) <= D('0.0005')
    asyncio.run(scenario())


def test_unrecorded_cash_loss_does_not_spend_protected_account_money():
    async def scenario():
        env, dry, db, rid, engine = await setup(paper='20000')
        # E.g. a funding debit while the round is running.
        dry._cash -= D(500)
        await finish(env, engine)
        assert (await dry.balances(['USDT']))['USDT'].avail_bal >= D(10010)
        assert engine.spent() < D(4800)
    asyncio.run(scenario())


def test_summary_warns_when_post_trade_price_move_erodes_reserve():
    async def scenario():
        env, dry, db, rid, engine = await setup()
        await finish(env, engine)
        env.api.books_[SWAP] = book('2491', '2491.01')
        await dry.print_summary(reserve_usdt=D(10))
        assert any('低于预留目标' in line for line in env.out.lines)
    asyncio.run(scenario())


def test_cli_total_mode_defaults_paper_balance_and_leaves_real_round_untouched(monkeypatch):
    env = make_env(status='new')
    env.api.usdt_eq = D(0)
    env.api.trade_fee = AsyncMock(return_value=(D('-0.0008'), D('-0.0005')))
    ctx = SimpleNamespace(api=env.api, out=env.out, cfg=env.cfg, db=env.db,
                          specs=AsyncMock(return_value=(SPOT_SPEC, SWAP_SPEC)),
                          pos_mode=AsyncMock(return_value='net_mode'))
    async def run(engine):
        assert engine.allocation.total_usdt == D(10000)
        assert engine.allocation.reserve_usdt == D(10)
        await finish(env, engine)
        assert D(4950) < engine.spent() < D(5010)
        return 'done'
    monkeypatch.setattr(BuyEngine, 'run', run)
    asyncio.run(cmd_buy(ctx, None, None, True, paper_slice_eth='0.1', total_usdt='10000'))
    assert env.db.get_allocation(env.rid) is None
    assert not env.db.orders(env.rid)
    assert env.db.get_round(env.rid).status == 'new'
    assert env.cfg.buy.margin_buffer == D('0.10')


@pytest.mark.parametrize('kwargs', [
    dict(usdt='20', total_usdt='100'), dict(usdt=None, reserve_usdt='10'),
    dict(usdt=None, total_usdt='100', reserve_usdt='100'),
    dict(usdt=None, total_usdt='NaN'), dict(usdt=None, total_usdt='100', reserve_usdt='0')])
def test_invalid_total_arguments_rejected_before_account_access(kwargs):
    with pytest.raises(SystemExit):
        asyncio.run(cmd_buy(None, None, dry_run=True, **kwargs))


def test_resume_cannot_replace_saved_cap():
    with pytest.raises(SystemExit, match='resume 使用本轮保存'):
        asyncio.run(cmd_buy(None, 'resume', None, False, total_usdt='20000'))


def test_parser_accepts_total_mode_and_rejects_mixed_budgets():
    args = build_parser().parse_args(['buy', '--total-usdt', '10000', '--reserve-usdt', '10', '--dry-run'])
    assert args.total_usdt == '10000' and args.usdt is None
    with pytest.raises(SystemExit):
        build_parser().parse_args(['buy', '--usdt', '5000', '--total-usdt', '10000'])


@pytest.mark.parametrize('command', ['buy', 'check'])
def test_total_mode_help_renders_default_percentage(command, capsys):
    with pytest.raises(SystemExit) as result:
        build_parser().parse_args([command, '--help'])
    assert result.value.code == 0
    assert '0.1%' in capsys.readouterr().out


def test_large_resting_order_survives_small_uptick_but_not_large_one():
    async def scenario():
        env, dry, db, rid, engine = await setup()
        env.cfg.buy.max_slice_eth = D(3)
        await engine.step()
        maker = db.open_orders(rid, 'maker_buy')[0]
        assert D(4950) < maker.sz * maker.px < D(5010)  # the whole spot leg in one order

        async def no_fill(inst, cid):
            from copy import copy
            return copy(dry._orders[cid])
        dry.get_order = no_fill
        env.api.books_[SWAP] = book('2481.5', '2481.51')  # ~1 USDT more margin: within half the reserve
        env.clock.t += 1
        await engine.step()
        assert not dry._orders[maker.cl_ord_id].terminal
        env.api.books_[SWAP] = book('2484', '2484.01')    # ~6 USDT more margin: beyond half the reserve
        env.clock.t += 1
        await engine.step()
        assert dry._orders[maker.cl_ord_id].terminal
    asyncio.run(scenario())
