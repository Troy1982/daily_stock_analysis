# 台股收盘分析 LINE Flex 手动推送

把已保存的台股分析结果（`analysis_history`）整理成一则 LINE Flex carousel，**手动**预览、验证后推送。
一般分析流程（`main.py`、排程、API）不会调用；不重新抓日线行情、不调用模型；只在预览时为卡片图表唯读抓取当日盘中每分钟价（见下文）。

## 1. 卡片内容与摘要规则

- 输入为指定的 `analysis_history` id（每个 id 一张卡片，最多 12 张）；只读结构化字段：
  `context_snapshot.enhanced_context` 的 `tw_market_data`（日线、法人、品质）、`today` / `yesterday`、
  `trend_analysis`（均线排列、`rsi_6` / `rsi_12` / `rsi_status`、`volume_ratio_5d`），以及 `raw_result` 中已通过
  guardrail 的 `dashboard`、`data_quality_limitations`、`financial_amount_audit`、`news_evidence_present`。不读取 `raw_response`。
- **第一张固定为大盘卡“大盘与柜买指数”**（`--no-market` 可省略）：资料日期取各档卡片中最新的资料日。
  - 加权指数与柜买指数各一段：指数（大字，颜色依涨跌）与成交金额（亿元）同一排；下一排 ▲/▼ 涨跌点数与涨跌幅。
    日资料取自 FinMind `TaiwanStockPrice`（`data_id` = `TAIEX` / `TPEx`，与个股日线同一来源），昨收 = 收盘 − `spread`。
  - 加权指数另画“今日盘中走势（每 20 分钟）”（Yahoo `^TWII` 1 分钟，规则与个股相同）与最高 / 最低；
    Yahoo 没有柜买指数代号，柜买指数只列最高 / 最低，不画图（原因写入 `market.json` warnings）。
  - 某指数当日无资料或 FinMind 取得失败时，卡上以橙色警示标出，不补零；本地检查确认警示在卡片中。
  - 预览目录多存 `market.json`（抓到的原始列与整理后的值）；大盘卡计入 carousel 12 张上限（个股最多 11 档）。
- 之后每档个股一张，依序为（不加“行情 / 收盘 / 成交量”等标签）：
  1. **标题列**：股票名称与代号（不显示 `.TW` / `.TWO` 后缀）；“资料日期：YYYY-MM-DD”，取最新日线 `trade_date`，不是发送日。
  2. **价格**：第一排为收盘价（大字，颜色依涨跌）与成交量（万股 / 亿股）；第二排为 ▲/▼ 涨跌额与涨跌幅
     （上涨红 `#DC2626`、下跌绿 `#16A34A`）。涨跌只在 `pct_chg` 存在（前一列为相邻交易日）时计算，否则“涨跌 不可用”。
     价格去掉多余的零：整数不显示小数（2,135），有小数才显示（92.5、94.45）；涨跌幅固定两位小数。
  3. **今日盘中走势（每 20 分钟）**：资料日当天 09:00、09:20 … 13:20 各取该时点（含）之前最后一分钟的价格，
     终点为 13:30 官方收盘，连成价格路径。
     - 来源：Yahoo Finance 1 分钟（`interval=1m`），仅供卡片显示，不进入分析、不写入 DB，卡上不另列来源；
       预览时抓取并存成 `intraday.json`。Yahoo 不含 13:25–13:30 收盘集合竞价，所以终点固定为官方收盘（FinMind 日线）。
     - 以 Flex box 绝对定位绘制（不需图片托管）：Flex 无法画斜线，每 20 分钟一段再沿两点直线切成数个小 box
       （3 档约 5 个，依 carousel 50 KB 上限自动减少；档数多到放不下时不画图），连成折线。
       颜色依价格相对昨收（红高于、绿低于、灰相等），灰色横线标示昨收，下方列出 09:00 / 昨收 / 13:30，
       再一排“最高 X　最低 Y”（官方日线）。
     - 不画图并写入 `cards.json` warnings 的情况：未取得资料、当日 09:00–13:30 内少于 60 笔、
       最后一笔与官方收盘差距超过 3%（视为错误序列）。
  4. **RSI(6/12)、量比(5日)、RSI 状态**：各一行（不加“技术状态”标题）。RSI 一行只列数值；量比 = 当日量 ÷ 前 5 个交易日均量；
     状态另起一行，超买红 `#DC2626`、超卖绿 `#16A34A`、其他灰。
  5. **法人净买卖超**（标题不带日期）：外资、投信、自营商、三大法人合计各一行，单位万股（股数 ÷ 10,000，两位小数）。
     外资自营商不单列；不为 0 时以一行小字注明“合计含外资自营商 N 万股”，合计口径不变。
     法人日期与日线不同时以下方警示标出。单位不是股数时改显示原始数值并在标题注明单位。
  6. **主要风险**：一句，取自已通过检查的报告 `intelligence.risk_alerts` 第一条（去掉编号），不新增投资结论；
     字级与上方数值相同（`xl`），与标题间留 `md` 间距、行距 6px。
  7. **新闻**：只有 `news_evidence_present` 为真且报告有 `intelligence.latest_news` 时才显示；没有新闻时整栏不显示。
- 均线排列与报告的观察不放在卡片上；`cards.json` 仍保留这些字段以便追溯。
- 卡上只额外显示会让当日数字不可靠的警示（橙色）：分析失败、日线非最新、行情日期与日线不一致、法人日期不同、法人状态非 ok。
  其余品质说明（分析窗缺日、财报金额 unverified / 已修正、法人未重新同步等）、来源与离线回放版本标注只写入
  `cards.json` / `manifest.json`，不放在卡片上。
- altText 保留“台股收盘分析【测试】｜资料日期｜档数”；多档日期不同时列出全部日期。
- 未知值显示“不可用”，不补零。简体文字以 OpenCC `s2twp` 转为繁体（台湾用语），并修正已知断词错误（如“空頭髮散”）。
- 视觉：bubble 尺寸 `giga`（最宽），收盘价 `3xl`、主要文字 `xl`、标签 `lg`；繁体中文、无装饰 emoji、主要文字可换行、不设 `maxLines`；不放本机路径或按钮。
  本地检查确认资料日期与每条警示都在卡片中，并检查 Flex 大小限制。

## 2. LINE 设定

与 line-push skill（美股晨报）相同的账号与接收对象，**只读**下列键：

- token：`LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TOKEN`
- 接收者：`LINE_TO` / `USER_ID` / `LINE_USER_ID`

顺序与 skill 相同：进程环境变量 → `$OPENCLAW_STATE_DIR/.env` → `~/.codex/.env` → `~/.openclaw/.env`
（`LINE_PUSH_ENV_FILES` 可改为逗号分隔的路径）。不 source 整份文件、不读其他服务凭证。
任何两个已定义的值不同（例如进程环境覆盖了文件中的接收者）即视为“设定来源冲突”，停止发送。
输出只显示来源路径、键名与短指纹（SHA-256 前 12 码），不显示 token 或完整接收者 ID。

## 3. 操作

```bash
# 1) 预览：message.json、preview.txt、preview.html、preview.png、cards.json、intraday.json、market.json、manifest.json
#    会向 FinMind 唯读抓取大盘 / 柜买指数日资料、向 Yahoo Finance 唯读抓取盘中每分钟价；
#    --no-market 不产生大盘卡，--no-intraday 不画盘中走势；两者都加则零 HTTP
python scripts/tw_line_push.py preview --db <analysis.db> --history-ids 5,6,7 --provenance offline_replay

# 2) 同样产生预览，并做唯读检查：设定一致性、GET /v2/bot/info、POST /v2/bot/message/validate/push（不发讯息）
python scripts/tw_line_push.py preview --db <analysis.db> --history-ids 5,6,7 --provenance offline_replay --validate

# 3) 发送前 dry-run（零 HTTP）
python scripts/tw_line_push.py send --preview <preview_dir>

# 4) 手动送出该预览的 message.json 一次
python scripts/tw_line_push.py send --preview <preview_dir> --confirm
```

输出目录：`$TW_LINE_PUSH_DIR`（默认 `~/.local/share/daily-stock-analysis/line_push`）下的
`tw_close_analysis/previews/<时间>_<payload hash>/` 与 `tw_close_analysis/send_ledger.json`。
`preview.png` 为依 Flex JSON 以 HTML 绘制的近似预览（需本机 Chrome），不是 LINE 官方渲染。

`send --confirm` 只在以下条件全部成立时发送：预览本地检查通过、`--validate` 通过、`message.json` 与预览时 hash 一致、
当前 LINE 设定无冲突且与验证时指纹相同。

## 4. 防重复与重试

- 去重键 = 报告类型 + history id/query id + 资料日期 + 接收者指纹 + payload hash；纪录与美股晨报分开。
- 第一次发送前产生 `X-Line-Retry-Key`（UUID）并写入 ledger；重试沿用相同 key、payload 与接收者。
- 每次执行最多 2 次 HTTP 尝试（首次 + 一次 2 秒退避重试），只对网络错误、DNS、timeout、5xx 重试；
  429 只有带 ≤5 秒 `Retry-After` 时才在上限内重试（官方未承诺 Retry-After，且月额度用尽也回 429）；
  400 / 401 / 403 等不重试。
- 200：LINE API 已接受，停止。409 且带 `x-line-accepted-request-id`、讯息为 “already accepted”：先前已受理，停止。
- 已接受的报告再执行会跳过；失败或 rate_limited 的纪录不会自动重送；retry key 超过 24 小时且结果不明则不再重送。
- HTTP 200 只代表 API 已接受，不代表手机已显示；实际收件需人工确认。

参考：[Retrying API requests](https://developers.line.biz/en/docs/messaging-api/retrying-api-request/)、
[Validate push message objects](https://developers.line.biz/en/reference/messaging-api/#validate-message-objects-of-push-message)。
Flex 限制：altText ≤ 1500 字、carousel ≤ 12 bubble、bubble JSON ≤ 30 KB、carousel JSON ≤ 50 KB。

## 5. 限制与回滚

- 本轮只有手动推送；排程、新闻、其他市场与条件提醒不在范围内。
- 盘中走势依赖 Yahoo 1 分钟资料，Yahoo 只保留约 30 天；较旧的资料日不会有盘中图。FinMind 分钟资料（`TaiwanStockKBar`）需付费等级，未使用。
- 近似预览不等于 LINE 实际渲染；以 validate endpoint 与手机实际显示为准。
- 回滚：删除 `scripts/tw_line_push.py`、`src/services/tw_market_data/line_flex.py`、
  `src/notification_sender/line_push_sender.py` 与对应测试即可；不影响分析与其他通知渠道。输出目录可手动删除。
