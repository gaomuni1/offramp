"""config.yaml + .env loading. Every setting has a default."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path

import yaml

from .util import dec


@dataclass
class OkxCfg:
    rest_base: str = "https://www.okx.com"
    spot_inst: str = "ETH-USDT"
    swap_inst: str = "ETH-USDT-SWAP"
    leverage: int = 1
    margin_mode: str = "cross"
    hedge_slippage_bps: Decimal = Decimal(15)
    timeout_s: float = 5.0


@dataclass
class OslCfg:
    rest_base: str = "https://trade-hk.osl.com"
    symbol: str = "ETHUSD"
    taker_fee: Decimal = Decimal("0.0005")
    min_qty: Decimal = Decimal("0.00052")
    qty_step: Decimal = Decimal("0.00001")


@dataclass
class BuyCfg:
    price_mode: str = "join"
    max_entry_basis_bps: Decimal = Decimal(20)
    max_slice_eth: Decimal = Decimal("0.010")
    requote_bps: Decimal = Decimal(5)
    requote_min_interval_s: float = 3.0
    max_order_age_s: float = 300.0
    margin_buffer: Decimal = Decimal("0.10")
    poll_s: float = 1.0
    postonly_cooldown_s: float = 30.0


@dataclass
class SellCfg:
    chunk_eth: Decimal = Decimal(2)
    order: str = "sell_first"
    confirm_big_input_eth: Decimal = Decimal("0.1")


@dataclass
class RiskCfg:
    stale_s: float = 5.0
    max_unhedged_eth: Decimal = Decimal("0.005")
    max_unhedged_s: float = 10.0
    max_hedge_failures: int = 3
    max_postonly_rejects: int = 5
    max_order_errors: int = 5
    min_margin_ratio: Decimal = Decimal(3)


@dataclass
class Credentials:
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""
    demo: bool = False

    @property
    def present(self) -> bool:
        return bool(self.api_key and self.secret_key and self.passphrase)


@dataclass
class Config:
    okx: OkxCfg = field(default_factory=OkxCfg)
    osl: OslCfg = field(default_factory=OslCfg)
    buy: BuyCfg = field(default_factory=BuyCfg)
    sell: SellCfg = field(default_factory=SellCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    data_dir: Path = Path("data")
    creds: Credentials = field(default_factory=Credentials)


def _fill(obj, values: dict | None, section: str) -> None:
    known = {f.name for f in fields(obj)}
    for key, value in (values or {}).items():
        if key not in known:
            raise SystemExit(f"config.yaml 里有不认识的配置项:{section}.{key}")
        current = getattr(obj, key)
        if isinstance(current, Decimal):
            value = dec(value)
        elif isinstance(current, bool):
            value = bool(value)
        elif isinstance(current, int):
            value = int(value)
        elif isinstance(current, float):
            value = float(value)
        else:
            value = str(value)
        setattr(obj, key, value)


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def load_config(root: Path | None = None) -> Config:
    root = root or Path.cwd()
    cfg = Config()
    path = root / "config.yaml"
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for section in ("okx", "osl", "buy", "sell", "risk"):
            _fill(getattr(cfg, section), raw.get(section), section)
        if raw.get("data_dir"):
            cfg.data_dir = Path(raw["data_dir"])
    if not cfg.data_dir.is_absolute():
        cfg.data_dir = root / cfg.data_dir
    env = load_env(root / ".env")
    env.update({k: v for k, v in os.environ.items() if k.startswith("OKX_")})
    environment = env.get("OKX_ENV", "live").strip().lower()
    if environment not in ("live", "demo"):
        raise SystemExit("OKX_ENV 只能是 live 或 demo,请检查 .env")
    cfg.creds = Credentials(
        api_key=env.get("OKX_API_KEY", ""),
        secret_key=env.get("OKX_SECRET_KEY", ""),
        passphrase=env.get("OKX_PASSPHRASE", ""),
        demo=environment == "demo",
    )
    if cfg.creds.demo:
        cfg.data_dir = cfg.data_dir / "demo"
    return cfg
