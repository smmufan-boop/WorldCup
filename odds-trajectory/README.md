# 本地足球盘口轨迹数据

本目录保存本地 Pankou 轨迹数据库的固定快照，供数据核查、轨迹研究和离线复现使用。当前快照为 [2026-10-13](snapshots/2026-10-13/)，冻结时间 **2026-10-13 02:47:02（北京时间）**。这是该时刻已提交的数据，不会随本地后续采集自动更新。

完整字段定义、盘口符号、赛前截断与比分核验口径见 [DATA_DICTIONARY.md](DATA_DICTIONARY.md)。各分块的行数、SHA-256、字节数、空字段统计和源时间范围见 [manifest.json](snapshots/2026-10-13/manifest.json)。

配套算法见 [V1 轨迹比对、评分与 T−10 规则](v1/README.md)，包含纯 Python 核心、固定参数和合成示例。

后续欧指／大小球补采单独发布于 [欧指与大小球阶段数据](euro-totals/README.md)，与本页的亚盘快照分别说明覆盖及时间。

## 数据规模

| 数据表 | 行数 | 用途 |
| --- | ---: | --- |
| matches | 220,936 | 比赛身份、赛程、状态、比分元数据 |
| odds_history | 1,808,375 | 逐条盘口和主客水位报价 |
| odds_minute_order | 59,693 | 同一分钟内的来源报价顺序证据 |
| match_score_aliases | 11,029 | 经核验的跨来源比赛 ID 与比分连接证据 |

报价表覆盖 **65,375 个来源比赛 ID**，均能连接 `matches`（孤立报价比赛 ID 为 0）。这个数不等于唯一实际比赛数，因为同场可能存在已核验 ID 别名。比赛元数据表包含无报价比赛和未来赛程。

共有 **24 个 gzip CSV 分块**，每块最多 100,000 行，合计 **121,184,529 字节（约 115.57 MiB）**；最大分块 7,641,957 字节。无需 Git LFS。每块均有 UTF-8 表头。

| company_id | 来源公司名称 | 市场 | 报价行数 |
| --- | --- | --- | ---: |
| 1 | 懂球帝澳彩 | asia | 113,541 |
| 3 | Royal（源名称可能遮蔽） | asia | 1,418,204 |
| 8 | Bet365（源名称可能遮蔽） | asia | 276,374 |
| 9998 | 北单**（来源遮蔽名） | asia | 256 |

本次快照实际只有亚盘 `asia`。公司 9998 的少量历史按原样保留，不能并入澳彩、Royal 或 Bet365 样本。公司间轨迹应分别研究。

## 时间范围与质量

下表为源库字段的非空最小/最大值，表示保存记录的边界，不承诺该范围每天都有完整数据。

| 字段范围 | 最早 | 最晚 |
| --- | --- | --- |
| 报价记录的 change_time_local（北京时间） | 2025-08-14 23:48 | 2026-10-13 02:41 |
| 报价记录内的 kickoff_local（北京时间） | 2025-12-18 00:00 | 2026-10-13 10:05 |
| 比赛元数据的 kickoff_local（北京时间） | 2025-06-11 00:00 | 2026-11-04 20:00 |
| 报价 fetched_at_utc（UTC） | 2026-08-31 16:43:16 | 2026-10-12 18:43:40 |

报价时间 `source_time` 原始精度为分钟且无年份，`change_time_local` 是结合开球年份推断的北京时间，可能存在来源或推断误差。上述日期边界不是从对应日期开始连续采集的证明。源报价时间和采集时间不能互相替代。

报价表的 `home_water`、`away_water` 各有 10,268 条为空；264,838 条的 `score` 非空（通常为滚球标记），全部保留供使用者自行筛选。不能把空水位改成 0，不能把整库直接当作有效赛前样本。比赛表的 `competition_id`、`team_A_id`、`team_B_id`、`status` 各有 767 条为空；其它字段空缺详见清单。这些是逐字段计数，不表示缺失行的交集。名称/盘口文本中的 Unicode 替换字符检测为 0，这不证明所有身份与来源字段均已核验正确。

同分钟重复路径必须结合 `odds_minute_order` 的来源顺序证据；多个来源 ID 必须结合 `match_score_aliases` 的身份锚点。赛前分析还需核验开球时间、报价新鲜度与常规 90 分钟比分。晚采历史只说明现在可以回顾，不能证明过去某个决策时点已取得这些信息。

## 文件布局

```text
odds-trajectory/
  README.md
  DATA_DICTIONARY.md
  tools/
    export_snapshot.py
    snapshot.py
  snapshots/2026-10-13/
    manifest.json
    schema.sql
    data/
      matches-part-0001.csv.gz           # 3 块
      odds_history-part-0001.csv.gz      # 19 块
      odds_minute_order-part-0001.csv.gz # 1 块
      match_score_aliases-part-0001.csv.gz # 1 块
```

分块按源表 `rowid` 扫描顺序切分，**文件顺序不是报价的业务时间顺序**。所有列保持文本精度，空 CSV 字段还原为空文本。本次导出确认不存在 SQL NULL；若后续源库出现 NULL 或非文本，导出工具会停止，避免静默损失。

`schema.sql` 包含上述四张表及相关普通索引，不含后台服务的触发器、缓存版本表或采集状态。还原后获得可离线查询的数据，若接入运行中的看板，应由应用自行完成所需初始化。

## 校验与还原

克隆整个仓库，或下载全部快照文件。需要 Python 3.10 或更高版本，仅用标准库，无需安装依赖。在仓库根目录执行：

```powershell
python odds-trajectory/tools/snapshot.py verify odds-trajectory/snapshots/2026-10-13
python odds-trajectory/tools/snapshot.py restore odds-trajectory/snapshots/2026-10-13 trajectory.sqlite3
```

`verify` 检查压缩文件 SHA-256/字节数、表头、列数、每块和每表行数，以及逐行内容哈希。`restore` 先校验，再批量导入新的 SQLite，复核全部表计数、内容哈希与 `PRAGMA integrity_check`；成功时输出 `"ok": true` 的 JSON。还原目的文件必须不存在。请预留约 2 GiB 可用磁盘空间。

本次发布前已实际执行全量 `restore`：24 个分块的字节数、SHA-256、CSV 结构和行数通过，四表还原内容哈希一致，SQLite 完整性检查为 `ok`。`.gitattributes` 固定说明、清单和 SQL 的 LF 换行，避免跨平台检出改变被校验文件的字节内容。

直接读取一个分块的示例：

```python
import csv
import gzip

path = "odds-trajectory/snapshots/2026-10-13/data/odds_history-part-0001.csv.gz"
with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
    rows = csv.DictReader(stream)
    print(next(rows))
```

## 来源与导出方法

权威来源是本地 `Pankou/live_dashboard/data/daqiu_odds/daqiu_odds.sqlite3`。采集器发现公开足球赛程，将聚合平台可见赔率历史标准化保存到 SQLite。对应公开来源为：

- 赛程聚合入口：`https://www.daqiuxiaoqiu.com/api/data/tab/new/important`
- 赔率入口：`https://www.daqiuxiaoqiu.com/sport-data/soccer/biz/dqd/v1/match/odds/detail/{match_id}?companyId={company_id}&type=asia`
- 比赛发现与身份/比分核验也使用懂球帝公开赛程，逐条来源以 `source_url` 和别名证据为准。

来源接口未公开承诺历史完整性，名称、保留范围与接口行为可能变化。数据保持原有来源属性，此快照不另行授予第三方原始内容权利。

导出使用 SQLite `mode=ro`、`query_only` 和一个贯穿四张表的读取事务，包含冻结时已提交的 WAL 内容，不直接复制运行中的数据库文件。逐表导出后与同一事务中的计数核对，同时保存内容哈希。复现新快照的命令为：

```powershell
python odds-trajectory/tools/export_snapshot.py PATH/TO/daqiu_odds.sqlite3 NEW/SNAPSHOT/DIRECTORY
```

目标目录必须不存在，旧快照不自动覆盖。该工具只读取源库、写入指定导出目录，不联网采集，也不修改源库。

旧 `Pankou/YYYY-MM-DD/` 日期目录主要为 2026-07-17 至 2026-08-30 的接口快照、首末端点宽表和信号分类，不能作为当前完整逐点轨迹使用；本次以权威 SQLite 四表为上传范围。重复 raw 响应、迁移 CSV、缓存、日志、备份、投注账本、模型和资讯数据库不属于本数据包。
