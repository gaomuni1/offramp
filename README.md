# offramp

**Convert USDT on OKX into USD on OSL, with the ETH price risk in between hedged by a 1x perpetual short.**

`offramp` is a command-line tool for a specific off-ramp route:

```
USDT (OKX) ──buy ETH──▶ ETH (OKX) ──withdraw──▶ hot wallet ──deposit──▶ ETH (OSL) ──sell──▶ USD (OSL)
                 │                                                                     │
                 └── open an equal ETH-USDT-SWAP short (1x) ─── close it as you sell ───┘
```

While the ETH is in transit (OKX → wallet → OSL), it is fully hedged by a short on the OKX
perpetual, so price moves during the transfer don't change what you end up with. The tool automates
the OKX side (maker buying, hedging, closing) and tells you exactly what to do by hand on OSL.

> 中文说明见 [README.zh-CN.md](README.zh-CN.md)。设计文档见 [docs/design.md](docs/design.md)。

> **Disclaimer.** This is personal tooling, not financial advice and not affiliated with OKX or OSL.
> It places real orders with real money. Read the whole README, run the dry runs, and start with a
> small amount. Use at your own risk.

---

## Table of contents

1. [How it works](#how-it-works)
2. [What is automated and what is manual](#what-is-automated-and-what-is-manual)
3. [Requirements](#requirements)
4. [Installation](#installation)
5. [Account setup (OKX, OSL, wallet)](#account-setup)
6. [Configuration (`.env` and `config.yaml`)](#configuration)
7. [Sizing a round](#sizing-a-round)
8. [Dry runs (no real orders)](#dry-runs-no-real-orders)
9. [Step-by-step: one live round](#step-by-step-one-live-round)
10. [Command reference](#command-reference)
11. [Costs](#costs)
12. [Safety design](#safety-design)
13. [Troubleshooting](#troubleshooting)
14. [Data, logs and files](#data-logs-and-files)
15. [Development and tests](#development-and-tests)
16. [Project layout](#project-layout)

---

## How it works

A conversion is done in **rounds**. Each round has four phases:

| Phase | What happens | Who does it |
|---|---|---|
| **P1 Buy** | Post-only (maker) limit orders buy ETH on OKX spot `ETH-USDT`. Every time a buy fills, the tool immediately sells the same amount of `ETH-USDT-SWAP` with an IOC order, so the short always equals the ETH bought. | Fully automatic |
| **P2 Transfer** | Move the ETH from the OKX trading account to the funding account, withdraw it on Ethereum mainnet to your own wallet, send it to your OSL deposit address, then move it into the OSL trading account. | You (by hand) |
| **P3 Sell** | You sell the ETH on OSL (`ETHUSD`). After each fill you type the cumulative amount sold; the tool closes exactly that much of the OKX short with a reduce-only IOC order. | You sell; the tool closes the short |
| **P4 Reconcile** | The tool compares the USDT the round consumed with the USD OSL paid you and breaks the difference into fees, transfer loss and price/basis. | Automatic (you enter the USD received) |

**Why the short?** During P2 you hold ETH you cannot sell yet. The 1x short on OKX gains exactly what
the ETH loses (and vice versa), so the round's result depends only on fees and spreads, not on where
ETH goes while it is in transit.

**Why 1x?** At 1x the short's liquidation price is roughly 2x the entry price, so a transfer that takes
hours (or a compliance hold of a day or two) is not a liquidation risk.

**The trade-off:** at 1x, half of the round's capital is margin for the short. Only about **half of
each round's USDT is converted**; the margin half returns to your OKX trading account when the short is
closed and can be used for the next round. See [Sizing a round](#sizing-a-round).

---

## What is automated and what is manual

| | The tool does | You do |
|---|---|---|
| Funding | (optional) OKX funding → trading transfer of USDT | Deposit USDT on OKX; withdraw ETH; wallet → OSL; OSL funding ↔ trading transfers; OSL fiat withdrawal |
| Trading | OKX spot maker buy + matching short; closes the short as you report OSL fills | Place sell orders on OSL |
| Monitoring | OKX balances, position, margin ratio; OSL public ticker; conversion rate; reconciliation | Watch OSL for fills and report them |

OSL's trading API is only available to professional / corporate / institutional accounts, so the OSL
leg is manual. The tool only reads OSL's **public** market data and never needs OSL credentials.

---

## Requirements

- **macOS or Linux** (uses `fcntl` for the single-instance lock; Windows is not supported).
- **Python ≥ 3.11** (the repo pins 3.12 in `.python-version`).
- **[uv](https://docs.astral.sh/uv/)** for environment and dependency management. Install it with:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

  or `brew install uv`. uv will download the right Python version automatically.
- An **OKX** account (main account) with an API key — see [Account setup](#account-setup).
- An **OSL** account that can trade `ETHUSD` and receive ETH deposits on Ethereum mainnet.
- A **self-custody wallet** ("hot wallet") on Ethereum mainnet that OSL has verified/whitelisted, with a
  little ETH for gas.

Runtime dependencies are just `httpx` and `pyyaml`; tests use `pytest`.

---

## Installation

```bash
git clone https://github.com/<your-username>/offramp.git
```

```bash
cd offramp
```

Create the virtual environment and install dependencies (including the dev group with pytest):

```bash
uv sync
```

Check the install:

```bash
uv run offramp -h
```

```bash
uv run pytest
```

All tests run offline against in-memory fakes; they never touch an exchange.

You can also install it as a global command with `uv tool install .`, but the rest of this README uses
`uv run offramp …` from the project directory, which always uses the project's `config.yaml`, `.env`
and `data/` folder.

---

## Account setup

### OKX

1. **Account mode → Futures mode.** OKX web: *Trade → Settings → Account mode*. The tool refuses to
   run in any other mode (it checks `acctLv = 2`). In this mode the USDT-margined perpetual uses only
   USDT as margin; the spot ETH you buy is *not* used as collateral.
2. **Position mode.** One-way (net) mode is recommended. Hedge (long/short) mode also works — the tool
   sends `posSide=short` in that case. You can only switch while you have no positions or open orders.
3. **No other ETH-USDT-SWAP position** on the account. `round new` requires the position to be zero,
   and you must not trade that perpetual by hand while a round is running.
4. **API key** (*Profile → API*):
   - Permissions: **Read + Trade only. Never enable Withdraw.** `offramp check` fails if withdraw is on.
   - IP whitelist: recommended. If you bind IPs, add the public IP of the machine that runs the tool
     (`curl -4 https://api.ipify.org`). If your IP changes (VPN, travel), requests fail with
     `50110` until you add the new IP. Keys without an IP whitelist expire after 14 days of inactivity.
   - Write down the API key, secret key and passphrase for `.env`.
5. **Withdrawal whitelist:** add your hot wallet address (Ethereum / ERC20) to OKX's withdrawal
   address book so withdrawals are quick.
6. Leverage does **not** need to be set by hand: the first live `buy` sets `ETH-USDT-SWAP` cross
   leverage to the configured value (1x).

**Demo trading (optional).** OKX has a separate demo-trading environment with its own API keys. Set
`OKX_ENV=demo` in `.env` to use it; records then go to `data/demo/` and never mix with live records.

### OSL

1. Complete the wallet verification so OSL accepts deposits from your hot wallet. OSL only accepts ETH
   on **Ethereum mainnet** and only from verified self-custody wallets (not directly from exchanges).
2. Link the bank account you will withdraw USD to.
3. No API key is needed.

### Hot wallet

Any Ethereum mainnet wallet you control, verified by OSL, holding a small amount of ETH for gas.

---

## Configuration

### 1. Secrets: `.env`

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Required | Meaning |
|---|---|---|
| `OKX_API_KEY` | yes | OKX API key |
| `OKX_SECRET_KEY` | yes | OKX secret key |
| `OKX_PASSPHRASE` | yes | Passphrase you chose when creating the key |
| `OKX_ENV` | no | `live` (default) or `demo` (OKX demo trading; uses a separate demo key and `data/demo/`) |

`.env` is in `.gitignore`. Never commit it or share it. Environment variables named `OKX_*` override
the file, so you can also `export OKX_API_KEY=...` instead.

### 2. Behaviour: `config.yaml`

Every setting has a default, so the file is optional; unknown keys are rejected with an error so
typos don't go unnoticed. Changes apply to the next command you run.

**`okx`**

| Key | Default | Meaning |
|---|---|---|
| `spot_inst` | `ETH-USDT` | Spot instrument to buy |
| `swap_inst` | `ETH-USDT-SWAP` | Perpetual used for the hedge |
| `leverage` | `1` | Hedge leverage. **Total-capital mode only supports 1.** |
| `margin_mode` | `cross` | `cross` margin. **Total-capital mode only supports cross.** |
| `hedge_slippage_bps` | `15` | Max distance of hedge IOC orders from the top of book (1 bp = 0.01%) |
| `rest_base` | `https://www.okx.com` | OKX REST endpoint |
| `timeout_s` | `5.0` | HTTP timeout |

**`osl`** (public data only)

| Key | Default | Meaning |
|---|---|---|
| `rest_base` | `https://trade-hk.osl.com` | OSL public REST endpoint |
| `symbol` | `ETHUSD` | Market you sell on |
| `taker_fee` | `0.0005` | OSL taker fee (0.05%), used for display and the report's fee estimate |
| `min_qty` | `0.00052` | OSL minimum order size; a remainder below this is treated as unsellable dust |
| `qty_step` | `0.00001` | OSL quantity step (5 decimals) |

**`buy`** (phase P1)

| Key | Default | Meaning |
|---|---|---|
| `price_mode` | `join` | `join` = quote at the best bid; `improve` = one tick above the best bid when the spread allows |
| `max_entry_basis_bps` | `20` | Never pay more than the perp best bid + this many bps for spot |
| `max_slice_eth` | `0.010` (repo config: `0.5`) | Maximum size of one maker buy order, in ETH |
| `requote_bps` | `5` | Re-price the resting order when the desired price moves this much |
| `requote_min_interval_s` | `3` | Minimum time between re-quotes |
| `max_order_age_s` | `300` | Refresh an order that has rested this long |
| `margin_buffer` | `0.10` | Extra margin buffer (10%) — used only by the legacy `--usdt` mode |
| `poll_s` | `1.0` | Main loop interval |
| `postonly_cooldown_s` | `30` | Pause after repeated post-only rejections |

**`sell`** (phase P3)

| Key | Default | Meaning |
|---|---|---|
| `chunk_eth` | `2.0` | Suggested chunk size in the non-maker sell assistant |
| `order` | `sell_first` | Non-maker assistant: `sell_first` (sell on OSL, then close) or `close_first` |
| `confirm_big_input_eth` | `0.1` | Inputs (or `sold` increments) above this ask for confirmation |

**`risk`**

| Key | Default | Meaning |
|---|---|---|
| `stale_s` | `5` | Cancel the buy order if market data is older than this |
| `max_unhedged_eth` | `0.005` | Unhedged ETH tolerated… |
| `max_unhedged_s` | `10` | …for at most this many seconds before buying pauses |
| `max_hedge_failures` | `3` | Consecutive failed hedges before buying pauses |
| `max_postonly_rejects` | `5` | Post-only rejections before a cooldown |
| `max_order_errors` | `5` | Rejected buy orders before a cooldown |
| `min_margin_ratio` | `3.0` | Pause buying if the OKX margin ratio drops below this |

A top-level `data_dir:` key (default `data`) moves the database and logs.

The repository's `config.yaml` uses `max_slice_eth: 0.5` (about 1,200 USDT per maker order at
ETH ≈ 2,400), which keeps a 10,000 USDT round to roughly five orders while capping how much can fill
at once before the hedge catches up.

---

## Sizing a round

Use **total-capital mode**: `--total-usdt T --reserve-usdt R`.

- `T` is the most the round may use from the OKX trading account: spot purchases + 1x short margin +
  fees. Anything else in the account is protected and never spent on spot (it does count as extra
  cross margin for the short, which only makes the hedge safer).
- `R` is a fixed amount left unallocated to absorb price moves during buying. About **0.3% of T** works
  well (e.g. `T=10050, R=30`); it must be greater than 0. If omitted it defaults to 0.1% (min 1 USDT).
- Before every order the tool solves for the largest spot size whose spot cost + full-hedge margin +
  opening fee + slippage allowance + reserve fits in `T`. Spot fees are charged in ETH, so the hedge
  matches the **net** ETH received.

Rule of thumb at 1x:

| `--total-usdt` | Spot bought / USD received | Returned to OKX after the round |
|---|---|---|
| 1,000 | ≈ 495 | ≈ 500 |
| 10,050 | ≈ 5,000 | ≈ 5,000 |
| 20,000 | ≈ 9,950 | ≈ 10,000 |

To convert a target amount `X` of USD in one round, use `T ≈ 2·X + R`. To convert an existing balance
completely you need several rounds (each converts about half of what is left), or add funds.

`offramp check --total-usdt T --reserve-usdt R` prints the plan (spot size, net ETH, short size,
margin) before you commit to anything.

> A legacy mode, `--usdt B`, sets a fixed spot budget `B` instead and uses `buy.margin_buffer`. Total
> mode is recommended. The two flags are mutually exclusive.

---

## Dry runs (no real orders)

Both trading phases have a local simulator. Dry runs read **real** market data, instrument specs and
your **real** fee rates and account mode (so they need a working API key), but every order, balance and
position lives in memory and is discarded on exit. Nothing is written to the round database, and
leverage is not changed.

**Buy + hedge** with a virtual balance (no funds needed):

```bash
uv run offramp buy --total-usdt 10050 --reserve-usdt 30 --dry-run
```

In total mode the virtual balance defaults to `T`; use `--paper-usdt` to set another. Maker fills are
synthetic (each order poll fills half of the original order); hedge IOC orders fill against the real
order book within the price limit. `--paper-slice-eth 0.1` overrides `max_slice_eth` for the simulation
only.

**Sell + close** starting from a simulated short as if `2.05` ETH had arrived on OSL:

```bash
uv run offramp sell --maker --dry-run --paper-eth 2.05
```

Type `sold 1`, `y`, `sold 2.05`, `y` at the prompt to watch the short being closed step by step. Try
mistakes on purpose (`sold 30`, a repeated value, answering `n`) to see how input is validated.

---

## Step-by-step: one live round

The example converts about 5,000 USDT (`--total-usdt 10050 --reserve-usdt 30`). Replace the numbers
with your own. **Do a small round first** (e.g. `--total-usdt 100 --reserve-usdt 3`) to validate the
whole chain, including the withdrawal and the OSL deposit.

### Step 0 — Fund the OKX trading account

Transfer at least `T` USDT into the OKX **trading** account (OKX app: *Assets → Transfer → Funding →
Trading*). Alternatively, `offramp round new --usdt <amount>` can do the funding → trading transfer for
you in Step 2, if your API key is allowed to transfer funds.

### Step 1 — Pre-flight check (read-only)

```bash
uv run offramp check --total-usdt 10050 --reserve-usdt 30
```

It verifies: clock skew vs OKX, instrument specs, maintenance notices, OSL public ticker, account mode,
position mode, API key permissions (trade yes / withdraw no), leverage, fee rates, balances, ETH
withdrawal status and fee — then prints the allocation plan:

```
[..] 账户模式:合约模式
[..] API key 权限:read_only,trade(没有提币权限 ✓)
[..] 交易账户 USDT 10050.00,ETH 0.000000;资金账户 USDT 0.00,ETH 0.000000
[..] 总资金模式:上限 10050 USDT,固定预留 30 USDT;...
[..] 当前估算:现货 5008.02 USDT,净到账约 2.069 ETH;空单约 2.069 ETH,1x 保证金约 5001.95 USDT
[..] 预检通过
```

(Console output is in Chinese.) Continue only when it ends with **预检通过** ("pre-flight passed").

### Step 2 — Open a round

```bash
uv run offramp round new
```

Requires no unfinished round and a zero `ETH-USDT-SWAP` position. It records the starting balance.
(With `--usdt <amount>` it first transfers that much USDT from funding to trading.) The second line it
prints suggests `buy --usdt`; ignore it when using total-capital mode.

### Step 3 — Buy and hedge (automatic)

```bash
uv run offramp buy --total-usdt 10050 --reserve-usdt 30
```

What you will see:

```
[..] 杠杆设为 1x                                              ← leverage set to 1x
[..] 挂买 0.5 ETH @ 2445.78(现货已花 0.00,总资金上限 10050 USDT)   ← maker buy placed
[..] 现货成交 +0.25 ETH @ 2445.78(这张单 0.25/0.5)              ← spot fill
[..] OKX 卖出开空 0.25 ETH @ 2444.01                           ← short opened 1–2 s later
[..] 撤单:跟价 2446.65 → 2446.60                               ← re-quote, normal
...
==== 买入小结 ====
买到 ETH(扣手续费后) 2.073500   花费 5010.64 USDT   均价 2414.58
OKX 空单 2.073 ETH   开空均价 2413.36   ✓ 合约步长内对齐(可能有尾差)
[..] 本轮买入完成。...
```

- Every spot fill is followed by a matching short. The short is rounded to the contract step
  (0.001 ETH), so a residual of up to ±0.0005 ETH is normal.
- The engine never buys more while the hedge is behind, pauses on stale data / hedge failures / low
  margin ratio, and re-quotes as the market moves. It finishes when the remaining capacity cannot fund
  another minimum order.
- **Ctrl+C is safe:** it cancels the resting buy, aligns the short with what was bought, and exits.
  Continue later with `uv run offramp buy resume` (never `round new` again; `resume` reuses the saved
  `T` and `R`).
- Maker orders can take seconds or minutes to fill. The tool never switches to taker to speed up.

### Step 4 — Verify

```bash
uv run offramp status
```

```
==== OKX ====
交易账户 USDT 5037.39(可用 29.13),ETH 2.073566
空单 2.074 ETH  均价 2412.68  未实现盈亏 -2.1592 USDT  保证金率 223.52  强平价 4819.82
==== 第 4 轮:已买完(转账中) ====
已买到 2.073566 ETH(花费 5010.11 USDT)
应有空单 2.0735658192 ETH,实际 2.074 ETH,偏差 0.0004341808 ✓ 步长内(仍可能有尾差)
```

Check that the round says *bought (in transit)*, the deviation is marked ✓, and the liquidation price
is roughly twice the current price. Note the "bought" ETH amount. Also glance at OKX itself: spot ETH
present, a 1x short of the same size, no leftover open orders.

### Step 5 — Move the ETH to OSL (manual)

1. **OKX trading → funding:** OKX app, *Assets → Transfer*, ETH, amount = the bought amount from
   Step 4. Only move ETH; the USDT margin stays in the trading account. (Or run
   `uv run offramp okx to-funding`, if your API key is allowed to transfer funds.)
2. **Withdraw from OKX** to your hot wallet. Network: **Ethereum (ERC20) mainnet** — OSL does not
   accept L2 deposits. Double-check the address. OKX deducts its withdrawal fee (≈ 0.0001 ETH).
3. **Hot wallet → OSL** ETH deposit address on Ethereum mainnet. Use the wallet's *Max* button so it
   reserves gas itself.
4. **On OSL:** once the deposit is credited, move the ETH from the funding account to the trading
   account. **Write down the exact credited amount** from OSL's deposit history.

The short stays open the whole time, so the position is hedged. Large deposits may be held for extra
checks; that only costs a little perpetual funding (usually well under 1 USDT per 8 h per 5,000 USDT).

### Step 6 — Sell on OSL; the tool closes the short

Start the sell assistant and leave it running:

```bash
uv run offramp sell --maker
```

It first asks for the amount that arrived on OSL. Enter the credited amount exactly (all decimals):

```
本轮买到(扣手续费后)2.073566 ETH;OKX 空单 2.074 ETH
OSL 交易账户实际到账多少 ETH?> 2.07345582
[..] 已记录到账 2.07345582 ETH。转账损耗 0.00011 ETH,多出来的空单现在平掉
[..] 对齐 空单 2.073 ETH ≈ 剩余 2.07345582 ETH ✓
 ...
 ▶ OSL 挂限价单后等待实际成交,输入 sold <本轮累计成交 ETH>;OKX 用 IOC 买入平空
>
```

The short in excess of what arrived (the withdrawal fee / gas) is closed immediately. If you enter more
than was bought, it asks to cap at the bought amount (answer `y`).

**Place sell orders on OSL** (`ETHUSD`, limit):

- **Price:** best ask − 0.01, and **strictly above the best bid** so the order rests as a maker
  (OSL maker fee is 0%; taker is 0.05%). If bid and ask are only 0.01 apart, use the best ask.
  If OSL offers *Post Only / Maker Only*, select it — an order that would cross is rejected instead
  of filling as taker.
- **Quantity:** OSL uses 5 decimals. Split ≈ 2 ETH into two orders, e.g. `1` and then
  `floor_to_5_decimals(arrived − 1)`. For an arrival of `2.07345582` that is `1` then `1.07345`.
  Do not use "100%" if OSL holds leftover dust from earlier rounds.
- If the price runs away and the order doesn't fill, cancel and re-place at the new best ask. You are
  hedged while waiting, so there is no rush.

**Report fills as soon as they happen.** Between an OSL fill and your `sold` input, the sold part is
unhedged (the short is still open). Type the **cumulative** amount sold this round:

```
> sold 1
累计 1 ETH,即本次新增 1 ETH,将在 OKX 平掉 1 ETH 空单。确认输入 y,其他取消> y
[..] OKX 买入平空 1 ETH @ 2445.21
[..] 对齐 空单 1.073 ETH ≈ 剩余 1.07345582 ETH ✓

> sold 2.07345
累计 2.07345 ETH,即本次新增 1.07345 ETH,将在 OKX 平掉 1.07345 ETH 空单。确认输入 y,其他取消> y
[..] OKX 买入平空 1.073 ETH @ 2444.46
[..] 对齐 空单 0 ETH ≈ 剩余 0.00000582 ETH ✓
[..] OSL 剩 0.00000582 ETH 尾数,低于最小下单量卖不了,报告里会单列
[..] 本轮卖完。下一步:offramp round close,然后 offramp report --osl-usd <...>
```

Rules for `sold`:

| Situation on OSL | Type |
|---|---|
| Order placed, nothing filled | nothing |
| 0.4 filled so far (this round, all orders) | `sold 0.4` |
| First 1 ETH order fully filled | `sold 1` |
| Second order (1.07345) fully filled | `sold 2.07345` |

- Always the **running total for the round**, never the increment. Re-entering the same total is
  harmless (no double close).
- Increments above `confirm_big_input_eth` (0.1 ETH) show the increment and the amount to close, and
  require `y`. If the increment doesn't match what just filled (e.g. you typed `sold 1.07345` instead of
  `sold 2.07345`), answer `n` and re-type.
- Totals lower than what's recorded or higher than what arrived are rejected; use `fix <total>` to
  correct a recorded total. The maker assistant only ever closes the short; it never re-opens it.
- Dust below OSL's minimum order size (0.00052 ETH) is left on OSL and the remaining short is closed.
- `m` refreshes and retries an unfinished close; `q` exits and keeps the short (run
  `offramp sell --maker` again to continue — it remembers the arrival amount and the sold total).
- If a close fails with a red error, stop selling on OSL and press `m`. If OKX is unreachable,
  close the corresponding amount of the short manually on the OKX website.

### Step 7 — Close the round

```bash
uv run offramp round close
```

Requires no open orders. It makes sure the short is zero, records the ending balance and marks the
round closed. If ETH is still unsold it refuses unless you pass `--force` (which closes the remaining
hedge and leaves that ETH unhedged).

### Step 8 — Reconcile

Add up, over all OSL sell orders of this round, **fill amount − fee** (if an order also sold dust from
earlier rounds, deduct that part pro rata). Then:

```bash
uv run offramp report --osl-usd 4971.49
```

```
====== 第 4 轮对账(closed)======
本轮开始时交易账户 USDT          10050.00
本轮结束时交易账户 USDT          5058.14
→ 消耗 USDT                       4991.86
OSL 收到 USD(你输入)            4971.49
→ 实际兑换率                      0.99592 USD/USDT
成本分解 ...
OKX 核对:按订单算应消耗 4991.86,实际 4991.86,未解释差额 0.0000 ✓
```

- *Consumed USDT* = start − end balance of the trading account for the round.
- *Rate* = USD received ÷ USDT consumed.
- The cost breakdown lists spot fee, perpetual open/close fees, funding, transfer loss (withdrawal fee +
  gas), unsold dust, the OSL fee, and the remainder ("price factor": OSL vs OKX price, spot–perp basis,
  timing). The OSL fee line assumes the taker rate; if your orders filled as maker, that amount actually
  belongs to the price factor.
- The last line checks the OKX ledger against the actual balance; it should be `0.0000 ✓`.
- `--gas-eth` adds wallet gas you paid separately; `--round N` reports an older round; `--all` lists
  every round.

The margin half of the capital is now back in the OKX trading account. For the next round, start again
at Step 0/1.

---

## Command reference

All commands: `uv run offramp <command> [options]`. Commands that can place orders take an exclusive
lock (`data/offramp.lock`) so two of them can never run at once.

| Command | Description |
|---|---|
| `check [--total-usdt T [--reserve-usdt R] \| --usdt B]` | Read-only pre-flight check; with an amount, also prints the plan. Never changes the account. |
| `market [--once]` | Live OKX spot / perp / OSL quotes, entry basis and the net sell conversion rate (OSL bid after taker fee ÷ OKX perp ask). Ctrl+C to exit. |
| `round new [--usdt A]` | Start a round. Optional funding → trading transfer of `A` USDT. |
| `buy --total-usdt T [--reserve-usdt R]` | P1: maker buys + hedge for the active round. |
| `buy resume` | Continue an interrupted buy with the saved parameters. |
| `buy ... --dry-run [--paper-usdt P] [--paper-slice-eth S]` | Simulated buy; no orders, no records. |
| `status` | OKX balances, short, margin ratio, liquidation price, round state, hedge deviation, funding fees. |
| `okx to-funding [--eth X]` | Move this round's ETH from trading to funding (needs a key that may transfer). |
| `sell --maker` | P3 assistant for manual OSL limit orders; report cumulative fills with `sold <total>`. |
| `sell [--close-first]` | P3 assistant for OSL market sells: press Enter after each displayed chunk, or type `0.01 @2480.5`; `fix`, `undo`, `m`, `q`. |
| `sell --maker --dry-run --paper-eth X` | Simulated close of a short of `X` ETH. |
| `round close [--force]` | Close the round (short must be zero, or `--force`). |
| `report [--osl-usd U] [--round N \| --all] [--gas-eth G]` | Reconciliation and cost breakdown. |

---

## Costs

Observed over eight live rounds of 250–5,000 USDT each, all-in cost was about **0.30%–0.41% per
round** (≈ 0.36% overall):

| Component | Typical | Notes |
|---|---|---|
| OKX spot maker fee | 0.08% | Regular-tier rate, charged in ETH |
| OKX perpetual open + close | 0.10% | Taker 0.05% each way, paid from margin (doesn't reduce the USD received) |
| OSL fee | 0% / 0.05% | Maker is free; taker 0.05% |
| Withdrawal fee | ≈ 0.0001 ETH per round | Fixed; negligible above a few hundred USDT |
| Price factor | ≈ 0.06%–0.17% | Mostly the USDT/USD rate (OSL ETH/USD vs OKX ETH/USDT) plus spot–perp basis at entry; also any delay between an OSL fill and `sold` |

Ways to keep it low: make every OSL order a maker order, report `sold` within seconds, avoid selling
during violent moves, and check `offramp market` for the current net rate before selling. Small rounds
cost more in percentage terms because of the fixed withdrawal fee.

---

## Safety design

- **Target-based hedging.** Every hedge action computes the *target* short (ETH bought, or ETH still
  unsold) and aligns the position to it. Re-running is idempotent, so retries, duplicates and restarts
  don't double-trade.
- **Write-ahead order records.** Each order is written to SQLite *before* it is sent, with a unique
  `clOrdId`. If a request times out, the tool looks the order up instead of re-sending; on restart it
  settles everything it may have left behind.
- **Position confirmation.** After a hedge fill, the tool waits until the OKX position reflects it; a
  mismatch halts the program instead of guessing.
- **Hedge first.** In the buy loop, the short is aligned before anything else; buying stops while the
  hedge is behind, after repeated hedge failures, on stale market data, or when the margin ratio is low.
- **Never flips long.** Closing orders are reduce-only (one-way mode) or `posSide=short` (hedge mode)
  and capped at the current short.
- **Spending cap.** In total mode the tool never spends account funds beyond `T`, even if more USDT is
  present.
- **Stop ≠ exit.** Ctrl+C, errors and halts cancel buy orders but **never** close the short
  automatically. The hedge stays in place until you sell.
- **No withdrawal permission.** The tool refuses keys that can withdraw, and never needs OSL
  credentials.
- **Single instance.** A file lock prevents two trading commands from running at the same time.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `OKX 50110: Your IP ... is not included in your API key's IP whitelist` | Add the machine's current public IP to the key's whitelist (`curl -4 https://api.ipify.org`). Keep the same network/VPN node during a round. |
| `账户模式是「…」,需要切换成「合约模式」` | Switch OKX to Futures mode (Trade → Settings → Account mode). |
| `这个 API key 开了「提币」权限` | Create a new key with Read + Trade only. |
| `本机时间和 OKX 相差 … 秒` | Enable automatic time sync on your machine. |
| `OSL 公开行情读取失败` | Transient OSL public API timeout; it only affects displayed reference prices. |
| Repeated `(canceled)` right after placing a buy | Post-only rejected because the price moved; it re-quotes automatically and cools down after several in a row. |
| `当前可用余额…低于预留目标` after buying | The price moved and used part of the reserve. Not a problem once buying is finished. |
| `程序停止(HALTED)` | A state the tool won't fix on its own (e.g. position mismatch, order status unknown). Check `offramp status` and OKX, then `offramp buy resume`. |
| `本轮买入已经开始过。继续用:offramp buy resume` | You ran `buy` again on a round that already started buying; use `resume`. |
| `另一个 offramp 命令正在运行` | Another trading command holds the lock; close it first. |
| `没有进行中的轮次` after `round close` | The round is already closed; nothing to do. |
| `--usdt 和 --total-usdt 不能同时使用` | Use one sizing mode per round. |

---

## Data, logs and files

| Path | Contents |
|---|---|
| `data/offramp.db` | SQLite: rounds, every order sent, OSL sells you reported, events |
| `data/offramp.log` | Full console log with timestamps |
| `data/offramp.lock` | Single-instance lock |
| `data/demo/` | Same files for `OKX_ENV=demo` |

`data/` and `.env` are git-ignored. Back up `data/offramp.db` if you care about history; it is the
source for `status` and `report`.

---

## Development and tests

```bash
uv run pytest
```

The suite (≈ 120 tests) covers allocation maths, the buy engine (partial fills, re-quotes, cancels
that fill, lagging positions, restarts, outages), the hedger (timeouts, rejections, reduce-only
limits), dry-run isolation, the sell assistant (cumulative fills, duplicates, typos, corrections,
confirmation) and reconciliation. It uses in-memory fakes (`tests/fakes.py`) and never touches the
network.

---

## Project layout

```
offramp/
├── offramp/
│   ├── cli.py          # command-line entry point, check / round / status / report commands
│   ├── buy_engine.py   # P1: post-only buying loop, risk pauses, re-quoting
│   ├── allocation.py   # total-capital sizing (spot + 1x margin + fees + reserve)
│   ├── hedger.py       # aligns the ETH-USDT-SWAP short to a target with IOC orders
│   ├── sell_assist.py  # P3: interactive assistant that closes the short as you report OSL fills
│   ├── dryrun.py       # local paper exchange for --dry-run
│   ├── report.py       # P4: reconciliation and cost breakdown
│   ├── okx.py          # minimal OKX v5 REST client (signing, retries)
│   ├── osl_public.py   # OSL public ticker
│   ├── db.py           # SQLite storage
│   ├── ledger.py       # quantities derived from order records
│   ├── models.py       # shared data types and errors
│   ├── config.py       # config.yaml + .env loading
│   └── util.py         # Decimal helpers, console output, instance lock
├── tests/              # pytest suite with exchange fakes
├── docs/
│   ├── design.md       # design document (Chinese)
│   └── runbook-20k.md  # operator checklist with expected outputs (Chinese)
├── config.yaml         # behaviour settings
├── .env.example        # template for secrets
├── LICENSE
└── pyproject.toml
```

---

## License

[MIT](LICENSE)
