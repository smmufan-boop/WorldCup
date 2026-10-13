# IE1 执行手册

本文是 [IE1 方案](README.md)每天怎么跑的操作说明：准备 → 赛前核对 → T−10 自动计算与登记 → 赛后结算 → 每周复盘 → 是否转实盘。方案原理、公式和回测见 README。

> 当前状态 `experimental_retrospective_only`：回测都是赛后回看。按本手册先做**模拟前瞻登记**，在达到第 7 节的门槛前不要用真钱。

## 0. 一次性准备

| 项目 | 要求 |
| --- | --- |
| Python | 3.10 及以上，只用标准库，无需安装依赖 |
| 网络 | 能访问 `www.daqiuxiaoqiu.com`（与 V1 采集器同一来源） |
| 赛程 | 二选一：本地 V1 采集库（如 `Pankou/live_dashboard/data/daqiu_odds/daqiu_odds.sqlite3`，读 `matches` 表），或自己维护的赛程 CSV |
| 工作目录 | 默认在当前目录建 `ie1-runtime/`，可用 `--workdir` 改到别处；该目录已加入 `.gitignore` |

以下命令都在仓库根目录执行，为简洁起见设：

```powershell
$IE1 = "python odds-trajectory/ie1/code/ie1.py"
```

先跑离线自检：

```powershell
python -m unittest discover -s odds-trajectory/ie1/tests
python odds-trajectory/ie1/code/demo.py
```

9 项测试全部通过、demo 能输出“推荐：合成主队 −0.5 @1.94（澳彩）｜等级 B”即可。

### 0.1 首次联网：核对报文字段（必须做）

本仓库所在环境访问不到该接口，报文字段是按欧指/大小球补采记录（`homeWin / draw / awayWin`、`MM-DD HH:MM` 时间、比分标记）写的默认值，**第一次联网前要先核对**。任选一场即将开赛、已有 Royal 盘口的比赛：

```powershell
& $IE1 probe --match-id 54580227 --kickoff 2026-10-14T19:30:00+08:00 --home 主队 --away 客队 --competition 英超
```

逐项核对输出：

1. 每个 `[公司/市场]` 的 `rows` 大于 0，原始报文已保存在 `ie1-runtime/evidence/`。
2. 示例行里亚盘的 `raw=[主水, 盘口文字, 客水]` 解析成正确的 `line`（如“受半球”→ 主队受让 0.5）；大小球 `2/2.5` → 2.25；欧指三个十进制赔率顺序是主/平/客。
3. 解析失败的行会写在 `rows dropped` 里，正常应接近 0。

字段名对不上时，只改 `code/parameters.json` 的 `payload` 段（`list`、`home`、`mid`、`away`、`time`、`score`、`handicapValue`），不要改代码。改完重新 probe，直到全部正确。字段名核对过之后，在 `parameters.json` 里把 `version` 升一位（如 `ie1-0.2.1`），这样台账里能区分。

## 1. 每日流程

赛日按 V1 口径划分：北京时间当天 12:00 到次日 12:00。

| 时间 | 动作 | 命令 |
| --- | --- | --- |
| 赛日开始前 | 确认赛程来源已更新（V1 采集库在运行，或 CSV 已写好） | — |
| 开球前任意时间，**须早于 T−10** | 有伤停、轮换、战意等确定信息时录入 M6 | `note` |
| 全天 | 后台持续扫描，到 T−10 的比赛自动计算并登记 | `run --loop` |
| 次日 | 结算前一赛日 | `settle` |
| 每周 | 复盘统计 | `stats` |

### 1.1 启动扫描

使用 V1 采集库：

```powershell
& $IE1 run --db G:\weixin\Pankou\live_dashboard\data\daqiu_odds\daqiu_odds.sqlite3 --loop
```

或使用赛程 CSV（列：`match_id,competition,home,away,kickoff_local`，格式见 [examples/fixtures_example.csv](examples/fixtures_example.csv)）：

```powershell
& $IE1 run --fixtures today.csv --loop
```

运行逻辑：

- 每 30 秒扫描一次（`fetch.scanSeconds`）。只读赛程库，不写入。
- 一场比赛在**截点 C = 开球 − 10 分钟之后、开球后 10 分钟之前**被处理，每场成功处理一次。
- 抓取 Royal 和澳彩各自的亚盘、大小球、欧指，共 6 个请求，间隔不少于 0.5 秒。原始报文先存档，再解析。
- 只使用源时间在截点之前的赛前报价：亚盘为 `ts ≤ C`，大小球/欧指要求整分钟落在 C 之前；带比分标记的滚球报价一律排除。所以晚几分钟运行也不会把截点后的价格带进来。
- 计算结果写入 `ie1-runtime/reports/<赛日>/<match_id>.txt`（截图式中文报告），同时记入台账 `ie1-runtime/ledger.sqlite3`。
- 终端每场打印一行，例如 `示例主队 vs 示例客队：示例主队 -0.5 @1.94（澳彩）｜已登记`。

**停机规则**：数据源返回 401/403/429 时程序立即停止，退出码为 2。先人工检查（被限流还是接口改了），确认后再重启，不要循环重试。

### 1.2 看到推荐后怎么做（模拟阶段）

1. 打开报告，确认第五节“推荐”有等级（A/B），且提示里没有需要人工推翻的内容。
2. 台账已经锁定：公司（澳彩）、方向、盘口、赔率、该报价的源时间，以及 EV。
3. 模拟阶段到此为止，不用下单。
4. 转实盘后：只在澳彩的**同盘口、赔率不低于锁定值**时按等级单位下单；盘口变了或赔率变低就放弃，并在复盘里记为“不可成交”。不要追更差的价格。

报告里标“仅观察”的大小球、欧指、Bet365，以及 C 级，都不下注。

报告结论出现“报价过旧：……作废”时，表示澳彩那条报价最后一次变动距截点超过 120 分钟，按硬门槛不登记。即使客户端上仍能看到同样的价格，也不要手动补下；可以在复盘记录里注明“作废候选当时是否可成交”，作为以后调整门槛的依据。Royal 末报价距截点超过 360 分钟时整场阻断，原因同理。

### 1.3 录入信息面（M6）

```powershell
& $IE1 note --match-id 54580227 --supremacy -0.10 --notes "主队主力前锋赛前确认伤缺"
```

- `--supremacy` 以“净胜球”为单位，正数对主队有利；`--total` 以总进球为单位。两者都会被截到 ±0.15。
- 录入时间由程序自动写入当前时间，不能补填。晚于截点的录入会被拒绝，报告提示里会写明。
- 只录**已经确认**的事实（官方首发、确认伤停）。传闻和“战意”之类的主观判断不要录。
- M6 方向与候选相反时会否决该候选。这是 M 信号里唯一会影响结果的一项，务必克制。

### 1.4 赛后结算

```powershell
& $IE1 settle --db G:\weixin\Pankou\live_dashboard\data\daqiu_odds\daqiu_odds.sqlite3
# 或手工比分 CSV：match_id,score_home,score_away（必须是常规 90 分钟比分）
& $IE1 settle --results results.csv
```

- 按锁定的盘口和赔率结算；四分之一盘拆成两半各 0.5 注（半赢/半输）。
- 采集库中只采用核验过的 90 分钟比分（`score_note` 中的“90分钟[主-客]”，或 Played + FT + 90 分钟且无加时/点球）。其余比赛保持待结算，下次再试。
- 有加时或点球的杯赛，比分 CSV 里也只能填 90 分钟比分。

## 2. 每周复盘

```powershell
& $IE1 stats
```

输出各等级的注数、平注 ROI 及其 95% 下限、按单位盈亏，以及是否达到实盘门槛。另外人工核对以下几项，可直接查台账：

```sql
-- sqlite3 ie1-runtime/ledger.sqlite3
SELECT status, COUNT(*) FROM runs GROUP BY status;                    -- late 不应常见
SELECT tier, COUNT(*), AVG(ev), SUM(units*profit) FROM picks GROUP BY tier;
SELECT match_id, (generated_at-cutoff)/60.0 AS delay_min FROM runs ORDER BY delay_min DESC LIMIT 20;
```

关注：

| 检查 | 正常 | 异常时处理 |
| --- | --- | --- |
| 每赛日登记数 | 一级赛事约 0–3 注（回测留出期 24 注/6 周） | 突然变多：先查澳彩报价是否异常、解析是否出错 |
| `late` 占比 | 接近 0 | 扫描机或网络延迟太大，检查运行环境 |
| 报告提示中的“末报价距截点 X 分钟” | 少见 | 经常出现说明数据源更新慢，降低信任 |
| “报价过旧……作废”的场次 | 回看中约占 EV≥2% 候选的六到七成 | 记录作废候选截点时能否按原价成交，攒够样本后再评估 120 分钟门槛 |
| 实际可成交率（实盘后） | 绝大多数能按锁定价下单 | 低于 70% 说明优势只是看得到、下不到，应停止 |

## 3. 不能做的事

- 不改截点、门槛或系数来“救”某一场。参数只能整体升版本，并在新版本下重新累计前瞻记录。
- 不在开球后补登记，不用当前行情改写已锁定的条款。
- 不把 C 级、仅观察的市场或报告中的 M 信号当成下注理由。
- 不把回测 ROI 当成预期收益。留出期样本只有几十注。

## 4. 手工模式（没有程序抓取时）

从客户端抄下 Royal（必填：亚盘至少 3 条，最好有大小球）和澳彩的赔率，按 [examples/offline_example.json](examples/offline_example.json) 的格式写成 JSON：

```powershell
& $IE1 offline --input my_match.json
```

输出与自动模式相同的报告，但**不写入台账**。手工抄录无法证明时间点，不能计入前瞻统计。

## 4.1 用快照回看任意一场

```powershell
& $IE1 replay --match-id 54569678 --cache fixtures.pkl
```

从仓库里的两份快照重建该场 T−10 盘口簿，输出完整报告和按锁定条款的结算。结果是赛后回看，不计入前瞻统计。完整示例见 [examples/report_54569678.md](examples/report_54569678.md)。

## 5. 每场计算的内容（与报告各节对应）

1. **市场公允价**：Royal 截点时的亚盘和大小球去水后，反解出 (总进球 T, 净胜球 S)，再用 Dixon–Coles（ρ = −0.05）生成比分矩阵。
2. **庄家意图 M1–M7**：欧亚背离、全程盘路、大小线、跨公司分歧、临场一致、信息面、V1 同轨。当前只有 M6 的系数不为 0，其余仅展示。
3. **模型**：公允价加上已启用的修正（上限 ±0.25 球），得出胜平负概率和比分参考。
4. **价值**：用模型给每家公司的截点报价算 EV。
5. **结论**：先过阻断（亚盘报价不足 3 条、Royal 报价超过 360 分钟未变、青年/女足/友谊赛、不在一级名单），作废超过 120 分钟未变的澳彩报价，再按 EV 和提示定级：A（EV≥4%，1 单位）、B（EV≥2%，0.5 单位）；有任何提示降一档；C 级不下注。

## 6. 参数文件（code/parameters.json）

| 段 | 内容 | 可以改吗 |
| --- | --- | --- |
| `version` / `status` | 版本号与状态 | 任何改动都要升版本号 |
| `cutoffSeconds`、`lateWindowSeconds` | 600 秒（T−10）、临场窗口 3600 秒 | 不建议改 |
| `referenceCompany`、`rho`、`priorTotal` | Royal、−0.05、2.6 | 只有重新回测后才能改 |
| `signals.*.beta*` | 全部为 0（诊断） | 只有 `backtest.py` 在开发期显著、并在新的前瞻期确认后才能打开 |
| `gates` | EV 门槛、一级名单、阻断/提示正则、单位、`stakeMarkets`、`stakeCompanies` | 同上 |
| `gates.maxStakeQuoteAgeMinutes` / `maxReferenceAgeMinutes` | 被定价报价 120 分钟、Royal 参考报价 360 分钟的硬门槛 | 只能在前瞻成交记录支持时升版本调整 |
| `fetch` | 请求间隔、超时、扫描周期、执行宽限 | 可按网络情况调整，但请求间隔不要低于 0.5 秒 |
| `payload` | 报文字段名 | 按第 0.1 节核对后修改 |
| `goLive` | 实盘门槛 | 只能调严，不能调松 |

## 7. 转实盘门槛

以下条件**同时满足**才考虑用真钱，且一开始按每单位不超过总资金 0.5% 执行：

1. 同一参数版本下已结算的前瞻登记 ≥ 200 注（`goLive.minSettled`）。
2. 平注 ROI 的 95% 下限 > 0（`stats` 最后一行显示“已达到实盘门槛”）。
3. 抽查“锁定价能否成交”的比例 ≥ 70%。
4. 前瞻期每月 ROI 没有一个月低于 −15%；如果有，先查原因。

转实盘后如果连续 100 注的平注 ROI < 0，停回模拟，重新评估。

## 8. 数据更新后重跑回测

欧指/大小球补采有新快照后，修改 `code/dataset.py` 里的 `ET` 路径（以及亚盘快照路径 `ASIA`），然后：

```powershell
python odds-trajectory/ie1/code/backtest.py --cache fixtures.pkl --out odds-trajectory/ie1/BACKTEST_REPORT.json --workers 4
```

开发期/留出期的分割点 `SPLIT`（2026-09-01）不要为了结果好看而移动。新数据应作为新的留出期单独报告。

## 9. 文件一览

| 文件 | 作用 |
| --- | --- |
| `code/ie1.py` | 命令行入口：probe / run / note / settle / stats / offline / replay |
| `code/source.py` | 限速抓取、401/403/429 停机、原始报文存档、赛程读取 |
| `code/parse.py` | 报文 → 统一行格式；中文盘口、进球线、无年份时间的解析 |
| `code/quotes.py` | 接口和公司 ID、T−10 路径构建（亚盘用 V1 点序；大小球/欧指用严格分钟和同分钟冲突规则） |
| `code/pricing.py` | 去水、Dixon–Coles、亚盘/大小球结算概率、(T, S) 拟合 |
| `code/signals.py` | M1–M7 |
| `code/engine.py` | 公允价 → 修正 → EV → 门槛 → 报告 |
| `code/ledger.py` | SQLite 台账：每次运行的冻结输入、首个合格推荐的锁定、结算 |
| `code/settle.py` | 锁定条款的结算 |
| `code/dataset.py`、`code/backtest.py` | 快照连接与回测 |
| `code/demo.py`、`examples/`、`tests/` | 合成示例、输入样例、单元测试 |
