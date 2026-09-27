# 数据接入、缓存与口径

## 输入契约

内部日期为 `datetime.date`，Asset / Bar / CorporateAction 构造器也接受 ISO 日期字符串。股票代码保持数据商唯一代码，例如 `000001.SZ`；价格与现金为人民币，成交量为股/份，成交额为元。

```python
from ashare.models import Asset, Bar, CorporateAction
from ashare.calendar import TradingCalendar
from ashare.data import DataBundle, DataPortal, actions_verified_metadata

assets = {"TEST": Asset("TEST", list_date="2024-01-02", lot_size=100)}
calendar = TradingCalendar(
    ["2024-01-02", "2024-01-03", "2024-01-04"],
    coverage_start="2024-01-01", coverage_end="2024-01-04",
)
bars = [
    Bar("TEST", "2024-01-02", 10, 10, 10, 10, 100000, adj_factor=1),
    Bar("TEST", "2024-01-03", 9, 9, 9, 9, 100000,
        pre_close=9, adj_factor=10/9),
    Bar("TEST", "2024-01-04", 9, 9, 9, 9, 100000,
        pre_close=9, adj_factor=10/9),
]
actions = [CorporateAction("TEST", "2024-01-03", cash_dividend=1,
                           record_date="2024-01-02", pay_date="2024-01-04")]
metadata = {"source": "audited_local"}
metadata.update(actions_verified_metadata(
    ["TEST"], "2024-01-02", "2024-01-04", "local-researcher",
    evidence="逐项核对样例中的分红及无其他行动日期",
))
bundle = DataBundle(assets, bars, calendar, actions, metadata)
portal = DataPortal(bundle)  # 重复行/生命周期/日期/未知证券等错误在这里抛出
```

`TradingCalendar` 接收交易所实际开市日，覆盖起止可包含休市日。不得用工作日生成器代替真实 A 股日历。识别最后一个月的月末仍需要下月第一个交易日，即使该日没有回测行情。多交易所时当前采用共同的给定 session 集合，交易所差异须额外校验；不自动拼接不一致日历。

当一个资产停牌时可以无 Bar，或输入事前已知的 `suspended=True` Bar。缺失行情不补成可以交易的价格。history 在所有模式下跳过停牌观测，current 仍返回原始停牌记录供策略检查。停牌日的净值估计由引擎按旧价与已生效公司行动单独处理。

当执行日的 `pre_close` 缺失时，引擎从最后一条非停牌原始 Bar 的收盘价出发，按日期顺序累计 `(原始收盘锚点日期, 执行日]` 内已生效的现金分红和拆分。停牌 Bar 中残留的除权前旧价不会替换该锚点；插入这种记录不会漏掉期间行动。官方限价和已提供的 `pre_close` 仍优先。历史成交量查询保持原有政策，价格锚点修复不改变开盘容量估计。

## 公司行动核验声明

手工 bundle 和下载适配器均默认公司行动未认证。仅有 `corporate_actions_complete=True`、传入空行动列表或非空行动列表，都不足以认证完整性。核验完成后使用 `actions_verified_metadata(symbols, start, end, verified_by, *, evidence='')` 生成以下 metadata；日期边界均包含当日：

```python
{
    "corporate_actions_complete": True,
    "corporate_actions_verification": {
        "symbols": ["TEST"],
        "start": "2024-01-02",
        "end": "2024-01-04",
        "verified_by": "local-researcher",
        "evidence": "核验依据或记录位置",  # 可选
    },
}
```

这份声明记录**调用者已完成的核验及其范围**，不会自动验证供应商是否漏掉事件。应按声明的资产和完整日期区间核查分红、送转等事件，包括没有行动的日期；确认期间没有公司行动时也可以明确传入 `actions=[]` 并认证该范围。建议同时保留数据来源、版本/提取时间和实际核验依据。

- Engine 默认要求声明覆盖 bundle 中全部资产、回测起止日期，并保守地将各资产在首个回测交易日前的最近有效原始收盘锚点纳入检查，以覆盖缺失 `pre_close` 的推导需要。即使本次提供了官方参考价，也执行这一范围检查。认证范围不足会拒绝运行，实际检查范围保存在 `action_verification_check`。
- `total_return` 要求覆盖目标资产从首条有效历史观测至 `as_of` 的整个区间，不能只覆盖请求的 lookback。回测前用于信号热身的行情也要核验，因为总收益指数从该资产历史起点累积。
- 仅做价格研究时可显式设置 `BacktestConfig(..., allow_incomplete_actions=True)`；覆盖失败警告进入运行元数据。这不会解除 `total_return` 检查，也不表示现金分红、账本或推导限价已经可信。
- Tushare / AkShare 的 `fetch_bundle` 参数 `actions_verification` 只接收上述 **内层** `corporate_actions_verification` 字典，并要求同时显式传入 `actions`。只提供证书而未传 `actions` 会报错；只传行动列表则仍为未认证。

## Tushare

可选安装与 Token：

```powershell
python -m pip install -e ".[tushare]"
$env:TUSHARE_TOKEN = '你的Token'
```

示例：下载原始行情、参考限价和调整因子，先核实公司行动，再存不可变快照。

```python
from ashare.adapters import TushareAdapter
from ashare.cache import SQLiteCache
from ashare.data import actions_verified_metadata

adapter = TushareAdapter()  # 读取 TUSHARE_TOKEN
master = adapter.fetch_stock_assets()  # L/D/P，包含退市/暂停列表
asset = master["000001.SZ"]
actions = adapter.fetch_corporate_actions(asset, "2022-01-01", "2024-12-31")
# 在此核验 actions 是否覆盖研究期间所有相关权益事件；dividend 不是完整公司行动库。
# 包含不受支持的事件时应扩展模型，不能删掉异常继续宣称完整。
verification = actions_verified_metadata(
    [asset.symbol], "2022-01-01", "2024-12-31", "research-team",
    evidence="填入实际核验记录或公告清单位置",
)["corporate_actions_verification"]
bundle = adapter.fetch_bundle(
    [asset], "2022-01-01", "2024-12-31",
    actions=actions, actions_verification=verification,
    calendar_end="2025-02-10",
    include_adjustments=True, include_limits=True,
)
with SQLiteCache("cache/market.sqlite") as cache:
    data_hash = cache.save("tushare-20260922-verified-v1", bundle)
    restored = cache.load("tushare-20260922-verified-v1")
```

ETF 用显式 `Asset(symbol, kind='etf', list_date=实际上市日, tick_size=实际单位, t_plus_one=实际规则)` 传入。股票 master 的 `delist_date` 可能为行政终止日；支持 `fetch_stock_assets(last_tradable_dates={symbol: 最后交易日期})` 覆盖。ETF 最后交易日和公司行动当前需外部提供，不能把基金终止日期直接当交易终止日期。

接口与规范化：

| 内容 | 调用 | 处理 |
|---|---|---|
| 股票原始日线 | daily | vol 手 ×100；amount 千元 ×1000；pre_close 为除权参考 |
| ETF 原始日线 | fund_daily | vol 手 ×100；amount 千元 ×1000 |
| 调整因子 | adj_factor / fund_adj | 必须与所需原始 Bar 对齐，缺失报错 |
| 官方限价 | stk_limit / etf_limit | 默认 ETF 全部历史走 etf_limit；旧服务可显式指定 etf_limit_endpoint='stk_limit' |
| 日历 | trade_cal | 检查整个请求区间的每个自然日返回，拒绝截断 |
| 股票分红送转 | dividend | 仅规范化实施记录，按 ex_date 过滤，不把预案当行动 |

按单资产、自然年拆请求，避免常见日线/因子行数上限；不把权限或网络错误当成空数据。未建立自动重试/速率限制器，也不绕过账户权限；下载失败可重新运行并保存新版本快照。

Tushare `daily` 的原始价格、停牌缺行和单位见[股票日线文档](https://tushare.pro/document/2?doc_id=27)；ETF 单位见[基金日线文档](https://tushare.pro/document/2?doc_id=127)，因子见[基金复权因子](https://tushare.pro/document/2?doc_id=199)。ETF 限价接口迁移信息见[官方更新记录](https://tushare.pro/document/1?doc_id=9)，迁移公告日期不用于拆分历史行情日期。

`fetch_corporate_actions` 使用税前 `cash_div_tax` 和总送转 `stk_div`，不会再加一遍送股/转增子字段。要求登记日、现金支付日和公告日；送转若红股上市日晚于除权日或缺失，明确拒绝，因为本版本还没有延迟上市股份批次模型。字段语义见[分红送股文档](https://tushare.pro/document/2?doc_id=103)。ETF 分红、配股、合并等不能由该函数自动认证。

## AkShare

```python
from ashare.adapters import AkShareAdapter
from ashare.data import actions_verified_metadata

# stock_zh_a_hist 的单位固定规范化；ETF单位需先核验源数据再显式设置。
adapter = AkShareAdapter(etf_volume_multiplier=100)
verified_symbols = (list(verified_assets) if isinstance(verified_assets, dict)
                    else [asset.symbol for asset in verified_assets])
verification = actions_verified_metadata(
    verified_symbols, "2022-01-01", "2024-12-31", "research-team",
    evidence="填入实际核验记录位置",
)["corporate_actions_verification"]
bundle = adapter.fetch_bundle(
    assets=verified_assets,
    start="2022-01-01", end="2024-12-31",
    calendar=verified_calendar,
    actions=verified_actions,
    actions_verification=verification,
    adjustment_factors=verified_adjustment_factors,  # 可省略，届时不支持adjusted模式
)
```

这里的 `verified_assets` 是 Asset 列表/映射，`verified_calendar` 是 TradingCalendar，`verified_actions` 是已核验的 CorporateAction 列表，`verified_adjustment_factors` 是 `{symbol: {datetime.date: factor}}`。示例变量表示必须由自己的数据源提供的已核验对象，不能用任意空值冒充完整数据。

- 股票走 `stock_zh_a_hist`，ETF 走 `fund_etf_hist_em`，始终 `adjust=''` 获取原始价格。
- 不抓最新前复权/后复权序列作为历史点时可见价格。若要使用 `adjusted`，需传规范化因子；可直接以原始价和完整行动生成 `total_return`。
- 股票文档声明成交量为手、成交额为元；当前 ETF 文档没有明确量单位，所以要求调用者显式配置 `etf_volume_multiplier`。没有静默猜测默认值。[股票文档](https://akshare.akfamily.xyz/data/stock/stock.html)，[ETF 文档](https://akshare.akfamily.xyz/data/fund/fund_public.html)。
- `tool_trade_date_hist_sina` 返回覆盖范围可能有限；适配器检查覆盖，不能满足请求会报错。可使用另一个已核验日历源传入。[工具与日历文档](https://akshare.akfamily.xyz/data/tool/tool.html)。
- 这些行情端点未给出足以完整审计的资产生命周期、所有公司行动和每日官方限价；这些是显式输入或规则覆盖，不能声称由 AkShare 行情自动还原。

## 快照管理

```python
from ashare.cache import SQLiteCache
with SQLiteCache("cache/market.sqlite") as cache:
    print(cache.list_snapshots())
    bundle = cache.load("tushare-20260922-verified-v1")
```

同一个名字再次存相同内容是幂等操作；不同内容拒绝覆盖，应换版本名。加载检查内容 SHA-256。缓存记录创建时间，但运行结果不包含随机时间戳，因此同代码、输入、配置可得到一致输出。`result.metadata` 保存运行数据指纹、源码指纹及完整执行参数。

旧缓存可以继续加载和读取原始行情，但没有显式资产/日期核验声明的旧数据不能自动通过默认回测或 `total_return` 检查。重新核验后给加载的 bundle 合并 `actions_verified_metadata(...)`，再用新快照名保存；声明改变输入指纹，不要覆盖旧版本。新版演示的合成数据明确认证其生成的完整行动范围，快照名为 `synthetic-v3-seed-20260922`，原 v2 快照保留。

无行情的日子会拒绝交易；静态快照的缺行无法自动判定是停牌、接口漏数还是供应商故障。真实研究应按资产和时间核验覆盖率，必要时设 `missing_price_policy='error'`。数据历史修订仍需要取得并保存相应时间版本，内容缓存本身无法创造不存在的历史版本。

## 当前验收边界

本次已基于上述官方文档实现接口，并通过注入客户端的离线单元测试验证调用、日期、单位、生命周期和错误行为。**尚未使用真实 Tushare Token 或真实 AkShare SDK 网络请求做端到端下载验收。** 上线研究前需用小区间实际下载，与行情终端及分红公告对照，并保存核验过的数据快照。
