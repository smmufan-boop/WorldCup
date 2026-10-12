# 赔率点位走势相似度：AI 实现规范 v1.0

本规范用于比较两场比赛的赛前亚洲让球盘报价变化。目标是：忽略真实时间间隔，将选定走势的首尾对应起来，比较相同点位进度上的涨跌方向，并单独衡量调整幅度。附带文件 `odds_point_similarity.py` 是 Python 3 标准库参考实现；`odds_point_similarity_example.json` 是可运行的合成输入。

## 可直接交给 AI 的任务指令

请严格按照本规范实现赔率走势相似度计算。采用“点序进度＋原始断点并集＋分段精确积分”的算法，主指标是方向同步分，变化幅度作为独立指标。不得使用实际经过时间作权重，不得用 DTW 移动峰谷位置，不得分别缩放每场的水位，不得把相关系数解释为相似百分比。必须保留原始转折，处理恒定曲线、点数不足、缺失字段和换盘。输出机器可读 JSON，缺乏方向证据时返回 null，不得伪造满分。所有公式、默认参数、数据清洗顺序和状态处理以下文为准。参考代码可以直接调用或移植。

## 1. 比较对象与适用范围

- 比较两场比赛 A、B，记录数量可以不同。
- 每条记录包含主队盘口、主队水位、客队水位。
- 采用港式水位：0.90 就是 0.90。其他赔率格式必须在调用前明确转换，不能混用。
- 当前版本固定使用主队视角：负的 `home_handicap` 表示主队让球。两场都比较主对主、客对客。
- 只比较同一公司、同一赛前亚盘市场、相同固定盘口的记录窗口。
- 两个窗口起止阶段由调用方预先选定；`window_id` 用于声明相同的选取规则，不要求两场窗口持续相同小时数。
- 比较的是点序进度同步。进度 50% 表示走到各自报价序列中间，不表示距离开赛相同分钟。
- v1.0 遇到任何升降盘，或者两场固定盘口不同，返回 `not_comparable`。不能把换盘引起的水位跳变当成同一盘口的普通涨跌。
- 如果业务改为“让球方对让球方”，需另行固定两场的角色映射，并保持整条序列角色不变；不得在本版本中自动交换主客。

## 2. 输入

```json
{
  "match_a": {
    "id": "match_A",
    "bookmaker": "example_book",
    "market": "asian_handicap",
    "phase": "prematch",
    "odds_format": "hong_kong",
    "perspective": "home",
    "window_id": "selected_prematch_window_v1",
    "records": [
      {"home_handicap": -1.0, "home_water": 0.90, "away_water": 0.90},
      {"home_handicap": -1.0, "home_water": 0.80, "away_water": 1.00},
      {"home_handicap": -1.0, "home_water": 0.90, "away_water": 0.90},
      {"home_handicap": -1.0, "home_water": 0.80, "away_water": 1.00},
      {"home_handicap": -1.0, "home_water": 0.90, "away_water": 0.90}
    ]
  },
  "match_b": {
    "id": "match_B",
    "bookmaker": "example_book",
    "market": "asian_handicap",
    "phase": "prematch",
    "odds_format": "hong_kong",
    "perspective": "home",
    "window_id": "selected_prematch_window_v1",
    "records": [
      {"home_handicap": -1.0, "home_water": 0.94, "away_water": 0.94},
      {"home_handicap": -1.0, "home_water": 0.89, "away_water": 0.99},
      {"home_handicap": -1.0, "home_water": 0.84, "away_water": 1.04},
      {"home_handicap": -1.0, "home_water": 0.89, "away_water": 0.99},
      {"home_handicap": -1.0, "home_water": 0.94, "away_water": 0.94},
      {"home_handicap": -1.0, "home_water": 0.89, "away_water": 0.99},
      {"home_handicap": -1.0, "home_water": 0.84, "away_water": 1.04},
      {"home_handicap": -1.0, "home_water": 0.89, "away_water": 0.99},
      {"home_handicap": -1.0, "home_water": 0.94, "away_water": 0.94}
    ]
  },
  "config": {
    "slope_epsilon": 1e-9,
    "channel_weights": {"home": 0.5, "away": 0.5}
  }
}
```

输入要求：

1. `records` 必须已按报价先后排列。可以保留 `timestamp`，参考函数不利用时间排序，也不使用时间差。数据接入层可按时间和记录序号稳定排序，再调用算法。
2. 元信息全部必填、非空。除比赛 `id` 外，上述比较元信息必须相同；固定字符串枚举必须符合本规范。
3. 原始每场至少有两条记录，才能描述一个观察过程。
4. 三个报价字段均为有限 JSON 数字，不能为布尔值、null、NaN、Infinity 或带文字的字符串。两个水位必须大于零；盘口必须是 0.25 的整数倍。
5. 缺失记录不得用零填补，不得推断不存在的报价。输入不合格时返回结构化错误。
6. 截图识别应先校对盘口、主客水位及记录顺序。不得按图片线条颜色猜测未确认的主客身份。

## 3. 清洗和共同去重

按以下顺序执行：

1. 校验字段、有限数值、元信息、原始记录数量和水位正数约束。
2. 数值统一保留 6 位小数，消除表示误差；然后检查水位仍大于零、盘口为0.25倍数。该精度是本版本输入精度约定，不是报价降噪模型。
3. 对完整报价状态 `(home_handicap, home_water, away_water)` 删除相邻完全重复项。
4. 保留非相邻的重复状态，例如 `0.90 → 0.80 → 0.90` 的最后一笔必须保留。
5. 主客共同去重，禁止分别去重，以保持同一条记录的主客对应关系。
6. 输出去重前、去重后点数及两场去重后点数比 `cleaned_point_count_ratio=max(n_A,n_B)/min(n_A,n_B)`。

原始有多笔报价、去重后只剩一笔，表示此窗口内观察到恒定状态，可以构造常函数。原始只有一笔报价属于信息不足，不能用相同逻辑视为已观察到全程不变。

## 4. 建立点位进度

对去重后有 n 个报价状态的序列，定义：

```text
u_i = i / (n - 1),  i = 0, ..., n - 1
```

起点为 0，终点为 1。主客水位共用这一组 u。

在相邻原始点之间，按直线建立连续函数：

```text
y(u) = y_i + (y_(i+1) - y_i) * (u - u_i) / (u_(i+1) - u_i)
```

这里的横轴是点位进度，因此采用线性连接，与截图中的折线含义一致。若去重后 n=1，则令整个 [0,1] 上 y(u)=y_0。

禁止用真实时间差设置 u；禁止将 u 改成累计水位变化距离或累计几何弧长。这些做法衡量的是其他对应关系。

## 5. 精确对齐：使用两条曲线断点的并集

构建：

```text
U = sort(unique({A 的全部 u} ∪ {B 的全部 u} ∪ {0, 1}))
```

推荐使用有理数保存 u（Python 使用 `fractions.Fraction`），避免 1/3、2/6 等同一进度被浮点误差拆成极短区间。

对每个相邻区间 `[U_j,U_(j+1)]`，A、B 在其中都各自为一段直线，因此方向和误差可以精确计算。

这个方法保留双方全部原始拐点，不需要先抽成 100 或 200 个点。若前端要画图，可以额外生成 101 个显示采样点；显示采样不得参与正式评分。

## 6. 每个水位通道的方向同步分

主、客通道分别计算。以下省略通道下标。

每个并集区间的长度为 `h = U_(j+1) - U_j`。令对应曲线斜率为：

```text
m_A = (A(U_(j+1)) - A(U_j)) / h
m_B = (B(U_(j+1)) - B(U_j)) / h
```

斜率单位为“水位 / 单位点位进度”。按统一的 `slope_epsilon` 分类：

```text
sign(m) = +1，若 m > epsilon
          -1，若 m < -epsilon
           0，其他情况
```

默认 epsilon=1e-9，仅用于浮点数值容差。它不是“低于某个水位变化就忽略”的经验阈值。需要实质降噪时应单独验证并修改配置，保持所有候选比赛采用相同参数。

累计五种进度长度：

```text
same = Σ h * I(sign_A = sign_B 且 sign_A ≠ 0)
opposite = Σ h * I(sign_A * sign_B = -1)
one_flat = Σ h * I(恰好一条曲线 sign = 0)
both_flat = Σ h * I(sign_A = sign_B = 0)
active = same + opposite + one_flat = 1 - both_flat
```

其中 `I(条件)` 在条件成立时为1，否则为0。

方向同步分：

```text
direction_score = 100 * same / active，若 active > 0
direction_score = null，若 active = 0
```

解释：在至少有一场发生变化的点位进度范围中，有多少比例是两场同时升或同时降。

- 双方都不变：单独计入 `both_flat`，不增加同步分，也不计入其分母。
- 一方不变、另一方变化：是不一致，必须计入分母。
- 一方上涨、另一方下跌：是不一致。
- 不是对每个并集小区间简单计数，而是按区间进度长度加权。否则断点密集处会得到额外权重。
- `active_coverage` 与分数同时返回。例如同步分100但活跃进度只有0.02，只说明很小一部分观察进度存在同向变化，不能省略这个信息。

## 7. 变化幅度和水位高低

定义两场相对各自起点的变化：

```text
z_A(u) = A(u) - A(0)
z_B(u) = B(u) - B(0)
```

### 7.1 相对变化 RMSE

```text
relative_rmse = sqrt( integral_0^1 (z_A(u) - z_B(u))² du )
```

越小表示变化幅度越接近，单位仍是原始港式水位。0 表示相对起点的变化曲线完全一致。

精确积分公式：在一个长度为 h 的并集区间内，差曲线两端值分别为 d0、d1：

```text
区间平方误差积分 = h * (d0² + d0*d1 + d1²) / 3
```

累加所有区间后开平方。区间总长度为1，无需再除以原始点数、并集点数或匹配长度。

### 7.2 原始水位 RMSE

把上述 d 改为 `A(u)-B(u)`，计算 `level_rmse`。它反映绝对报价高低的差异，不减首点。

例如两条线全程相差0.04，但升降过程和变化幅度完全一样，则方向同步分100、relative_rmse=0、level_rmse=0.04。这是预期结果。

### 7.3 其他幅度指标

```text
start_gap = abs(A(0) - B(0))
end_gap = abs(A(1) - B(1))
net_change_A = A(1) - A(0)
net_change_B = B(1) - B(0)
net_change_gap = abs(net_change_A - net_change_B)
range_gap = abs((max(A)-min(A)) - (max(B)-min(B)))
```

线性折线的最大值、最小值可直接从原始水位点取值。

禁止分别进行 min-max 归一化或 Z 标准化。它们会让小幅波动和大幅波动失去量级区别。

## 8. 相关系数仅作辅助诊断

可计算在 u∈[0,1] 上按进度均匀加权的连续 Pearson 相关系数。不能直接对不等点数数组求相关，也不能对断点并集数组无权求相关。

```text
mean_A = ∫ A(u) du
mean_B = ∫ B(u) du
var_A = ∫ (A(u)-mean_A)² du
var_B = ∫ (B(u)-mean_B)² du
cov_AB = ∫ (A(u)-mean_A)*(B(u)-mean_B) du
r = cov_AB / sqrt(var_A*var_B)
```

每个区间的端点为 a0,a1 和 b0,b1：

```text
∫ A du = h*(a0+a1)/2
∫ A² du = h*(a0²+a0*a1+a1²)/3
∫ A*B du = h*(2*a0*b0 + a0*b1 + a1*b0 + 2*a1*b1)/6
```

任一曲线恒定时 r=null。为减少浮点消减误差，可先减去固定基准或均值再求中心矩。数值误差导致略超范围时，将 r 限制到[-1,1]。

相关系数不参与方向分。r=0.9 不表示90%的区间同向，r=1也不代表报价幅度相同。

## 9. 主客通道汇总

默认 `channel_weights={home:0.5,away:0.5}`。也可固定只比较主水：`{home:1,away:0}`。参数必须非负，至少一个正数，并统一归一化到权重之和为1。

### 9.1 方向主分

只在权重大于0、且该通道方向分不为null的通道中重新归一化权重，计算加权平均。

```text
valid = 有正权重且 active > 0 的通道
direction_score = Σ(w_c * direction_score_c) / Σ(w_c)，c∈valid
```

valid为空时，整体方向分为null。

主客往往联动，此处是两个报价维度的平均，不可宣称拥有两份独立预测证据。每个通道的活跃覆盖率都应保留。

### 9.2 幅度汇总

对有正权重的所有通道，包括恒定通道，计算：

```text
relative_rmse = sqrt(Σ w_c * relative_rmse_c²)
level_rmse = sqrt(Σ w_c * level_rmse_c²)
net_change_gap = Σ w_c * net_change_gap_c
range_gap = Σ w_c * range_gap_c
start_gap = Σ w_c * start_gap_c
end_gap = Σ w_c * end_gap_c
active_coverage = Σ w_c * active_coverage_c
```

默认不将方向、幅度、相关性揉成一个经验综合分。`direction_score` 就是本算法的0～100方向同步分，幅度保留原始单位。

## 10. 状态与排名资格

| 状态 | 条件 | 分数/排名 |
|---|---|---|
| invalid_input | 缺字段、非有限数值、非法格式或配置 | 不评分；rankable=false |
| insufficient_data | 任一场原始记录少于2条 | 不评分；rankable=false |
| not_comparable | 元信息或固定盘口不一致，或任一场换盘 | 不评分；rankable=false |
| numerical_error | 极端数值导致计算溢出等数值失败 | 不评分；rankable=false |
| no_directional_evidence | 所有正权重通道都没有活跃变化 | 方向分null；幅度指标可返回；rankable=false |
| insufficient_points | 有方向证据，但任一动态序列去重后只有2个点 | 可返回诊断分；rankable=false |
| low_information | 有方向证据，任一动态序列去重后只有3或4个点 | 可返回诊断分；rankable=false |
| ok | 通过校验、存在方向证据，所有动态序列去重后至少5个点 | rankable=true |

“动态序列”指完整报价状态发生变化的序列；去重后一个点的常值序列不套用动态点数门槛。原始至少两笔的常值曲线与有足够点数的变化曲线比较，可以得到方向分0，而不是被忽略。

3、4、5点门槛是本版本防止过少转折就进入正式排序的工程默认，不是统计显著性界限。单一通道变化少、采样缺失、两场记录点数相差悬殊等信息仍需通过诊断审查；rankable=true不等于可靠预测。

## 11. 候选比赛排序

给定目标比赛，对每个历史候选分别运行相同配置：

1. 将 `rankable=false` 的结果放入单独诊断列表，不进入默认推荐排序。
2. 默认按以下字段依次排序：

```text
direction_score 降序
relative_rmse 升序
net_change_gap 升序
候选比赛 id 字符串升序（稳定打破完全并列）
```

这是“先看方向同步，再看幅度”的字典序规则；极小方向分差异也优先于幅度差异。如果业务希望允许方向分在某个范围内并列，必须额外定义并公开分箱或容差规则，不得悄悄改排序。

不设置“80分以上一定相似”等通用阈值。需要二分类时，应根据同数据源的人工标注样本确定方向分下限、允许的幅度误差和活跃覆盖率门槛。若后续用于预测赛果，必须另外做按时间划分的样本外验证，不能把本分数当胜率。

## 12. 完整伪代码

```text
function compare_odds(A, B, config):
    validate metadata, values, config, original counts
    reject different markets / perspectives / windows / fixed handicaps
    reject any handicap change inside either record window

    A = round_quotes_to_6_decimals_and_jointly_deduplicate(A)
    B = round_quotes_to_6_decimals_and_jointly_deduplicate(B)

    build rational progress knots i/(n-1)
    if deduplicated n=1: build a constant function on [0,1]
    U = sorted union of both rational knot sets and [0,1]

    for channel in [home, away]:
        initialize direction lengths and moment/error integrals
        for each interval [u0,u1] in U:
            h = u1-u0
            evaluate A and B at both ends by their original linear segments
            classify slopes using common slope_epsilon
            accumulate same/opposite/one_flat/both_flat weighted by h
            integrate squared baseline-removed error exactly
            integrate squared raw-level error exactly
            integrate means, squares and cross-product
        direction = 100*same/active if active>0 else null
        relative_rmse = sqrt(total relative squared error integral)
        level_rmse = sqrt(total raw squared error integral)
        calculate start/end/net/range gaps and optional correlation

    aggregate valid direction channels with renormalized weights
    aggregate amplitude with all positive channel weights
    determine status and rankable flag
    return JSON with version, config, counts, per-channel and combined metrics
```

若并集有 L 个区间，直接有序合并和双指针计算可达 O(n+m) 时间、O(n+m) 空间；参考实现采用有理数并集排序和查找时可为 O((n+m)log(n+m))。无须运行动态规划。

## 13. 必须通过的验收案例

| 案例 | 预期 |
|---|---|
| 完全相同且有足够变化点的曲线 | 方向分100；相对/原始RMSE均0 |
| 每个原始线段均等细分，点数增加但折线不变 | 评分不变 |
| 相同变化曲线整体上移0.04 | 方向分100；relative_rmse=0；level_rmse=0.04 |
| 方向相同、变化幅度翻倍 | 方向分100；relative_rmse>0 |
| 每个非平区间方向完全相反 | 方向分0 |
| 两场都是恒定报价，原始各有多笔 | no_directional_evidence；方向分null |
| 一场恒定、另一场变化 | 对应通道方向分0；双方不变区间不抬分 |
| 仅一个通道双方恒定、另一通道有变化 | 恒定通道方向分null，不稀释另一个通道的分数 |
| 插入或删除相邻完全重复的报价 | 评分不变 |
| 非相邻回到相同水位 | 保留转折，不当成重复删除 |
| 盘口-1与盘口-0.75比较 | not_comparable |
| 一场由-1升至-1.25 | not_comparable |
| 原始只有一条记录 | insufficient_data |
| 任一曲线恒定 | 该通道相关系数null |

注意：均等细分每个线段可以保持点序比例；只在局部额外插入不同报价点会改变其他转折的进度位置，因此不保证分数不变。这是点位进度定义的限制。

## 14. 调用方法

命令行：

```bash
python odds_point_similarity.py odds_point_similarity_example.json
```

写入结果文件：

```bash
python odds_point_similarity.py odds_point_similarity_example.json --output result.json
```

Python：

```python
import json
from odds_point_similarity import compare_odds

with open("odds_point_similarity_example.json", encoding="utf-8") as f:
    payload = json.load(f)

result = compare_odds(payload["match_a"], payload["match_b"], payload.get("config"))
print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
```

附带样例是人工构造的5点对9点。B在A各段中点增加记录，并将两个水位整体上移0.04。预期方向分100，相对变化RMSE约0，原始水位RMSE约0.04，盘口一致；极小浮点残差应按数值容差理解。

参考实现的实际运行输出，以下只摘录核心字段：

```json
{
  "algorithm_version": "odds-point-progress-v1.0.0",
  "status": "ok",
  "rankable": true,
  "direction_score": 100.0,
  "relative_rmse": 6.409875621278547e-17,
  "level_rmse": 0.039999999999999966,
  "net_change_gap": 0.0,
  "cleaned_point_count_ratio": 1.8
}
```

完整输出还包含 `config`、`counts`、`alignment`、`direction_channel_weights`、`channels.home`、`channels.away`、`active_coverage` 和其他幅度诊断。`channels` 内的 `progress_fractions` 给出同向、反向、单方平稳及双方平稳的进度比例，四者之和为1。无效输入包含 `reason`，不应假设失败结果仍有完整通道数据。

交付前已通过13项独立检查：样例5/9点等价、共同去重、完全反向、双方恒定、单方恒定、恒定通道权重与L2汇总、解析积分对照、A/B对称性、换盘拒绝、单点拒绝、缺失值拒绝、权重校验和实际命令行运行。其中解析积分案例的预期方向分为50，RMSE为 `sqrt(1/600)`，与实现一致。

## 15. 明确的限制与禁止事项

- 点数不同可以计算，但本算法不是对任意记录密度变化都不变。两场应尽量使用相同数据来源和采集规则，并披露原始/清洗点数。
- 不使用真实时间，所以不能推断实际临场同一分钟是否同步。
- 小幅反复与大幅反复在方向上都算反复；幅度区别由RMSE体现。
- 不进行平滑，不默认消除尖峰，也不将少数反转视为噪声删掉。
- 不能仅凭首尾相同认定全程相似。
- 不能把主、客走势分别独立拉伸后再拼接结果。
- 不能用实际不存在的“等效水位”把不同盘口硬拼成同一序列。
- 不能把方向分、相关系数或人工阈值解释为赢盘概率。

## 方法依据

本文件的方向积分、幅度指标与数据处理约定是一套为当前需求定义的工程算法，不宣称为已有行业统一评分标准或已验证预测模型。

- 线性插值与一维插值基础：[SciPy 官方说明](https://docs.scipy.org/doc/scipy/tutorial/interpolate/1D.html)。
- Pearson相关系数含义及常量输入边界：[SciPy 官方说明](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.pearsonr.html)。
- DTW允许匹配位置发生局部改变；本需求采用固定点位进度，因此未将其用于主算法：[tslearn 官方说明](https://tslearn.readthedocs.io/en/stable/user_guide/dtw.html)。
