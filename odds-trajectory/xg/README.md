# 本地模型预期进球 vs Royal 亚盘

- `predictions.csv`：本地模型的赛前预测，一行一场。
  - 必需列：`match_id`、`kickoff`（北京时间）、`home`、`away`、`home_xg`、`away_xg`、`predicted_at`；
  - 可选列：`local_home_xg` / `local_away_xg`（市场融合前）、`model_version`、`lineup_known` 等。
- `check_predictions.py`：数据检查、与 Royal T−10 记录匹配，并比较模型预期净胜球和市场 μ。

```bash
python odds-trajectory/xg/check_predictions.py            # 默认读 xg/predictions.csv
```

匹配顺序：
1. `match_id`；
2. 主客队 + 日期；
3. 同一开球时间且有一队同名（用于处理译名不同）。

## 2026-10-13 第一批（217 行，09-06 … 09-20）

数据检查：

- 匹配到有赛果的比赛：183 场。
  - 按 match_id 143 场，按主客队 + 日期 4 场，按开球时间 + 一队同名 36 场（已逐一核对，都是同一场比赛）；
  - 34 场未匹配：英文队名，或 Royal 没有这场。
- 时间：所有预测都早于开球 13 分钟以上，中位数约 6 小时。
- 重复：没有。
- 一致性：`total_xg`、`expected_goal_difference` 与主客预期进球一致，胜平负概率之和为 1。
- `match_id` 缺失 72 行；`neutral` 全部为空；共有 3 个 `model_version`。
- `expectation_basis` 全部是 "final_expectation_may_include_market"，即最终值可能已经含有市场信息。

初步结果（[`results-check.json`](results-check.json)）：

| | 与实际净胜球的相关 | 平均绝对误差 | 模型−市场 与 市场误差 的相关 | 按模型方向下注：全部 / 差距 ≥0.2 球 |
|---|---|---|---|---|
| Royal T−10 μ | +0.465 | 1.282 | — | — |
| 最终预期 | +0.437 | 1.315 | +0.027 | 49.1% / 46.3% |
| 本地模型（融合前） | +0.387 | 1.352 | +0.023 | 51.5% / 48.0% |

183 场的标准误约为相关 ±0.07、赢盘率 ±4 个百分点，所以还不能下结论。目前市场比模型更准。模型与市场的差距不能预测市场误差，差距越大的比赛反而赢盘率越低。
