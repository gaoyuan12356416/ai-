# SocialKit 数据关系与源库核验

## 路线

- CPU：`43.166.187.96`。使用已配置的 `server-43-166-187-96` SSH helper。
- 外部源：`hk-cynosdbmysql-grp-dejftnkp.sql.tencentcdb.com:28914/socialkit`，读取任务、短链与非敏感账号字段。
- 内部只读源：`101.32.56.53:63350`。连接后确认 `@@read_only=1`。收入位于 `kunlunads_dev`，中间表位于 `ads_ai`。
- 服务器已有环境配置供进程读取。不要输出环境变量的值、读取/复制原始 Token，也不要在脚本参数、SQL 日志或结果文件保存数据库密码。
- `63353` 不是查询入口。本技能不连接写端，不执行 DDL/DML。自动刷新必须使用现有 SQL FIFO Gate；人工核验如使用保留连接池，也需遵循服务器技能中的连接限额。

## 表与粒度

| 表 | 关键字段 | 注意 |
|---|---|---|
| `social_auto_publish_task` | `id,team_id,account_id,drama_app_id,drama_content_id,schedule_date,reason,task_status,item_total,item_success,item_failed,deleted_at` | 日期为账号当地日期；一任务可以有多条内容 |
| `social_short_link` | `short_code,team_id,drama_app_id,drama_content_id,platform_code,external_account_id,material_id,material_pool_type,creation_source,created_at,deleted_at` | `short_code` 唯一；收入归属来源；创建时间不是发帖时间 |
| `social_center_accounts` | `id,team_id,external_account_id,platform,account_name,is_deleted` | 只选需要的非敏感字段；同名账号不可合并 |
| `ads_facebook_page_insight` | `dt,site_id,campaign,channel,country,platform,clicks,views,installs,recharge,revenue,refund_revenue,dnu_revenue,dnu_recharge,updated_at` | 此处 `platform` 是 iOS/Android，不是社媒平台 |

收入源粒度保留 `dt × site_id × 精确 campaign × country × 设备 platform`。先汇总同粒度行，再按所需维度聚合。

短链关联使用原始短码精确匹配，不做大小写归一化。短链唯一索引采用不区分大小写排序规则，所以批量 `IN` 只是候选获取；应用内仍按精确原值匹配。未知键、空键和格式异常键保留“未归属”桶。

历史短链被逻辑删除不意味着历史收入应消失；发布统计仍按明确的 `deleted_at=0` 口径。账号先限定团队、转换后的平台和外部 ID，再使用唯一当前记录；仍不唯一时保留稳定 ID 和归属异常，不随意选择名称。

## 平台编号

| 统一平台 | 短链 `platform_code` | 任务 `account_platform` / 账号 `platform` |
|---|---:|---:|
| TikTok | 1 | 3 |
| Facebook | 2 | 1 |
| Instagram | 3 | 4 |
| YouTube | 4 | 2 |
| X | 5 | 5 |
| 通用 | 0 | 不强行对应 |

产品以短链 `drama_app_id` 为准：`1479=Dramawave`，`3302=HotDrama`。发现新 App ID 或平台编号时保留原值，不静默映射为已有产品/平台。不要硬编码团队 2。

`creation_source`：0 历史/未知、1 自动发布素材维度、2 排行榜剧维度、3 社媒浏览器外部 API。它不是任务 `reason`。

`material_pool_type`：0 普通/无素材、1 新剧、2 爆款、3 历史爆款、4 老剧。`material_id=0` 表示此次创建没有素材；不要把所有类型 0 都解释为有素材的普通广告。

## 有界的只读 SQL

以下参数通过驱动绑定；不要直接拼接用户输入。先限定日期和渠道，避免用几千个短码对大收入表散读。

```sql
SELECT dt, site_id, MIN(campaign) AS short_code,
       MIN(country) AS country, MIN(platform) AS device_os,
       SUM(clicks) AS clicks, SUM(views) AS views,
       SUM(installs) AS installs, SUM(recharge) AS conversions,
       SUM(revenue) AS revenue, SUM(refund_revenue) AS refunds,
       SUM(dnu_revenue) AS dnu_revenue,
       SUM(dnu_recharge) AS dnu_conversions,
       MAX(updated_at) AS source_updated_at
FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(c_dt)
WHERE channel = 'socialkit' AND dt = %s
GROUP BY dt, site_id, BINARY campaign, BINARY country, BINARY platform;
```

通过本轮实际出现的短码批量访问短链 `uk_short_code` 索引，再在进程内精确匹配；不复制整个短链表。外部、内部源属于不同端点，不能假设一条跨库 SQL 能直接联通。

```sql
SELECT id, team_id, account_id, account_platform,
       drama_app_id, drama_content_id, drama_name, product,
       account_lang, account_country, schedule_date,
       reason, task_status, item_total, item_success, item_failed
FROM socialkit.social_auto_publish_task FORCE INDEX(idx_team_date_del)
WHERE team_id = %s AND schedule_date BETWEEN %s AND %s AND deleted_at = 0;
```

先发现相关团队，再逐团队读取。收入表没有 `updated_at` 索引，也没有小时收入粒度：不要按更新时间水位扫全表，或把日汇总强行转换为北京时间日。默认任务重算最近 14 个日期，吸收迟到收入、退款和归因修正。

## 快照核对与解释

中间表为 `ads_ai.socialkit_effect_daily`、`socialkit_publish_daily`、`socialkit_refresh_runs`。批次版本规则以服务当前实现为准；优先使用报表 query 模块，避免绕开有效版本读取未完成批次。

每个日期核对收入、退款、点击、安装等源总额与归属后总额一致；同一过滤下的平台/产品/账号/剧目分组应守恒。采集失败是缺失，不是零值。

11 月 1 日曾核验收入 $5,050.37 全部归属，仍有 7 个无收入的未匹配站点/短链组合。这只是历史核验样本，重新查询可能回补；不能写成固定断言或自动修正目标。
