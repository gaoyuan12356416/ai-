# 部署与验收记录

运行代码：`ee954e1be6813a5499e51ac60837fa3a0c331185`，GitHub 分支 `codex/social-analytics-timeout-20261027`。之后仅补充验收文档，不需再部署。

服务器 `43.166.187.96`，运行目录 `/root/drama_material_service`。候选 release 为 `/mnt/data-disk/social-analytics/releases/ee954e1be6813a5499e51ac60837fa3a0c331185`，由服务器从 GitHub fetch 建立精确提交工作树。

备份：`/mnt/data-disk/social-analytics/backups/20261027T040642Z-web-scope-ee954e1be681`。部署清单状态已由 `active_pending_http_acceptance` 更新为 `verified`。

仅原子替换 `features/social_analytics/{routes,report,scoped_metrics}.py`，只重启 `drama-material-api.service`；新 PID 3444426，active、NRestarts=0。健康接口返回 200，匿名报表仍为 401。冻结账本指纹、发布服务 PID 和独立日报 release 均核对未变。

验收范围为 UTC 2026-09-01 至 2026-10-26，用户指定的单个 YouTube post，人员/剧目/频道分组。报表与 CSV 返回 200，单行数据、合计与独立索引 SQL 一致。页面截图与 CSV 在私有审计目录留存；详细指标、耗时及缺失日期见 test-report.md。

生产验收路径：`/mnt/data-disk/social-analytics/validation/query-timeout-20261027/postdeploy-http/acceptance.json`；候选独立 SQL 验证为同级 `predeploy-final/acceptance.json`。不提交管理员会话、用户身份、原始账本或生产截图到 Git。

精确回滚（在服务器执行）：

```bash
cd /mnt/data-disk/social-analytics/releases/ee954e1be6813a5499e51ac60837fa3a0c331185
python3 scripts/deploy_social_query_timeout.py rollback --backup /mnt/data-disk/social-analytics/backups/20261027T040642Z-web-scope-ee954e1be681
```

回滚前检查目标文件未被后续版本修改；恢复备份中的旧代码并重启主 API，保留现有 SQLite、短链、发布结果、独立日报及投递记录。
