# 欧指与大小球数据字段说明

此包对应本轮独立 `markets.sqlite3` 的五张表。与先前亚盘全TEXT CSV包不同，本包以压缩JSON Lines保存，每行为原表顺序的JSON数组，字段顺序、SQLite声明类型和主键在 `manifest.json` 中。SQL NULL保留为JSON null，空文本仍为空字符串，整数和实数不转换为文本。

## 公司与市场

| company | market | source_cid | 数据含义 |
| --- | --- | --- | --- |
| Royal | euro | 545 | 皇家主/平/客十进制欧赔 |
| Royal | size | 3 | 皇家大/小香港净水位及总进球线 |
| Macau | euro | 80 | 澳彩主/平/客十进制欧赔 |
| Macau | size | 1 | 澳彩大/小香港净水位及总进球线 |

这是大球小球/懂球帝聚合接口的市场专用ID。皇家欧指545不能当作亚盘/大小球的3，澳彩欧指80不能当作1。未采其他公司。

## matches：固定采集清单

| 字段 | 含义 |
| --- | --- |
| match_id | 来源API比赛ID，字符串主键，别名分别保留 |
| kickoff | 清单开球时间，UTC epoch秒，可空 |
| priority | 0为ENN引用的fixture group，1为已存在皇家/澳彩亚盘覆盖，2为其他清单项 |
| metadata_json | 清单原行的JSON文本；球队/赛事ID与名称、UTC/北京时间赛程、来源状态/阶段/比分/观察时间、fixture_group、别名核验信息、候选依据和历史覆盖标签 |

同场多个API ID分别请求，`fixture_group`供后续真实比赛去重。`alias_source_id`、`alias_verified`保留映射证据状态，不能凭数字ID差值推断映射。

来源 `Played` 或清单包含比分不构成独立核验的常规90分钟训练标签。空状态且开球已过去3小时的候选标记 `past_kickoff_unconfirmed_final`，不能直接按终场样本结算。`inventory.completed`只是固定清单建成，不代表赔率采集结束。

## tasks：请求资格、结果与未完成范围

主键为id，唯一键为 `(match_id,market,source_cid)`，每个清单API ID固定四项。

| 字段 | 含义 |
| --- | --- |
| id | 任务整数主键 |
| match_id / market / source_cid / company | 请求目标身份、市场、来源公司ID和归一公司名称 |
| kickoff / priority | 清单赛程和调度优先级 |
| state | pending待处理；running执行中；retry待重试；ok取得明细；missing_company缺目标公司；empty_detail目标明细为空；failed请求/结构终态失败 |
| attempts | 单调尝试序号，取消等事件也不复用序号 |
| next_retry | 计划重试UTC epoch秒，0表示无计划重试时间 |
| last_error | 最近原因文本，可能是接口确认缺失说明，不能一律视为网络故障 |
| latest_attempt | 最近已落库结果的尝试号，未有结果可为null |
| raw_rows | 目标公司detail数组长度，不是原始响应文件数 |
| valid_rows | 数值/盘口有效报价行数，不保证赛前或T−10合格 |
| t10_rows / t10_strict_rows | 对应T−10资格标记的报价行数 |
| summary_json | 该任务明细的统计和时间质量信息，JSON文本 |
| updated_at | 任务最近落库UTC ISO时间，独立于源报价变化时间和HTTP响应时间 |

`missing_company`和`empty_detail`为接口确认的真实返回缺失；`failed`表示请求或结构失败。`pending/running/retry`属于尚未处理完的范围。不能把这几类统一称作没有市场，也不能补零或填中性赔率。

`ok`表示返回可标准化明细，不表示每行都有效、没有滚球或有完整T−10轨迹。

## attempts：HTTP尝试与原始证据索引

复合主键为 `(task_id,attempt_no)`，关联tasks。

| 字段 | 含义 |
| --- | --- |
| task_id / attempt_no | 所属任务和单调尝试编号 |
| requested_at / response_at | 实际请求与响应/异常完成UTC ISO时间；started占位/取消项的请求时间可能只是调度占位 |
| source_url / final_url | 请求URL与实际返回最终URL |
| http_status / content_type | HTTP状态码与类型；未取得响应可为空 |
| raw_path | 相对原run目录的gzip HTTP证据路径；本包未附该目录的raw原件 |
| raw_sha256 | 已捕获HTTP body原字节的SHA-256，不是gzip文件哈希 |
| body_bytes / body_truncated | 已捕获HTTP body字节数与超过限制的截断标记；截断不能计为可用成功 |
| elapsed_seconds | 用单调时钟测量的请求耗时 |
| status | 每次尝试状态，如started/ok/missing_company/empty_detail/failed/interrupted/cancelled_before_request；不同于任务retry队列状态 |
| reason | 对应尝试原因，可空 |

原本地gzip为 `http-evidence-v1` 封装，以base64保存捕获body、其哈希和请求/响应元信息。本包只发布五表及来源说明材料，保留证据路径和body哈希供追溯；不能凭本包单独审计未附的HTTP body。

## quotes：欧赔／大小球逐条报价

复合主键为 `(task_id,source_order)`，`WITHOUT ROWID`；通过tasks连接比赛与公司。一条记录对应来源detail数组中的一个元素，包括封盘、无效报价、滚球和重复条目。

| 字段 | 含义 |
| --- | --- |
| task_id | 请求任务ID |
| source_order | 目标detail数组的0基索引，仅保存源数组位置 |
| changed_at | 根据来源分钟时间和清单开球时间推断的UTC epoch秒，可空 |
| source_time | 来源MM-DD HH:MM原串，通常无年份且只有分钟精度 |
| source_score | 来源报价时的滚球比分标记文本，不是最终比分 |
| home_decimal / draw_decimal / away_decimal | 欧指主胜、平、客胜十进制赔率；大小球市场这套字段为空 |
| over_hk / total_line / under_hk | 大球净水位、总进球线、小球净水位；欧指市场这套字段为空 |
| quote_valid | 报价数值与盘口内容有效，0/1；不包含时间有效性判定 |
| prematch | 时间可用且早于开球、来源score为空的赛前标记，0/1 |
| t10_eligible | 内容有效、赛前且源分钟时间≤开球−600秒，0/1 |
| t10_strict_eligible | 常规T−10合格且源分钟起点+60秒≤截点，0/1 |
| t10_cutoff_minute_ambiguous | 常规T−10合格但来源分钟区间跨截点，0/1 |
| same_minute_conflict | 同分钟存在不同报价/比分签名，包括无效或封盘条目的冲突，0/1 |
| consecutive_duplicate | 来源数组相邻条目报价签名完全相同，未删除；不要求来源时间相同 |
| invalid_reason | 内容不合格原因的分号连接文本，如suspended/invalid_decimal_prices/invalid_hk_prices/invalid_total_line/row_not_object |
| home_source / draw_source / away_source | 原homeWin/draw/awayWin值的文本保存；size的draw_source为进球线原串，euro的draw_source为平赔原串 |

欧指三项需有限且>1；大小球两边香港净水位需有限且≥0。大小球进球线为非负四分之一档，可解析相邻0.5半线，如 `2/2.5→2.25`；原串同时保留。香港净水位转十进制报价通常加1，欧指十进制报价不再加1。

不同市场无关字段的null是正常的结构性空值，不能当成采集缺失。无效时间不一定出现在 `invalid_reason`，应同时查看changed_at为空和summary的invalid_time_rows。数据库未保存去返还率概率，不应将本包表述为已保存预测胜率。

## summary_json和时间边界

summary包括原始/有效/无效/时间异常/滚球/赛前计数，T−10普通/严格/边界/无冲突行数，同分钟冲突及相邻重复计数。

- `latest_pre_t10_at`、`earliest_pre_t10_at`只针对有效T−10报价。
- `latest_visible_pre_t10_at`包含来源封盘/无效条目；需结合 `latest_visible_pre_t10_conflicting` 与 `latest_visible_pre_t10_all_quotes_valid` 判断末分钟是否存在可交易疑点。
- `t10_quote_age_seconds`是截点减最新有效源报价时间，仅表示年龄。
- `newest_first_minute_order_observed`只检查已解析分钟是否降序；`within_minute_order_verified=false`。
- `available_at`是本轮实际HTTP response_at，`availability_basis=historical_response_fetched_now`，不回填到历史开球或决策时点。

source_time按北京时区，在开球年前一年/本年/后一年取最近年份；距离平局或无法解析时保留原串，changed_at为空。source_order不能当作已核验的同分钟真实先后，不能补造秒数或按赔率/抓取时间排序。同分钟冲突包括封盘条目，不能只挑有效价消除冲突。

严格T−10只解决分钟区间跨截点，未排除same_minute_conflict；无冲突T−10计数只排除同分钟签名冲突，未解决截点分钟边界。两套资格应分别筛选。单独拥有T−10报价，不等于有完整无冲突轨迹、截点可交易或可供V1直接评分。

## meta与来源说明材料

meta的key/value_json保存创建时间、运行状态、清单数量、会话时间等运行JSON，不是市场业务数据。

`source-evidence/progress.json`及progress.md在导出过程中另行复制，其自带观察时间可能晚于一致SQL截点；用于说明后台运行，不替代manifest中从同一读取事务计算的计数和覆盖。固定清单审计的完成标记也不等于市场采集完成。

本次数据仅为阶段快照，完整完成需最终审计通过、未完成任务为0、失败任务为0并核对completion.status。后采市场历史适合归档研究，不能证明历史T−10时点实际已取得这些信息。
