# 前瞻规则：使用说明

本目录用来**每天出信号、记录台账、结算统计**。规则本身（方向规则、门控、等级、评价方法、历史参考）见 [RULES.md](RULES.md)；本文只说明怎么用。

目前同时记录三套规则，都在同一个 T−10 决策上判断：

| 规则 | 说明 | 前瞻起点（北京时间） |
|---|---|---|
| **forward-rule-v1**（L0 / L1 / **L2**） | 主规则。L2 = 无模型方向规则 + T−120 V1 同向 + T−10 V1 不反向 + 盘口朝所选球队移动 | 2026-10-13 12:00 |
| **V1-四分之一**（影子） | L2 中只保留四分之一盘（.25 受让、.75 让球） | 2026-10-14 12:00 |
| **V1-多阶段**（影子） | 四分之一盘 + 升盘 + T−120/T−60/T−30 中至少 2 个 V1 同向，且没有任何截点反向 | 2026-10-14 12:00 |

## 文件一览

| 文件 | 作用 |
|---|---|
| `forward_rule.py` | 主程序，三个子命令：`scan` 出信号、`settle` 结算、`backtest` 历史回测 |
| `rule_spec.json` | v1 登记参数（已冻结，不要修改） |
| `shadow_spec.json` | 两个影子版本的登记参数（已冻结，不要修改） |
| `RULES.md` | 规则全文、评价方法、历史参考、局限 |
| `review_l2.py` | 复盘工具：逐场诊断（盘口类型、T−10 后走势、澳彩/Bet365 差异），并与历史同类比较 |
| `t10_correction.py` | 检验 T−10 的 V1 信号能否在下注前识别出会输的注 |
| `backtest-forward-rule-v1.json`、`backtest-shadows.json` | `backtest` 的输出 |
| `REVIEW-2026-10-09-to-10-12.md` 及几个 `*-retrospective.csv` | 10-09…10-12 的回顾复盘与逐场明细 |

## 安装

需要 Python 3.10 或更新版本：

```bash
pip install -r odds-trajectory/requirements.txt     # numpy、pandas
```

以下命令都在仓库根目录执行。程序依赖同仓库的 `research/` 和 `v1/code/`，请保持目录结构不变。

## 日常使用

### 1. 定时运行 `scan`

数据源是本地 Pankou 采集库（表结构与快照相同），程序只读打开它：

```bash
python odds-trajectory/forward/forward_rule.py scan --db PATH/TO/daqiu_odds.sqlite3 --ledger forward-ledger.jsonl
```

建议**每 5 分钟运行一次**，例如 Windows 计划任务：

```powershell
schtasks /create /sc minute /mo 5 /tn ForwardRuleScan /tr "python C:\path\WorldCup\odds-trajectory\forward\forward_rule.py scan --db C:\path\daqiu_odds.sqlite3 --ledger C:\path\forward-ledger.jsonl"
```

每次运行会：
- 对开球前 120、60、30 分钟这几个截点已到的比赛，计算并冻结该截点的 V1 结果；
- 对已到 T−10 的比赛，按 T−10 的 Royal 报价给出决策，并判断 v1 的 L0/L1/L2 和两个影子版本是否下注；
- 把结果**追加**写入台账（不改动旧记录），并在屏幕上列出本次新出现的 L2、V1-四分之一、V1-多阶段 比赛。

注意事项：
- **采集程序必须在 T−10 前已更新到最新报价**。`scan` 只能用运行当时库里已有的数据。
- 单次运行约 2–2.5 分钟，峰值内存约 5 GB。
- 同一场比赛只决策一次。同一场比赛有多个来源 ID 时，只记一次。
- 开球后才生成的决策标记为 `late`，不进入统计。

### 2. 查看信号与结算

```bash
# 汇总：v1 的 L0/L1/L2、一级赛事、两个影子版本（在同一批决策上附 L2、L0 对照）
python odds-trajectory/forward/forward_rule.py settle --db PATH --ledger forward-ledger.jsonl --output report.json

# 逐场列出（可选 L0、L1、L2、V1-四分之一、V1-多阶段），并另存为 Excel 可打开的 CSV
python odds-trajectory/forward/forward_rule.py settle --db PATH --ledger forward-ledger.jsonl --list L2 --list-csv l2.csv
python odds-trajectory/forward/forward_rule.py settle --db PATH --ledger forward-ledger.jsonl --list V1-四分之一
```

- 赢盘率口径：(全赢 + 赢半) / (全赢 + 赢半 + 输半 + 全输)，走盘剔除，与模拟投注页一致。
- 同时给出"相对基准的提升"（扣除盘口类型和水位本身带来的赢盘率）和 ROI（按冻结水位、每注 1 单位）。
- 结算使用冻结的 T−10 报价和已核验的常规 90 分钟比分（含补时，不含加时和点球）。没有比分的显示为"待结算"。
- 逐场表中的盘口和初盘都从所选球队的角度表示：负数为让球，正数为受让。
- 检查点：L2 和每个影子版本各自累计到 150 注、300 注时汇报一次，期间不改参数。

### 3. 复盘

```bash
python odds-trajectory/forward/review_l2.py --snapshot odds-trajectory/snapshots/2026-10-13 --ledger forward-ledger.jsonl --from 2026-10-14T12:00:00+08:00 --csv review.csv
python odds-trajectory/forward/t10_correction.py --ledger forward-ledger.jsonl --snapshot odds-trajectory/snapshots/2026-10-13
```

`review_l2.py` 目前只接受快照目录（`--snapshot`）作为数据源。

## 台账格式（JSONL，每行一条）

| `kind` | 写入时机 | 主要字段 |
|---|---|---|
| `v1_t120`、`v1_t60`、`v1_t30` | 到达该截点后第一次 `scan` | `matchId`、`kickoff`、`generatedAt`、`score`（V1 原分 D）、`effectiveN`、`sampleCount`、`line`（该截点盘口）、`featureHash`、`lastQuote`；无法计算时为 `unavailable` |
| `decision` | 到达 T−10 后第一次 `scan` | `team`/`teamName`（规则方向）、`frozenQuote`（T−10 报价）、`lineOpen`（初盘）、`v1T10`/`v1T120`/`v1T60`/`v1T30` 及各自的 `…Source`（`frozen` 或 `recomputed_at_decision`）、`gates`、`levels`（L0/L1/L2）、`shadows`（各影子版本的 `bet` 及判断依据）、`late`、`fixtureKey` |

台账只追加不改写。请保留整个文件，结算和复盘都从它读取。

## 回放测试

用快照模拟过去某段时间的扫描（只用于测试，不是前瞻证据）：

```bash
python odds-trajectory/forward/forward_rule.py scan --snapshot odds-trajectory/snapshots/2026-10-13 --ledger test.jsonl \
  --now 2026-10-12T17:00:00+08:00 --until 2026-10-13T02:40:00+08:00 --step 5
python odds-trajectory/forward/forward_rule.py settle --snapshot odds-trajectory/snapshots/2026-10-13 --ledger test.jsonl --retrospective --list L2
```

`--retrospective` 会把前瞻起点之前的决策也算进来，输出会标注 "RETROSPECTIVE TEST, not evidence"。

## 常见问题

- **某场比赛没有决策**：T−10 时有效赛前报价少于 3 条，或者同一分钟有冲突报价无法排序，或者该场已经用另一个来源 ID 决策过。
- **`recomputed_at_decision`**：程序没有在该截点和 T−10 之间运行过（比如刚启动），所以在 T−10 时按该截点补算。结果相同，但不是当时冻结的。
- **想修改规则**：不要改 `rule_spec.json`、`shadow_spec.json` 或判断逻辑。应当登记一个新版本，并设定新的前瞻起点，否则之前的累计就不能作为证据。
- **内存不足**：可以降低运行频率，或者先只在比赛集中的时段运行。
