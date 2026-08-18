# 048.x-account-operating-stats 需求与技术设计

## 背景与目标

管理员在 `/x-account-list.html` 按账号查看 X 运营产出和收入；页面打开时不得扫描 MySQL。统计在北京时间 09:10、21:10 刷新到数据盘缓存。

## 范围

- 每账号：累计/昨日 Post、累计/昨日 Repost、累计/昨日收入 USD。
- 页面顶部：无法可靠归属账号的 X 累计/昨日收入。
- 公众指标仅保留 followers、tweet、like，移除 following、listed、media。
- 本地 JSON 缓存、缺失/过期提示、systemd oneshot/timer、只读查询和测试。
- 不修改 `features/x_accounts/oauth_service.py`、Sidecar DTO、授权/发布合同；不新增 X 写入、运行时 DDL、收入推断或生产部署。

## 冻结业务规则

1. 直接 Post：`delivery_mode='direct'` 且 log 确认 `published`，actor 为 `q.account_id`。
2. Premium relay 原 Post：存在确认的 `source_post_id/source_published_at`，actor 为 `q.relay_account_id`；目标 Repost 失败不抹去已存在原 Post。
3. Repost：仅 `x_post_repost_ledger.status='reposted'`，归属 `target_account_id`。
4. 昨日为北京时间自然日；UTC ledger 时间转换到 `Asia/Shanghai` 后判断。
5. 收入只读 `kunlunads_dev.ads_drama_bills`，严格 `site_id='2116'`、`event_revenue_usd`；收入表归属列为 `campaign`，并强制使用已确认索引 `idx_site_event_time`。昨日为 DB `+08:00` 下 `DATE(FROM_UNIXTIME(event_time))`。
6. 仅从确认发布的 log（`status='published'` 且 `x_post_id` 非空）读取 `long_url` 中唯一、非空的 W2A 参数 `c`，再与收入表 `campaign` 精确等值匹配并归属对应 `q.account_id`。同一 W2A c 指向多账号、缺失或收入侧不匹配均进入“未归属”，不得猜测或分摊；failed/reserved log 不提供归属证据。
7. Decimal 聚合，缓存保存 6 位小数字符串，页面显示 USD 两位。

## 技术设计

`X SQLite read-only + /usr/bin/mysql(host gate, 63350 read-only) -> refresh script -> atomic current.json -> admin API cache merge -> UI`

- 缓存固定 `/mnt/data-disk/x-account-operating-stats/current.json`；验证根目录、fsync 临时文件并原子替换。
- 只执行 `/usr/bin/mysql`，且该入口必须精确解析到 `/usr/local/bin/mysql-gated`；任何 mysql.real、ELF/其他 binary、mariadb 或漂移路径均 fail closed。密码仅进入子进程 `MYSQL_PWD`。
- relay 统计把 ledger 与 queue 按 `queue_id` JOIN，并校验 target/relay/delivery mode 一致；actor 最终取 queue 冻结的 `q.relay_account_id`。冲突只记录证据，不计入任何账号。
- API 继续 Cookie 管理员门禁和 no-store；缓存缺失不阻断账号列表。15 小时未刷新、snapshot 北京业务日不等于当前业务日，或 `generated_at` 超前超过 5 分钟均标 stale。UI 明示 snapshot 的 `yesterday_date`，保留旧值并告警。

## 验收标准

- 六项账号统计、未归属收入、公众指标删减正确；actor、北京时间边界、site/campaign 均有测试。
- 页面请求不连接 MySQL；OAuth Sidecar 文件零改动；timer 为 09:10/21:10。
- 无运行时 DDL、生产写入或真实 X Post。

## 风险与变更记录

- 首次部署须用只读 63350 校验真实 `campaign` 列、查询耗时与金额总和；本地仅 fixture/mock。
- 2026-08-18：冻结本合同。
