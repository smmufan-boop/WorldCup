# 轨迹数据字段说明

本说明对应 `snapshots/2026-10-13/`。数据库字段均按原始 TEXT 导出；空字段表示空文本或缺失，不能自动转换为 0。每个压缩 CSV 都有表头，以 UTF-8 无 BOM 保存。使用 CSV 解析器读取，JSON 字段内的逗号与引号不能手工拆分。

## matches：比赛元数据

主键为 `match_id`。该表包含没有赔率记录的比赛与未来赛程，表行数不等于轨迹比赛数。

| 字段 | 含义 |
| --- | --- |
| match_id | 源平台比赛 ID，保持字符串 |
| cmp_type | 运动类型；足球为 soccer |
| competition_id / competition_name | 来源赛事 ID / 赛事名称；部分旧记录 ID 缺失 |
| team_A_id / team_A_name | 主队的来源 ID / 名称 |
| team_B_id / team_B_name | 客队的来源 ID / 名称 |
| kickoff_utc | 来源开球时间，ISO 8601，UTC |
| kickoff_local | 北京时间开球时间，ISO 8601，通常带 +08:00 |
| status | 来源比赛状态，如 Fixture、Playing、Played、Postponed、Cancelled；空值保留 |
| minute | 来源比赛分钟数/显示文本 |
| score_home / score_away | 来源主客比分字符串；并非一律为可结算的常规时间比分 |
| fetched_at_utc | 该条元数据保存的采集时间，UTC；不构成每一次状态变化的事件日志 |
| period | 来源比赛阶段信息，原样保留；识别常规时间、加时、点球时需核验 |
| score_note | 比分附注，原样保留 |
| team_A_logo / team_B_logo | 来源主客队徽 URL，可缺失 |

## odds_history：逐条报价轨迹

主键为 `dedupe_key`。使用 `match_id` 连接比赛元数据，按 `company_id`、`market_type` 分开处理。这里保留源库所有已保存报价，不预先删除滚球、缺失报价或不适合某种分析的记录。

| 字段 | 含义 |
| --- | --- |
| dedupe_key | 采集器使用的报价去重 SHA-256，保留原值 |
| match_id | 来源比赛 ID |
| cmp_type | 来源运动类型 |
| competition_name | 报价记录内保存的赛事名称；可能与后来更新的比赛表不同 |
| home_team / away_team | 报价记录保存的主客队名称 |
| kickoff_utc / kickoff_local | 报价记录保存的开球时间，UTC / 北京时间；可能为较早赛程版本 |
| company_id / company_name / company_abbr | 来源公司 ID / 名称 / 简称；名称可能遮蔽 |
| market_type | 市场类型：asia 为亚盘，euro 为胜平负，size 为大小球；以实际清单为准 |
| source_time | 源报价时间文本，通常 MM-DD HH:MM，不带年份，精度为分钟 |
| change_time_local | 结合该条开球年份推断的报价时间，通常带 +08:00；不能当成来源原始秒级时间 |
| home_water / away_water | 来源两边水位字符串；亚盘分别为主队/客队净水位，转十进制赔率时通常加 1，不能直接当成概率 |
| handicap_text | 来源中文盘口文本；亚盘以主队视角显示，含“受”的文本表示受让 |
| handicap_value | 来源数值盘口字符串；不能只凭正负号判断让球方 |
| score | 来源报价时附带的比分/滚球标记文本；非空通常说明滚球，空值本身不能证明赛前 |
| fetched_at_utc | 采集器保存的报价获取时间，独立于源报价变化时间；旧记录更新后可能变化，不能一律视为首次获取，也不是每次重抓的完整日志 |
| source_url | 获取该记录的公开来源 URL，含比赛 ID、companyId、type |

去重输入依次为 `match_id, company_id, market_type, source_time, home_water, handicap_text, handicap_value, away_water, score`，各值以 U+001F 连接后计算 SHA-256。同一分钟出现不同报价时仍为不同记录；同一分钟相同报价再次出现可能共用同一去重键，完整的 A→B→A 顺序要结合下一张表。

公司常用映射为 `1=懂球帝澳彩`、`3=Royal`、`8=Bet365`。这是大球小球/懂球帝聚合条目，不代表从公司官网直接采集。历史轨迹分析按同公司比较；本地澳彩/Royal策略不等于 Bet365 的研究口径。

本地亚盘方向转换规则：以 `abs(float(handicap_value))` 为盘口幅度，文本以“受”开头或包含“受让”时记为负值，表示主队受让；普通文本记为正值，表示主队让球；“平手”且幅度为 0 时记为 0。例如“受一/球半”表示主队受让 1.25。本地该方向值与一般结算公式中加到主队比分上的让球数符号相反，使用前应明确约定。

## odds_minute_order：同一分钟内部顺序证据

复合主键为 `(match_id, company_id, market_type, change_time_local, sequence_hash)`。一条记录是一份来源数组证据，同一分钟可能保存多份，不要只取采集时间最新的一份。

| 字段 | 含义 |
| --- | --- |
| match_id / company_id / market_type | 对应比赛、公司与市场 |
| change_time_local | 对应报价分钟，带北京时间时区 |
| sequence_hash | sequence_json 原文本的 UTF-8 SHA-256 |
| sequence_json | JSON 二维数组，每项为 `[handicap_text, handicap_value, home_water, away_water]`，从旧到新 |
| source_url | 该份顺序证据的公开来源 URL |
| fetched_at_utc | 获取顺序证据的 UTC 时间 |

源接口 `detail` 数组为最新在前，采集器反转后保存。只折叠连续相同报价，保留 A→B→A。使用证据前要确认来源 URL 的比赛/公司/市场、有效报价覆盖和各份证据兼容；冲突或覆盖不足时顺序未知。不能用水位大小、SQLite rowid、文件行序或采集时间补造分钟内先后，也不能补造秒数。

## match_score_aliases：已核验比赛 ID 对照

主键为 `alias_id`。不同来源可能给同一实际比赛分配不同 ID；保留已核验对照供比分连接，原 ID 不重写。

| 字段 | 含义 |
| --- | --- |
| alias_id | 需要映射的原比赛 ID |
| source_id | 已核验的对应来源比赛 ID |
| identity_json | 核验身份数组，包含运动、赛事 ID、主客队 ID、开球 epoch 等锚点 |
| first_verified_utc / last_verified_utc | 首次/最近核验时间，UTC |
| original_json | 核验时保存的原比赛元数据 JSON |
| source_json | 核验时保存的对应来源比赛元数据 JSON |
| basis | 映射依据，如 exact-sport-league-team-ids-kickoff |

按完整身份锚点核验后再连接比分；不能凭相似队名合并。不同 `match_id` 的数量不一定等于唯一实际比赛数。该表不是完整的别名目录；没有映射时保留不确定性。

别名用于在保留原轨迹 ID 的前提下刷新比分，不会重编号原轨迹。`original_json` 保存首次归档的比赛，`source_json` 保存已核验来源的后续元数据。发生改期时，赛前截断采用 `original_json`、`identity_json`、`source_json` 中已核验的最早开球，避免以延后的时间纳入实际滚球；不能仅凭相近时间创建映射。

## 时间与分析口径

- 北京时间赛日按当天 12:00（含）至次日 12:00（不含）划分；自然日与赛日可能不同。
- 常规赛前报价要求可靠报价时间早于可靠开球时间，并排除滚球标记；延期或赛程改动要先核对最新比赛元数据与报价内的旧时间。
- T−60/T−30/T−10 截点要按真实北京时间计算，记录采用的报价时间与新鲜度；缺失不能倒填。
- 晚于决策截点采集的历史可用于回溯研究，但不能证明该截点实际已获得这些信息。赛后重建不能标作前瞻样本。
- 赛果必须核验常规 90 分钟（含补时）、加时和点球范围；Played 状态和显示总比分不足以单独确认结算。本地规则优先采用 `score_note` 的明确“90分钟[主-客]”证据；否则要求 Played、`period=FT`、`minute=90`、非负整数比分且无加时/点球/120 提示，其余保留未核验。
- 同一场的多公司、多截点和多个信号不是独立比赛样本。若沿用本地轨迹赢盘率，计算为（全赢＋半赢）/（全赢＋半赢＋半输＋全输），剔除走盘，并保留五类计数；该比例不等于模型概率。
- 所有内容为已保存快照，来源接口不承诺完整历史；缺字段、缺公司、同分钟冲突与不连续采集都应在分析中单独报告。数据不附带新的推荐或收益结论。
