# A 股 / ETF 事件驱动研究回测引擎

可运行的 Python 3.11+ 研究项目。核心、测试、报告均只依赖标准库；Tushare / AkShare 下载为可选扩展。信号、执行、现金流和估值按事件推进，目标是让每一笔结果能回到输入数据与账本核验。

本项目借鉴 Zipline 的事件驱动与数据视图思想，采用较小的模块边界，没有复制其资产数据库、Pipeline 或分钟仿真体系。现有工作区论文资料未改动。

## 立即运行

在本目录的 PowerShell 中（启动脚本会优先使用 `.venv`，其次使用当前 Codex 已有 Python）：

```powershell
.\run.ps1 test
.\run.ps1 demo -Research
```

也可使用任意 Python 3.11+，无需安装本包：

```bash
python -m unittest discover -s tests -v
python -m ashare demo --research --output outputs/demo
python tools/verify_demo.py --replay
```

打开 `outputs/demo/index.html`。演示包含 Buy & Hold、趋势过滤、月末 Top-K 动量的次日开盘与次日收盘两个版本，以及网格、消融、两折样本外测试。

`tools/verify_demo.py --replay` 会核验各运行的现金、股份、文件哈希，并再次运行检查输出是否逐字节一致。参数试验的完整日志保存在 `outputs/demo/research_runs/<run_id>/`。

**演示全部使用固定种子的合成价格和工作日历，不是 A 股实际历史表现，也不是中国交易所日历。** 它特意含分红、拆分、停牌、缺失行情、新上市资产。真实回测必须通过数据适配器或显式核验过的交易日历输入。

如需安装为可导入包或使用下载 SDK：

```bash
python -m pip install -e .
python -m pip install -e ".[tushare]"
# 或 python -m pip install -e ".[akshare]"
```

离线核心没有要下载的依赖；SDK 会引入其自己的依赖。Token 仅从环境变量或调用参数读取，不写入缓存。

## 功能范围

| 模块 | 已实现 |
|---|---|
| 数据 | Tushare / AkShare 股票与 ETF 原始日线、单位规范化、显式交易日历、资产生命周期、调整因子、原始/复权/total return 研究序列 |
| 缓存 | SQLite 内容指纹、不可覆盖的命名快照、读取完整性校验、离线重放 |
| 时点 | 收盘后信号、下一交易日开盘或收盘执行、日/月频调仓、持有收益区间记录、禁止视图读取未来 |
| 交易 | 目标权重、先卖后买、整手/T+1、停牌/缺失行情拒单、涨跌停、历史量开盘容量、部分成交、日订单到期 |
| 账本 | 现金、股份、可卖批次、分红应收/到账、送转/拆分、现金计息、现金替代标的 |
| 成本 | 佣金与最低佣金、卖出税、过户费、日期税率、固定基点和容量冲击滑点 |
| 研究 | Buy & Hold、趋势过滤、Top-K 动量、网格、具名消融、训练选参后冻结参数的滚动样本外测试 |
| 分析 | 日净值、收益/年化/波动/Sharpe/Sortino/Calmar、最大回撤与恢复区间、双边和半换手、交易与现金日志、HTML/CSV/JSON |
| 审计 | 输入哈希、配置/策略/撮合参数、源代码哈希、Python 版本、run_id、输出文件 SHA-256 清单 |

完整假设和未实现项目见 [ASSUMPTIONS.md](docs/ASSUMPTIONS.md)，数据接入见 [DATA_GUIDE.md](docs/DATA_GUIDE.md)，测试与验收说明见 [VALIDATION.md](docs/VALIDATION.md)。

## 核心时间约定

```text
T 日公司行动 → 到账 → 执行前一天的目标 → T 日收盘估值
                                             ↓
                                只用截至 T 收盘的数据生成信号
                                             ↓
                              T+1 个交易日 open 或 close 执行
```

例如 1 月 31 日为日历认定的月末：

| execution_price | signal_date | execution_date | 新持仓开始承担的收益 |
|---|---|---|---|
| open | 1 月 31 日收盘后 | 2 月首个交易日开盘 | 成交后的日内及后续变动；不获得建仓前隔夜跳空 |
| close | 1 月 31 日收盘后 | 2 月首个交易日收盘 | 从成交收盘起；不获得此前日内上涨 |

旧持仓在调仓前仍承担市场变动。`holding_periods.csv` 用 `[execution, next_rebalance)` 记录目标组合周期；最后一段截至回测末日收盘，交易费用计入该段。该区间是实际账户的组合收益段，包含未成交后遗留的持仓，并非假定目标组合完全成交。

日历必须包含下一交易日才能识别末月月末；数据截断本身不触发月末。最终信号执行日在回测区间外时记录为 `outside_backtest`，不会提前成交。所有时间为中国交易所本地日期，当前无盘中 timestamp 模型。

## Python 用法

```python
from ashare.demo import synthetic_bundle
from ashare.engine import Engine, BacktestConfig
from ashare.strategies import TopKMomentum
from ashare.execution import Broker, TradingRules, FeeModel, SlippageModel
from ashare.analytics import export_result

bundle = synthetic_bundle()  # 换成核验过的真实 DataBundle 即可
strategy = TopKMomentum(
    symbols=("STOCK_A", "ETF_A", "ETF_B", "ETF_NEW"),
    lookback=63, top_k=2, skip=0, positive_only=True,
    cash_proxy="CASH_ETF", mode="total_return",
)
config = BacktestConfig(
    start="2023-01-02", end="2024-12-31",
    initial_cash=1_000_000, frequency="monthly",
    execution_price="open", cash_buffer=0.002,
)
broker = Broker(
    rules=TradingRules(limit_pct=0.10, max_participation=0.05),
    fees=FeeModel(commission_rate=0.0003, min_commission=5,
                  stamp_tax_rate=0.0005, transfer_fee_rate=0.00001),
    slippage=SlippageModel(bps=2, impact_bps=10),
    execution_mode="baseline",  # 默认：滑点越界时缩量至价格边界
)
result = Engine(bundle, config, broker).run(strategy)
export_result(result, "outputs/my_run")
print(result.metrics)
```

上面的费率、限价和成交容量是示例研究参数，不是适用于所有历史时期、板块和券商的规则表。需要按实际样本覆盖，见假设文档。

`Broker` 的 `execution_mode` 控制候选数量对应的滑点后理论价格越过涨跌停边界时的处理：

| 模式 | 越界处理 |
|---|---|
| `baseline`（默认） | 在合法整手数量中找到不过界的最大数量，只成交该数量；无可行整手则拒单 |
| `optimistic` | 将价格截到边界，保留全部候选数量，兼容原来的乐观假设 |
| `conservative` | 整笔拒单 |

候选数量先受持仓可卖量、成交容量和整手规则约束，再应用上述模式，最后检查买入现金。买卖对称处理；滑点后的价格恰好等于边界允许成交，但原始参考价已处于涨停的买单或跌停的卖单仍按原有规则拒绝。`baseline` 用单调滑点模型下的整数手二分搜索缩量，按实际数量重新计算滑点和费用；余量当日到期，日志记 `partial / price_limit`，无可行数量记 `rejected / price_limit`。如果之后现金约束进一步缩量，则原因记为 `insufficient_cash`。

可用同一数据和策略分别运行三种模式做敏感性分析；每次构造独立账户和策略，模式随 Broker 配置进入审计元数据与 `run_id`：

```python
brokers = {
    mode: Broker(slippage=SlippageModel(bps=2, impact_bps=10), execution_mode=mode)
    for mode in ("baseline", "optimistic", "conservative")
}
for mode, candidate_broker in brokers.items():
    candidate_strategy = TopKMomentum(("ETF_A", "ETF_B"), lookback=63, top_k=1)
    candidate_result = Engine(bundle, config, candidate_broker).run(candidate_strategy)
    export_result(candidate_result, f"outputs/mode_{mode}")
```

真实数据必须显式声明公司行动已核验的资产、日期范围和核验人。手工 `DataBundle` 未提供声明、仅设置旧布尔标记 `corporate_actions_complete=True`，或适配器仅传入 `actions=[]`，都不再视为完整。使用 `ashare.data.actions_verified_metadata(...)` 创建声明；接入示例见 [DATA_GUIDE.md](docs/DATA_GUIDE.md)。声明是调用者的核验记录，不是引擎自动证明了源数据完整。

Engine 默认要求声明覆盖 bundle 中所有资产的回测区间，以及推导缺失 `pre_close` 所需的更早有效原始收盘锚点。`total_return` 还要求覆盖该资产首条有效历史观测至查询日，热身期也包含在内，不能只认证最近的 lookback。显式 `allow_incomplete_actions=True` 可用于接受现金流缺失风险的价格研究，并记录警告；它不放开 `total_return`。

已缓存的数据可完全离线回测：

```bash
python -m ashare cache-list outputs/demo/market.sqlite
python -m ashare run --cache outputs/demo/market.sqlite --snapshot synthetic-v3-seed-20260922 --start 2023-01-02 --end 2024-12-31 --strategy momentum --symbols STOCK_A,ETF_A,ETF_B --cash-proxy CASH_ETF --lookback 63 --top-k 2 --execution close --output outputs/cached_run
```

CLI 提供常用选项；复杂规则和费用配置使用 Python API，所有实际配置均进入审计结果。

旧快照仍可读取原始数据，但缺少范围声明的快照不能自动用于默认收益回测。复核后添加声明并另存新快照名；新版演示使用 `synthetic-v3-seed-20260922`，不会覆盖原有 v2 快照。

## 自定义策略

```python
class MyStrategy:
    def generate(self, data, context):
        # data.as_of == context.signal_date，不能调用未来日期
        history = data.history("ETF_A", 20, mode="total_return")
        if len(history) < 20 or history[-1].date != data.as_of:
            return None
        return {"ETF_A": 0.6}  # 其余现金；未列出的已有资产目标为 0
```

- `None` 表示维持当前持仓，不创建新目标；`{}` 表示全部目标为现金。
- `BacktestConfig.cash_proxy` 会把目标权重剩余部分分配给指定资产；显式设定前 `{}` 就是现金，设定后 `{}` 就是全部买入现金替代资产。
- 策略只收到 `HistoryView` 与只读持仓快照。`history(..., end=未来日期)` 抛错。`assets()` 仅返回当日已上市且未退市资产，不暴露未来退市日。
- 不允许负权重、杠杆或非有限数；权重和最多为 1。`cash_buffer` 在形成目标时按比例留下费用缓冲。
- `lookback` 和 `skip` 均按有效行情观测数计，不是自然日或月。当前资产没有当日有效行情时，示例轮动策略不把它列入新买入选择。
- Buy & Hold 在首次产生持仓后保持份额，不做持续目标再平衡；首次订单部分成交后也保持已有份额。若需要补足目标，应使用自己的重复目标策略。

## 参数实验

```python
from ashare.experiments import grid_search, ablation, walk_forward

def runner(params, start, end):
    # 每次构造全新策略、账户；可读 start 前的历史热身，不能带入热身收益。
    s = TopKMomentum(("ETF_A", "ETF_B"), **params)
    return Engine(bundle, BacktestConfig(start, end), broker).run(s)

grid = {"lookback": [42, 63, 126], "top_k": [1, 2]}
training = grid_search(runner, grid, "2023-01-02", "2023-12-29", metric="sharpe")
variants = ablation(runner, {"lookback": 63, "top_k": 2},
                   {"no_positive_filter": {"positive_only": False}},
                   "2023-01-02", "2023-12-29")
oos = walk_forward(runner, grid, [
    ("2023-01-02", "2023-12-29", "2024-01-02", "2024-06-28"),
    ("2023-07-03", "2024-06-28", "2024-07-02", "2024-12-31"),
], gap_days=1)
```

网格同分时稳定选择第一组；未定义或非有限选择指标直接报错，可显式选择 `total_return`。`gap_days` 是训练与测试之间完整自然日数；若要按交易日 embargo，应使用交易日历构造边界。每个测试折从现金开始，报告不把不同重置账户伪装成连续持仓业绩。月频测试折会等该折内第一个月末信号，因此最初可能保持现金。

## 输出和源码

每个回测输出 `result.json`、`metadata.json`、`metrics.json`、`equity.csv`、`positions.csv`、`signals.csv`、`orders.csv`、`fills.csv`、`ledger.csv`、`holding_periods.csv`、`drawdowns.csv`、`report.html`、`manifest.json`。导出到同一目录会覆盖该目录内这些固定报告文件；输入缓存快照不可覆盖。

```text
ashare/
  models.py       共享资产、行情、行动、订单、成交、快照类型
  calendar.py     显式交易日历与月末判定
  data.py         校验、三种价格口径、PIT 视图
  adapters.py     数据商边界、日期/单位/字段规范化
  cache.py        本地不可变快照
  strategies.py   策略协议与示例
  execution.py    规则、税费、滑点和订单撮合
  ledger.py       持仓、现金、可卖批次与权益应收
  engine.py       事件时序、目标权重与组合收益周期
  analytics.py    指标、回撤与审计报告
  experiments.py  网格、消融和样本外
  demo.py/cli.py  可重复运行入口
tests/            离线回归测试，无 SDK 账户要求
```
