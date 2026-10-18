import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from offramp.cli import positive_amount, preview_buy
from offramp.config import load_config
from tests.fakes import D, SPOT_SPEC, SWAP_SPEC, make_env


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "abc", ""])
def test_invalid_budget_rejected(value):
    with pytest.raises(SystemExit):
        positive_amount(value)


def preview_ctx(balance):
    env = make_env()
    env.api.usdt_eq = D(balance)
    env.api.trade_fee = AsyncMock(return_value=(D("-0.001"), D("-0.0005")))
    return SimpleNamespace(cfg=env.cfg, api=env.api, out=env.out)


def test_50_balance_20_budget_preflight_only_reads():
    ctx = preview_ctx("50")
    asyncio.run(preview_buy(ctx, SPOT_SPEC, SWAP_SPEC, D(20)))
    assert not ctx.api.placed
    assert any("尾差" in line for line in ctx.out.lines)


def test_20_balance_10_budget_fails_before_trading():
    ctx = preview_ctx("20")
    with pytest.raises(SystemExit, match="不足"):
        asyncio.run(preview_buy(ctx, SPOT_SPEC, SWAP_SPEC, D(10)))
    assert not ctx.api.placed


def test_budget_below_perp_minimum_fails():
    ctx = preview_ctx("50")
    with pytest.raises(SystemExit, match="最低下单"):
        asyncio.run(preview_buy(ctx, SPOT_SPEC, SWAP_SPEC, D("0.5")))


def test_demo_and_live_records_are_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("OKX_ENV", "demo")
    assert load_config(tmp_path).data_dir == tmp_path / "data" / "demo"
    monkeypatch.setenv("OKX_ENV", "live")
    assert load_config(tmp_path).data_dir == tmp_path / "data"


def test_environment_typo_does_not_silently_select_live(tmp_path, monkeypatch):
    monkeypatch.setenv("OKX_ENV", "demoo")
    with pytest.raises(SystemExit, match="OKX_ENV"):
        load_config(tmp_path)
