# 测试与验收记录

## v0.1.0 固定环境验收（2026-09-27）

在 Windows 上使用 Python 3.12.14，通过 `uv sync --locked` 新建环境；构建工具固定为 setuptools 84.0.0。未启用可选数据 SDK，核心运行依赖为标准库。环境管理工具在发布前固定为 uv 0.12.19，以支持两平台的 Python 3.12.14 下载。

```bash
uv run --no-sync python -m unittest discover -s tests -v
uv run --no-sync python -m ashare demo --research --output outputs/release-v0.1.0
uv run --no-sync python tools/verify_demo.py outputs/release-v0.1.0 --replay
```

本次结果：**139 项测试全部通过；22 个独立归档的现金与持仓对账、文件哈希校验及逐字节重放全部通过。** 输出保存在被 Git 忽略的 `outputs/release-v0.1.0/` 中。GitHub Actions 配置采用相同的固定解释器和锁文件，在 Windows 与 Linux 上执行测试、合成演示和重放检查；各次运行结果以仓库 Actions 记录为准。

本次整理未改变回测引擎、策略或测试实现，不新增真实市场实证结果。研究模块范围及未实现部分见 [REPRODUCIBILITY.md](REPRODUCIBILITY.md)。

## 初始功能验收

执行日期：2026-09-22。环境：Windows / Python 3.12.14；核心无第三方依赖。

## 已执行

```powershell
.\run.ps1 test
.\run.ps1 demo -Research
```

结果：**139 项测试全部通过**（初始版本 96 项，停牌除权修复与显式认证新增 21 项，本次涨跌停执行模式新增 22 项）；四个示例策略成功生成报告、JSON/CSV 和内容清单。演示研究包含 6 组参数网格、基准加 3 个消融、两折训练选参/冻结测试。去除重复 run_id 后，主示例与研究共 **22 个独立归档结果**。

同时执行了：

```bash
python tools/verify_demo.py --replay
python -m ashare run --cache outputs/demo/market.sqlite --snapshot synthetic-v3-seed-20260922 --start 2023-01-02 --end 2024-12-31 --strategy momentum --symbols STOCK_A,ETF_A,ETF_B --cash-proxy CASH_ETF --lookback 63 --top-k 2 --execution close --output outputs/cached_run
python -m compileall -q ashare tests tools
```

`verify_demo.py --replay` 对 22 个归档分别核验：

- 所有清单文件的 SHA-256 与实际内容一致。
- 从初始现金逐条累计账本现金变动，与每条余额和每日日终现金一致。
- 从成交/拆分的股份变动重建每个标的持仓，与每日持仓一致。
- 每日 `equity = cash + market_value + receivables`。
- 成交日期晚于信号日，成交数量与订单终结状态相符。
- 全部数据指纹与研究摘要所引用的数据一致。
- 公司行动认证声明覆盖运行元数据记录的资产与日期范围。
- 重跑完整演示与实验后，summary 和全部归档 JSON/CSV/HTML/manifest 逐字节一致。

缓存命令行回测与编译检查也成功。脚本结果为：

```json
{
  "audited_runs": 22,
  "cash_and_positions_reconciled": true,
  "manifest_hashes_verified": true,
  "deterministic_replay": true
}
```

输出位置：`outputs/demo/index.html` 为入口；每个主策略单独目录；`outputs/demo/research_runs/<run_id>/` 为网格/消融/样本外每次运行的完整审计文件；`summary.json` 保存选择过程和结果指纹。

## 自动测试覆盖

| 文件 | 关键验证 |
|---|---|
| test_engine.py | 月末跨月、次日开/收盘收益起点、未来行情扰动、分红拆分、计息、现金替代、退市策略、重复运行 |
| test_audit.py | 已有多资产仓位开盘重配的信息边界、停牌除息、缺失除权行情、登记日校验、组合持有区间收益连乘、三种执行模式的换手/费用/持仓/导出与运行指纹 |
| test_suspended_reference.py | 添加停牌旧价不改变涨停拒单、连续除权行动累计、排除锚点当日和未来行动、官方参考优先、无有效收盘时禁止拾回停牌旧价 |
| test_execution.py | 禁读执行日未来 OHLCV、历史量容量、多订单共享容量、限价及日期覆盖、手数、T+1/T+0、最低费和现金部分成交 |
| test_price_limit_modes.py | baseline/optimistic/conservative 双边与开收盘、最大可行整手、恰好边界、无可行量、现金二次缩量、费用税费/账本/容量、零股、单调非线性滑点与异常契约 |
| test_ledger.py | 现金/股份守恒、应收到账不双记、登记日权益、拆分可卖批次、重复成交拒绝 |
| test_data.py | as-of 复权、未来访问拒绝、历史前缀不变性、三种价格口径、停牌和未知行动拒绝、生命周期 |
| test_adapters.py | 股/ETF单位、raw请求、SDK调用、历史分段、公司行动字段、日历截断/节假日覆盖、显式ETF限价端点 |
| test_cache.py | 不可变命名、持久化重读、内容损坏检测、日历覆盖边界及旧payload兼容 |
| test_analytics.py | 初始资金回撤、首日损益、年化及RF、换手、未恢复回撤、零方差/零净值、确定性文件和导出配置 |
| test_experiments.py | 仅训练选择参数、冻结测试、时间隔离、间隔/重叠拒绝、同分稳定选择、非法指标、每次参数隔离 |
| test_strategies.py | 动量排名和同分、skip 窗口、正动量过滤、趋势热身、Buy & Hold 与只读组合快照 |

## 本次涨跌停执行模式的回归证据

- 新增 21 项成交模式测试和 1 项引擎到导出的集成审计测试，139 项全量测试通过。
- 参考价 10、历史量 10,000、候选量 1,000 股、`impact_bps=10000`、上下限价 10.55/9.45：baseline 买入 500 股、均价 10.5，卖出 500 股、均价 9.5；下一手会越界。optimistic 保留 1,000 股并截价至 10.55/9.45，conservative 拒单且不消耗成交容量。
- 固定 bps 本身越界、连一手也不可行时，baseline 拒单；滑点后恰好边界允许成交。原始参考价已处于同向限价的拒单规则保持不变。
- 缩量后重新计算成交额、佣金、卖出税、过户费和滑点成本；现金进一步不足时继续缩量。逐项验证现金/持仓/账本恒等式、实际成交容量和订单余量到期，不会对同一订单重复执行。
- 引擎集成测试分别覆盖开盘和收盘，核验实际成交量对应的净值、换手、费用及 JSON/CSV 导出；不同模式产生不同 `run_id`，相同模式重跑指纹一致。
- 原“官方昨收优先”测试曾使用参考价 4.4、官方昨收 5，对应跌停价 4.5；旧成交依赖越界截价。现仅将该测试的 OHLC 改为 4.6，仍超过行动推导涨停价 4.4，且位于官方区间 [4.5, 5.5] 内，保留原优先级断言。行情与限价不一致的拒单/乐观截价另有双边专项测试。
- 使用默认 baseline 重跑演示研究与缓存 CLI；22 个归档的现金/持仓、清单哈希及逐字节重放核验全部通过。

## 此前两项修复的回归证据

- 先在原实现复现：有效收盘 10、停牌除息 1、恢复日收盘 9.9。无停牌 Bar 时订单为 `rejected: limit_up`；仅加一条停牌日旧价 10 的 Bar 后变为 `filled`。修复后两种输入的推导参考均为 9，订单均被 `limit_up` 拒绝。
- 针对价格参考新增 5 项测试；最初 4 项对旧实现产生 3 个失败、1 个通过，修复后全部通过。使用收盘执行隔离历史成交量的影响，另核验官方参考价/限价优先和多次行动的时序。
- 认证回归覆盖：缺省元数据、只有旧布尔标记、空或非空 actions 列表都不自动认证；不完整的资产/日期范围不能运行；总收益的完整历史与热身、空历史查询也受范围约束；证书字段校验与缓存往返保留。
- 仅价格研究 override 会保存未认证警告与 `action_verification_check.verified=False`，不会放行 `total_return`。
- 合成演示保存新 `synthetic-v3-seed-20260922` 快照；旧快照不覆盖、不自动加证书。重跑 22 个归档后，会计核验、认证覆盖检查和逐字节复现均通过。

## 审查中修复的边界

1. 停牌除息时不能把未更新旧报价与分红相加而制造正收益；研究 history 跳过停牌 Bar，账本独立调整除权估值。
2. 覆盖区间内的非交易日登记日不能默认为空持仓，现明确拒绝。
3. 日历覆盖区间与首尾交易日分开保存，元旦/周末边界不会被误判成未覆盖。
4. ETF 限价 API 的迁移公告日期不能被误用为历史数据切分日期。
5. 导出重算指标沿用配置年化系数及无风险利率；参数试验保留运行指纹和选择方向。

## 未声称完成的验收

- 未使用真实 Token 或实际 AkShare SDK 网络请求下载；数据适配器测试通过注入的确定性客户端完成。
- 不用合成演示收益验证真实市场策略有效性。
- 不声称通过这些测试就能保证任何自定义策略/外部数据没有前视；数据修订时点、历史成分与复杂公司行动仍必须核验。
- 没有分钟成交/真实集合竞价回放、券商逐笔对账或全市场大规模性能基准。具体建模边界见 `ASSUMPTIONS.md`。
