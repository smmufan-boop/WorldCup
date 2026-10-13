# 欧指与大小球数据：2026-10-13阶段快照

本目录发布本轮 Royal/澳彩欧指与大小球补采的**阶段快照**。冻结时间为 **2026-10-13 16:36:45（北京时间）**，导出于16:43:54结束；后台继续采集，后续新增数据不自动进入此快照。

这一步尚未采集完成。固定清单为 **219,519个API比赛ID、212,764个fixture group**，每ID四项请求，共878,076项。所有进度、覆盖和报价数均从同一个SQLite只读事务计算，详见 [manifest.json](snapshots/2026-10-13-partial/manifest.json)。

- [数据字段与质量口径](DATA_DICTIONARY.md)
- [完整快照文件](snapshots/2026-10-13-partial/)
- [校验／SQLite还原工具](tools/snapshot.py)

## 冻结时的采集情况

| 状态 | 任务数 | 含义 |
| --- | ---: | --- |
| ok | 56,159 | 取得目标公司的明细，未保证每条报价有效 |
| missing_company | 24,067 | 接口中没有目标公司条目 |
| empty_detail | 2 | 目标公司明细为空 |
| failed | 0 | 请求／结构终态失败 |
| pending | 797,844 | 尚待采集 |
| running | 4 | 截点时正在执行 |
| retry | 0 | 等待重试 |

已处理 **80,228／878,076项，9.1368%**；尚未完成 **797,848项**。进度是请求处理比例，不是市场完整率或模型可用率。缺公司与空明细属于接口确认缺失，和请求失败分开保存；尚未采集的部分不能当成没有市场。

## 公司／市场覆盖

每个公司市场的计划请求均为219,519项，下面为冻结时取得数据的部分。

| 公司 | 市场／源ID | 成功请求 | 原明细报价行 | 内容有效行 | 有常规T−10报价的API ID | 有严格T−10报价的API ID |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Royal | 欧指 euro／545 | 19,934 | 2,512,394 | 2,391,373 | 19,931 | 19,931 |
| Royal | 大小球 size／3 | 19,966 | 3,875,177 | 3,737,598 | 19,963 | 19,963 |
| 澳彩 | 欧指 euro／80 | 8,116 | 94,997 | 92,012 | 8,116 | 8,116 |
| 澳彩 | 大小球 size／1 | 8,143 | 317,494 | 311,159 | 8,142 | 8,142 |

同场多个来源ID分别采集，表中API ID计数不能直接称为独立比赛数。欧指三项为主／平／客**十进制赔率**；大小球为大／小**香港净水位**和总进球线。本轮仅采皇家、澳彩；其欧指ID与亚盘／大小球ID不同。

“有T−10报价”只表示该任务至少有一条对应标记报价，不保证足够点数、完整无冲突轨迹、截点可交易，或当时真实可得。不能把这些数量直接作为已通过V1轨迹评分的样本。

## 五张表与保存规模

| 表 | 行数 | 用途 |
| --- | ---: | --- |
| matches | 219,519 | 固定采集清单、赛程身份和来源标签 |
| tasks | 878,076 | 四个公司市场请求的成功、缺失、失败和未完成范围 |
| attempts | 80,253 | 各次尝试、实际UTC观察时间、HTTP与body证据索引 |
| quotes | 6,800,062 | 全部已落库目标detail条目，保留滚球、无效、冲突和重复 |
| meta | 6 | 冻结时运行元数据 |

共 **83个JSONL.gz分块**（matches 3、tasks 9、attempts 1、quotes 69、meta 1），每块最多100,000行；压缩数据合计 **137,245,989字节，约130.89 MiB**，最大分块11,140,051字节。无需Git LFS。

没有把实时主库、WAL／SHM直接上传。JSONL保留SQLite整数、实数、文本和NULL；每行是数组，字段顺序及类型由manifest声明。`metadata_json`、`summary_json`等源JSON文本仍保留为文本，需要时再解析。

## 质量与时间边界

本次报价表中：

- 内容有效报价6,532,142条；标记赛前721,764条，二者为不同条件，不能混为同一集合。
- 常规T−10合格685,812条；严格分钟边界合格682,409条；跨截点分钟的3,403条另行标记。
- 同分钟签名冲突4,920,985条，相邻重复10,358条，均未删除。这是全报价表计数，包含滚球，不能当成T−10冲突行数。

常规T−10要求内容有效、赛前且源分钟时间≤开球−600秒；严格口径再要求该分钟起点+60秒≤截点。严格口径不自动排除同分钟冲突，同分钟无冲突也不自动解决截点分钟边界。

固定清单开球范围为北京时间 **2025-06-11 00:00至2026-10-13 03:00**，它表示计划清单范围，不表示欧指／大小球已经覆盖全部日期。优先处理ENN引用样本，其次已有亚盘覆盖，最后其余清单，当前覆盖不是随机抽样。

本快照已保存报价的源变化时间边界为北京时间 **2026-01-25 21:13至2026-10-12 08:11**；实际HTTP响应观察时间为 **2026-10-13 05:02:18至16:36:44**。这些是非空字段的最早／最晚值，不承诺范围内逐日连续完整覆盖。

清单中218,752个ID的来源状态为Played，另767个为空状态但开球已过去3小时的候选；来源状态与比分未在本次补采中独立核验为常规90分钟训练标签。

源报价时间是无年份的分钟文本，按清单开球年附近推断；同分钟真实顺序尚未核实。`source_order`只保留detail数组索引，不能用于补造秒级路径。抓取／available_at是本轮实际UTC时间，不能回填到历史赛前决策时点。

尚未取得最终完成文件与最终全批次审计。本包的上传校验只验证冻结内容能完整保存和还原，不等于市场采集完成或最终历史轨迹质量验收。

## 读取、校验与还原

需要Python 3.10或更新版本，仅使用标准库。在仓库根目录运行：

```powershell
python odds-trajectory/euro-totals/tools/snapshot.py verify odds-trajectory/euro-totals/snapshots/2026-10-13-partial
python odds-trajectory/euro-totals/tools/snapshot.py restore odds-trajectory/euro-totals/snapshots/2026-10-13-partial euro_totals.sqlite3
```

verify检查文件SHA-256／字节数、JSON数组结构、原生标量类型、每块与每表计数、全表内容哈希。restore先verify，再创建五表及原索引，批量导入，按相同主键顺序核对全表内容，并检查SQLite完整性和外键。目的文件必须不存在，请预留约2 GiB以上可用磁盘。

发布前已实际完成一次全量restore：83个数据分块及5个辅助文件校验通过，五表还原后原生数值／文本／NULL内容哈希一致，完整性与外键检查均为ok。结果见 [VALIDATION.json](VALIDATION.json)。这项验证不替代尚未完成的全批次源数据审计。

读取报价分块并映射字段名：

```python
import gzip
import json
from pathlib import Path

root = Path("odds-trajectory/euro-totals/snapshots/2026-10-13-partial")
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
columns = manifest["tables"]["quotes"]["columns"]
with gzip.open(root / "data/quotes-part-0001.jsonl.gz", "rt", encoding="utf-8") as stream:
    first_quote = dict(zip(columns, json.loads(next(stream))))
    print(first_quote)
```

还原后通过 `quotes.task_id→tasks.id→matches.match_id`连接公司、市场和元数据；attempts也通过task_id连接。完整五表包含大量待采任务，不能把整表直接当成有报价的训练集合。

## 来源与导出方法

权威来源为本地 `Pankou/ENN/market_backfill/run_20261013_045725_1f74d9/markets.sqlite3`。公开赔率入口为：

```text
https://www.daqiuxiaoqiu.com/sport-data/soccer/biz/dqd/v1/match/odds/detail/{match_id}?companyId={source_cid}&type={euro|size}
```

导出使用mode=ro、query_only和贯穿五表的单一SQLite读取事务，包含冻结时已提交WAL。每表按主键排序分块，同时校验源表计数；quotes行数与冻结tasks.raw_rows总和一致。工具不调用网络、不写源库、不另开采集器。

`source-evidence`包含采集协议、固定清单审计和另行复制的进度材料；这些材料可能晚于SQL截点，统计以manifest为准。`attempts.raw_path/raw_sha256`索引的原HTTP gzip响应仍在本地，未加入本数据包，因此本包不支持单独逐body复审。

后续完整成功须最终审计通过、未完成任务为0、失败为0，并核对completion.status；completion.complete或清单审计completed单独为true都不足以证明全部取得市场。
