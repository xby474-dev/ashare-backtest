# 研究代码与复现说明

仓库地址：<https://github.com/xby474-dev/ashare-backtest>

申请书引用的固定版本：**`v0.1.0`**

固定版本入口：<https://github.com/xby474-dev/ashare-backtest/tree/v0.1.0>

本版本提供 A 股与 ETF 的事件驱动回测基础引擎、合成数据演示及审计工具。它用于核验信号时点、成交、现金、费用和报告生成流程；申请书中的完整实证研究仍需进一步实现研究信号、预测模型并接入核验后的真实数据。后续代码变更应使用新版本，复现本版本时应检出上述标签。

## 环境文件

| 文件 | 用途 |
|---|---|
| [`.python-version`](../.python-version) | 指定参考解释器 Python `3.12.14` |
| [`pyproject.toml`](../pyproject.toml) | 包版本、Python 兼容范围、构建配置及可选数据 SDK 的依赖声明 |
| [`uv.lock`](../uv.lock) | 固定依赖解析结果，与 `uv sync --locked` 配合，防止运行前静默更新依赖 |

参考环境管理工具为 uv `0.12.19`，其解释器下载目录包含 Windows 与 Linux 的 Python `3.12.14`。核心回测、合成演示、报告和自动测试只使用 Python 标准库；安装项目时仍需相应的构建工具。锁文件可能记录可选 SDK 的依赖，但默认 `uv sync --locked` 不安装这些扩展。Tushare、AkShare 的联网下载不属于本版本离线演示的复现前提，真实 SDK 与数据账户尚未完成联网端到端验收。

## 从固定版本运行

预先安装 Git 和 uv，在终端依次执行：

```bash
git clone https://github.com/xby474-dev/ashare-backtest.git
cd ashare-backtest
git checkout --detach v0.1.0
uv sync --locked
uv run --no-sync python -m unittest discover -s tests -v
uv run --no-sync python -m ashare demo --research --output outputs/demo
uv run --no-sync python tools/verify_demo.py --replay
```

首次创建环境可能需要联网下载解释器及安装所需工具；环境准备完成后，上述测试和合成演示无需行情账户，也无需请求市场数据。请使用不含 `--all-extras` 的同步命令复现核心环境。

打开 `outputs/demo/index.html` 查看结果。演示包括买入持有、趋势过滤、月末 Top-K 动量，以及参数网格、消融和分段样本外实验。`verify_demo.py --replay` 会核对现金与持仓、结果文件 SHA-256 清单，并重新运行演示检查输出是否一致。应先完成前一条演示命令，再执行重放核验；每个命令均应正常退出。

**演示使用固定种子的合成价格和工作日历，不是真实 ETF 历史数据，也不是交易所日历。** 演示收益仅用于验证软件流程，不能作为申请书研究假设或策略有效性的实证证据。测试记录及其适用边界见 [VALIDATION.md](VALIDATION.md)。

## 研究模块与现有实现的对应关系

申请书中的 `src/` 目录是拟开展研究的职责划分；本版本的实际 Python 包为 `ashare/`，没有同名的七个 `src/*.py` 文件。二者对应如下：

| 拟定研究模块 | 本版本实现与后续内容 |
|---|---|
| `data_pipeline.py`：数据处理 | `ashare/adapters.py`、`data.py`、`cache.py` 提供数据适配、校验、价格口径和快照缓存；真实样本的数据收集与核验仍需完成。 |
| `universe.py`：动态基金池 | `ashare/data.py` 的 `HistoryView.assets()` 按信号日期过滤上市与退市状态；申请书要求的完整动态基金池、分组及筛选规则仍需补充。 |
| `signals.py`：动量、趋势、回调 | `ashare/strategies.py` 提供动量排序与趋势过滤示例；申请书中的一般回调、条件回调及其组合规则尚未实现。现有窗口以有效行情观测数计，不能直接等同于申请书的月度窗口。 |
| `regression.py`：嵌套预测模型 | 尚未实现嵌套预测回归。`ashare/experiments.py` 的网格、消融与滚动样本外工具用于策略实验，不能替代预测模型估计。 |
| `portfolio.py`：选券及配置 | `ashare/strategies.py` 形成示例持仓权重，`ashare/engine.py` 组织目标执行；研究特定的候选池、评分和配置规则仍需实现。 |
| `backtest.py`：成交、现金、费用 | `ashare/engine.py`、`execution.py`、`ledger.py` 处理事件推进、交易约束、成交费用、现金及持仓账本。 |
| `evaluation.py`：预测及绩效评价 | `ashare/analytics.py` 提供收益、风险、回撤、换手等绩效指标及审计报告；预测 MSE、Rank IC 尚未实现。 |

完整工程目录与 API 示例见 [README.md](../README.md)。上述对应关系用于说明现有基础与研究计划，不表示所有拟定模块均已完成。

## 真实数据与版本记录

开展真实样本研究前，需准备包含历史上市、退市信息的基金基础资料，核验实际交易日历、原始行情、复权因子和公司行动，并记录数据来源、提取日期、样本范围及单位。基金池和信号只能使用决策时点已可获得的信息。公司行动须提供明确的核验资产、日期范围与核验人声明；数据接入要求见 [DATA_GUIDE.md](DATA_GUIDE.md)，成交与估值边界见 [ASSUMPTIONS.md](ASSUMPTIONS.md)。

每次正式研究运行应保留以下信息：

- 代码标签及 `git rev-parse HEAD` 得到的完整提交号，同时确认没有未记录的本地源码修改。
- `.python-version`、`pyproject.toml`、`uv.lock`，以及实际 Python、uv 和操作系统版本。
- 数据来源及快照名称、数据指纹、样本与训练验证测试区间、参数选择记录和最终冻结参数。
- 输出中的配置、`run_id`、`metadata.json`、`manifest.json`，以及成交、现金、持仓和净值记录。

真实数据快照依赖相应数据来源及使用权限；仓库提供代码与合成演示，未附带已完成申请书全部分析所需的真实市场样本。`outputs/` 为本地生成目录，不随源码提交。相同环境下的合成演示重放核验与真实数据研究的可复现性应分别记录。

申请书可引用：**研究代码仓库：https://github.com/xby474-dev/ashare-backtest；固定版本：v0.1.0；环境文件：.python-version、pyproject.toml、uv.lock；复现方法：docs/REPRODUCIBILITY.md。该版本为研究基础引擎与合成数据演示版本。**
