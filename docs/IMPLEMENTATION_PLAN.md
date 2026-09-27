# 实施计划和模块契约

新建独立 Python 3.11+ 包；核心只用标准库，数据商 SDK 为可选依赖。与采用完整 Zipline 依赖栈或纯向量化相比，显式日事件循环更容易审计 A 股交易约束、权益到账和时间边界。现有论文资料不改动。

1. MVP：共享类型、显式交易日历、数据视图、账本、目标权重执行、离线策略演示。
2. 完善：公司行动、费用、T+1、限价/容量、数据适配/快照缓存、研究实验、绩效及审计导出。
3. 验证：单元及集成测试、前缀不变性/未来数据扰动、现金守恒、月末跨月开收盘执行；完整中文文档。

## 统一接口（并行开发约定）

- `models.py` 共享 dataclass，所有日期内部使用 `datetime.date`，资金人民币，量为股/份。
- `calendar.py`: `TradingCalendar(sessions)`, `.sessions` tuple, `next_session(day) -> date|None`, `previous_session(day)`, `is_month_end(day)`, `between(start,end)`。
- `data.py`: `DataBundle(assets: dict[str,Asset], bars: list[Bar], calendar: TradingCalendar, actions: list[CorporateAction]=[], metadata: dict={})`；`DataPortal(bundle)` 提供 `bar(symbol, day) -> Bar|None`、`previous_bar(symbol, day)`、`history(symbol, as_of, lookback, mode='raw') -> list[PricePoint]`、`view(as_of) -> HistoryView`、`fingerprint()`。HistoryView 提供 `as_of`, `assets()`、`history(symbol, lookback, mode='raw', end=None)`、`current(symbol)`，禁止读取未来。`adjusted`=以 as_of 的因子归一化，`total_return`=逐步复投现金分红和送转的指数，不能从最新前复权序列推断现金分红。
- `execution.py`: `TradingRules`, `FeeModel`, `SlippageModel`, `Broker(rules=None, fees=None, slippage=None)`, `execute(order, asset, bar, previous_bar, portfolio) -> Fill|None`，更新 order 状态和 portfolio。无成交量历史时开盘容量为 0，不读取执行日收盘/最高最低/全天量。日订单剩余量到期，下一次信号重新下单。
- `ledger.py`: `Portfolio(initial_cash)`, `.cash`, `.positions` dict symbol -> quantity；`quantity(symbol)`, `sellable(symbol, day, t_plus_one)`, `apply_fill(fill)`, `apply_action(action, day, eligible_quantity=None)`, `pay_receivables(day)`, `accrue_interest(day, amount)`, `equity(prices)`, `.receivables` numeric property, `.entries` list。权益在除权日确认为应收款，到账日可用于交易。
- `analytics.py`: `compute_metrics(snapshots, initial_cash, annualization=252, risk_free_rate=0.0) -> dict`, `drawdown_episodes(snapshots, initial_cash) -> list[dict]`, `export_result(result, directory)`，导出 JSON/CSV、静态 HTML。
- `experiments.py`: 通用 runner(params,start,end)->BacktestResult；parameter_grid/grid_search/ablation/walk_forward，逐次全新策略/账户，训练和测试时间不重叠，训练后只能使用测试之前历史热身。
- 根任务负责 `engine.py`, `strategies.py`, `calendar.py`, 演示、CLI、集成测试及用户文档。

## 核心边界

信号在收盘之后生成，至少下一个交易日才执行；目标权重在执行时价格转换成数量是明确的组合执行服务。下一日开盘执行不能读取当日收盘和全天量。月末必须由完整交易日历决定，截断数据尾部不是自动月末。现金替代标的遵守资产自己的交易约束。资产退市不假定可以在末价出售，默认报错并要求明确估值政策。

日线撮合不能还原集合竞价排队；默认限价方向禁成交是保守约定。规则与税费参数属于显式研究假设，应按研究期间和标的覆盖。所有输入数据、配置和研究结果需要内容指纹及完整事件记录。

## 实施结果（2026-09-22）

- [x] MVP：核心事件循环、权重策略、账本、离线运行。
- [x] 完善：数据商接口/缓存、公司行动、交易规则、成本、研究实验、报告。
- [x] 初始验收：96 项自动测试通过；22 个唯一示例/研究运行完整账本核验；全套输出重复运行逐字节一致。后续修复的当前验证结果见 `VALIDATION.md`。
- 实际实验接口为 `walk_forward(..., gap_days=...)`，间隔定义为自然日；无额外 `purge_sessions` 参数。
- 真实数据商账户下载尚未联网端到端验收；不支持的复杂市场情形已单独列入 `ASSUMPTIONS.md`，不是静默降级。

## 后续修正契约

- 缺失 `pre_close` 的限价参考从上一条非停牌原始 Bar 的收盘价开始，累计 `(锚点日期, 执行日]` 的已生效行动；不使用停牌旧价重新起算。官方限价和显式 `pre_close` 的优先级不变，历史量查询政策不变。
- `actions_verified_metadata(symbols, start, end, verified_by, *, evidence='')` 生成公司行动核验 metadata。未声明、只有旧布尔标记或只有行动列表一律不自动认证。下载适配器接收 `actions_verification` 内层字典，并要求显式行动列表。
- Engine 默认检查全部 bundle 资产、回测日期和限价推导所需历史锚点的认证覆盖；total return 检查资产首条有效历史至查询日的覆盖，包括热身期。`allow_incomplete_actions=True` 仅放开有警告的价格研究，不放开 total return。
- 旧快照可读，核验声明补齐后另存新版本；新版合成演示使用 `synthetic-v3-seed-20260922`。核验声明只证明调用者声明过相应范围，源数据真实性仍须外部核查。
