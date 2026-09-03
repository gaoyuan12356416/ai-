# 003.tt-minis-bid-protection 需求与技术设计

## 背景

TikTok 已开放出价保护（自动赔付）状态与历史接口。当前 TT 小程序缺少一份可按产品、日期、账户和广告对象查询的赔付明细，需要将接口返回的日粒度历史持久化到 `ads_ai`。

## 目标

- 建设一张 TT 小程序赔付日明细表和一个同步任务。
- 仅支持 Campaign 层级查询赔付状态、金额和币种。
- 初次重建最近 30 个完整自然日，之后每天两次更新最近 14 个完整自然日。
- 安全替换 CPU 服务器共享的 TT Business API Token，且不影响现有 Native Growth 任务。

## 范围

### 包含

- `ads_ai.ads_tiktok_minis_bid_protection_daily` 单表 DDL、索引和幂等写入。
- 账户范围严格使用 `ads_accounts_setting` 中 `account_stats like '%minis_id%' and platform_id='3'` 的去重账户集合。
- 仅因 TikTok 接口强制要求，从上述账户当天 Campaign 投放数据取得 `query_ids`；不再经过产品、发布队列或 Ad Group 范围筛选。
- 北京时间每天 `09:25`、`21:25` 两次调度，首次回填 30 天，每轮刷新 14 天。
- CPU 服务器 Token 预检、备份、替换、回读及失败回滚。

### 不包含

- 查询页面、内部 HTTP API、飞书通知或汇率换算。
- Ad Group 层数据。
- 超过最近 30 个完整自然日的历史补抓。

## 用户故事 / 业务规则

1. 目标账户以用户确认的 SQL 为唯一范围：`SELECT DISTINCT account_id FROM kunlunads_dev.ads_accounts_setting WHERE account_stats like '%minis_id%' and platform_id='3'`。
2. 每个统计日期仅查询这些账户当天有正消耗的 Campaign；对象按 `advertiser_id` 分组，同一请求不混入其他账户。Campaign ID 仅用于满足 TikTok `query_ids` 必填约束，不再承担产品筛选作用。
3. 只写 `data_level='CAMPAIGN'`，`campaign_id=query_id`，`adgroup_id` 固定为空。
4. `credit_amount_scaled` 原样保存接口整数；`credit_amount = credit_amount_scaled / 100000`，保留 5 位小数；`currency` 原样保存，不换算。零赔付且上游未给币种时允许空串。
5. 每天 `09:25`、`21:25` 各运行一次，每次处理昨天起向前 14 个完整自然日；`UNDER_PROTECTION`、`CONFIRMING` 在窗口内回刷，终态不重复请求。
6. 当天历史金额不作为正式结果采集；当天数据从次日开始进入明细表。
7. API 成功记录使用唯一键 upsert；单账户或单批次失败不得删除、清空或覆盖已有成功数据，任务需记录失败并返回非成功状态。
8. Token、数据库密码及完整鉴权请求头不得进入 Git、命令行参数、日志或测试夹具。

## 交互与流程

1. 先执行用户确认的账户 SQL，读取账户 `account_stats.minis_id`；当前映射为 DramaWaveMinis `3346`、BestReelsMinis `3380`、MyShort `3416`，明确不使用 `1479`。
2. 按日期读取上述账户有正消耗的 Campaign，按 `advertiser_id` 分组并按 TikTok 限制切分强制的 `query_ids`。
3. 日常同步调用历史接口取得日记录；Token 轮换前后另用状态接口、历史接口和现有 Native Growth 接口做兼容性 canary。
4. 校验日期、层级、对象 ID、状态、币种和金额缩放关系。
5. 在单连接、小批次事务中 upsert 到明细表；输出脱敏运行摘要和失败账户数。
6. 定时任务由独立 `flock` 防重入；首次 30 天回填人工执行并可安全重跑。日常入口固定为 `--daily`，代表最近 14 个完整自然日；失败 API 批次写入数据盘脱敏重试状态。

## 技术设计

### 影响模块

- GitHub 源码：`ops/tt-minis-bid-protection/`。
- 目标库表：`ads_ai.ads_tiktok_minis_bid_protection_daily`。
- CPU Token 库：`/root/codex_test/tt_business_api_tokens.sqlite3`，键 `native_growth_default`。
- CPU 定时任务：root crontab 独立任务，不改现有 TT 投放或播报任务。

### 数据结构

业务粒度为 `record_date + advertiser_id + data_level + query_id`。该组合建立唯一键以保证重跑幂等；产品日、账户日、Campaign 日、Ad Group 日及待结算状态日期均建立二级索引。完整 DDL 见 `ops/tt-minis-bid-protection/001_create_ads_tiktok_minis_bid_protection_daily.sql`。

### API / 接口

- 状态：`GET /open_api/v1.3/report/bid_protection/status/get/`。
- 历史：`GET /open_api/v1.3/report/bid_protection/detail/get/`。
- `data_level` 仅使用 `CAMPAIGN`。生产实测证明历史接口不支持账户层级：缺少 `query_ids` 返回 `40002`，空数组返回 `52404`，`ADVERTISER` 层级被拒绝且仅允许 `CAMPAIGN/ADGROUP`。
- 本任务按单日、单账户分批，单批最多 200 个 Campaign ID，详见 `api-doc.md`。

### 异常与边界

- `query_id` 必须属于请求的 `advertiser_id`；映射冲突或缺少产品/minis 标识时失败关闭，不写入猜测结果。
- 非法日期、未来日期、超过 30 天、非 Campaign 层级或金额不能精确缩放时拒绝写入；仅零赔付记录允许上游币种为空。
- HTTP 429/5xx 可做有上限退避重试；鉴权/权限错误不盲目重试。部分成功保留，失败对象留待下一次任务。
- 单表方案不记录运行审计行，因此“表内无数据”不能单独证明真实零赔付；须结合进程退出码和脱敏日志判断同步是否完整。

## 验收标准

- DDL 在 MySQL 5.7 创建成功，字段、唯一键和六个计划索引读回一致。
- 用户 SQL 返回的账户全部进入范围；当前三款产品账户数与 `minis_id` 一致，未知或冲突映射失败关闭。
- 最近 30 天 Campaign 回填完成，重复执行不增加重复行，表内不存在 Ad Group 数据。
- 未结算记录在后续每日任务中可更新为终态和最终金额，已成功数据不因局部失败丢失。
- 金额缩放逐行满足 `credit_amount_scaled / 100000 = credit_amount`，并按原币种查询。
- 日常查询使用计划索引；所有事实均为 Campaign 层。
- 新 Token 通过状态、历史和现有 Native Growth 三类只读校验后才落库；日志和 Git 中无明文 Token。
- DramaWaveMinis `2026-09-02` 输出按币种的 Campaign 层赔付汇总、Campaign 明细和失败账户数。

## 风险与待确认

- TikTok 上游按日更新，刚进入窗口的数据可能仍处于 `CONFIRMING`；每日回刷解决最终金额迟到问题。
- 共享 Token 替换会同时影响现有 Native Growth 任务，必须在变更前后都做兼容性只读校验。
- 无独立运行审计表是本次“小需求、单表”边界；运行完整性依赖 cron 进程退出码与脱敏日志。
- 无待确认产品决策。

## 变更记录

- 2026-09-03：需求确认，仅建设单表、同步脚本、每日任务和安全 Token 替换。
- 2026-09-03：按用户调整，账户范围改为 `ads_accounts_setting` 精确 SQL，仅同步 Campaign，旧数据备份后清空并回填 30 天；每天两次刷新最近 14 天。因 TikTok 强制 `query_ids`，保留最小 Campaign ID 枚举作为接口兼容层。
