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
| `AD_CONTROL_LIVE_EXECUTE_MAX_WORKERS` | 4 | execute跨账户并发 |
| `AD_CONTROL_ACTION_LOG_DB_NAME` | ads_ai | 日志库 |
| `AD_CONTROL_ACTION_LOG_TABLE` | ad_control_action_log | 日志表 |
| `AD_CONTROL_ACTION_LOG_LOCAL_OFFSET_HOURS` | 8 | 日期筛选本地时区 |
| `AD_CONTROL_RUNNER_MAX_CONTINUATIONS` | 24 | 同事件最大续跑次数 |

## 数据库变更

执行 `001_create_ad_control_action_log.sql`。生产 MySQL 5.7、utf8mb4，当前服务账号已验证对 `ads_ai` 有 CREATE/INSERT/UPDATE/DELETE/INDEX/ALTER 权限，对 `kunlunads_dev` 只读。

## 部署步骤

1. 本地测试，提交并推送 `codex/ai-auto-rule-control`。
2. 服务器新建 GitHub checkout，拉取精确 commit；不在现网目录直接开发。
3. 备份现网 `app.py`、runner、feature、静态资源和 SQLite DB，记录 SHA256。
4. 对真实 `/root/drama_material_service/app.py` 运行补丁 `--check`，生成补丁前后 diff，确认仅 ad-control 块变化。
5. 执行 SQL 建表并 `SHOW CREATE TABLE` 回读。
6. 将 feature module、runner、migration、静态资源从同一 commit 复制到现网；对 app 应用窄补丁。
7. `py_compile` app/runner/feature/migration；`node --check` JS。
8. 运行 migration 回填；立即二次运行，确认 existing_skipped 等于已存在数量且行数不变。
9. 重启且只重启 `drama-material-api.service`；cron配置不改。
10. 执行 health、日志 GET、详情 lazy GET、preview/dry-run 和 runner只读状态回归。

## 验证步骤

```bash
systemctl is-active drama-material-api.service
python3 -m py_compile app.py scripts/ad_control_rule_runner.py features/ad_control_execution_log/service.py
mysql ... -e "SELECT COUNT(*),MIN(created_at),MAX(created_at) FROM ads_ai.ad_control_action_log"
python3 scripts/migrate_ad_control_action_logs.py
python3 scripts/migrate_ad_control_action_logs.py
tail -n 100 /var/log/ad_control_rule_runner.log
```

上线后不得手工触发正式 pause 作为验证；先使用已有登录态读取日志，再做 live preview/dry-run。

## 回滚方案

- 代码：恢复本次带时间戳备份的 `app.py`、runner、feature和静态资源，重新 `py_compile` 后只重启 API 服务。
- 数据：日志表为新增独立表，代码回滚时无需删除；若必须回滚DDL，先导出表再 `RENAME TABLE`，不直接 DROP。
- runner：恢复旧 runner 后，确认 cron仍只有一条且锁文件机制正常。
- 回滚点：部署时记录备份目录、Git commit和现网原SHA。

## 注意事项

- 生产目录不是 Git 仓库，严禁 `git reset` 或整仓覆盖。
- 复制静态资源后必须保留 `v=20260715log1`，避免浏览器继续使用旧JS/CSS。
- 运行 migration 默认安全跳过已有action；除非明确恢复历史数据，不使用 `--force`。
- 建议后续增加180天归档/清理任务，在未评审前不自动删除日志。
