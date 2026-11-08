# 台股资料底座（第一阶段）

输入一个台股代号，取得经过格式统一与品质检查的**股票主档、历史日线与个股法人资料**，保存于本机 SQLite，供后续报告与条件提醒共用。

本阶段**只建立资料层**：不含 LINE 推送、AI 报告、盘中监控、正式排程、新前端或下单。也不改动既有分析流程的台股路由（`DataFetcherManager` / `stock_daily` 维持原状），第二阶段透过本文件的服务接口接入。

- 服务：`src/services/tw_market_data/`（`TwMarketDataService`）
- 来源适配：`data_provider/finmind_client.py`、`data_provider/tw_finmind_adapter.py`、`data_provider/tw_yahoo_daily_adapter.py`；法人官方对照沿用 `data_provider/tw_institutional_fetcher.py`
- 储存：`src/storage.py` 中的 `tw_*` 资料表 + `src/repositories/tw_market_repo.py`
- CLI：`scripts/tw_market_data.py`

## 1. 资料来源与官方限制（2026-10 查证）

| 资料 | 来源 | 说明 |
| --- | --- | --- |
| 股票主档 | FinMind `TaiwanStockInfo` | 免费；字段 `stock_id / stock_name / type(twse,tpex,emerging) / industry_category / date`。同代号可多列；**无上市日、下市状态** |
| 日线 raw | FinMind `TaiwanStockPrice`（主） | 免费；周一至五 17:30 更新；`Trading_Volume` 单位为**股**（文件载明）；无成交时价格栏为 0 |
| 日线 backward_adjusted | FinMind `TaiwanStockPriceAdj` | **Backer / Sponsor 限定**；免费等级回 `HTTP 400 Your level is free` → `permission_denied`，不写资料 |
| 日线 split_adjusted | Yahoo Finance（备援） | 仅在明确允许替代口径时回传，见第 5 节 |
| 交易日历 | XTAI（exchange-calendars）+ 本机覆写；FinMind `TaiwanStockTradingDate` 只确认历史 | 见第 4 节 |
| 个股法人 | FinMind `TaiwanStockInstitutionalInvestorsBuySell`（主）；TWSE T86 / TPEx OpenAPI（官方对照） | 周一至五 20:00 更新 |
| 全市场法人 | FinMind `TaiwanStockTotalInstitutionalInvestors` | 15:00 更新；与个股法人分开储存（`scope=market`） |

FinMind API：`GET https://api.finmindtrade.com/api/v4/data`，参数 `dataset / data_id / start_date / end_date`，token 以 `Authorization: Bearer` 头传送（不放在 URL）。每小时额度：未带 token 300、免费注册 600、Backer 1600、Sponsor 6000。参考：<https://finmind.github.io/llms.txt>、<https://finmind.github.io/tutor/TaiwanMarket/DataList/>。

**实测验证（2026-10-27，匿名额度）**：

- FinMind 个股法人 `buy/sell` 与 TWSE T86（2330，2026-10-22）、TPEx OpenAPI（6488，2026-10-27）逐笔一致 → 单位确认为**股**（`unit_status=verified`）。
- TWSE T86「三大法人买卖超股数」= 外资（不含外资自营商）+ 外资自营商 + 投信 + 自营商（自行+避险）。
- FinMind `TaiwanStockTotalInstitutionalInvestors` 数值量级为金额（TWD），**尚未**与官方表比对 → `unit_status=unverified`，不做衍生合计或跨来源比较。
- Yahoo 台股成交量与交易所成交股数不同（2330 2026-10-19：Yahoo 23,696,850 vs FinMind 27,694,594），标 `definition_differs`。
- Yahoo 在休市日（2026-10-23）及 0050 分割停牌期间（2025-06-11~17）回传成交量 0、OHLC 等于前收的占位列；0050 于 2025-06 分割后价格已调整，但 `Stock Splits` 栏未回报事件。
- 0050 分割前（2025-06-05）：FinMind raw 收盘 181.65、成交 10,845,068 股；Yahoo 同日 45.41、40,696,376 股（约 ÷4 / ×4）。两者口径不同，不可混接。FinMind 在停牌期间无资料，这些 XTAI 交易日会列为 `missing_unexplained`，不推论为停牌。
- 抽查 2330、6488、0050、00878 于 2026-10-13~27 共 36 列：FinMind 原始回传与储存后的日期、OHLC、成交股数、成交金额完全一致。

## 2. 资料契约

### 2.1 主档 `tw_security`

| 字段 | 说明 |
| --- | --- |
| `security_id` | 内部唯一识别，`TW:<stock_id>`，例如 `TW:00878` |
| `stock_id` | 原始代号，**字串**，保留 ETF 前导零 |
| `name` / `industry` | 名称；产业（同日多个类别以 `/` 合并，例如 `半导体业/电子工业`） |
| `current_market` | `TWSE` / `TPEx` / `emerging` / `unknown`，由来源最新快照决定，**不以代号长度猜测** |
| `security_type` / `type_basis` | `common_stock`（4 码数字且来源类别非 ETF/ETN/存托凭证…，`inferred`）/ `etf`（来源类别，`source_category`）/ `other` / `unknown` |
| `yahoo_symbol` | TWSE → `.TW`、TPEx → `.TWO`；兴柜不提供 |
| `listing_status` | `listed_per_source`（出现在最新快照）/ `absent_from_latest_snapshot`（可能下市，未确认）/ `unknown` |
| `listing_date` | 来源未提供 → 一律为空，**不以首笔资料日期替代** |
| `source` / `source_as_of` / `fetched_at` | 来源、来源快照日期、取得时间 |

`tw_security_market_history` 只追加来源的市场别**观察**（`observed_date` 为来源 `date` 栏）；`effective_start/effective_end/effective_basis` 只在有来源证据时填写。观察日期不等于转换生效日。

支援边界：第一版只支援 `TWSE`/`TPEx` 的 `common_stock` 与 `etf`；其他类型（ETN、存托凭证、受益证券、权证、特别股 `2881A` 等）与兴柜，查询时回 `unsupported_security`。

### 2.2 日线 `tw_daily_bar`

唯一键：`(security_id, trade_date, provider, adjustment, series_version)`。

| 字段 | 说明 |
| --- | --- |
| `trade_date` | 资料所属**台湾市场日期** |
| `open/high/low/close` | 价格；来源的 0 价格视为“无成交”，存为空值并标 `no_trade_zero_price` |
| `volume_shares` / `volume_unit_raw` / `volume_unit_status` | 统一为股；`documented`（文件载明）/ `verified` / `unverified` / `definition_differs` |
| `turnover_twd` | 成交金额，来源未提供为空（不以量×价估算） |
| `currency` | `TWD` |
| `adjustment` | `raw` / `split_adjusted` / `backward_adjusted`；不同口径**不混用** |
| `series_version` | raw 固定为 `raw`；还原序列为版本 id |
| `provider` / `fetched_at` / `provider_updated_at` | 来源、程式取得时间；来源更新时间只在来源提供时保存（FinMind 不提供 → 空，不捏造） |
| `row_quality` / `issues` | 列品质与问题清单（JSON） |
| `market_segment` | 该日所属市场区段；无证据时为 `uncertain` |

### 2.3 时间慣例

- 所有时间戳以 **UTC ISO8601 字串**保存（`2026-10-27T09:31:05.123456+00:00`，`UTCDateTime` 型别）；写入 naive datetime 直接报错，避免台北时间被误当 UTC。已测试写入 → 关闭 → 重新开启后值与时区不变。
- 显示时同时给出 `Asia/Taipei` 与 UTC（JSON 中 `fetched_at_utc` / `fetched_at_taipei`）。
- `trade_date` 为台湾市场日期（`DATE`）。

### 2.4 法人 `tw_institutional_flow`

唯一键：`(scope, security_id, trade_date, provider, category)`；`scope=stock`（个股）与 `scope=market`（全市场，`security_id=TW:MARKET`）不混用。

| 规范类别 | 定义 | FinMind `name` |
| --- | --- | --- |
| `foreign_ex_dealer` | 外资及陆资（不含外资自营商） | `Foreign_Investor` |
| `foreign_dealer_self` | 外资自营商（TWSE 2017-12-18、TPEx 2018-01-15 起） | `Foreign_Dealer_Self` |
| `investment_trust` | 投信 | `Investment_Trust` |
| `dealer_self` / `dealer_hedging` | 自营商自行买卖 / 避险（2014-12-01 起） | `Dealer_self` / `Dealer_Hedging` |
| `dealer_combined` | 自营商合并值（2014-12 前或来源只给合并值） | `Dealer` |
| `dealer_total` | = `dealer_self + dealer_hedging`（或 `dealer_combined`，二者互斥，不重复计算） | — |
| `total` | = `foreign_ex_dealer + foreign_dealer_self + investment_trust + dealer_total` | 全市场资料集的 `total` |

- 官方汇总（T86 / TPEx `TotalDifference`）存 `is_derived=false`；由基础类别加总者存 `is_derived=true`、`components` 与 `derivation_completeness`（缺任一组成 → `incomplete` 且 `net` 为空）。
- 无资料为空值，来源回报的 0 保留为 0，二者不混用。
- **单位安全加总**：组成类别全部存在、非空、`unit_status=verified` 且单位一致时才产生衍生数值。否则 `net/buy/sell` 为空，并以 `derivation_completeness` 区分：
  - `incomplete`：缺组成类别或组成为空值。
  - `not_summable`：组成齐全但单位未验证或不一致。

  原因列在 `derivation_reasons`（`missing_component` / `null_value` / `unit_unverified` / `unit_mismatch`），不以 0 填补。
- 跨来源比较遵守同一规则：单位不一致回 `unit_mismatch`，未验证回 `unit_unverified`，二者都不比较数值。
- **逐日完整性**（`institutional` 回传 `daily_status`）：只看主来源，官方对照资料不参与判定。
  - 必需类别：外资（不含外资自营商）、投信；外资自营商依市场揭露起日（TWSE 2017-12-18、TPEx 2018-01-15）；自营商为自行+避险（2014-12-01 起），之前为合并值。
  - 判定结果：任一必需类别缺少或为空值 → `incomplete`；单位未验证 → `unverified`；衍生合计无法产生 → `incomplete`。
  - 不完整的日期列入 `range.incomplete_dates`（问题码 `incomplete_day`，与整日无资料的 `missing_dates` 分开），区间整体为 `partial`。
  - 新鲜度仍独立呈现：“有资料的最新日”与“当日是否完整”是两件事。
- 官方对照：上市 → TWSE T86（可指定日期）；上柜 → TPEx OpenAPI（**仅服务最新交易日**，日期不符回报 `date_mismatch`）。

## 3. 状态模型（四轴分离）

| 轴 | 对象 | 值 |
| --- | --- | --- |
| `capability` | 来源 × 资料集 × 口径 | `supported` / `unsupported` / `permission_denied` / `unit_unverified` |
| `row_quality` | 单一资料列 | `ok` / `warning` / `invalid` / `uncertain` |
| `freshness` | 最新资料查询 | `fresh` / `pending_publication` / `delayed` / `stale` / `unknown` |
| `completeness` | 单次查询的指定范围 | `complete` / `partial` / `empty` / `unknown`（日历无法判定） |

可用于“最新完整日线”的列品质为 `ok`、`warning`。问题码（`issues[].code`）为程式可判断的字串，主要包括：`missing_field`、`invalid_date`、`duplicate_date`、`out_of_order`、`high_low_inconsistent`、`negative_volume`、`non_finite_value`、`no_trade_zero_price`、`placeholder_bar`、`missing_unexplained`、`not_yet_expected`、`calendar_conflict`、`bar_on_non_session`、`bar_after_last_closed`、`published_earlier_than_expected`、`emerging_open_definition`、`market_segment_uncertain`、`volume_definition_differs`、`cross_source_mismatch`、`not_comparable`、`upstream_revision`、`pending_publication`、`date_mismatch`、`unit_unverified`、`source_failed`、`permission_denied`、`substitute_not_allowed`、`master_cache_stale`。

缺资料的原则：查无日线不推论为停牌或休市（休市只由日历判定）；无法确认时标 `uncertain`；不以 0 补价格或法人资料；AI 不参与数字修补与品质判定。

## 4. 交易日历与三种日期

- 判定顺序：本机覆写（`calendar override` 或 `TW_MARKET_CLOSURE_OVERRIDES`）> XTAI 预定行事历。
- FinMind `TaiwanStockTradingDate` 存于 `tw_calendar_source_day`，覆盖范围与取得时间存于 `tw_calendar_source_coverage`。其清单会延伸到未来，因此**只在 `[最早日期, min(最晚日期, 取得日前一天)]` 内确认历史**；未被覆盖不代表休市。与 XTAI 不一致时标 `calendar_conflict`（`confidence=conflict`），**不自动改判**，由使用者以覆写解决。
  - 实测：2026-07-10、2026-10-23 XTAI 列为交易日，FinMind 无交易日且无成交资料 → `calendar sync` 会列为冲突。
- 收盘时间以 13:30（含 13:25–13:30 收盘集合竞价）计。

三种日期分开呈现：

| 日期 | 意义 |
| --- | --- |
| `market_last_closed_session` (L) | 市场最近已收盘交易日 |
| `source_expected_date` (E) | 来源**预期**已发布的最近交易日：L 的发布时间（日线 17:30、法人 20:00，可设定）之后为 L，否则为 L 的前一交易日。仅为预期，不保证来源已更新 |
| `actual_data_date` (A) | 实际资料日期：≤ L 且列品质可用的最新列 |

### 4.1 最新资料新鲜度判断表（由上而下第一个命中）

| # | 条件 | freshness | 附注 |
| --- | --- | --- | --- |
| 1 | 无资料 | `unknown` | 先处理，不做日期比较；`completeness=empty` |
| 2 | 日历无法判定 L/E，或 A < E 且 E 为日历冲突日 | `unknown` | `calendar_unavailable` / `calendar_conflict` |
| 3 | A > L | `unknown` | `bar_after_last_closed`；服务层先将此类列排除到 `newer_unusable_bars` |
| 4 | E < A ≤ L | `fresh` | `published_earlier_than_expected`（资讯性）；是否可用由列品质决定 |
| 5 | A = E | `fresh` | `market_session_pending = (L > E)` |
| 6 | A < E，且现在 < 最早缺少交易日的发布时间 + 宽限 | `pending_publication` | `past_scheduled_publish` 表示是否已过预期发布时间 |
| 7 | A < E，落后一个交易日 | `delayed` | |
| 8 | A < E，落后超过一个交易日 | `stale` | |

`market_session_pending` 是独立事实：例如 15:00 时“前一交易日资料符合来源预期（fresh）”与“今天已收盘但尚未到发布时间”会同时呈现。

### 4.2 历史区间完整性

`daily_range` / `backfill` 以**指定范围**验收：期望交易日 = 日历交易日 ∩ `[start, min(end, E)]`；晚于 E 的交易日列为 `not_yet_expected_dates`，不算缺漏；冲突日列为 `uncertain_dates`。历史区间查询**不产生 stale/delayed**，也不触发新资料抓取。

## 5. 价格口径与备援契约

- 查询参数：`adjustment`（`raw` / `backward_adjusted` / `split_adjusted`）+ `allow_substitutes`（预设空）。
- **raw 查询不会回传 Yahoo `split_adjusted`**。主来源无可用资料时回 `bar=null`、`available_substitutes` 列出可用替代与 `substitute_not_allowed`。
- 明确传入 `--allow-substitute split_adjusted` 且主来源不新鲜、替代资料较新时才回传替代，结果带 `requested_adjustment`、`actual_provider`、`actual_adjustment`、`is_substitute=true` 与 `limitations`。
- 主来源失败时 Yahoo 资料仍以独立 provider 列写入（`fallback_from=finmind` 记入抓取纪录），不覆盖主来源。
- 跨来源检查只比较 `adjustment` 相同的资料；不同口径回报 `not_comparable`，不比较、不混接。成交量只在双方单位状态为 `documented/verified` 时比较。

## 6. 快取、增量与还原版本

### 6.1 raw 日线

`update` 对每个（证券, 资料集, 来源, 口径）分开判断：

1. **新资料**：`source_expected_date > 本机最新日` → 立即抓取，**不受重验间隔限制**（例：09:00 执行后，同日 18:00 再执行仍会抓当日日线）。
2. **历史重验**：距上次重验超过 `TW_REVALIDATE_HOURS`（预设 20h）→ 重抓最近 `TW_DAILY_OVERLAP_DAYS`（预设 5）个交易日；值有变动记为 `upstream_revision`（写入抓取纪录与回传 issues）。已有最新日**不会**跳过重验。
3. 两者都不需要 → `cache_hit`，不呼叫 API；计划区间内尚无预期交易日 → `nothing_expected_yet`，也不呼叫 API。`cache_hit` / `nothing_expected_yet` 只是 action，**不代表结果品质**。

**同步结果依本机实际内容判定，而非 HTTP 是否成功或本次是否抓取**（`update` / `backfill` / 法人同步一致）。

`update` 日线的**验收范围**固定为：截至 L（市场最近已收盘交易日）的最近 `TW_DAILY_OVERLAP_DAYS` 个交易日（起点不早于 `TW_DAILY_BACKFILL_START`）。抓取路径（含首次回补）与 `cache_hit` 路径用同一范围、同一政策，因此相同本机状态得到相同 status 与 `range`；`range` 一律回传，不为 null。范围外的旧历史缺漏不影响 `update` 状态，请以 `range` / `backfill` 检查。

| 情境（验收范围内） | status | CLI |
| --- | --- | --- |
| 本机无任何可用列，且范围内已有预期交易日（含首次回补回空） | `failed`，问题码 `empty_response`（仅本次抓取回空时） | 1 |
| 有可用列，但最新资料不新鲜（如增量回空、待发布、延迟）、本机首笔日线之后有缺日、日历无法判定或有不确定日 | `partial`（`reason` 列出缺日） | 2 |
| 新鲜且无缺日（不论本次是抓取或 `cache_hit`）/ 尚无预期交易日 | `ok` | 0 |

- 回传零列的抓取区间在 `tw_fetch_log` 记为 `empty`。同步状态只在有回传时更新 `last_success_at`，否则 `last_error=empty_response: …`。
- `empty` 纪录**不算**已覆盖，因此不会被当作历史参考证据（见 6.2）。
- 法人同步以同一套逐日评估判定：全空 → `failed`；有资料但不完整、不新鲜或对照不一致 → `partial`。

抓取范围延伸到 L：来源若提前发布已收盘交易日，交由列品质决定是否可用。

`backfill --start --end`（以指定范围验收）：

- 范围已完整 → `cache_hit`，不呼叫 API。
- 只重抓真正缺的区间：日历可判定时，抓取范围收窄为缺日集合的首尾（`[第一个缺日, 最后一个缺日]`）。
- **缺漏重试节流**以“同一来源 + 口径 + 请求区间涵盖本次缺漏”的抓取纪录判断（任何结果都算一次尝试，包括回空与失败）：`TW_REVALIDATE_HOURS` 内同一缺漏最多请求两次（首次 + 一次重试，第二次 action 为 `gap_retry`），之后回 `gap_retry_throttled` 与问题码 `retry_throttled`。不同的未抓区间互不阻挡。
- 来源预期日前进后，新交易日使缺漏区间改变，旧纪录不再涵盖 → 不会被节流；`update` 的新资料抓取走独立路径，也不受此节流影响。
- 全空结果为 `failed`（`source returned no usable rows`），部分缺漏为 `partial`。
- `--force` 绕过快取与节流，重抓整个指定范围（`range_refetch`）。

### 6.2 还原序列版本（`backward_adjusted`、Yahoo `split_adjusted`）

还原资料在除权息/分割后整段历史会改变，因此以版本保存（`tw_adjusted_series`）：

| 流程 | 行为 |
| --- | --- |
| 首次建立 | 抓整段 → 写入 `building` 版本 → 验证 → 设为 active |
| 正常新增交易日 | 重叠窗与 active 一致 → 新交易日直接加入 active 版本 |
| 历史修正 | 重叠窗不一致 → 整段建新版本 → 验证 → 单一交易内停用旧版、启用新版 |
| 失败 | 新版标 `failed`，旧版维持 active 且可查 |
| 强制 | `backfill --adjustment backward_adjusted --full-refresh` |

| 分割事件 | Yahoo 回报尚未纳入 active 的 `Stock Splits` 事件 → 即使重叠价格相同也整段重建（`split_rebuild`） |

**切换前验证**（`src/services/tw_market_data/versioning.py`，任一失败即拒绝切换、新版标 `failed`、旧版维持 active，问题码写入回传 `issues` 与版本的 `validation` / `failure_reason`）：

| 检查 | 失败码 |
| --- | --- |
| 至少一列可用资料 | `no_rows` |
| 无 `invalid` 列（Yahoo 休市/停牌占位列除外） | `invalid_rows` |
| 交易日历可判定 | `calendar_unavailable` |
| 起点：首列晚于请求起点的第一个交易日时，须有 raw 参考证明首列即来源首笔 | `head_truncated` / `start_unverified` |
| 内部：首末列之间的交易日缺列，只在 raw 参考也缺时视为来源缺口（记入 `source_gap_dates`） | `internal_gap` / `internal_gap_unverified` |
| 尾端：末列之后至来源预期日的交易日，只在 raw 参考也缺时视为来源落后（记入 `tail_lag_dates`） | `tail_truncated` / `tail_unverified` |
| 基准：末列收盘须等于 raw 同日收盘（容差 0.05%） | `base_mismatch` / `base_unverified` |
| 不短于旧版（结束日、可用列数 ≥ 98%） | `shorter_than_previous` |

- **raw 参考证据**（`RawReference`）：先即时读取 FinMind raw 同区间；失败时才用本机 raw。
  - **覆盖范围**：只认同来源同口径“成功且有回传”的抓取纪录，每笔贡献 `[requested_start, min(requested_end, 该次最后回传日)]`，多笔取联集（相邻区间可合并）。最后回传日之后的日期可能只是尚未发布，不算已证实。失败与 `empty` 纪录不贡献覆盖。
  - **逐日状态**：
    - `present`：已覆盖，且有可用 raw 列（品质 ok/warning、收盘为有限数值）。
    - `absent`：已覆盖，但来源确实没有该日资料。
    - `unusable`：有 raw 列，但为 invalid、无成交或非有限收盘，不作证据。
    - `unknown`：从未被成功请求覆盖。
  - **判定**：只有 `absent` 能解释缺日（记为来源缺口）；`unknown` 或 `unusable` 一律回 `start_unverified` / `internal_gap_unverified` / `tail_unverified` 并拒绝切换。两次孤立的单日抓取不能证明中间日期。
- **上市日不明**：首列晚于请求起点且与 raw 首笔一致时，`validation.start_basis` 记为 `raw_reference_first_record ...; listing date unknown`，不写入上市日，也不宣称“自请求起点完整”。
- **基准日契约**：来源不提供确切还原基准，因此 `base_date` 为推定值，依据写在 `base_date_basis`：
  - 建置时：`base_date` = 末列交易日，依据为“末列收盘等于 raw 同日收盘”。FinMind 文件说明后复权以最新交易日为基准，Yahoo 分割调整在最后一次分割后等于 raw。
  - 正常追加时：本次回应重新包含所有已存的重叠交易日，且价格一致、无新分割事件，代表整段在新的最后交易日下仍成立。`base_date` 前进到新的末列，依据写明重叠区间与“自旧基准延续”。
- **正常追加的验证**（`validate_append`；失败时不写入任何列、不更新 metadata，旧 active 维持原验证范围）：
  - 重叠证据：本机重叠窗内每个已存交易日都必须在本次回应中以可用列出现。只回新日或缺重叠列 → `overlap_unverified`。
  - 新列品质：invalid 新列 → `invalid_rows`；晚于市场最近已收盘交易日的列不追加（问题码 `bar_after_last_closed`，结果 `partial`）。
  - 新区间缺日：依 raw 参考证据，raw 有该日 → `internal_gap`；无法证明 → `internal_gap_unverified`；证实来源也缺 → 接受并记入 `source_gap_dates`。
  - 尾端（新末列之后至来源预期日）不阻挡已验证列的追加，但回报 `tail_truncated` / `tail_unverified` / `pending_publication`，结果为 `partial`。
  - 写入新列与更新 active 的 `end_date` / `row_count` / `base_date` / `validation` 在同一 SQLite 交易内完成。若版本已非 active，整笔回滚。
  - `validation.appends` 保留最近 20 次追加的验证报告，`validation.usable_end` 随成功追加更新。
- **分割事件**：版本保存已纳入的事件（`split_events`）；重建成功后事件即视为已处理，同一事件不会再次触发。重建失败时，`TW_REVALIDATE_HOURS` 内对同一事件回 `split_rebuild_throttled`（不重抓整段），`--full-refresh` 可强制。
- 同（证券, 来源, 口径）只有一个 active：SQLite partial unique index `WHERE is_active = 1` 强制。
- 查询只读单一 active 版本，回传 `series_version` 与 `base_date`。
- **Yahoo `split_adjusted` 限制**：Yahoo 的分割事件栏不完整（0050 2025-06 未回报，此时只能靠重叠窗差异侦测）；台股减资、股票股利是否反映为分割未验证；重叠窗之外且未回报事件的回溯修正侦测不到。备援版本的**首次建置**需要 raw 参考（FinMind 即时或本机），主来源完全中断且本机无 raw 时首次建置会以 `base_unverified` 被拒绝；要让备援可用，需在主来源正常时先执行 `update --adjustment raw split_adjusted` 建好版本，之后中断期间只做追加。

### 6.3 逾时、重试与速率

FinMind（`data_provider/finmind_client.py`）：

| 情况 | 类别 | 重试 |
| --- | --- | --- |
| 网络中断、逾时、HTTP 5xx | `FinMindTransientError` | 有限重试（`FINMIND_MAX_RETRIES`，退避 1s、2s… 上限 8s） |
| HTTP 429（官方文件未定义，视为暂时节流） | `FinMindThrottledError` | 有限重试；遵守 `Retry-After`（秒或 HTTP 日期），单次等待上限 30 秒；超过上限则不等待，改报额度错误 |
| HTTP 402（官方 llms.txt：“HTTP 402 when quota exceeded”），或 429 讯息为额度用尽 | `FinMindQuotaExceededError` | 不重试 |
| HTTP 402/400/401/403 且讯息为会员等级不足 | `FinMindPermissionError` | 不重试；能力状态为 `permission_denied` |
| 本机小时预算用尽（预设无 token 280 / 有 token 550） | `FinMindLocalBudgetError` | 不重试，也不发出请求 |

另有请求间隔 0.25 秒、连续失败熔断 5 分钟。TWSE T86 沿用既有 1.8 秒节流。单一证券失败不阻断整批，结果逐档回报并写入 `tw_fetch_log`。

与原计划的差异：原计划写“429 有限重试”，已依此实作；402 依官方文件归为额度用尽、不重试（除非讯息明确为等级不足）。

## 7. 设定

全部可选，不配置也能以匿名额度运行（见 `.env.example`）：

```env
# FINMIND_TOKEN=                      # 只以 Authorization 头发送，不写日志
# FINMIND_REQUESTS_PER_HOUR=0          # 0=自动
# FINMIND_TIMEOUT_SECONDS=20
# FINMIND_MAX_RETRIES=2
# TW_DAILY_BACKFILL_START=2015-01-01
# TW_DAILY_OVERLAP_DAYS=5
# TW_REVALIDATE_HOURS=20
# TW_YAHOO_FALLBACK_ENABLED=true
# TW_DAILY_PUBLISH_TIME=17:30
# TW_INSTITUTIONAL_PUBLISH_TIME=20:00
# TW_SOURCE_DELAY_GRACE_HOURS=6
# TW_MARKET_CLOSURE_OVERRIDES=2026-07-10,2026-10-23
```

资料库沿用 `DATABASE_PATH`（预设 `./data/stock_analysis.db`）。

## 8. 操作（Mac / Python 虚拟环境）

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python scripts/tw_market_data.py master refresh          # 1. 更新股票主档
python scripts/tw_market_data.py calendar sync           #    同步历史交易日并列出日历冲突
python scripts/tw_market_data.py calendar override 2026-10-23 --closed --reason 颱風假
python scripts/tw_market_data.py search 台積             # 2. 查询名称/代号
python scripts/tw_market_data.py resolve 00878           #    解析为唯一证券（歧义回报候选）
python scripts/tw_market_data.py backfill 2330 6488 0050 00878 --start 2020-01-01   # 3. 首次回补
python scripts/tw_market_data.py backfill 2330 --start 2026-09-01 --end 2026-09-30 --force  # 指定范围重抓
python scripts/tw_market_data.py update                  # 4. 增量更新 STOCK_LIST 中 .TW/.TWO 自选股
python scripts/tw_market_data.py update --stocks 2330 6488 0050 00878
python scripts/tw_market_data.py latest 2330 6488        # 5. 最新完整日线 + 品质摘要
python scripts/tw_market_data.py latest 2330 --allow-substitute split_adjusted --json
python scripts/tw_market_data.py range 0050 --start 2026-10-01                  #    历史区间与完整性
python scripts/tw_market_data.py institutional 2330 --start 2026-10-01          # 6. 个股法人
python scripts/tw_market_data.py institutional --market --start 2026-10-20      #    全市场法人（单位未验证）
python scripts/tw_market_data.py log --symbol 2330       #    抓取纪录、失败原因与来源切换
```

- 所有子命令支援 `--json`。结束码先逐项（每个代号或同步结果）归类，再聚合：全部 `ok` → 0，全部 `failed` → 1，其余 → 2。JSON 每项带 `result_status`。各子命令的逐项归类如下：

| 子命令 | ok | partial（有可用资料但未达标） | failed |
| --- | --- | --- | --- |
| `latest` | 有列、`fresh`、近期窗口 `complete`、非替代口径、列品质 `ok` | 有列但任一条件不满足（如 stale、替代口径） | 无效代号、无可用列 |
| `backfill` / `update` | 同步 `ok`（含 `cache_hit`） | 部分缺漏、节流中但已有部分资料 | 来源失败、权限不足、全空、无效代号 |
| `institutional` | 有列、范围 `complete`、`fresh`、无对照差异 | 有列但不完整/不新鲜/对照不一致 | 无效代号、无资料 |
| `institutional --market` | 有列、`fresh`、同步成功（`unit_unverified` 属文件化能力边界，不单独降级） | 有列但不新鲜或同步失败 | 无资料 |
| `range` | 范围 `complete` | `partial` / `unknown` | `empty` |
- `update` 预设只取 `STOCK_LIST` 中带 `.TW` / `.TWO` 后缀的代号（与既有分析流程的 suffix-only 语义一致）；裸代号请用 `--stocks`。

### 8.1 品质摘要范例

以下为**真实资料**（2026-10-27 23:52 台北时间以 FinMind 匿名额度抓取，2026-10-28 以 `--no-refresh` 读出；本机已覆写 2026-10-23 为休市）：

```text
2330 台積電（TWSE，common_stock）security_id=TW:2330
  能力狀態: supported
  價格口徑: 要求 raw → 實際 raw（provider=finmind，series=raw）
  交易日期: 2026-10-27　取得時間: 2026-10-27 23:52:06 Asia/Taipei (2026-10-27T15:52:06.059795+00:00)
  OHLC: 2,380 / 2,405 / 2,380 / 2,400 TWD
  成交量: 38,173,147 股（原始單位 shares，單位狀態 documented）　成交金額: 91,313,227,194 TWD
  列品質: ok　市場區段: TWSE
  日期: 市場最近收盤 2026-10-27｜來源預期 2026-10-27｜實際 2026-10-27（日曆 scheduled）
  新鮮度: fresh（規則 5）　市場已收盤待發布: 否
  近期完整性: complete（5/5 交易日，2026-10-19..2026-10-27；缺 無）
  本機資料範圍: 2026-09-01 .. 2026-10-27
  缺漏/問題: 無
```

JSON（**真实资料**，00878，节录）：

```json
{
  "requested_adjustment": "raw",
  "capability": "supported",
  "actual_provider": "finmind",
  "actual_adjustment": "raw",
  "is_substitute": false,
  "series_version": "raw",
  "data_range": {"start": "2026-09-01", "end": "2026-10-27"},
  "dates": {
    "market_last_closed_session": "2026-10-27",
    "source_expected_date": "2026-10-27",
    "actual_data_date": "2026-10-27",
    "calendar_confidence": "scheduled"
  },
  "freshness": {"freshness": "fresh", "rule": 5, "market_session_pending": false, "past_scheduled_publish": null, "sessions_behind": 0, "issues": []},
  "completeness": "complete",
  "bar": {
    "security_id": "TW:00878", "trade_date": "2026-10-27",
    "open": 35.68, "high": 35.75, "low": 35.18, "close": 35.4,
    "volume_shares": 30625066, "volume_unit_raw": "shares", "volume_unit_status": "documented",
    "turnover_twd": 1083218122.0, "currency": "TWD", "adjustment": "raw",
    "provider": "finmind",
    "fetched_at_utc": "2026-10-27T15:51:51.790601+00:00",
    "fetched_at_taipei": "2026-10-27T23:51:51.790601+08:00",
    "provider_updated_at_utc": null,
    "row_quality": "ok", "issues": []
  },
  "limitations": [],
  "issues": []
}
```

以下为**测试资料**（离线 fixture，`tests/test_tw_market_service.py::test_raw_never_silently_returns_yahoo`，主来源失败且只有 Yahoo 资料时的严格 raw 查询）：

```json
{"requested_adjustment": "raw", "actual_adjustment": null, "is_substitute": false, "bar": null,
 "available_substitutes": [{"provider": "yahoo", "adjustment": "split_adjusted", "end_date": "2026-10-27"}],
 "issues": [{"code": "substitute_not_allowed", "detail": "primary has no usable bar; pass --allow-substitute to use another price basis", "trade_date": null}]}
```

## 9. 第二阶段可用接口

```python
from src.services.tw_market_data.service import TwMarketDataService
from src.services.tw_market_data.contracts import Adjustment

svc = TwMarketDataService()
security = svc.resolve("00878")                      # SecurityRecord；歧义 → AmbiguousSymbolError(candidates)
latest = svc.latest_complete_bar("2330")             # LatestBarResult（bar + dates + freshness + recent_window + issues）
history = svc.daily_range(security, start=..., end=...)       # DailyRangeResult（bars + range completeness）
flows = svc.institutional("2330", start=..., end=...)         # 个股法人（含衍生合计与官方对照）
market = svc.market_institutional(start=..., end=...)         # 全市场法人（unit unverified）
outcomes = svc.update(["2330.TW", "6488.TWO"])                # 逐档 SyncOutcome，部分失败不阻断
```

月营收、财报：在 `FinMindTwAdapter` 增加一个对应 dataset 的方法、在 `storage.py` 增加一张表，沿用同一套 `SyncOutcome` / `tw_fetch_log` / 品质与新鲜度工具即可，不需另建平行系统。

## 10. 已知限制

- FinMind 主档无上市日与下市状态：`listing_date` 为空，`absent_from_latest_snapshot` 只是“可能下市”。
- 免费等级无法取得 `backward_adjusted`；本阶段未以付费权限实测还原序列（离线测试覆盖四种版本流程）。
- TPEx 官方法人只服务最新交易日，历史日期无法官方对照。
- 全市场法人单位与涵盖市场（仅上市或含上柜）未验证。
- 兴柜股不在支援范围；转上市前的兴柜历史若无生效区间证据标 `market_segment_uncertain`，不清洗、不改值（兴柜 `open` 为前一日均价）。
- XTAI 不含临时休市（台风假）；需执行 `calendar sync` 发现冲突后以 `calendar override` 处理。
- Yahoo 备援的限制见第 5、6.2 节。
- 已接入既有分析主流程（见第 11 节）；Web K 线/历史图表、告警、回测、ETF 轮动等仍走既有 `DataFetcherManager` / `stock_daily` 路径，未接入排程与通知变更。
- `FINMIND_TOKEN` 未注册进 Web 设置的 `config_registry`：注册需要 Web 端说明文案（`settingsHelp.ts`）与 Web 构建/截图验证，超出本阶段“不做前端”范围。在 `.env` 设定时，设置 API 的推断元数据仍将其视为敏感值（`is_sensitive=true`、密码栏位），与 `TUSHARE_TOKEN` 同等处理（有测试锁定）。

## 11. 接入既有分析主流程（第二阶段）

接入层为 `src/services/tw_market_data/analysis_bridge.py`，只做路由、转接与品质摘要，不重新实现底座规则。CLI（`main.py`）、API / Web 分析任务、Bot 共用 `StockAnalysisPipeline.process_single_stock`，接入点即在此。

### 11.1 实际呼叫链

```
输入代号
 → main.py 交易日过滤：analysis_bridge.route_market（台股按 XTAI 过滤；无法安全路由者保留给主流程回报）
 → StockAnalysisPipeline.process_single_stock
     → _route_tw_code → analysis_bridge.route_code（主档 + 既有股票索引）
         ├─ 非台股 → 既有流程（fetch_and_save_stock_data / stock_daily / DataFetcherManager），完全不变
         ├─ 路由错误 → 明确失败结果（success=False，不调用 LLM）
         └─ 台股 → 代号正规化为 2330.TW / 6488.TWO
     → _prepare_tw_analysis_data → analysis_bridge.prepare_analysis(as_of=current_time, target_date=断点续传目标日)
         → TwMarketDataService.sync_daily(raw)（新资料 / 重验政策不变，一律以真实时钟同步）
         → 以 as_of 只读视图评估：最近收盘日、来源预期日、新鲜度；日线只读 cutoff 之前的列
         → daily_range（分析窗完整性）+ institution_block（sync_institutional + 截至 cutoff 的逐日评估）
         → 可用列（ok/warning）转 DataFrame；资料暂存于本档执行期间（失败也暂存，保留台股身分）
         → 准备异常或无可用 raw 日线 → 本档明确失败（不调用 LLM、不进 stock_daily / Yahoo 路径）
     → analyze_stock
         → 台股身分由路由决定，不依赖暂存是否存在；未准备资料即明确失败
         → 不取 Yahoo 即时行情、不并入 raw 序列
         → analysis_bridge.run_trend_analysis（只用底座可用列；讯号所需指标不足则不产生趋势结果）
         → _get_analysis_context_with_market_fallback：today/yesterday 由底座列组成，附 tw_market_data 品质摘要
         → get_fundamental_context：institution 区块由底座提供（同一份，不重抓）
         → AnalysisContextBuilder：daily_bars 来源 tw_market_data:finmind:raw，限制代码进入数据质量提醒
         → LLM prompt：「台股资料品质（程序判定）」区块
         → _apply_tw_data_quality：data_sources 加 daily:tw:finmind:raw / tw_data:<status> / institution:<status>，
           risk_warning 前置「资料限制（程序判定）」
 → Agent 工具：history_loader.load_history_df 对台股只读底座可用列（不超过 explicit target_date / frozen target），
  不读 stock_daily、不回落 Yahoo；路由错误回空；analyze_trend 工具对台股走同一套指标门槛
```

### 11.2 代号路由契约

| 输入 | 结果 |
| --- | --- |
| `2330.TW`、`0050.TW`、`00878.TW` | 主档确认上市 → 台股（保留前导零） |
| `6488.TWO` | 主档确认上柜 → 台股 |
| `6488`（裸代号，仅主档有、既有股票索引无其他市场） | 台股 `6488.TWO` |
| `2330`、`0050`、`00878`（裸代号，既有索引另有 `02330.HK`、`00050.HK`、`00878.HK`） | `ambiguous_market` 失败，提示改用 `2330.TW` 等完整代号 |
| `2330.TWO`、`6488.TW` | `tw_suffix_conflict` 失败 |
| `9999.TW` / 存托凭证等 | `tw_symbol_not_found` / `tw_unsupported_security` 失败 |
| `600519`、`00700`、`HK00700`、`AAPL`、`7203.T`、`005930.KS` 等 | 不进入台股，既有判定不变 |

- 裸代号不只靠位数判定：必须本机主档有此代号，且既有股票索引（`stocks.index.json`，港股以去前导零为键）没有其他市场的同号标的。
- 裸代号路由**只读本机主档**，不为裸代号发网路请求；主档为空时裸代号维持既有市场判定（日志提示改用后缀或先 `master refresh`）。
- 后缀代号需要主档：主档为空或超过 24 小时会尝试刷新；刷新失败且无快取 → `tw_master_unavailable` 失败；有快取 → 沿用并列入限制 `tw_master_cache_stale`。

### 11.3 资料与品质政策

- 只用 FinMind raw（`series_version=raw`）；不混接 provider、口径或版本。raw 不可用时即使存在 Yahoo `split_adjusted`，分析也明确失败并说明“未使用替代资料”（分析入口无替代口径开关，维持严格 raw）。
- 台股不读写 `stock_daily`（避免与既有 Yahoo 列在 `(code, date)` 键上混合）；既有 `stock_daily` 中的台股旧列保留不动、不被分析使用。
- 栏位转接：`open/high/low/close` 对应 FinMind `open/max/min/close`，`volume` = 成交股数（股），`amount` = 来源 `Trading_money`（缺则空，不以量×价代替），`pct_chg` 仅在前一列为日历上紧邻交易日时计算，`ma5/10/20` 以实际可用列滚动、样本不足为空。
- invalid / uncertain 列排除于指标之外并列出日期；缺日不补零、不虚构交易日。
- 名称与筹码隔离：已确认的台股名称取自主档，批量预取（名称、即时行情、日线）排除台股底座代号；`.TW/.TWO` 主档未确认时名称 fallback 只走 Yahoo，不进 A 股来源。台股没有筹码来源：不查询、上下文无 `chip`、提示词与报告标示不可用、context pack 的 `chip` 区块为 `not_supported`；Agent 分支不提供 `get_chip_distribution`，直接呼叫回 `tw_chip_not_supported`。法人资料不受影响。
- 估值隔离：基本面 `valuation` 区块原本以即时行情（台股为 Yahoo）取 PE/PB/市值；台股不呼叫即时行情，`valuation` 为 `not_supported`，原因 `tw_realtime_quote_excluded` 只记在 errors，`source_chain` 为空（未使用任何来源，不当作基本面来源名），提示词与报告不出现 Yahoo 推导的估值。
- 殖利率隔离：Yahoo `info` 会带 `currentPrice` / `regularMarketPrice` / `previousClose` 与现成殖利率；台股一律不用。TTM 股息率为空（`ttm_dividend_yield_unavailable_reason=quote_price_excluded`），prompt 标示不可用并要求模型勿自行推算，报告显示“不可用（不以 Yahoo 价格计算）”。不改用 FinMind 收盘计算，因为 Yahoo 配息序列的期间与每股口径（分割调整）未验证与 FinMind raw 一致；无法确认即留空、不补零。每股现金分红、财报、成长指标与产业仍取自 Yahoo，并在来源列标明；财报只在有实质数值（营收、归母净利、经营现金流、ROE）时才算有资料，只有币别或报告期（如 ETF）不列为财报来源。
- 来源列（程序判定）：台股报告的数据来源由程序依实际使用的区块产生，例如“日线、三大法人：FinMind；技术指标：程序依日线计算；财报、股利、成长指标、产业：Yahoo Finance；未使用：即时行情、估值（台股不使用 Yahoo 价格）”，后接机器标记（`daily:tw:finmind:raw` 等）；模型自写的来源说明不保留。
- 换手率与筹码文字：台股底座没有换手率来源，模型填的换手率会被清空，报告显示“无数据”（不显示 0%；其他市场来源真的回传 0 时仍显示 0%）。筹码区块显示“台股目前不支持筹码分布”，不写成暂时查询失败。
- 量能倍数：`volume_ratio` 标为“量比(5日)”（当日量 / 前 5 笔可用列均量）；较前一交易日只在日期相邻时写成“为前一交易日的 X 倍”，表达增减时写“增加约 Y%”，不写“放大 X 倍”。
- 财务金额：prompt 中营收、归母净利、经营现金流以程序换算后的金额与单位提供（与财务表相同，如 `919776000000 TWD` → “9197.76 亿新台币”，并附原始值），模型不自行换算。保存前程序只改写能确认为本期绝对金额的写法（句中先有程序报告期锚点，锚点到金额之间只有已知的本期项目与分隔符，标签后为“为/达/约”等白名单字词，币别不冲突），并记录 `financial_amount_audit`（`corrected` / `unverified` 与原因 `period_unconfirmed`、`not_absolute_amount`、`currency_conflict`、`report_period_unknown`）；去年、其他季度、增减额与预测等无法确认者不改写，只在“数据限制（程序判定）”列出。价格、百分比与无单位数字不动，`raw_response` 保留模型原文。
- 日期边界：分析时间 `as_of` 依既有优先序取 `current_time`（naive 视为台北时间，与 `get_market_now` 一致；不晚于当下），未给为当下；读取上限 `cutoff = min(as_of 的最近收盘日, target_date)`。日线、最新价、品质摘要、法人区块都只看 cutoff 之前的列，新鲜度相对 `as_of` 判定（历史时点的“待发布/落后”按当时判断）。Agent 历史依 explicit `target_date` → frozen target 截断，暂存资料晚于目标日时重新以目标日评估。限制：以交易日界定，不还原“当时尚未抓到的修正值”（列为目前储存值）。
- 相邻交易日：`pct_chg` 只在前一可用列为日历上的前一交易日（含周末、正式休市与本机覆写）时计算；最新列不紧邻时上下文不提供 `yesterday`、`price_change_ratio`、`volume_change_ratio`，改以 `previous_available_bar`（明示“非前一交易日”）呈现，并列入限制 `tw_latest_previous_not_adjacent`。
- 缺日时的衍生讯号：相邻性在分析器产生讯号**之前**传入（`StockTrendAnalyzer.analyze(..., previous_session_known=False)`，其他市场预设 `True`，行为不变）。此时：
  - `volume_ratio_5d` 保留原定义“当日量 / 前 5 笔可用列均量”，但这 5 笔跨越缺日，`volume_trend` 明示统计范围；
  - `volume_status` 为 `量价方向不可判定`，不产生放量/缩量上涨或下跌；
  - MACD 只保留水平状态（多头/空头），不输出当日金叉、死叉、零轴穿越；
  - 量能分项缺输入，不以中性分代替：`signal_score`、`buy_signal` 为空（`None`），提示词显示“不可用（必要输入缺失）”，context pack 技术区块为 `partial` 并带 `trend_signal_withheld_missing_input`，Agent `analyze_trend` 同样回空值；Agent 失败时的趋势兜底不会把空评分转成买卖决策；
  - 均线、乖离率、RSI、支撑压力与趋势排列以可用列计算（缺日已由 `tw_daily_missing_sessions` 揭露），不依赖单日方向，照常输出。
- 交易日历为 XTAI 预定行事历 + 本机覆写；XTAI 不含台风等临时休市（实测 2026-07-10、2026-10-23），未执行 `calendar sync` / `calendar override` 时这些日期会被列为分析窗缺日（状态 partial）。
- 分析窗为截至最新可用日线的 120 个交易日（不早于本机首笔日线）。指标所需有效列对应 `src/stock_analyzer.py` 实际周期：MA5 5、MA10 10、MA20 20、MA60 60、MACD(12,26,9) 35、RSI6 7、RSI12 13、RSI24 25、5 日量能 6、20 日支撑压力 20；趋势讯号/评分（`trend_signal`）依赖除 MA60 外全部项目，需 35 列。
- 不可计算即不输出：少于 35 列不产生趋势结果（无趋势状态、评分、买卖讯号、支撑判断，上下文无 `trend_analysis`、context pack 技术块为 missing、prompt 无趋势区块）；35–59 列照常计算讯号，但 `ma60` 为空、不以 MA20 代替（MA60 不参与讯号）；上下文 `ma5/10/20` 样本不足者不出现。Agent `analyze_trend` 工具对台股使用同一门槛。
- 新鲜度与分析窗完整性分开：`market_session_pending`（尚未到发布时间，状态 ok + 说明）、`pending_publication`（发布宽限内，来源尚未发布）、`delayed/stale`（来源真正落后）各有不同限制代码。

| 限制代码 | 意义 | 使整体为 partial |
| --- | --- | --- |
| `tw_market_session_pending` | 市场已收盘、来源发布时间未到 | 否 |
| `tw_daily_pending_publication` | 来源尚未发布预期日（宽限内） | 是 |
| `tw_daily_delayed` / `tw_daily_stale` / `tw_daily_freshness_unknown` | 来源落后或无法判定 | 是 |
| `tw_daily_missing_sessions` | 分析窗缺日（列出日期） | 是 |
| `tw_daily_calendar_uncertain` | 分析窗有日历无法确认的日期 | 是 |
| `tw_daily_rows_excluded` | 排除 invalid/uncertain 列 | 是 |
| `tw_indicator_insufficient_rows` | 有效样本不足，部分指标不可计算且不输出 | 是 |
| `tw_latest_previous_not_adjacent` | 最新列的前一可用列并非前一交易日 | 是 |
| `tw_preparation_failed` | 资料准备异常（主档/日历/资料库/来源） | 分析失败 |
| `tw_institution_unavailable` / `tw_institution_limited` | 法人不可用或改报较早完整日/不新鲜 | 是 |
| `tw_master_cache_stale` | 主档刷新失败沿用快取 | 是 |
| `raw_unavailable_substitute_not_used` | raw 不可用、替代口径未使用 | 分析失败 |

- 整体状态：`failed`（无可用 raw 日线 → 不调用 LLM，结果 `success=false`、`data_sources=tw:unavailable`）/ `partial`（可分析但有上表限制）/ `ok`。
- AI 不负责修补数值或判定品质：品质由程序产生，经 prompt 区块、context pack 数据质量提醒、结果 `data_sources`、`risk_warning` 与 `data_quality_limitations`（报告「数据限制（程序判定）」区块）呈现。
- 持久化：`data_quality_limitations` 附加在 analysis history 的 `raw_result` JSON（不改表结构）；`risk_warning` 亦含同一段文字。Web 历史详情页目前不单独渲染该栏位。

### 11.4 法人区块

- 既有 `fundamental_context.institution` 区块改由底座提供，T86 / TPEx 仅作底座内的官方对照，不另外并列，避免重复。
- 只回报最近一个逐日评估为 `complete` 的交易日；不完整、空值、单位未验证或不可加总的日期不回报数值（`partial`），无资料或来源失败为 `failed`。
- 栏位语义沿用原 T86 / TPEx 区块：`foreign_net` 不含外资自营商、`dealer_net` 为自营商合计、`total_net` 为三大法人合计（含外资自营商，另以 `foreign_dealer_self_net` 列出，`total_is_official=false`）。另附 `unit_status`、`freshness`、`limitations`。
- 其他市场不触及底座；`FUNDAMENTAL_FETCH_TIMEOUT_SECONDS=0` 与阶段逾时语义不变。

### 11.5 操作

```bash
python scripts/tw_market_data.py master refresh          # 裸代号路由需要本机主档
python main.py --stocks 2330.TW,6488,0050.TW --no-notify  # 裸 2330 / 0050 与港股同号，需加 .TW
```

### 11.6 相容性与回滚

- A 股、港股、美股、日股、韩股：路由检查只对 4–6 位数字（可带 `.TW/.TWO`）做本机主档查询，不命中即走既有流程。
- 行为变化：本机已载入台股主档后，与台股同号且被既有索引登记为港股/A 股的裸代号会报歧义并要求加市场代号。台股 ETF 编号 `00xxx` 与五位港股代号大量重叠，例如裸 `00700`（腾讯）与 `00700.TW`（富邦恒生国企）同号，此时港股请改用 `HK00700` 或 `0700.HK`；未载入台股主档的环境不受影响。`.TW/.TWO` 分析改用 FinMind raw，不再使用 Yahoo 日线与盘中即时价。
- 回滚：移除 `analysis_bridge.py` 及 `pipeline.py`、`history_loader.py`、`analysis_context_builder.py`、`analyzer.py`、`main.py`、`data_provider/base.py` 的台股分支，`institution` 区块即恢复 `TwInstitutionalFetcher`（可由 git 还原 `data_provider/base.py` 与 `tests/test_tw_institution_report_wiring.py`）。不涉及资料表变更。

回滚方式：删除 `scripts/tw_market_data.py`、`src/services/tw_market_data/`、`src/repositories/tw_market_repo.py`、三个 `data_provider/` 新模块与 `src/storage.py` 中的 `tw_*` 模型；`tw_*` 资料表为独立新表，可直接 `DROP TABLE`，不影响既有资料表。
