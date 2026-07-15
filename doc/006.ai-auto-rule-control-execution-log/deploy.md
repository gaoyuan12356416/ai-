# 部署文档

## 变更内容

- 新增 `ads_ai.ad_control_action_log` 及历史回填。
- 生产复合 app 窄补丁：公平选批、账户并发、应用限流熔断、MySQL日志API。
- 更新 runner、feature module 和日志静态资源。

## 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AD_CONTROL_MAX_LIVE_EXECUTE` | 200 | 单批总上限 |
| `AD_CONTROL_MAX_LIVE_EXECUTE_PER_ACCOUNT` | 20 | 单账户单批上限 |
| `AD_CONTROL_LIVE_MAX_WORKERS` | 4 | live preview跨账户并发，API与runner统一上限 |
| `AD_CONTROL_LIVE_EXECUTE_MAX_WORKERS` | 4 | execute跨账户并发 |
| `AD_CONTROL_ACTION_LOG_DB_NAME` | ads_ai | 固定日志库，不允许覆盖 |
| `AD_CONTROL_ACTION_LOG_TABLE` | ad_control_action_log | 固定日志表，不允许覆盖 |
| `AD_CONTROL_ACTION_LOG_MYSQL_HOST/PORT` | 101.32.56.53 / 63353 | 固定写端点，仅允许日志适配器执行单行 INSERT/UPDATE |
| `AD_CONTROL_ACTION_LOG_MYSQL_USER/PASSWORD` | 无 | 必须显式配置，不继承通用MySQL账号变量 |
| `AD_CONTROL_ACTION_LOG_READER_MYSQL_HOST/PORT` | 101.32.56.53 / 63350 | 固定只读端点，日志列表与详情查询只走此端点 |
| `AD_CONTROL_ACTION_LOG_READER_MYSQL_USER/PASSWORD` | 写账号 | 可显式指定独立只读账号 |
| `AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT` | 3 | 代码硬上限3秒 |
| `AD_CONTROL_ACTION_LOG_IO_TIMEOUT` | 5 | 代码硬上限5秒 |
| `AD_CONTROL_ACTION_LOG_LOCAL_OFFSET_HOURS` | 8 | 日期筛选本地时区 |
| `AD_CONTROL_RUNNER_MAX_CONTINUATIONS` | 24 | 同事件最大续跑次数 |

## 数据库变更

由部署人员在维护窗口通过 `63353` 手工执行一次 `001_create_ad_control_action_log.sql`。运行时代码不包含建表入口，不允许自动 `CREATE/ALTER/DROP`。2026-07-15 已从 `43.166.187.96` 实测 `63353` 为 `@@read_only=0`，并完成正式表创建与回读：`ads_ai.ad_control_action_log` 共31列、5个索引、InnoDB；先前随机探针表及上线写探针均已清理，无残留。

运行时写保护固定如下：只允许 `ads_ai.ad_control_action_log`；单条 INSERT...ON DUPLICATE KEY UPDATE 或按主键 `LIMIT 1` UPDATE；主机级文件锁并发1；令牌桶突发2、平均1次/秒；JSON合计最多512KiB；数据库失败零重试并保留SQLite outbox。写端不得承担列表、详情、统计、扫描或迁移源查询。

## 部署步骤

1. 本地测试，提交并推送 `codex/ai-auto-rule-control`。
2. 服务器新建 GitHub checkout，拉取精确 commit；不在现网目录直接开发。
3. 备份现网 `app.py`、runner、feature、静态资源和 SQLite DB，记录 SHA256。
4. 对真实 `/root/drama_material_service/app.py` 运行补丁 `--check`，生成补丁前后 diff，确认仅 ad-control 块变化。
5. 执行 SQL 建表并 `SHOW CREATE TABLE` 回读。
6. 将 feature module、runner、migration、静态资源从同一 commit 复制到现网；对 app 应用窄补丁。
7. `py_compile` app/runner/feature/migration；`node --check` JS。
8. 运行 migration 回填，每次最多20条、每条至少间隔1秒；使用 `--offset` 分段推进。立即二次运行同一段，确认 existing_skipped 等于已存在数量且行数不变。
9. 重启且只重启 `drama-material-api.service`；cron配置不改。
10. 确认服务 active 且 journal 无错误；通过公网 nginx 检查两个静态页，通过API检查鉴权，再使用登录态验证日志列表与详情 lazy GET。正式Meta写操作只允许在另行批准后执行。

## 验证步骤

```bash
systemctl is-active drama-material-api.service
python3 -m py_compile app.py scripts/ad_control_rule_runner.py features/ad_control_execution_log/service.py scripts/migrate_ad_control_action_logs.py
mysql ... -e "SELECT COUNT(*),MIN(created_at),MAX(created_at) FROM ads_ai.ad_control_action_log"
python3 scripts/migrate_ad_control_action_logs.py --limit 20 --offset 0
python3 scripts/migrate_ad_control_action_logs.py --limit 20 --offset 0
tail -n 100 /var/log/ad_control_rule_runner.log
```

上线后不得手工触发正式 pause 作为验证；先使用已有登录态读取日志，再做 live preview/dry-run。

## 生产部署记录（2026-07-15）

- 生产源码版本：`82fdab6a5c88565a45d1e7d2ac2a9dddf9bb3dce`；服务器 checkout 与 GitHub 精确一致且 clean。
- 上线前完整备份：`/root/drama_material_service/backups/ad-control-execution-log-20260715-130314`；清单 SHA256 为 `3a825eaa1b98f3e931661da9d1719e847d410aee22ad3ecfc8a4af03c7a01aaf`。
- 仅重启 `drama-material-api.service`；服务为 active，部署后 journal error 为0，cron未变，验证时主机负载保持低位。
- 公网页面 `/ad-control.html` 与 `/ad-control-logs.html` 均返回200；未登录访问日志API返回401，符合鉴权预期。
- SQLite源与MySQL目标均为16个 action_id，集合完全相同；同一批次二次迁移后仍为16行，证明幂等跳过生效。
- 使用真实Chrome登录态验证：列表显示16张日志卡；首条日志的目标明细懒加载成功，展示186/186条，原始结果也是186条。
- 首次上线演练因把后端直连 `/ad-control.html` 当作静态健康检查而得到404，自动回滚已完整恢复并经SHA比对确认。最终部署改用正确的API与公网静态页检查后通过。

## 回滚方案

- 代码：从 `/root/drama_material_service/backups/ad-control-execution-log-20260715-130314` 恢复 `app.py`、runner、`.env` 和静态资源；删除部署前清单标记为不存在的 `features/ad_control_execution_log/` 与 `scripts/migrate_ad_control_action_logs.py`，重新 `py_compile` 后只重启 API 服务。
- 数据：日志表为新增独立表，代码回滚时无需删除；若必须回滚DDL，先导出表再 `RENAME TABLE`，不直接 DROP。
- runner：恢复旧 runner 后，确认 cron仍只有一条且锁文件机制正常。
- 回滚点：上述备份目录、生产源码版本 `82fdab6a5c88565a45d1e7d2ac2a9dddf9bb3dce` 及备份内 SHA256 清单。

## 注意事项

- 生产目录不是 Git 仓库，严禁 `git reset` 或整仓覆盖。
- 复制静态资源后必须保留 `v=20260715log1`，避免浏览器继续使用旧JS/CSS。
- migration 固定每次最多20条并安全跳过已有action；不存在强制覆盖参数。
- 建议后续增加180天归档/清理任务，在未评审前不自动删除日志。
