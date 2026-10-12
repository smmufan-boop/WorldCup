# V1 轨迹比对、评分与 T−10 规则

整理日期：2026-10-13。此包按本地看板当前实际加载的 V1 方向参数整理，包含算法说明、纯 Python 核心与合成示例。与已发布的[轨迹数据快照](../README.md)配套使用。

## 阅读入口

- [ALGORITHM.md](ALGORITHM.md)：严格/近似匹配、点序相似分、换盘分段、历史方向聚合和展示分的完整公式。
- [T10_RULES.md](T10_RULES.md)：T−10 时间截点、执行宽限、目标资格、橙色样本门槛与冻结规则。
- [点序参考规范原件](reference/odds_point_similarity_spec.md)：原始工程定义及输入输出约定。
- [当前方向参数原件](code/parameters.json)、[T−10 规则清单](rules/t10_contract.json)、[一级赛事精确名单](rules/first-tier-competitions.json)。
- [源码溯源清单](code/SOURCE_MANIFEST.json)：源文件与分发文件的 SHA-256、提取与适配说明。
- [发布文件校验清单](RELEASE_MANIFEST.json)：本包各文件字节数、SHA-256与规则说明来源。

## 版本对应

“V1”指方向聚合算法，不能用它代替整条链路的所有版本号。

| 层 | 实际版本/配置 |
| --- | --- |
| 轨迹特征及严格/近似匹配 | same-company-path-v2-source-order |
| 点序相似度 | odds-point-progress-v1.0.0 |
| 换盘段扩展 | odds-point-progress-v1.0.0:handicap-nodes-v1 |
| 完整观察窗口适配 | 上述扩展加 :full-prematch-window-v2 |
| 观察窗口 | first-valid-prematch-to-equal-lead-cutoff-v1 |
| 历史方向聚合 | trajectory-direction-v1:e485690037a4 |
| 展示变换 | tanh-scale-5-v1 |
| Royal 自动模拟登记政策 | royal-t10-first-tier-one-unit-v1；2026-10-11追加橙色有效样本门槛 |

当前参数：澳彩 `power=2, shrinkage=50`；Royal `power=2, shrinkage=20`。配置记录的训练截止为 2026-09-28，生效时间为 2026-10-04 12:53:49（北京时间），状态为 `experimental_no_validated_gain`。时间留出验证尚未证明优于等权；方向原分、展示分和相似分均不是经过校准的胜率。

## 离线运行

需要 Python 3.10 或更新版本，无额外依赖。在仓库根目录执行：

```powershell
python odds-trajectory/v1/code/demo.py
```

示例只使用合成报价与合成结算结果，展示点序/分段相似度、V1 历史方向聚合及边界检查。`matching_core.py` 提供点序清洗、轨迹特征与严格/近似分类；`odds_point_similarity.py`、`odds_segment_similarity.py` 为参考评分核心；`direction_v1.py` 提供固定 V1 聚合。调用方向聚合时应传入所选公司的 `parameters.json` 配置，不能用缺省 Royal 参数计算澳彩。

此包提供离线计算函数与规则说明；赛程身份核验、历史候选资格、来源冲突检查、阶段调度和账本事务仍须由调用方按文档实现。历史候选筛选和 T−10 自动登记目标筛选是不同步骤。

发布前已验证14组方向聚合与原V1输出一致、20项非法参数与原实现同样拒绝、9项V2参数拒绝，以及严格/近似阈值、让球方反转、同分钟A→B→A和歧义换盘边界。离线示例中澳彩方向原分为51.67464114832536，Royal为53.93258426966292；两段相似度汇总为45.45454545454545。全部案例为合成数据。

## 与数据快照的衔接

先按[数据说明](../README.md#校验与还原)还原 SQLite，再从 `matches`、`odds_history`、`odds_minute_order` 与 `match_score_aliases` 取得元数据和报价证据。分别处理公司 1/3，验证身份和历史标签可用时间，按截点取得完整赛前点序，严格/近似分类后评分。

2026-10-13 的数据快照是后来保存的历史状态，不包含过去每一次模型输出、评分日记和状态发布时间。它可用于回顾重算，不能单独复现当时实际已发出的 T−10 信号，也不能证明当时可取得完整比分或行情。

本次发布包含算法与规则、纯合成示例及公开赛事名称名单。原运行服务、评分数据库、个人模拟账本和影子实验数据没有加入此算法包。
