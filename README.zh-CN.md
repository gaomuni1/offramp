# offramp

把 OKX 上的 USDT 换成 OSL 里的 USD,中间 ETH 的价格风险用 OKX 1x 永续空单对冲。

- **P1 买入(全自动)**:OKX 现货挂 maker 单买 ETH,每成交一点就自动开等量空单。
- **P2 转账(你手动)**:OKX 提币(以太坊主网)→ hot wallet → OSL,再在 OSL 划到交易账户。
- **P3 卖出(你卖,程序平空)**:你在 OSL 市价卖,回到终端按回车,程序立刻在 OKX 平掉同样数量的空单。
- **P4 对账**:本轮消耗的 USDT 对比 OSL 收到的 USD,算出实际兑换率和每一项成本。

完整设计见 [docs/design.md](docs/design.md)。

## 准备

1. OKX 主账户:
   - 账户模式切换成 **合约模式**(网页:交易 → 设置 → 账户模式)
   - 持仓模式建议 **单向持仓**
   - 建 API key:权限只开 **读取 + 交易**,**不要开提币**。IP 不固定可以不绑(不绑的 key 连续 14 天不用会失效)
   - 提币白名单加上 hot wallet 地址
   - 主账户里不要有别的 ETH-USDT 永续仓位
2. OSL:hot wallet 先在 OSL App 做钱包白名单验证;绑定出金银行账户。OSL 不需要 API key。
3. 本机:

```bash
cd ~/offramp
```

```bash
cp .env.example .env
```

用编辑器打开 `.env` 填好 key(不要发给任何人),然后:

```bash
uv sync
```

```bash
uv run offramp check
```

想先在模拟盘试:在 OKX 模拟交易里单独建一个模拟盘 key,`.env` 里设 `OKX_ENV=demo`。
模拟盘记录放在 `data/demo/`,与实盘 `data/` 分开。已有旧模拟盘记录不要直接当作实盘轮次使用。

## 自动分配总资金:尽量接近一半现货、一半 1x 空单

使用 `--total-usdt` 指定本轮现货、空单保证金和费用的总资金上限,不再手工指定现货预算。
该模式用固定 USDT 预留替代 `buy.margin_buffer` 的比例缓冲。默认预留为总额的 0.1%、最低 1 USDT,
也可以用 `--reserve-usdt` 明确指定。只支持全仓 1x。

10,000 USDT 总资金、预留 10 USDT 的模拟测试:

```bash
uv run offramp buy --total-usdt 10000 --reserve-usdt 10 --dry-run --paper-slice-eth 0.1
```

总资金模式搭配 `--dry-run` 时,默认以总资金作为虚拟余额,无需入金。
日志会显示初始预计现货支出、ETH 净到账、空单数量、保证金、开仓费用与滑点预留。
程序按现货数量步长求最大可负担数量,现货手续费从 ETH 净量计入,避免重复按 USDT 扣费。
每张挂单前按本轮已花资金、实际费用、空单浮盈亏和行情重算;已有挂单无法继续满足资金约束时会撤单重算。
账户其他余额不会自动加入本轮预算。比例缓冲的配置文件保持原样,旧的 `--usdt` 现货预算模式仍照常使用它。

最终目标是少量预留和取整零钱,不是强行把余额归零。默认预留是演练参数,并非能覆盖任意行情的保证:
行情变化、手续费、资金费和数量步长都会影响最后余量;行情变化过快可能仍导致对冲失败。
没有使用真实撮合的保证,也不保证两边恰好各 5,000 USDT。空单始终按净 ETH 数量取整匹配。

准备实盘时先运行只读预检:

```bash
uv run offramp check --total-usdt 10000 --reserve-usdt 10
```

通过预检并确定开始实盘后,在已有新轮次内使用 `buy --total-usdt ... --reserve-usdt ...`。
实际轮次会保存总资金与预留;中断后直接 `buy resume`,不能用新的金额覆盖原轮次。
`--total-usdt` 与 `--usdt` 互斥;`--reserve-usdt` 必须搭配 `--total-usdt`。
`--paper-slice-eth` 仍仅限模拟,实盘分单上限继续取自配置。

## 首次小额测试:总资金 50 USDT,现货预算 20 USDT

这次只验证「现货 maker 成交 → 自动开 1x 永续空单」,买完后停在持仓核对。
50 USDT 都放在 OKX 交易账户;最多用 20 USDT 买现货,剩余 USDT 为全仓空单提供保证金和费用余量。
两条腿按 ETH 数量匹配,不是各花一笔完全相同的 USDT。默认额外留 10% 保证金缓冲。

1. `.env` 填 `OKX_API_KEY`、`OKX_SECRET_KEY`、`OKX_PASSPHRASE`;实盘设 `OKX_ENV=live`。
   key 只开读取和交易权限。账户设为合约模式、建议单向持仓,本次使用全仓 1x。
   使用没有其他仓位、挂单或交易程序干扰的账户;测试期间不要手动交易 ETH 永续。
2. 手动把 **50 USDT** 放入交易账户。运行只读预检:

   ```bash
   uv run offramp check --usdt 20
   ```

   预检会读取权限、费率、余额和实时规格,显示预算所需资金与对冲步长。
   `check` 不改杠杆,正式 `buy` 才会设置配置中的 1x。账户权限、交易限制和下单接受情况仍需实际验证。
3. 先演练,让程序模拟买完预算并开空,也可按 Ctrl+C 提前停止:

   ```bash
   uv run offramp buy --usdt 20 --dry-run
   ```

   演练使用真实行情、规格和费率,把可用余额复制为本地初始资金。
   现货每次查询按原订单的一半合成成交(按数量步长取整),扣 ETH 手续费;
   每次净成交后自动模拟开空,永续 IOC 按限价内盘口深度成交并扣 USDT 手续费。
   模拟账户会冻结买单资金、占用保证金,显示手续费、现金余额和现货/空单尾差。
   如果还没转入资金,可跳过资金准备和预算预检,直接使用虚拟余额演练:

   ```bash
   uv run offramp buy --usdt 20 --dry-run --paper-usdt 50
   ```

   此时按虚拟的 50 USDT 计算,仍读取真实行情、账户模式和费率。无需 `round new`,
   不会修改账户杠杆、下真实订单或写入真实轮次。`--paper-usdt` 仅限搭配 `--dry-run`。
   正式测试前仍需准备真实资金并通过 `check --usdt 20`。

   要演练 5,000 USDT 总资金、2,300 USDT 现货预算:

   ```bash
   uv run offramp buy --usdt 2300 --dry-run --paper-usdt 5000
   ```

   实盘每单上限取自 `config.yaml` 的 `buy.max_slice_eth`(当前 0.5 ETH,1 万总资金约 4～5 张单)。
   单子再大也是按每次实际成交量逐笔开空,不是等整单成交才对冲。
   演练时可以只改本次模拟上限,例如 0.1 ETH:

   ```bash
   uv run offramp buy --usdt 2300 --dry-run --paper-usdt 5000 --paper-slice-eth 0.1
   ```

   先按 Ctrl+C 结束旧演练再重新运行。该参数不改 `config.yaml`,不能用于实盘。
   每单仍受剩余预算、可用资金和保证金限制;永续 IOC 仍检查限价与盘口深度。
   增大模拟分单会减少轮询次数,不代表实盘 maker 的成交速度也会同比提高。

   所有订单和仓位仅在内存中,退出后丢弃,日志保留。无需运行 `status` 或 `round close`,
   也不支持 `buy resume` 恢复模拟。现货成交是预设情景,不预测真实排队或成交速度;
   永续浮盈亏采用盘口卖一作为价格近似,未模拟资金费、强平或真实交易所对订单的接受情况。
4. 正式开始测试时才运行以下两条命令。资金已经在交易账户时,`round new` 不带金额:

   ```bash
   uv run offramp round new
   uv run offramp buy --usdt 20
   ```

   `buy --usdt 20` 指 **20 USDT 现货预算**,不含空单保证金。
   如果 50 USDT 仍在资金账户,可改用 `round new --usdt 50` 新建轮次并划转,不要重复建轮次。
   maker 单可能一直不成交;程序不会为了成交把现货改成 taker。
5. 买完或 Ctrl+C 停止后,核对:

   ```bash
   uv run offramp status
   ```

   同时在 OKX 查看:现货确实成交、ETH-USDT-SWAP 为 1x 空仓、未成交现货单已撤。
   永续按合约步长四舍五入,可有少量净多或净空尾差。以当次预检的规格为准。
   中途停止后需要继续,用 `uv run offramp buy resume`,不要重建轮次。

**实盘停止程序不等于退出持仓。** Ctrl+C 会撤剩余现货买单并尝试补齐对冲,已买 ETH 和空单保留。
如果提示无法确认撤单,需在 OKX 立即核对。网络完全中断时不能保证自动撤单成功。
本次无需 OSL、钱包或提币。`round close --force` 只平空单,不会卖出现货,不能当作测试的双腿退出命令。

## 每一轮

| 步骤 | 命令 / 操作 |
|---|---|
| 预检 | `uv run offramp check` |
| 看行情 | `uv run offramp market`(Ctrl+C 退出) |
| 建轮次并划转 USDT | `uv run offramp round new --usdt 300`(第二轮不用再划转:`round new` 即可) |
| 先演练 | `uv run offramp buy --usdt 72 --dry-run` |
| 买入 + 开空 | `uv run offramp buy --usdt 72`(Ctrl+C 安全退出,`buy resume` 继续) |
| ETH 转到资金账户 | `uv run offramp okx to-funding` |
| 手动 | OKX 提币到 hot wallet(以太坊主网)→ 转到 OSL → OSL 资金账户划到交易账户 |
| 卖出 + 平空 | `uv run offramp sell`,先输入 OSL 到账数量;之后每次在 OSL 市价卖完按回车 |
| 收尾 | `uv run offramp round close` |
| 对账 | `uv run offramp report --osl-usd <OSL 本轮实际收到的 USD>` |
| 随时查看 | `uv run offramp status` |

卖出助手里的命令:回车 = 按显示的数量记卖出并平空;`0.01 @2480.5` = 卖了别的数量;`fix 0.02` = 把已卖总数改成 0.02;`undo` = 撤销上一次;`m` = 刷新;`q` = 退出(空单保留)。

## OSL 手动 maker 卖出,OKX 分批平空

先单独演练平仓,无需买入、转钱或新建真实轮次:

```bash
uv run offramp sell --maker --dry-run --paper-eth 1
```

它假设 OSL 已到账 1 ETH,在内存里初始化相应空单,均价用启动时的永续买一。
虚拟现金自动覆盖保证金并留余量;不读取真实仓位、不发送真实订单、不修改真实轮次。
之前 `buy --dry-run` 的仓位已丢弃,这个命令是独立演练,不继承之前的成本或收益。

在程序的 `>` 提示符中依次输入:

| 假设 OSL 本轮累计实际成交 | 输入 | 本次 OKX 平空 | 剩余空单 |
|---|---|---|---|
| 0.2 ETH | `sold 0.2` | 0.2 ETH | 0.8 ETH |
| 0.5 ETH | `sold 0.5` | 0.3 ETH | 0.5 ETH |
| 1 ETH | `sold 1` | 0.5 ETH | 0 ETH |

`sold` 是本轮所有 OSL 订单的累计成交量,换单、撤单重挂或重启助手都不归零。
本次新增超过 0.1 ETH 时,助手会显示「本次新增 / 将平空」数量,输入 `y` 才执行(防止把累计数错输成增量或多打一位)。
重复输入同一个累计值不会重复记成交;空回车不会触发平空。输入 `m` 重试未完成的平仓,
`q` 退出。maker 助手只平空;`fix` / `undo` 改的是记录,不会撤销真实交易或补开空。
小额成交按合约步长取整,可能累积到下一次再平;不足 OSL 最小交易量的现货尾数会单列并平掉剩余空单。
模拟按真实盘口和 IOC 限价成交,显示平仓费用、盈亏和剩余保证金;
不模拟 OSL 挂单排队、OSL 费用、资金费或强平,退出即丢弃,不需要 `round close`。

实盘仅在本工具的买入轮次完成、ETH 全部到 OSL 后使用:

```bash
uv run offramp sell --maker
```

首次输入本轮实际到账 ETH;助手会先平掉转账损耗对应的多余空单。
然后在 OSL 手动挂卖单,**只有实际成交后才输入累计 `sold`**。仅挂出未成交不能报成交。
助手无法自动看到 OSL 成交,需及时手动输入;否则已卖出的部分仍有空头敞口。
OKX 使用带限价保护的 IOC 买入平空,可能部分成交或失败,确认显示对齐后再继续卖。
`--maker` 强制成交后平空,不使用配置中的 close-first,也不允许 `--close-first`。
退出实盘助手会保留剩余空单,下次用同一命令继续;模拟命令不会修改这些真实记录。

## 安全规则

- 程序以「空单 ≈ 还没卖掉的 ETH」为目标,按合约步长取整;通过订单记录和仓位确认减少重复开平仓风险。
- 平空单一律只减仓,永远不会变成多单。
- Ctrl+C 或出错时只撤买单,**不会一键平掉空单**。
- 部分仓位异常会触发停止;不要在程序运行期间手动交易同一永续,程序不能可靠区分所有外部仓位变化。
- 数据和日志在 `data/`(不进 git)。

## 测试

```bash
uv run pytest
```
